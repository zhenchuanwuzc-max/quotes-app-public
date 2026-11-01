#!/usr/bin/env python3
"""
quotes-app 本地 HTTP 服务（v0.1.0：JSON-as-truth + git 同步，弃 SQLite）

- GET    /              → 返回 index.html
- GET    /quotes?q=&offset=&limit=  → 内存 filter 检索 text/source/use_case；分页
- POST   /quotes/add    → 单条写入 {text, source?, use_case?, attachments?, source_url?, capture_id?}
- POST   /attachments   → 验证并保存 PNG/JPEG/WebP；GET /attachments/<sha256>.<ext> 读取
- DELETE /quotes/<id>   → 删一条（从数组移除；跨机删除靠 git base-diff，无 tombstone）
- PATCH  /quotes/<id>     → 编辑正文 {text, source?, use_case?, expected_updated_at?}（CAS 防 stale write）
- PATCH  /quotes/<id>/pin → 切换收藏置顶 {pinned: bool}
- POST   /sync-now      → 手动触发 git 同步，回 sync.sh 写的状态
- POST   /wallpaper     → {name, png(dataURL), at:{x,y}} 存 PNG 到 ~/Pictures/quotes-app/ 并设为窗口所在显示器的桌面壁纸
- GET    /health        → {ok, total}

数据：~/quotes-data/quotes.json（单 JSON 对象，本地真库，git 仓根；不进 iCloud）
  格式：{"updated": ISO, "quotes": [{id,text,source,use_case,created_at,updated_at,pinned,pinned_at}]}
  可变字段（推翻原 append-only 设计）：
    - 正文原子组 {text,source,use_case,attachments,source_url,updated_at}：编辑走 updated_at LWW（整组覆盖，禁逐字段拼）
    - pin 原子组 {pinned,pinned_at}：独立走 pinned_at LWW
    两组在 json-merge.py 解耦合并。历史条目无 updated_at → 一律按 created_at 兜底。
    🔴 严禁一次性迁移脚本批量补 updated_at——会让所有历史条目 o!=bse，删除传播全局瘫痪。
同步：git 私有仓 + json-merge.py union 合并驱动（照抄 daily-todo 范式）
端口：localhost:8767（占用 fallback 8770，install.sh 检测）

范式来源：~/daily-todo/server.py（_atomic_write + 锁内 read-modify-write + schedule_sync 5s debounce）
"""
import base64
import binascii
import hashlib
import re
import stat
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    TZ = None

# 端口：默认 8767，可由 install.sh 通过环境变量改成 8770
PORT = int(os.environ.get("QUOTES_PORT", 8767))

# 数据仓路径：QUOTES_DATA_DIR env 优先；否则优先新名 ~/quotes-data，回退旧名 ~/quotes（改名零中断）
DATA_DIR = os.environ.get("QUOTES_DATA_DIR", "")
if not DATA_DIR:
    for _cand in ("~/quotes-data", "~/quotes"):
        _p = os.path.expanduser(_cand)
        if os.path.isdir(_p):
            DATA_DIR = _p
            break
    else:
        DATA_DIR = "~/quotes-data"
DATA_DIR = os.path.expanduser(DATA_DIR)
DATA_FILE = os.path.join(DATA_DIR, "quotes.json")
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_JSON_BYTES = 24 * 1024 * 1024
MAX_ATTACHMENTS = 4
_ATTACHMENT_NAME = re.compile(r"^([a-f0-9]{64})\.(png|jpg|webp)$")
_IMAGE_MIMES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
_UNSET = object()

# 备份路径（进 iCloud，跨机/误删恢复用）—— 与 git 历史互补的第二道保险
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.environ.get("QUOTES_BACKUP_DIR") or os.path.join(SCRIPT_DIR, "_backups")  # env 仅供自动化测试隔离备份目录
BACKUP_KEEP_DAYS = 30
BACKUP_KEEP_COUNT = 50

# 静态资源（index.html 跟 server.py 同目录 / .app bundle Resources）
# index.html 在 iCloud，会被 evict 成 dataless 占位符；启动时载入内存，规避请求线程触发
# iCloud 按需下载与同步守护进程抢锁报 EDEADLK（2026-06-02 踩坑，沿用 v0.0.1 修法）。
INDEX_HTML = "<h1>index.html not loaded</h1>"


def get_resource_path(name: str) -> str:
    """找 index.html：优先 .app bundle Resources，回退 __file__ 同目录，再回退 DATA_DIR"""
    try:
        from Foundation import NSBundle  # type: ignore
        rp = NSBundle.mainBundle().resourcePath()
        if rp:
            full = os.path.join(str(rp), name)
            if os.path.exists(full):
                return full
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    full = os.path.join(here, name)
    if os.path.exists(full):
        return full
    return os.path.join(DATA_DIR, name)


