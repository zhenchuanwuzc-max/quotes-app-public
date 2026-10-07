#!/usr/bin/env python3
"""quotes-app 主流程冒烟测试（只测主流程，不追求全覆盖）。

隔离措施（全部在每个测试里强制）：
- 数据目录 / 备份目录 / 壁纸目录 / HOME 全部指向临时目录，绝不碰 ~/quotes-data 与 _backups
- 随机空闲端口起 server.py 子进程，绝不碰线上 8767
- 临时数据目录里不放 sync.sh → server 的 schedule_sync() 直接 return，不做 git/网络
- HTTP 客户端禁用系统代理
- 每个测试 30 秒硬上限（SIGALRM），结束必杀子进程并删临时目录

运行：python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_PY = os.path.join(ROOT, "server.py")
EXAMPLE = os.path.join(ROOT, "quotes.example.json")

TEST_TIME_LIMIT = 30   # 每个测试硬上限（秒）
START_TIMEOUT = 10     # 等 server 起来的上限（秒）
HTTP_TIMEOUT = 5

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, base: str, path: str, body=None):
    """返回 (status, headers, text)；4xx/5xx 不抛异常。"""
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=HTTP_TIMEOUT) as r:
            return r.status, r.headers, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode("utf-8")


class ServerHandle:
    """一个临时数据目录 + 一个可反复起停的 server 子进程。"""

    def __init__(self, tmp: str):
        self.tmp = tmp
        self.data_dir = os.path.join(tmp, "data")
        self.backup_dir = os.path.join(tmp, "backups")
        self.home = os.path.join(tmp, "home")
        for d in (self.data_dir, self.backup_dir, self.home):
            os.makedirs(d)
        shutil.copy(EXAMPLE, os.path.join(self.data_dir, "quotes.json"))
        with open(EXAMPLE, encoding="utf-8") as f:
            self.seed_count = len(json.load(f)["quotes"])
        self.proc = None
        self.port = None
        self.base = None
        self._log = None

    def start(self):
        self.port = free_port()
        self.base = "http://127.0.0.1:%d" % self.port
        log_path = os.path.join(self.tmp, "server.log")
        self._log = open(log_path, "ab")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.home,
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "QUOTES_PORT": str(self.port),
            "QUOTES_DATA_DIR": self.data_dir,
            "QUOTES_BACKUP_DIR": self.backup_dir,
            "QUOTES_WALLPAPER_DIR": os.path.join(self.tmp, "wallpaper"),
            "NO_PROXY": "*",
            "PYTHONUNBUFFERED": "1",
        }
        self.proc = subprocess.Popen(
            [sys.executable, SERVER_PY], env=env, cwd=self.tmp,
            stdout=self._log, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError("server 提前退出，日志：\n" + self.log_text())
            try:
                st, _, _ = http("GET", self.base, "/health")
                if st == 200:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise AssertionError("server %ds 内没起来，日志：\n%s" % (START_TIMEOUT, self.log_text()))
        # 安全闸：确认 server 真的在用临时数据目录，而不是线上数据仓
        expect = os.path.join(self.data_dir, "quotes.json")
        if ("data:    " + expect) not in self.log_text():
            self.stop()
            raise AssertionError("server 没有使用临时数据目录，终止测试以保护真实数据。日志：\n" + self.log_text())

    def log_text(self) -> str:
        if self._log:
            self._log.flush()
        try:
            with open(os.path.join(self.tmp, "server.log"), encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def stop(self):
        p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=5)
        if self._log:
            self._log.close()
            self._log = None

    def restart(self):
        self.stop()
        self.start()


class SmokeTest(unittest.TestCase):
    def setUp(self):
        def _timeout(signum, frame):
            raise AssertionError("测试超过 %d 秒硬上限" % TEST_TIME_LIMIT)
        signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(TEST_TIME_LIMIT)
        self.addCleanup(signal.alarm, 0)

        tmp = tempfile.mkdtemp(prefix="quotes-smoke-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.srv = ServerHandle(tmp)
        self.addCleanup(self.srv.stop)
        self.srv.start()

    # ---- 1. 首页 ----
    def test_01_home_page_is_html(self):
        st, headers, body = http("GET", self.srv.base, "/")
        self.assertEqual(st, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn("<html", body.lower())
        self.assertNotIn("index.html not loaded", body)

    # ---- 2. 读列表 ----
    def test_02_list_reads_seed_data(self):
        st, _, body = http("GET", self.srv.base, "/quotes")
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertTrue(d["ok"])
        self.assertEqual(d["total"], self.srv.seed_count)
        self.assertEqual(len(d["quotes"]), self.srv.seed_count)
        st, _, body = http("GET", self.srv.base, "/health")
        self.assertEqual(json.loads(body)["total"], self.srv.seed_count)

    # ---- 3. 新增 + 再读 ----
    def test_03_add_then_visible_in_list(self):
        st, _, body = http("POST", self.srv.base, "/quotes/add",
                           {"text": "冒烟测试金句 alpha", "source": "测试来源", "use_case": "smoke"})
        self.assertEqual(st, 200, body)
        d = json.loads(body)
        self.assertTrue(d["ok"])
        qid = d["quote"]["id"]
        self.assertEqual(d["total"], self.srv.seed_count + 1)

        st, _, body = http("GET", self.srv.base, "/quotes?limit=100")
        d = json.loads(body)
        self.assertEqual(d["total"], self.srv.seed_count + 1)
        added = [q for q in d["quotes"] if q["id"] == qid]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["text"], "冒烟测试金句 alpha")
        self.assertEqual(added[0]["source"], "测试来源")

    def test_04_add_empty_text_rejected(self):
        st, _, _ = http("POST", self.srv.base, "/quotes/add", {"text": "   "})
        self.assertEqual(st, 400)
        st, _, body = http("GET", self.srv.base, "/quotes")
        self.assertEqual(json.loads(body)["total"], self.srv.seed_count)

    # ---- 5. 持久化：重启后数据仍在 ----
    def test_05_data_survives_restart(self):
        st, _, body = http("POST", self.srv.base, "/quotes/add",
                           {"text": "重启后还在 beta", "source": "persist"})
        self.assertEqual(st, 200, body)
        qid = json.loads(body)["quote"]["id"]

        self.srv.restart()

        st, _, body = http("GET", self.srv.base, "/quotes?limit=100")
        d = json.loads(body)
        self.assertEqual(d["total"], self.srv.seed_count + 1)
        self.assertIn(qid, [q["id"] for q in d["quotes"]])
        # 文件也确实落在临时数据目录
        with open(os.path.join(self.srv.data_dir, "quotes.json"), encoding="utf-8") as f:
            self.assertIn(qid, [q["id"] for q in json.load(f)["quotes"]])

    # ---- 6. 搜索 ----
    def test_06_search_hits_and_misses(self):
        # 命中种子数据（来源字段）
        st, _, body = http("GET", self.srv.base, "/quotes?q=" + urllib.parse.quote("Drucker"))
        d = json.loads(body)
        self.assertEqual(st, 200)
        self.assertEqual(d["total"], 1)
        self.assertIn("measured", d["quotes"][0]["text"])

        # 新增一条中文，搜索能命中（含 URL 编码的中文）
        st, _, body = http("POST", self.srv.base, "/quotes/add", {"text": "搜索专用独特词汇蓝莓气泡"})
        self.assertEqual(st, 200, body)
        st, _, body = http("GET", self.srv.base, "/quotes?q=" + urllib.parse.quote("蓝莓气泡"))
        d = json.loads(body)
        self.assertEqual(d["total"], 1)
        self.assertEqual(d["quotes"][0]["text"], "搜索专用独特词汇蓝莓气泡")

        # 不存在的词 → 0 条
        st, _, body = http("GET", self.srv.base, "/quotes?q=" + urllib.parse.quote("绝对不存在的词xyz"))
        self.assertEqual(json.loads(body)["total"], 0)

    # ---- 7. 隔离自检：不碰网络/真实目录 ----
    def test_07_isolation_guards(self):
        self.assertFalse(os.path.exists(os.path.join(self.srv.data_dir, "sync.sh")),
                         "临时数据目录不应有 sync.sh（否则 server 会触发 git 同步）")
        http("POST", self.srv.base, "/quotes/add", {"text": "触发一次写入"})
        time.sleep(0.3)
        self.assertTrue(any(f.startswith("quotes-") for f in os.listdir(self.srv.backup_dir)),
                        "备份应写入临时备份目录")
        self.assertNotIn("sync", " ".join(os.listdir(self.srv.data_dir)).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
