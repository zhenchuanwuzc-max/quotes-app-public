#!/usr/bin/env python3
"""真浏览器使用测试：像人一样点一遍 App（对着隔离的临时 server，不碰线上 8767）。

流程：打开 → 目录列出示例金句 → 点一条进海报 → 回目录 → 搜索 → 用界面新增一条 → 在海报页删掉它。

用 Python playwright（`/usr/bin/python3 -m pip install --user playwright` + `python3 -m playwright install chromium`）。
缺了就直接失败并提示怎么装；确实要跳过（比如在没装浏览器的机器上只跑接口测试）设 QUOTES_SKIP_BROWSER=1。
页面发往 127.0.0.1 以外的请求一律拦掉（检查更新等），保证不联网。
"""
import json
import os
import re
import time
import unittest

from _support import ServerTest, http, load_example

try:
    from playwright.sync_api import sync_playwright
    _IMPORT_ERROR = None
except Exception as e:  # pragma: no cover - 取决于机器环境
    sync_playwright = None
    _IMPORT_ERROR = e

EX = load_example()["quotes"]
STEP_TIMEOUT_MS = 10_000


def squash(s: str) -> str:
    """去掉所有空白再比较：排版会把正文拆成很多 span、插入窄空格。"""
    return re.sub(r"\s+", "", s or "")


class BrowserUsageTest(ServerTest):
    time_limit = 90

    @classmethod
    def setUpClass(cls):
        if os.environ.get("QUOTES_SKIP_BROWSER") == "1":
            raise unittest.SkipTest("QUOTES_SKIP_BROWSER=1，跳过浏览器测试")
        if sync_playwright is None:
            raise AssertionError(
                "没装 Python playwright，浏览器使用测试跑不了（%s）。装法：\n"
                "  /usr/bin/python3 -m pip install --user playwright && /usr/bin/python3 -m playwright install chromium\n"
                "确实要跳过：QUOTES_SKIP_BROWSER=1" % _IMPORT_ERROR)
        cls._pw = sync_playwright().start()
        try:
            cls.browser = cls._pw.chromium.launch()
        except Exception:
            cls._pw.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._pw.stop()

    def setUp(self):
        super().setUp()
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 860})
        self.addCleanup(self.context.close)
        self.context.set_default_timeout(STEP_TIMEOUT_MS)
        self.blocked = []
        self.context.route(re.compile(r"^https?://(?!127\.0\.0\.1[:/])"), self._block)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("dialog", lambda d: d.accept())      # 删除确认框：点「确定」
        self.timings = {}

    def _block(self, route):
        self.blocked.append(route.request.url)
        route.abort()

    def step(self, name):
        """记录每步耗时，失败信息里带上是哪一步。"""
        test = self

        class _Step:
            def __enter__(self):
                self.t = time.time()

            def __exit__(self, et, ev, tb):
                test.timings[name] = time.time() - self.t
                if ev is not None and not isinstance(ev, AssertionError):
                    raise AssertionError("「%s」这一步失败：%s" % (name, str(ev).splitlines()[0])) from ev
        return _Step()

    def tearDown(self):
        self.assertEqual(self.errors, [], "页面有 JS 报错")

    # ---- helpers ----
    def rows(self):
        return self.page.locator("#list .row")

    def row_with(self, text):
        return self.page.locator("#list .row", has_text=text)

    def poster_text(self):
        return squash(self.page.locator("#qt").text_content())

    def open_app(self):
        self.page.goto(self.srv.base + "/")
        self.page.wait_for_function("n => document.querySelectorAll('#list .row').length === n",
                                    arg=len(EX))

    # ---- tests ----
    def test_browse_poster_back_and_search(self):
        with self.step("打开目录"):
            self.open_app()
            listed = squash(self.page.locator("#list").text_content())
            for q in EX:
                self.assertIn(squash(q["text"])[:20], listed)
            self.assertTrue(self.page.locator("#index").is_visible())
            self.assertTrue(self.page.locator("#app").is_hidden())

        target = EX[1]
        with self.step("点一条进海报"):
            self.row_with("Simplicity").click()
            self.page.locator("#app").wait_for(state="visible")
            self.assertTrue(self.page.locator("#index").is_hidden())
            self.assertIn(squash(target["text"]), self.poster_text())
            self.assertEqual(self.page.evaluate("location.hash"), "#/q/" + target["id"])

        with self.step("海报翻下一条"):
            before = self.poster_text()
            self.page.locator("#btnNext").click()
            self.page.wait_for_function("h => location.hash !== h", arg="#/q/" + target["id"])
            self.assertNotEqual(self.poster_text(), before)

        with self.step("回目录"):
            self.page.locator("#btnBack").click()
            self.page.locator("#index").wait_for(state="visible")
            self.assertTrue(self.page.locator("#app").is_hidden())
            self.assertEqual(self.rows().count(), len(EX))

        with self.step("搜索"):
            self.page.locator("#q").fill("Drucker")
            self.page.wait_for_function("() => document.querySelectorAll('#list .row').length === 1")
            self.assertIn("measured", self.rows().first.text_content())
            self.page.locator("#q").fill("绝对不存在的词xyz")
            self.page.locator("#list .inone").wait_for()
            self.assertEqual(self.rows().count(), 0)
            self.page.locator("#q").fill("")
            self.page.wait_for_function("n => document.querySelectorAll('#list .row').length === n",
                                        arg=len(EX))

    def test_add_then_delete_through_the_ui(self):
        new_text = "浏览器测试新增 gamma 一句"
        self.open_app()

        with self.step("新增：打开编辑面板"):
            self.page.locator("#btnNew").click()
            self.page.locator("#compose").wait_for(state="visible")
            # Vditor 在同一拍里把编辑区挂进页面并执行 after()（设初值、标记就绪），
            # 所以编辑区一出现就可以输入，不会被 after() 的 setValue 冲掉
            self.page.locator("#cedit .vditor-ir .vditor-reset").wait_for(state="visible")

        with self.step("新增：输入并存入"):
            self.page.locator("#cedit .vditor-ir .vditor-reset").click()
            self.page.keyboard.type(new_text)
            self.page.locator("#cSrc").fill("浏览器测试")
            self.page.locator("#cSave").click()
            self.page.locator("#compose").wait_for(state="hidden")
            self.page.wait_for_function("n => document.querySelectorAll('#list .row').length === n",
                                        arg=len(EX) + 1)
            self.assertEqual(self.row_with("gamma").count(), 1)

        st, _, body = http("GET", self.srv.base, "/quotes?limit=100")
        added = [q for q in json.loads(body)["quotes"] if q["text"] == new_text]
        self.assertEqual(len(added), 1, "界面存入的内容应原样进库")
        self.assertEqual(added[0]["source"], "浏览器测试")
        qid = added[0]["id"]

        with self.step("删除：进海报点删除"):
            self.row_with("gamma").click()
            self.page.locator("#app").wait_for(state="visible")
            self.assertEqual(self.page.evaluate("location.hash"), "#/q/" + qid)
            self.page.locator("#btnDel").click()
            self.page.locator("#index").wait_for(state="visible")
            self.page.wait_for_function("n => document.querySelectorAll('#list .row').length === n",
                                        arg=len(EX))
            self.assertEqual(self.row_with("gamma").count(), 0)

        st, _, body = http("GET", self.srv.base, "/quotes?limit=100")
        ids = [q["id"] for q in json.loads(body)["quotes"]]
        self.assertNotIn(qid, ids)
        self.assertEqual(len(ids), len(EX))


if __name__ == "__main__":
    unittest.main(verbosity=2)