HTML_FILE = get_resource_path("index.html")


def load_index_html():
    """启动时载入内存（非 HTTP 请求上下文，不重蹈 EDEADLK）；失败 brctl 拉取重试一次。"""
    global INDEX_HTML
    for _ in (1, 2):
        try:
            with open(HTML_FILE, "r", encoding="utf-8") as f:
                INDEX_HTML = f.read()
            return
        except OSError as e:
            print(f"⚠️  index.html 读失败({e})，brctl 拉取后重试", file=sys.stderr)
            try:
                subprocess.run(["brctl", "download", HTML_FILE],
                               timeout=20, capture_output=True)
            except Exception:
                pass


def now_iso() -> str:
    """ISO 8601 with timezone"""
    now = datetime.now(TZ) if TZ else datetime.now()
    return now.isoformat(timespec="seconds")


# ============== 自动更新（照抄 daily-todo：菜单栏检查 → GitHub Release → 下载替换重启）==============
# 代码仓（托管 Release 的 .app.zip）；与数据仓 quotes-app-data 分开。
GITHUB_CODE_REPO = "zhenchuanwuzc-max/quotes-app-public"
# token 优先数据仓里的，回退 daily-todo 的（同一个 PAT）
_GH_TOKEN_FILES = [
    os.path.join(DATA_DIR, ".gh-token"),
    os.path.expanduser("~/daily-todo/.gh-token"),
]
APP_BUNDLE = os.path.expanduser("~/Applications/quotes.app")
APP_BUNDLE_ID = "com.ocean.quotes-app"


def get_version() -> str:
    """优先从自己的 .app Info.plist 读（校验 bundleId 防读到 Python.app 的版本），回退 VERSION 文件。"""
    try:
        from Foundation import NSBundle  # type: ignore
        bundle = NSBundle.mainBundle()
        bid = bundle.bundleIdentifier()
        if bid and str(bid) == APP_BUNDLE_ID:
            v = bundle.infoDictionary().get("CFBundleShortVersionString")
            if v:
                return str(v)
    except Exception:
        pass
    for path in (os.path.join(SCRIPT_DIR, "VERSION"), os.path.join(DATA_DIR, "VERSION")):
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return f.read().strip()
            except Exception:
                pass
    return "0.0.0"


VERSION = get_version()


def read_gh_token() -> str:
    for p in _GH_TOKEN_FILES:
        try:
            if os.path.exists(p):
                t = open(p).read().strip()
                if t:
                    return t
        except Exception:
            pass
    return ""


def fetch_latest_release() -> dict:
    import urllib.request
    token = read_gh_token()
    url = f"https://api.github.com/repos/{GITHUB_CODE_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "quotes-app"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


_progress_lock = threading.Lock()
_progress = {"status": "idle", "downloaded": 0, "total": 0, "error": None}


def _set_progress(**kw):
    with _progress_lock:
        _progress.update(kw)


def get_progress() -> dict:
    with _progress_lock:
        return dict(_progress)


