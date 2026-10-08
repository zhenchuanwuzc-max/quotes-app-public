"""测试共用设施：隔离的临时 server + 不走代理的 HTTP 客户端 + 每测 30 秒硬上限。

隔离措施（每个用到 ServerHandle 的测试都强制）：
- 数据目录 / 备份目录 / 壁纸目录 / HOME 全部指向临时目录，绝不碰 ~/quotes-data 与 _backups
- 随机空闲端口起 server.py 子进程，绝不碰线上 8767
- 临时数据目录里不放 sync.sh → server 的 schedule_sync() 直接 return，不做 git/网络
- HTTP 客户端禁用系统代理
- 起来后核对 server 日志里的数据路径，不是临时目录就立刻停（保护真实数据）

种子数据只用仓库里的 quotes.example.json —— 本仓库是公开仓，测试里绝不放真实金句。
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
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_PY = os.path.join(ROOT, "server.py")
MERGE_PY = os.path.join(ROOT, "json-merge.py")
EXAMPLE = os.path.join(ROOT, "quotes.example.json")

TEST_TIME_LIMIT = 30   # 每个测试硬上限（秒）
START_TIMEOUT = 10     # 等 server 起来的上限（秒）
HTTP_TIMEOUT = 5

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def load_example() -> dict:
    with open(EXAMPLE, encoding="utf-8") as f:
        return json.load(f)


def http(method: str, base: str, path: str, body=None, raw: bytes = None):
    """返回 (status, headers, text)；4xx/5xx 不抛异常。raw 用于发非法 body。"""
    data = raw
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if data is not None:
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
        shutil.copy(EXAMPLE, self.data_file)
        self.seed_count = len(load_example()["quotes"])
        self.proc = None
        self.port = None
        self.base = None
        self._log = None

    @property
    def data_file(self) -> str:
        return os.path.join(self.data_dir, "quotes.json")

    def read_file(self) -> dict:
        with open(self.data_file, encoding="utf-8") as f:
            return json.load(f)

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
        if ("data:    " + self.data_file) not in self.log_text():
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


class TimeLimitedTest(unittest.TestCase):
    """每个测试 TEST_TIME_LIMIT 秒硬上限 + 一个自动清理的临时目录 self.tmp。"""
    time_limit = TEST_TIME_LIMIT

    def setUp(self):
        def _timeout(signum, frame):
            raise AssertionError("测试超过 %d 秒硬上限" % self.time_limit)
        signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(self.time_limit)
        self.addCleanup(signal.alarm, 0)
        self.tmp = tempfile.mkdtemp(prefix="quotes-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)


class ServerTest(TimeLimitedTest):
    """setUp 里起好一个隔离 server：self.srv。"""

    def setUp(self):
        super().setUp()
        self.srv = ServerHandle(self.tmp)
        self.addCleanup(self.srv.stop)
        self.srv.start()

    def get(self, path):
        st, _, body = http("GET", self.srv.base, path)
        return st, json.loads(body)

    def list_all(self) -> list:
        st, d = self.get("/quotes?limit=1000")
        self.assertEqual(st, 200)
        return d["quotes"]

    def find(self, qid):
        return next((q for q in self.list_all() if q["id"] == qid), None)
