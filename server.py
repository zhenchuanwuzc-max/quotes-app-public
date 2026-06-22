#!/usr/bin/env python3
"""
quotes-app 本地 HTTP 服务（v0.1.0：JSON-as-truth + git 同步，弃 SQLite）

- GET    /              → 返回 index.html
- GET    /quotes?q=&offset=&limit=  → 内存 filter 检索 text/source/use_case；分页
- POST   /quotes/add    → 单条写入 {text, source?, use_case?}
- DELETE /quotes/<id>   → 删一条（从数组移除；跨机删除靠 git base-diff，无 tombstone）
- PATCH  /quotes/<id>     → 编辑正文 {text, source?, use_case?, expected_updated_at?}（CAS 防 stale write）
- PATCH  /quotes/<id>/pin → 切换收藏置顶 {pinned: bool}
- POST   /sync-now      → 手动触发 git 同步，回 sync.sh 写的状态
- GET    /health        → {ok, total}

数据：~/quotes/quotes.json（单 JSON 对象，本地真库，git 仓根；不进 iCloud）
  格式：{"updated": ISO, "quotes": [{id,text,source,use_case,created_at,updated_at,pinned,pinned_at}]}
  可变字段（推翻原 append-only 设计）：
    - 正文原子组 {text,source,use_case,updated_at}：编辑走 updated_at LWW（整组覆盖，禁逐字段拼）
    - pin 原子组 {pinned,pinned_at}：独立走 pinned_at LWW
    两组在 json-merge.py 解耦合并。历史条目无 updated_at → 一律按 created_at 兜底。
    🔴 严禁一次性迁移脚本批量补 updated_at——会让所有历史条目 o!=bse，删除传播全局瘫痪。
同步：git 私有仓 + json-merge.py union 合并驱动（照抄 daily-todo 范式）
端口：localhost:8767（占用 fallback 8770，install.sh 检测）

范式来源：~/daily-todo/server.py（_atomic_write + 锁内 read-modify-write + schedule_sync 5s debounce）
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime
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

# 备份路径（进 iCloud，跨机/误删恢复用）—— 与 git 历史互补的第二道保险
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(SCRIPT_DIR, "_backups")
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

def add_quote(text: str, source: str = "", use_case: str = "") -> dict:
    """写入一条金句，返回完整记录。锁内 read-append-write，复用 _atomic_write。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("text 不能为空")
    source = (source or "").strip()
    use_case = (use_case or "").strip()
    ts = now_iso()
    quote = {
        "id": str(uuid.uuid4()),
        "text": text,
        "source": source,
        "use_case": use_case,
        "created_at": ts,
        "updated_at": ts,  # 新增即等于 created_at；编辑时刷新，走正文 LWW
        "pinned": False,
        "pinned_at": None,
    }
    with _write_lock:
        data = read_data()
        data["quotes"].append(quote)
        _atomic_write(data)
    schedule_sync()
    return quote


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
               expected_updated_at=None) -> dict:
    """编辑正文原子组 {text,source,use_case,updated_at}，刷新 updated_at 走 LWW。
    不动 created_at / pinned / pinned_at（与 pin 原子组解耦）。
    expected_updated_at 非 None 时做 CAS：与库内当前值不符则抛 StaleEditError（防 stale write）。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("text 不能为空")
    source = (source or "").strip()
    use_case = (use_case or "").strip()
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
        target["text"] = text
        target["source"] = source
        target["use_case"] = use_case
        target["updated_at"] = now_iso()
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
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
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
        if self.path == "/quotes/add":
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception as e:
                self._send(400, json.dumps({"error": f"invalid json: {e}"}))
                return
            try:
                quote = add_quote(
                    text=payload.get("text", ""),
                    source=payload.get("source", ""),
                    use_case=payload.get("use_case", ""),
                )
                self._send(200, json.dumps({
                    "ok": True, "quote": quote, "total": count_quotes(),
                }, ensure_ascii=False))
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
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception as e:
                self._send(400, json.dumps({"error": f"invalid json: {e}"}))
                return
            try:
                quote = edit_quote(
                    qid,
                    text=payload.get("text", ""),
                    source=payload.get("source", ""),
                    use_case=payload.get("use_case", ""),
                    expected_updated_at=payload.get("expected_updated_at"),
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