def download_asset(asset_api_url: str, dest_path: str) -> None:
    import urllib.request
    token = read_gh_token()
    req = urllib.request.Request(asset_api_url, headers={
        "Accept": "application/octet-stream", "User-Agent": "quotes-app"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=180) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        _set_progress(status="downloading", downloaded=0, total=total, error=None)
        with open(dest_path, "wb") as f:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                with _progress_lock:
                    _progress["downloaded"] += len(chunk)
    _set_progress(status="downloaded")


def install_update() -> dict:
    rel = fetch_latest_release()
    asset = next((a for a in rel.get("assets", []) if a.get("name", "").endswith(".zip")), None)
    if not asset:
        raise RuntimeError("release 里没找到 .zip 附件")

    def _worker():
        try:
            _do_install_update(rel, asset)
        except Exception as e:
            _set_progress(status="error", error=str(e))

    threading.Thread(target=_worker, daemon=True).start()
    return {"ok": True, "started": True, "version": (rel.get("tag_name") or "").lstrip("v")}


def _do_install_update(rel: dict, asset: dict) -> None:
    update_dir = os.path.join(DATA_DIR, ".update")
    if os.path.exists(update_dir):
        shutil.rmtree(update_dir)
    os.makedirs(update_dir, exist_ok=True)
    zip_path = os.path.join(update_dir, asset["name"])
    download_asset(asset["url"], zip_path)
    _set_progress(status="installing")

    log_path = "/tmp/quotes-updater.log"
    updater_sh = os.path.join(update_dir, "updater.sh")
    script = f"""#!/bin/bash
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8
exec > "{log_path}" 2>&1
set -e
echo "[updater] start at $(date)"
for i in $(seq 1 60); do
    if ! pgrep -f "Applications/quotes.app/Contents/MacOS" > /dev/null; then break; fi
    sleep 0.5
done
pkill -9 -f "Applications/quotes.app/Contents/MacOS" 2>/dev/null || true
sleep 1
cd "{update_dir}"
mkdir -p extracted
ditto -x -k "{zip_path}" extracted/
NEW_APP=$(find extracted -maxdepth 2 -name "*.app" -type d | head -1)
if [ -z "$NEW_APP" ]; then echo "[updater] new .app not found"; exit 1; fi
echo "[updater] new app: $NEW_APP"
xattr -dr com.apple.quarantine "$NEW_APP" 2>/dev/null || true
rm -rf "{APP_BUNDLE}"
mv "$NEW_APP" "{APP_BUNDLE}"
echo "[updater] replaced"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "{APP_BUNDLE}"
sleep 1
open -a "{APP_BUNDLE}"
echo "[updater] reopened"
cd /tmp
rm -rf "{update_dir}/extracted"
"""
    with open(updater_sh, "w", encoding="utf-8") as f:
        f.write(script)
    os.chmod(updater_sh, 0o755)
    env = os.environ.copy()
    env["LANG"] = "en_US.UTF-8"
    env["LC_ALL"] = "en_US.UTF-8"
    subprocess.Popen(["/bin/bash", updater_sh],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, env=env)
    _set_progress(status="restarting")
    threading.Timer(1.5, lambda: os._exit(0)).start()


# ============== 存储层（JSON-as-truth，照抄 daily-todo 原子写 + 锁内读改写）==============

_write_lock = threading.RLock()


def read_data() -> dict:
    """读整个 quotes.json；不存在返回空骨架。"""
    if not os.path.exists(DATA_FILE):
        return {"updated": now_iso(), "quotes": []}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "quotes" not in data or not isinstance(data.get("quotes"), list):
        data["quotes"] = []
    return data


def compose_defaults(data: dict) -> dict:
    value = data.get("compose_defaults")
    if isinstance(value, dict) and all(isinstance(value.get(key), str)
                                       for key in ("source", "use_case", "updated_at")):
        try:
            stamp = datetime.fromisoformat(value["updated_at"].replace("Z", "+00:00"))
            if stamp.utcoffset() is not None:
                return {key: value[key] for key in ("source", "use_case", "updated_at")}
        except ValueError:
            pass
    def recency(quote):
        try:
            stamp = datetime.fromisoformat(str(quote.get("updated_at") or quote.get("created_at") or "").replace("Z", "+00:00"))
            if stamp.utcoffset() is not None:
                return stamp
        except ValueError:
            pass
        return datetime.min.replace(tzinfo=timezone.utc)
    quotes = data.get("quotes") or []
    latest = max(quotes, key=lambda quote: (recency(quote), str(quote.get("id") or ""))) if quotes else {}
    return {"source": latest.get("source") if isinstance(latest.get("source"), str) else "",
            "use_case": latest.get("use_case") if isinstance(latest.get("use_case"), str) else "",
            "updated_at": latest.get("updated_at") or latest.get("created_at") or ""}


def saved_compose_defaults(source: str, use_case: str) -> dict:
    return {"source": source, "use_case": use_case,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="microseconds")}


def backup_data_file():
    """写入前时间戳备份 quotes.json，保留 30 天 OR 50 份取大值。失败不阻断。"""
    if not os.path.exists(DATA_FILE):
        return
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        ts = datetime.now(TZ).strftime("%Y%m%d-%H%M%S") if TZ else datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(DATA_FILE, os.path.join(BACKUP_DIR, f"quotes-{ts}.json"))
        _cleanup_old_backups()
    except Exception as e:
        print(f"⚠️  备份失败：{e}", file=sys.stderr)


def _cleanup_old_backups():
    """保留最近 30 天 OR 最近 50 份，取大值（兼容旧的 .db 备份与新 .json 备份）。"""
    if not os.path.isdir(BACKUP_DIR):
        return
    files = [f for f in os.listdir(BACKUP_DIR)
             if f.startswith("quotes-") and (f.endswith(".json") or f.endswith(".db"))]
    if len(files) <= BACKUP_KEEP_COUNT:
        return
    fm = [(f, os.path.getmtime(os.path.join(BACKUP_DIR, f))) for f in files]
    fm.sort(key=lambda x: x[1], reverse=True)
    keep = set(f for f, _ in fm[:BACKUP_KEEP_COUNT])
    cutoff = datetime.now().timestamp() - BACKUP_KEEP_DAYS * 86400
    for f, mt in fm:
        if mt >= cutoff:
            keep.add(f)
    for f, _ in fm:
        if f not in keep:
            try:
                os.unlink(os.path.join(BACKUP_DIR, f))
            except Exception:
                pass


def _atomic_write(data: dict) -> None:
    """假定调用方已持有 _write_lock。刷新 updated → 备份 → tmp + fsync + rename。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    data["updated"] = now_iso()
    backup_data_file()
    tmp = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=DATA_DIR, delete=False, suffix=".tmp"
    )
    try:
        json.dump(data, tmp, ensure_ascii=False, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, DATA_FILE)
    except Exception:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
        raise


# ============== 壁纸：存 PNG + osascript 设为桌面 ==============
# 前端把海报画到 canvas（屏幕像素尺寸）→ POST 过来；这里落盘并调 System Events 设壁纸。
# 存到 ~/Pictures 而不是数据仓：PNG 不该进 git 同步；壁纸文件删了桌面会回退，所以保留最近几张。

WALLPAPER_DIR = os.path.expanduser(os.environ.get("QUOTES_WALLPAPER_DIR", "~/Pictures/quotes-app"))
WALLPAPER_KEEP = 12


def save_wallpaper(png_data_url: str, name: str = "") -> str:
    """dataURL/base64 → PNG 文件，返回绝对路径。名字只留 [A-Za-z0-9_-]，时间戳保证每次新路径（同路径 macOS 不刷新）。"""
    raw = png_data_url.split(",", 1)[1] if png_data_url.startswith("data:") else png_data_url
    data = base64.b64decode(raw)
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG")
    safe = "".join(ch for ch in (name or "") if ch.isalnum() or ch in "_-")[:40] or "quote"
    ts = datetime.now(TZ).strftime("%Y%m%d-%H%M%S") if TZ else datetime.now().strftime("%Y%m%d-%H%M%S")
    os.makedirs(WALLPAPER_DIR, exist_ok=True)
    path = os.path.join(WALLPAPER_DIR, f"quote-{safe}-{ts}.png")
    with open(path, "wb") as f:
        f.write(data)
    _cleanup_old_wallpapers(keep=path)
    return path


def _cleanup_old_wallpapers(keep: str) -> None:
    try:
        files = [os.path.join(WALLPAPER_DIR, f) for f in os.listdir(WALLPAPER_DIR)
                 if f.startswith("quote-") and f.endswith(".png")]
        files.sort(key=os.path.getmtime, reverse=True)
        for f in files[WALLPAPER_KEEP:]:
            if f != keep:
                os.unlink(f)
    except Exception:
        pass


def _screens() -> list:
    """所有显示器：{name,id,x,y,w,h}。走 JXA 读 NSScreen——launchd 用的 /usr/bin/python3 没有 PyObjC，
    osascript 任何进程都有。坐标是 Cocoa 的（主屏左下角为原点，向上为正），id 是 CGDirectDisplayID。"""
    js = ('ObjC.import("AppKit");var ss=$.NSScreen.screens,o=[];for(var i=0;i<ss.count;i++){'
          'var s=ss.objectAtIndex(i),f=s.frame;o.push({name:ObjC.unwrap(s.localizedName),'
          'id:ObjC.unwrap(s.deviceDescription.objectForKey("NSScreenNumber")),'
          'x:f.origin.x,y:f.origin.y,w:f.size.width,h:f.size.height})}JSON.stringify(o)')
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", js], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "读取显示器失败").strip()[:200])
    return json.loads(r.stdout.strip() or "[]")


def display_at(px: float, py: float, screens=None) -> dict:
    """浏览器给的窗口中心点（主屏左上角为原点、向下为正、单位 pt）落在哪块显示器上。
    Cocoa → 浏览器坐标：top = 主屏高 - (y + h)。找不到返回 None。"""
    screens = _screens() if screens is None else screens
    if not screens:
        return None
    main_h = screens[0]["h"]  # screens[0] 是主屏（带菜单栏那块），Cocoa 原点在它左下角
    for s in screens:
        top = main_h - (s["y"] + s["h"])
        if s["x"] <= px < s["x"] + s["w"] and top <= py < top + s["h"]:
            return s
    return None


def _osa(script: str) -> None:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "osascript 失败").strip()[:200])


def apply_wallpaper(path: str, display: dict = None) -> str:
    """只设窗口所在的那块显示器（System Events 的 desktop id == CGDirectDisplayID，按 id 找；
    找不到再按显示器名字；都不行退到 current desktop）。首次会弹系统「自动化」授权。返回实际落到哪。"""
    posix = 'POSIX file "%s"' % path.replace("\\", "\\\\").replace('"', '\\"')
    if display:
        for sel in ('(first desktop whose id is %d)' % int(display["id"]),
                    '(first desktop whose display name is "%s")' % str(display["name"]).replace('"', '\\"')):
            try:
                _osa('tell application "System Events" to set picture of %s to %s' % (sel, posix))
                return str(display["name"])
            except RuntimeError:
                continue
    _osa('tell application "System Events" to set picture of current desktop to %s' % posix)
    return ""


# ============== git 同步触发（防抖，照抄 daily-todo schedule_sync）==============

_sync_timer = None
_sync_lock = threading.Lock()


def schedule_sync(delay: float = 5.0) -> None:
    """防抖触发 git sync：delay 秒内多次写入只跑一次。sync.sh 在 DATA_DIR。"""
    global _sync_timer
    sync_sh = os.path.join(DATA_DIR, "sync.sh")
    if not os.path.exists(sync_sh):
        return
    with _sync_lock:
        if _sync_timer is not None:
            _sync_timer.cancel()
        _sync_timer = threading.Timer(delay, _run_sync, args=[sync_sh])
        _sync_timer.daemon = True
        _sync_timer.start()


def _run_sync(sync_sh: str) -> None:
    try:
        subprocess.Popen(
            ["/bin/bash", sync_sh],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=DATA_DIR, start_new_session=True,
        )
    except Exception:
        pass


# ============== 业务操作（锁内 read-modify-write）==============

def _string(value, label):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{label} 必须为文本")
    return value.strip()


def _source_url(value):
    value = _string(value, "source_url")
    parsed = urlparse(value)
    if value and (len(value) > 4096 or parsed.scheme != "https" or not parsed.hostname
                  or parsed.username is not None or any(ord(c) < 32 for c in value)):
        raise ValueError("source_url 必须是有效的 HTTPS 链接")
    return value


def _attachment_directory(create=False):
    root = os.path.join(DATA_DIR, "attachments")
    try:
        if create:
            os.makedirs(DATA_DIR, exist_ok=True)
            try:
                os.mkdir(root, 0o700)
            except FileExistsError:
                pass
        return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError("附件目录不可用") from exc


def _read_attachment(name):
    match = _ATTACHMENT_NAME.fullmatch(name)
    if not match:
        raise ValueError("附件路径无效")
    root_fd = _attachment_directory()
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
        with os.fdopen(fd, "rb") as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_IMAGE_BYTES:
                raise ValueError("附件文件无效")
            data = f.read(MAX_IMAGE_BYTES + 1)
        if hashlib.sha256(data).hexdigest() != match[1]:
            raise ValueError("附件内容与校验值不符")
        return data, _IMAGE_MIMES[match[2]]
    except OSError as exc:
        raise ValueError("附件不存在或不可访问") from exc
    finally:
        os.close(root_fd)


def upload_attachment(data_url, name=""):
    if not isinstance(data_url, str) or len(data_url) > ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 80:
        raise ValueError("单张图片不能超过 4 MiB")
    match = re.fullmatch(r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)", data_url)
    if not match:
        raise ValueError("只支持 PNG、JPEG、WebP 的 base64 图片")
    mime = match[1]
    try:
        data = base64.b64decode(match[2], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("图片 base64 无效") from exc
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("单张图片不能超过 4 MiB")
    valid = ((mime == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n") and data.endswith(b"IEND\xaeB`\x82"))
             or (mime == "image/jpeg" and data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"))
             or (mime == "image/webp" and len(data) >= 16 and data[:4] == b"RIFF"
                 and data[8:12] == b"WEBP" and int.from_bytes(data[4:8], "little") + 8 == len(data)))
    if not valid:
        raise ValueError("图片格式与内容不符")
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[mime]
    filename = hashlib.sha256(data).hexdigest() + "." + ext
    alt = _string(name, "name")[:300]
    root_fd = _attachment_directory(create=True)
    temporary = ".upload-" + uuid.uuid4().hex
    try:
        try:
            existing = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise ValueError("附件文件不可访问")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, filename, src_dir_fd=root_fd, dst_dir_fd=root_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=root_fd)
        except FileNotFoundError:
            pass
        os.close(root_fd)
    return {"path": "attachments/" + filename, "mime": mime, "alt": alt}


def _attachments(value):
    if not isinstance(value, list) or len(value) > MAX_ATTACHMENTS:
        raise ValueError("每条金句最多 4 张图片")
    result = []
    for attachment in value:
        if not isinstance(attachment, dict):
            raise ValueError("附件引用无效")
        relative = attachment.get("path", "")
        if not isinstance(relative, str) or not relative.startswith("attachments/"):
            raise ValueError("附件路径无效")
        filename = relative[len("attachments/"):]
        _, mime = _read_attachment(filename)
        if attachment.get("mime") != mime:
            raise ValueError("附件类型无效")
        result.append({"path": relative, "mime": mime, "alt": _string(attachment.get("alt", ""), "alt")[:300]})
    return result


def add_quote(text: str, source: str = "", use_case: str = "", *, attachments=None,
              source_url="", capture_id="") -> tuple:
    """同一 capture_id 的重试在写锁内去重，返回 (quote, deduplicated)。"""
    text, source, use_case = (_string(v, label) for v, label in
                              ((text, "text"), (source, "source"), (use_case, "use_case")))
    images = _attachments(attachments if attachments is not None else [])
    if not text and not images:
        raise ValueError("正文和图片不能同时为空")
    source_url = _source_url(source_url)
    capture_id = _string(capture_id, "capture_id")
    if len(capture_id) > 256 or any(ord(c) < 32 for c in capture_id):
        raise ValueError("capture_id 无效")
    ts = now_iso()
    quote = {
        "id": str(uuid.uuid4()), "text": text, "source": source, "use_case": use_case,
        "created_at": ts, "updated_at": ts, "pinned": False, "pinned_at": None,
        "attachments": images, "source_url": source_url,
    }
    if capture_id:
        quote["capture_id"] = capture_id
    with _write_lock:
        data = read_data()
        if capture_id:
            previous = next((q for q in data["quotes"] if q.get("capture_id") == capture_id), None)
            if previous is not None:
                return previous, True
        data["quotes"].append(quote)
        data["compose_defaults"] = saved_compose_defaults(source, use_case)
        _atomic_write(data)
    schedule_sync()
    return quote, False


def delete_quote(qid: str) -> bool:
    """从数组移除一条；返回是否真删了。跨机删除由 git base-diff 识别，不记 tombstone。"""
    with _write_lock:
        data = read_data()
        before = len(data["quotes"])
        data["quotes"] = [q for q in data["quotes"] if q.get("id") != qid]
        deleted = len(data["quotes"]) < before
        if deleted:
            _atomic_write(data)
    if deleted:
        schedule_sync()
    return deleted


def set_pin(qid: str, pinned: bool) -> dict:
    """切换收藏置顶。pinned 是唯一可变字段，刷新 pinned_at 供跨机 LWW 合并。
    注：pin / unpin 都刷新 pinned_at——记录'最后一次操作的时间'，
    merge 时晚动作（不论 pin 还是 unpin）胜出，破解 un-pin trap。"""
    with _write_lock:
        data = read_data()
        target = None
        for q in data["quotes"]:
            if q.get("id") == qid:
                target = q
                break
        if target is None:
            raise KeyError(f"quote {qid} not found")
        target["pinned"] = bool(pinned)
        target["pinned_at"] = now_iso()
        _atomic_write(data)
    schedule_sync()
    return target


class StaleEditError(Exception):
    """编辑时 expected_updated_at 与库内当前值不符（另一端已改过）→ 前端应重载。"""
    def __init__(self, current_updated_at):
        self.current_updated_at = current_updated_at
        super().__init__("stale edit: quote changed since loaded")


def edit_quote(qid: str, text: str, source: str = "", use_case: str = "",
               expected_updated_at=None, *, attachments=_UNSET, source_url=_UNSET) -> dict:
    """编辑正文原子组 {text,source,use_case,attachments,source_url,updated_at}，刷新 updated_at 走 LWW。
    不动 created_at / pinned / pinned_at（与 pin 原子组解耦）。
    expected_updated_at 非 None 时做 CAS：与库内当前值不符则抛 StaleEditError（防 stale write）。"""
    text = _string(text, "text")
    source = _string(source, "source")
    use_case = _string(use_case, "use_case")
    images = _attachments(attachments) if attachments is not _UNSET else _UNSET
    link = _source_url(source_url) if source_url is not _UNSET else _UNSET
    with _write_lock:
        data = read_data()
        target = None
        for q in data["quotes"]:
            if q.get("id") == qid:
                target = q
                break
        if target is None:
            raise KeyError(f"quote {qid} not found")
        # CAS：历史条目无 updated_at → 用 created_at 兜底比对
        if expected_updated_at is not None:
            cur = target.get("updated_at") or target.get("created_at") or ""
            if str(expected_updated_at) != str(cur):
                raise StaleEditError(cur)
        if not text and not (images if images is not _UNSET else target.get("attachments")):
            raise ValueError("正文和图片不能同时为空")
        if images is not _UNSET:
            target["attachments"] = images
        if link is not _UNSET:
            target["source_url"] = link
        target["text"] = text
        target["source"] = source
        target["use_case"] = use_case
        target["updated_at"] = now_iso()
        data["compose_defaults"] = saved_compose_defaults(source, use_case)
        _atomic_write(data)
    schedule_sync()
    return target


def list_quotes(q: str = "", limit: int = 30, offset: int = 0) -> tuple:
    """内存 filter：q 命中 text/source/use_case 任一即收。
    排序：pinned desc → created_at desc。返回 (页内列表, 过滤后总数)。"""
    data = read_data()
    quotes = data["quotes"]
    if q:
        ql = q.lower()
        quotes = [x for x in quotes
                  if ql in (x.get("text", "") or "").lower()
                  or ql in (x.get("source", "") or "").lower()
                  or ql in (x.get("use_case", "") or "").lower()]
    quotes = sorted(
        quotes,
        key=lambda x: (1 if x.get("pinned") else 0, x.get("created_at", "")),
        reverse=True,
    )
    total = len(quotes)
    page = quotes[offset:offset + limit]
    return page, total


def count_quotes() -> int:
    return len(read_data()["quotes"])


# ============== HTTP ==============

class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.wfile.write(body)

    def _json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                raise ValueError("invalid Content-Length")
            if length > MAX_JSON_BYTES:
                self.close_connection = True
                self._send(413, json.dumps({"error": "JSON 请求不能超过 24 MiB"}, ensure_ascii=False))
                return None
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            return payload
        except (ValueError, UnicodeDecodeError) as exc:
            self._send(400, json.dumps({"error": str(exc)}))
            return None

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/attachments/"):
            try:
                data, mime = _read_attachment(parsed.path[len("/attachments/"):])
                self._send(200, data, mime)
            except ValueError:
                self._send(404, json.dumps({"error": "attachment not found"}))
            return
        if parsed.path in ("/", "/index.html"):
            self._send(200, INDEX_HTML, "text/html")
            return

        # vendored 第三方静态资源（Vditor 编辑器 JS/CSS/lute 等）
        # 走 get_resource_path 解析：py2app 打包后 __file__ 在 site-packages.zip 内，
        # SCRIPT_DIR ≠ Contents/Resources/，直接拼 SCRIPT_DIR/vendor 会 404（index.html 早已用此法适配）。
        if parsed.path.startswith("/vendor/"):
            rel = os.path.normpath(parsed.path[len("/vendor/"):].lstrip("/"))
            if rel.startswith("..") or os.path.isabs(rel):
                self._send(403, json.dumps({"error": "forbidden"}))
                return
            fpath = get_resource_path(os.path.join("vendor", rel))
            if os.path.isfile(fpath):
                ext = os.path.splitext(fpath)[1]
                ctype = {".js": "application/javascript", ".css": "text/css",
                         ".wasm": "application/wasm", ".json": "application/json",
                         ".map": "application/json"}.get(ext, "application/octet-stream")
                try:
                    with open(fpath, "rb") as f:
                        self._send(200, f.read(), ctype)
                except Exception as e:
                    self._send(500, json.dumps({"error": str(e)}))
            else:
                self._send(404, json.dumps({"error": "vendor not found"}))
            return

        if parsed.path == "/quotes":
            qs = parse_qs(parsed.query)
            q = (qs.get("q") or [""])[0]
            try:
                limit = int((qs.get("limit") or ["30"])[0])
            except Exception:
                limit = 30
            try:
                offset = int((qs.get("offset") or ["0"])[0])
            except Exception:
                offset = 0
            quotes, total = list_quotes(q=q, limit=limit, offset=offset)
            self._send(200, json.dumps({
                "ok": True, "query": q, "count": len(quotes),
                "offset": offset, "limit": limit, "total": total,
                "quotes": quotes,
                "compose_defaults": compose_defaults(read_data()),
            }, ensure_ascii=False))
            return

        if parsed.path == "/health":
            self._send(200, json.dumps({"ok": True, "total": count_quotes()}))
            return

        if parsed.path == "/version":
            self._send(200, json.dumps({"version": VERSION, "repo": GITHUB_CODE_REPO}))
            return

        if parsed.path == "/update-progress":
            self._send(200, json.dumps(get_progress()))
            return

        if parsed.path == "/check-update":
            try:
                rel = fetch_latest_release()
                self._send(200, json.dumps({
                    "current": VERSION,
                    "latest": (rel.get("tag_name") or "").lstrip("v"),
                    "html_url": rel.get("html_url"),
                    "has_token": bool(read_gh_token()),
                }))
            except Exception as e:
                self._send(200, json.dumps({
                    "current": VERSION, "error": str(e), "has_token": bool(read_gh_token()),
                }))
            return

        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path in ("/attachments", "/quotes/add"):
            payload = self._json_body()
            if payload is None:
                return
            try:
                if self.path == "/attachments":
                    attachment = upload_attachment(payload.get("data_url"), payload.get("name", ""))
                    self._send(200, json.dumps({"ok": True, "attachment": attachment}, ensure_ascii=False))
                    return
                quote, deduplicated = add_quote(
                    text=payload.get("text", ""), source=payload.get("source", ""),
                    use_case=payload.get("use_case", ""), attachments=payload.get("attachments", []),
                    source_url=payload.get("source_url", ""), capture_id=payload.get("capture_id", ""),
                )
                self._send(200, json.dumps({"ok": True, "quote": quote, "total": count_quotes(),
                                           "deduplicated": deduplicated}, ensure_ascii=False))
            except ValueError as e:
                self._send(400, json.dumps({"error": str(e)}))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
            return

        if self.path == "/install-update":
            try:
                self._send(200, json.dumps(install_update()))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
            return

        if self.path == "/wallpaper":
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                path = save_wallpaper(payload.get("png", ""), payload.get("name", ""))
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "error": f"保存失败：{e}"}, ensure_ascii=False))
                return
            try:
                at = payload.get("at") or {}
                display = None
                try:
                    display = display_at(float(at.get("x")), float(at.get("y"))) if at else None
                except Exception:
                    display = None
                where = apply_wallpaper(path, display)
                self._send(200, json.dumps({"ok": True, "path": path, "applied": True,
                                            "display": where}, ensure_ascii=False))
            except subprocess.TimeoutExpired:
                self._send(200, json.dumps({"ok": True, "path": path, "applied": False,
                                            "error": "等待系统授权超时"}, ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"ok": True, "path": path, "applied": False,
                                            "error": str(e)}, ensure_ascii=False))
            return

        if self.path == "/sync-now":
            # 手动同步：跑 sync.sh（pull+push），读它写的状态文件回前端
            sync_sh = os.path.join(DATA_DIR, "sync.sh")
            status_file = "/tmp/quotes-sync.status"
            if not os.path.exists(sync_sh):
                self._send(200, json.dumps({"ok": False, "status": "sync.sh 不存在"}, ensure_ascii=False))
                return
            try:
                os.remove(status_file)
            except OSError:
                pass
            try:
                subprocess.run(["/bin/bash", sync_sh], cwd=DATA_DIR, timeout=45,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                status = ""
                try:
                    with open(status_file, encoding="utf-8") as f:
                        status = f.read().strip()
                except OSError:
                    pass
                self._send(200, json.dumps({"ok": status.startswith("ok"), "status": status or "未知"}, ensure_ascii=False))
            except subprocess.TimeoutExpired:
                self._send(200, json.dumps({"ok": False, "status": "同步超时（45s），网络慢稍后再试"}, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "status": str(e)}, ensure_ascii=False))
            return

        self._send(404, json.dumps({"error": "not found"}))

    def do_PATCH(self):
        # PATCH /quotes/<id>/pin  body {pinned: bool}
        # PATCH /quotes/<id>      body {text, source?, use_case?, expected_updated_at?}
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "quotes":
            qid = parts[1]
            payload = self._json_body()
            if payload is None:
                return
            if "capture_id" in payload:
                self._send(400, json.dumps({"error": "capture_id 不能编辑"}, ensure_ascii=False))
                return
            try:
                quote = edit_quote(
                    qid,
                    text=payload.get("text", ""),
                    source=payload.get("source", ""),
                    use_case=payload.get("use_case", ""),
                    expected_updated_at=payload.get("expected_updated_at"),
                    attachments=payload.get("attachments", _UNSET),
                    source_url=payload.get("source_url", _UNSET),
                )
                self._send(200, json.dumps({"ok": True, "quote": quote}, ensure_ascii=False))
            except StaleEditError as e:
                # 409：另一端已改过，前端应提示重载
                self._send(409, json.dumps({
                    "error": "stale", "current_updated_at": e.current_updated_at,
                }, ensure_ascii=False))
            except ValueError as e:
                self._send(400, json.dumps({"error": str(e)}))
            except KeyError as e:
                self._send(404, json.dumps({"error": str(e)}))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
            return
        if len(parts) == 3 and parts[0] == "quotes" and parts[2] == "pin":
            qid = parts[1]
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception as e:
                self._send(400, json.dumps({"error": f"invalid json: {e}"}))
                return
            try:
                quote = set_pin(qid, bool(payload.get("pinned", True)))
                self._send(200, json.dumps({"ok": True, "quote": quote}, ensure_ascii=False))
            except KeyError as e:
                self._send(404, json.dumps({"error": str(e)}))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
            return
        self._send(404, json.dumps({"error": "not found"}))

    def do_DELETE(self):
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "quotes":
            qid = parts[1]
            try:
                if delete_quote(qid):
                    self._send(200, json.dumps({"ok": True, "deleted": qid}))
                else:
                    self._send(404, json.dumps({"error": "id not found"}))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
            return
        self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, fmt, *args):
        return


def serve_forever():
    load_index_html()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        if e.errno in (48, 98):
            print(f"quotes-app already running on {PORT}, skip.", file=sys.stderr)
            return
        raise
    print(f"quotes-app running at http://localhost:{PORT}")
    print(f"  data:    {DATA_FILE}")
    print(f"  backups: {BACKUP_DIR}")
    print(f"  html:    {HTML_FILE}")
    # 启动时若 sync.sh 在，后台跑一次首同步（非阻塞）
    schedule_sync(delay=2.0)
    srv.serve_forever()


if __name__ == "__main__":
    serve_forever()
