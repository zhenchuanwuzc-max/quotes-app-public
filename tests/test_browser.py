#!/usr/bin/env python3
"""真浏览器使用测试：像人一样点一遍 App（对着隔离的临时 server，不碰线上 8767）。

流程：打开 → 目录列出示例金句 → 点一条进海报 → 回目录 → 搜索 → 用界面新增一条 → 在海报页删掉它。

用 Python playwright（`/usr/bin/python3 -m pip install --user playwright` + `python3 -m playwright install chromium`）。
缺了就直接失败并提示怎么装；确实要跳过（比如在没装浏览器的机器上只跑接口测试）设 QUOTES_SKIP_BROWSER=1。
页面发往 127.0.0.1 以外的请求一律拦掉（检查更新等），保证不联网。
"""
import base64
import hashlib
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
            engine = os.environ.get("QUOTES_TEST_BROWSER", "chromium")
            cls.browser = getattr(cls._pw, engine).launch()
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
    def test_directory_controls_stay_to_the_left_of_the_quotes(self):
        self.open_app()
        for width in (1180, 720, 375):
            self.page.set_viewport_size({"width": width, "height": 700})
            with self.subTest(width=width):
                row = self.rows().first.bounding_box()
                for selector in ("#q", "#btnSync", "#btnNew", "#btnHelpA", "#rail .rbtn"):
                    control = self.page.locator(selector).first.bounding_box()
                    self.assertLessEqual(control["x"] + control["width"], row["x"], selector)
                self.assertGreaterEqual(row["width"], width * 0.60)
                self.assertLessEqual(row["x"] + row["width"], width)

        self.page.locator('#rail [data-key="__pin__"]').click()
        self.assertEqual(self.rows().count(), sum(bool(quote.get("pinned")) for quote in EX))
        self.page.locator('#rail [data-key="__all__"]').click()
        self.assertEqual(self.rows().count(), len(EX))
        self.page.locator("#q").fill("Drucker")
        self.assertEqual(self.rows().count(), 1)

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


    def image_fixture(self):
        # A real PNG, shared between disk seeding and the file-picker flow.
        return base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")

    def seed_image_quote(self, source_url="https://www.youtube.com/watch?v=example&t=42s"):
        image = self.image_fixture()
        rel = "attachments/" + hashlib.sha256(image).hexdigest() + ".png"
        folder = os.path.join(self.srv.data_dir, "attachments")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(self.srv.data_dir, rel), "wb") as f:
            f.write(image)
        data = self.srv.read_file()
        q = data["quotes"][0]
        q["text"] = "视频里值得保存的原话"
        q["attachments"] = [{"path": rel, "mime": "image/png", "alt": "视频截图"}]
        q["source_url"] = source_url
        with open(self.srv.data_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return q

    def test_video_image_thumbnail_detail_and_return_link(self):
        q = self.seed_image_quote()
        self.open_app()
        row = self.row_with("视频里值得保存的原话")
        thumbnail = row.locator(".row-media img")
        thumbnail.wait_for(state="visible")
        self.assertEqual(thumbnail.get_attribute("loading"), "lazy")
        row.click()
        self.page.locator("#posterImages img").wait_for(state="visible")
        self.assertEqual(self.page.locator("#sourceLink").get_attribute("href"), q["source_url"])
        self.assertEqual(self.page.locator("#sourceLink").get_attribute("rel"), "noopener noreferrer")
        self.page.locator("#posterImages button").click()
        self.page.locator("#imageViewer").wait_for(state="visible")
        self.page.wait_for_function("() => document.querySelector('#imageViewer img').naturalWidth > 0")
        self.page.keyboard.press("Escape")
        self.page.locator("#imageViewer").wait_for(state="hidden")
        self.assertTrue(self.page.locator("#app").is_visible(), "关图应留在原条目，而非跳回目录")
        self.page.locator("#posterImages button").focus()
        self.page.keyboard.press("Space")
        self.page.locator("#imageViewer").wait_for(state="visible")
        self.assertEqual(self.page.evaluate("location.hash"), "#/q/" + q["id"], "键盘看图不应触发海报翻页")
        self.page.locator("#imageClose").click()
        self.page.locator("#btnBack").click()
        self.assertTrue(self.page.locator("#index").is_visible())

    def test_upload_image_only_edit_preserve_and_remove(self):
        self.open_app()
        self.page.locator("#btnNew").click()
        self.page.locator("#cedit .vditor-ir .vditor-reset").wait_for(state="visible")
        self.page.locator("#cImages").set_input_files({
            "name": "课程截图.png", "mimeType": "image/png", "buffer": self.image_fixture(),
        })
        self.page.locator("#cAttachments .attachment-ready").wait_for()
        self.page.locator("#cSrc").fill("图片测试来源")
        self.page.locator("#cSave").click()
        self.page.locator("#compose").wait_for(state="hidden")
        self.page.wait_for_function("n => document.querySelectorAll('#list .row').length === n", arg=len(EX)+1)
        quote = next(q for q in self.list_all() if q["source"] == "图片测试来源")
        self.assertEqual(quote["text"], "")
        self.assertEqual(len(quote["attachments"]), 1)
        row = self.row_with("图片测试来源")
        row.locator(".row-media img").wait_for(state="visible")
        row.click()
        self.assertTrue(self.page.locator("#btnWall").is_disabled(), "只有图片时不能生成空白文字壁纸")
        self.page.locator("#btnEdit").click()
        self.page.locator("#cAttachments .attachment-ready").wait_for()
        self.page.locator("#cSrc").fill("图片编辑后来源")
        self.page.locator("#cSave").click()
        self.page.locator("#compose").wait_for(state="hidden")
        self.assertEqual(len(self.find(quote["id"])["attachments"]), 1)
        self.page.locator("#btnEdit").click()
        editor = self.page.locator("#cedit .vditor-ir .vditor-reset")
        editor.click()
        self.page.keyboard.type("移除图片后保留正文")
        self.page.locator("#cAttachments button[aria-label='移除图片']").click()
        self.page.locator("#cSave").click()
        self.page.locator("#compose").wait_for(state="hidden")
        self.assertEqual(self.find(quote["id"])["attachments"], [])
        self.assertTrue(self.page.locator("#posterImages").is_hidden())

    def test_failed_image_upload_can_retry_without_losing_draft(self):
        self.open_app()
        self.page.locator("#btnNew").click()
        editor = self.page.locator("#cedit .vditor-ir .vditor-reset")
        editor.wait_for(state="visible")
        editor.click()
        self.page.keyboard.type("上传失败也要保留的草稿")
        self.page.route("**/attachments", lambda route: route.fulfill(
            status=503, content_type="application/json", body='{"error":"temporary upload failure"}'))
        self.page.locator("#cImages").set_input_files({
            "name": "截图.png", "mimeType": "image/png", "buffer": self.image_fixture(),
        })
        self.page.locator("#cAttachments button", has_text="重试").wait_for(state="visible")
        self.assertTrue(self.page.locator("#cSave").is_disabled())
        self.assertIn("上传失败也要保留的草稿", editor.text_content())
        self.page.unroute("**/attachments")
        self.page.locator("#cAttachments button", has_text="重试").click()
        self.page.locator("#cAttachments .attachment-ready").wait_for()
        self.assertFalse(self.page.locator("#cSave").is_disabled())
        self.page.locator("#cSave").click()
        self.page.locator("#compose").wait_for(state="hidden")
        quote = next(q for q in self.list_all() if q["text"] == "上传失败也要保留的草稿")
        self.assertEqual(len(quote["attachments"]), 1)

    def test_untrusted_source_url_is_not_linked(self):
        self.seed_image_quote(source_url="javascript:alert(1)")
        self.open_app()
        self.row_with("视频里值得保存的原话").click()
        self.assertTrue(self.page.locator("#sourceLink").is_hidden())

    def seed_text_quote(self, text):
        data = self.srv.read_file()
        quote = data['quotes'][0]
        quote['text'] = text
        with open(self.srv.data_file, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False)
        return quote

    def test_plain_paragraphs_are_not_promoted_to_a_title(self):
        first = '第一段是普通正文，应该保持正文大小，也保留句尾标点；'
        second = '第二段也是普通正文，两段应使用相同的字号，而不是自动变成标题与说明。'
        quote = self.seed_text_quote(first + '\n\n' + second)
        self.open_app()
        self.row_with('第一段是普通正文').click()
        self.page.locator('#app').wait_for(state='visible')
        paragraphs = self.page.locator('#qt p')
        self.assertEqual(paragraphs.count(), 2)
        self.assertEqual(paragraphs.nth(0).inner_text(), first)
        self.assertEqual(paragraphs.nth(1).inner_text(), second)
        sizes = paragraphs.evaluate_all('(els) => els.map(e => parseFloat(getComputedStyle(e).fontSize))')
        self.assertAlmostEqual(sizes[0], sizes[1], delta=0.1)
        self.assertLessEqual(max(sizes), 28, '普通正文不能自动放成海报大字')
        self.page.locator('#btnEdit').click()
        editor = self.page.locator('#cedit .vditor-ir .vditor-reset')
        editor.wait_for(state='visible')
        self.assertEqual(editor.locator('h1,h2,h3,h4,h5,h6').count(), 0)
        self.page.locator('#cSave').click()
        self.page.locator('#compose').wait_for(state='hidden')
        self.assertEqual(self.find(quote['id'])['text'].strip(), quote['text'])

    def test_bold_toolbar_handles_punctuation_without_added_spaces(self):
        self.seed_text_quote('强调。紧接正文')
        self.open_app()
        self.row_with('强调。紧接正文').click()
        self.page.locator('#btnEdit').click()
        editor = self.page.locator('#cedit .vditor-ir .vditor-reset')
        editor.wait_for(state='visible')
        editor.locator('p').first.evaluate("""el => {
            const range = document.createRange();
            const node = el.firstChild;
            range.setStart(node, 0); range.setEnd(node, 3);
            const selection = window.getSelection();
            selection.removeAllRanges(); selection.addRange(range);
            el.focus();
        }""")
        self.page.locator('#cedit button[data-type="bold"]').click()
        self.assertEqual(editor.locator('strong').inner_text(), '强调。')
        self.page.locator('#cSave').click()
        self.page.locator('#compose').wait_for(state='hidden')
        self.assertEqual(self.page.locator('#qt strong').inner_text(), '强调。')
        self.assertEqual(self.page.locator('#qt').inner_text().strip(), '强调。紧接正文')

    def test_last_saved_metadata_defaults_survive_edit_cancel_and_restart(self):
        self.open_app()
        self.page.locator('#btnNew').click()
        editor = self.page.locator('#cedit .vditor-ir .vditor-reset')
        editor.wait_for(state='visible')
        editor.click()
        self.page.keyboard.type('Metadata defaults sample')
        self.page.locator('#cSrc').fill('Sample source')
        self.page.locator('#cUse').fill('Sample use')
        self.page.locator('#cSave').click()
        self.page.locator('#compose').wait_for(state='hidden')
        self.page.locator('#btnNew').click()
        self.assertEqual(self.page.locator('#cSrc').input_value(), 'Sample source')
        self.assertEqual(self.page.locator('#cUse').input_value(), 'Sample use')
        self.page.locator('#cSrc').fill('Cancelled source')
        self.page.locator('#cCancel').click()
        self.page.locator('#btnNew').click()
        self.assertEqual(self.page.locator('#cSrc').input_value(), 'Sample source')
        self.assertEqual(self.page.locator('#cUse').input_value(), 'Sample use')
        self.page.locator('#cCancel').click()
        self.row_with('Simplicity').click()
        self.page.locator('#btnEdit').click()
        self.assertEqual(self.page.locator('#cSrc').input_value(), EX[1]['source'])
        self.page.locator('#cSrc').fill('Edited source')
        self.page.locator('#cUse').fill('Edited use')
        self.page.locator('#cSave').click()
        self.page.locator('#compose').wait_for(state='hidden')
        self.srv.restart()
        self.context.close()
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.context.set_default_timeout(STEP_TIMEOUT_MS)
        self.context.route(re.compile(r'^https?://(?!127\.0\.0\.1[:/])'), self._block)
        self.page = self.context.new_page()
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.goto(self.srv.base)
        self.page.locator('#list .row').first.wait_for()
        self.page.locator('#btnNew').click()
        self.page.locator('#cedit .vditor-ir .vditor-reset').wait_for(state='visible')
        self.assertEqual(self.page.locator('#cSrc').input_value(), 'Edited source')
        self.assertEqual(self.page.locator('#cUse').input_value(), 'Edited use')

    def test_failed_save_does_not_replace_defaults_and_empty_save_clears_them(self):
        status, _, _ = http('POST', self.srv.base, '/quotes/add',
                            {'text': 'Defaults baseline', 'source': 'Keep source', 'use_case': 'Keep use'})
        self.assertEqual(status, 200)
        self.page.goto(self.srv.base)
        self.page.locator('#list .row').first.wait_for()
        self.row_with('Defaults baseline').click()
        self.page.locator('#btnEdit').click()
        self.page.locator('#cedit .vditor-ir .vditor-reset').wait_for(state='visible')
        self.page.locator('#cSrc').fill('Conflict source')
        self.page.route('**/quotes/*', lambda route: route.fulfill(status=409,content_type='application/json',body='{"error":"stale"}'))
        self.page.locator('#cSave').click()
        self.page.locator('#compose').wait_for(state='hidden')
        self.page.unroute('**/quotes/*')
        self.page.locator('#btnBack').click()
        self.page.locator('#btnNew').click()
        editor = self.page.locator('#cedit .vditor-ir .vditor-reset')
        editor.wait_for(state='visible')
        editor.click()
        self.page.keyboard.type('A failed draft')
        self.page.locator('#cSrc').fill('Must not stick')
        self.page.locator('#cUse').fill('Must not stick')
        self.page.route('**/quotes/add', lambda route: route.fulfill(status=503,content_type='application/json',body='{"error":"unavailable"}'))
        self.page.locator('#cSave').click()
        self.page.wait_for_function('() => !document.querySelector("#cSave").disabled')
        self.page.locator('#cCancel').click()
        self.page.unroute('**/quotes/add')
        self.page.locator('#btnNew').click()
        self.assertEqual(self.page.locator('#cSrc').input_value(), 'Keep source')
        self.assertEqual(self.page.locator('#cUse').input_value(), 'Keep use')
        editor.click()
        self.page.keyboard.type('Explicitly cleared defaults')
        self.page.locator('#cSrc').fill('')
        self.page.locator('#cUse').fill('')
        self.page.locator('#cSave').click()
        self.page.locator('#compose').wait_for(state='hidden')
        self.page.reload()
        self.page.locator('#list .row').first.wait_for()
        self.page.locator('#btnNew').click()
        self.assertEqual(self.page.locator('#cSrc').input_value(), '')
        self.assertEqual(self.page.locator('#cUse').input_value(), '')

    def test_initial_load_blocks_new_until_metadata_defaults_are_ready(self):
        pending = []
        self.page.route('**/quotes?*', lambda route: pending.append(route))
        self.page.goto(self.srv.base + '/', wait_until='domcontentloaded')
        self.page.wait_for_timeout(100)
        self.assertTrue(pending)
        self.assertTrue(self.page.locator('#btnNew').is_disabled())
        self.page.keyboard.press('n')
        self.assertTrue(self.page.locator('#compose').is_hidden())
        pending.pop().continue_()
        self.rows().first.wait_for()
        self.page.locator('#btnNew').click()
        self.page.locator('#compose').wait_for(state='visible')
        self.assertTrue(self.page.locator('#cSrc').is_visible())

    def test_cjk_bold_punctuation_can_touch_following_text(self):
        self.seed_text_quote('**强调。**紧接正文\n\n前文**加粗片段，**后文\n\n\\*\\*保留字面星号。\\*\\*\n\n`**代码。**紧接`')
        self.open_app()
        self.row_with('紧接正文').click()
        self.page.locator('#app').wait_for(state='visible')
        self.assertEqual(self.page.locator('#qt strong').all_text_contents(), ['强调。', '加粗片段，'])
        self.assertIn('**保留字面星号。**', self.page.locator('#qt').inner_text())
        self.assertEqual(self.page.locator('#qt code').inner_text(), '**代码。**紧接')
        self.page.locator('#btnEdit').click()
        editor = self.page.locator('#cedit .vditor-ir .vditor-reset')
        editor.wait_for(state='visible')
        self.assertEqual(editor.locator('strong').count(), 2)
        self.page.locator('#cSave').click()
        self.page.locator('#compose').wait_for(state='hidden')
        self.page.reload()
        self.page.locator('#qt strong').first.wait_for()
        self.assertEqual(self.page.locator('#qt strong').all_text_contents(), ['强调。', '加粗片段，'])

    def test_add_edit_save_shortcuts_respect_typing_fields(self):
        self.open_app()
        self.page.locator('#q').focus()
        self.page.keyboard.type('ae')
        self.assertTrue(self.page.locator('#compose').is_hidden())
        self.assertEqual(self.page.locator('#q').input_value(), 'ae')
        self.page.locator('#q').fill('')
        self.page.locator('#q').blur()
        self.page.keyboard.press('a')
        editor = self.page.locator('#cedit .vditor-ir .vditor-reset')
        editor.wait_for(state='visible')
        editor.click()
        self.page.keyboard.type('Shortcut ae sample')
        self.page.keyboard.press('ControlOrMeta+s')
        self.page.locator('#compose').wait_for(state='hidden')
        self.row_with('Shortcut ae sample').click()
        self.page.keyboard.press('e')
        editor.wait_for(state='visible')
        self.page.locator('#cSrc').fill('Keyboard saved')
        self.page.keyboard.press('ControlOrMeta+s')
        self.page.locator('#compose').wait_for(state='hidden')
        saved = next(q for q in self.list_all() if q['text'] == 'Shortcut ae sample')
        self.assertEqual(saved['source'], 'Keyboard saved')
        self.page.locator('#btnBack').click()
        self.row_with('Shortcut ae sample').focus()
        self.page.keyboard.press('e')
        editor.wait_for(state='visible')
        self.assertEqual(self.page.locator('#cSrc').input_value(), 'Keyboard saved')

    def test_shortcuts_preserve_button_activation_and_ime_search(self):
        self.open_app()
        search = self.page.locator('#q')
        search.focus()
        search.dispatch_event('keydown', {'key': 'Enter', 'isComposing': True, 'keyCode': 229})
        self.assertTrue(self.page.locator('#index').is_visible(), '输入法确认不能打开海报')
        self.assertTrue(search.evaluate('(el) => el === document.activeElement'))
        self.page.locator('#btnHelpA').focus()
        self.page.keyboard.press('Enter')
        self.page.locator('#help').wait_for(state='visible')
        self.page.keyboard.press('Escape')
        self.page.locator('#btnNew').focus()
        self.page.keyboard.press('Enter')
        self.page.locator('#compose').wait_for(state='visible')
        self.page.locator('#cCancel').click()
        self.row_with('Simplicity').focus()
        self.row_with('Simplicity').locator('[data-act="edit"]').focus()
        self.page.keyboard.press('Enter')
        self.page.locator('#compose').wait_for(state='visible')
        self.assertTrue(self.page.locator('#app').is_hidden(), '编辑按钮不能先切进海报')
        self.assertEqual(self.page.locator('#cSrc').input_value(), EX[1]['source'])

    def test_shortcut_menu_change_cancel_restore_and_restart(self):
        self.open_app()
        self.page.locator('#btnHelpA').click()
        add = self.page.get_by_label('新增快捷键', exact=True)
        add.wait_for()
        self.assertEqual(add.input_value(), 'a')
        add.select_option('b')
        self.page.get_by_role('button', name='取消', exact=True).click()
        self.page.locator('#btnHelpA').click()
        self.assertEqual(add.input_value(), 'a', '取消不能生效')
        add.select_option('b')
        self.page.get_by_label('保存快捷键', exact=True).select_option('mod+shift+s')
        self.page.get_by_role('button', name='保存设置', exact=True).click()
        self.page.locator('#help').wait_for(state='hidden')
        self.page.keyboard.press('a')
        self.assertTrue(self.page.locator('#compose').is_hidden())
        self.page.keyboard.press('b')
        editor = self.page.locator('#cedit .vditor-ir .vditor-reset')
        editor.wait_for(state='visible')
        editor.click()
        self.page.keyboard.type('Customized shortcut sample')
        self.page.keyboard.press('ControlOrMeta+s')
        self.assertTrue(self.page.locator('#compose').is_visible(), '旧保存键应停用')
        self.page.keyboard.press('ControlOrMeta+Shift+s')
        self.page.locator('#compose').wait_for(state='hidden')
        self.assertTrue(self.row_with('Customized shortcut sample').is_visible())
        self.srv.restart()
        fresh = self.browser.new_context(viewport={'width': 1280, 'height': 860})
        self.addCleanup(fresh.close)
        fresh.route(re.compile(r'^https?://(?!127\.0\.0\.1[:/])'), self._block)
        self.page = fresh.new_page()
        self.page.on('pageerror', lambda e: self.errors.append(str(e)))
        self.page.goto(self.srv.base + '/')
        self.page.locator('#btnHelpA').click()
        add = self.page.get_by_label('新增快捷键', exact=True)
        add.wait_for()
        self.assertEqual(add.input_value(), 'b', '新浏览器及服务重启后仍保留')
        self.page.get_by_role('button', name='恢复默认', exact=True).click()
        self.assertEqual(add.input_value(), 'a')
        self.page.get_by_role('button', name='保存设置', exact=True).click()
        self.page.locator('#help').wait_for(state='hidden')
        self.page.keyboard.press('a')
        self.page.locator('#compose').wait_for(state='visible')

    def test_shortcut_menu_conflict_failed_save_and_escape(self):
        self.open_app()
        self.page.locator('#btnHelpA').click()
        edit = self.page.get_by_label('编辑快捷键', exact=True)
        edit.wait_for()
        edit.select_option('a')
        save = self.page.get_by_role('button', name='保存设置', exact=True)
        self.assertTrue(save.is_disabled(), '冲突不能保存')
        self.assertIn('冲突', self.page.locator('#help').inner_text())
        edit.select_option('b')
        self.page.route('**/settings/shortcuts', lambda route: route.fulfill(
            status=503, content_type='application/json', body='{"error":"Settings write failed"}')
            if route.request.method == 'PUT' else route.continue_())
        save.click()
        self.page.get_by_text('Settings write failed', exact=False).wait_for()
        self.assertTrue(self.page.locator('#help').is_visible())
        self.assertEqual(edit.input_value(), 'b', '失败保留草稿')
        edit.focus()
        self.page.keyboard.press('Escape')
        self.page.locator('#help').wait_for(state='hidden')
        self.page.unroute('**/settings/shortcuts')
        self.page.locator('#btnHelpA').click()
        self.assertEqual(edit.input_value(), 'e')
        self.page.keyboard.press('Tab')
        self.assertTrue(self.page.evaluate("document.getElementById('help').contains(document.activeElement)"))
        self.page.keyboard.press('Escape')
        self.page.keyboard.press('n')
        self.page.locator('#compose').wait_for(state='visible')

    def test_markdown_heading_levels_survive_edit_save_and_reload(self):
        text = '\n\n'.join(['开头仍然是普通正文。'] +
                           ['#' * level + ' 层级' + str(level) for level in range(1, 7)] +
                           ['末尾正文包含 **加粗内容**。'])
        quote = self.seed_text_quote(text)
        self.open_app()
        self.row_with('开头仍然是普通正文').click()
        self.page.locator('#app').wait_for(state='visible')
        for level in range(1, 7):
            heading = self.page.get_by_role('heading', name='层级' + str(level), exact=True)
            self.assertEqual(heading.get_attribute('aria-level') or heading.evaluate('(e) => e.tagName[1]'), str(level))
        sizes = self.page.locator('#qt h1,#qt h2,#qt h3,#qt h4,#qt h5,#qt h6').evaluate_all(
            '(els) => els.map(e => parseFloat(getComputedStyle(e).fontSize))')
        self.assertTrue(all(a > b for a, b in zip(sizes, sizes[1:])), sizes)
        self.assertEqual(self.page.locator('#qt strong').inner_text(), '加粗内容')
        self.page.locator('#btnEdit').click()
        editor = self.page.locator('#cedit .vditor-ir .vditor-reset')
        editor.wait_for(state='visible')
        for level in range(1, 7):
            self.assertEqual(editor.locator('h' + str(level)).count(), 1)
        self.page.locator('#cSave').click()
        self.page.locator('#compose').wait_for(state='hidden')
        self.assertEqual(self.find(quote['id'])['text'].strip(), text)
        self.page.reload()
        self.page.locator('#qt h6').wait_for(state='visible')
        for level in range(1, 7):
            self.assertEqual(self.page.locator('#qt h' + str(level)).inner_text(), '层级' + str(level))

    def test_heading_syntax_matches_editor_and_does_not_activate_html(self):
        text = ('下划线一级\n======\n\n下划线二级\n------\n\n'
                '\\# 这是正文\n\n```text\n# 代码里的井号\n```\n\n'
                '<script>window.quoteInjected = true</script>\n\n'
                '<img src="https://example.invalid/tracker.png" onerror="window.quoteInjected=true">')
        self.seed_text_quote(text)
        self.open_app()
        self.row_with('下划线一级').click()
        self.page.locator('#app').wait_for(state='visible')
        self.assertEqual(self.page.locator('#qt h1').inner_text(), '下划线一级')
        self.assertEqual(self.page.locator('#qt h2').inner_text(), '下划线二级')
        self.assertEqual(self.page.locator('#qt h3,#qt h4,#qt h5,#qt h6').count(), 0)
        self.assertIn('# 这是正文', self.page.locator('#qt').inner_text())
        self.assertIn('# 代码里的井号', self.page.locator('#qt pre').inner_text())
        self.assertFalse(self.page.evaluate('Boolean(window.quoteInjected)'))
        self.assertEqual(self.page.locator('#qt script,#qt iframe,#qt img').count(), 0)
        self.assertFalse(any('example.invalid' in url for url in self.blocked), '展示不应尝试加载外网图片')

    def test_heading_sizes_are_distinct_from_body_in_editor_menu_and_display(self):
        text = '\n\n'.join(['正文示例'] + ['#' * level + ' 标题' + str(level) for level in range(1, 7)])
        self.seed_text_quote(text)
        self.open_app()
        self.row_with('正文示例').click()
        self.page.locator('#app').wait_for(state='visible')

        def assert_size_ladder(host):
            sizes = [host.locator('h' + str(level)).evaluate('(e) => parseFloat(getComputedStyle(e).fontSize)')
                     for level in range(1, 7)]
            sizes.append(host.locator('p').first.evaluate('(e) => parseFloat(getComputedStyle(e).fontSize)'))
            self.assertTrue(all(a > b for a, b in zip(sizes, sizes[1:])), sizes)

        assert_size_ladder(self.page.locator('#qt'))
        self.page.locator('#btnEdit').click()
        editor = self.page.locator('#cedit .vditor-ir .vditor-reset')
        editor.wait_for(state='visible')
        assert_size_ladder(editor)
        editor.locator('p').first.click()
        self.page.wait_for_function('() => !document.querySelector("#cedit button[data-type=headings]").classList.contains("vditor-menu--current")')
        self.page.locator('#cedit button[data-type="headings"]').click()
        self.page.locator('#cedit button[data-tag="h1"]').wait_for(state='visible')
        sizes = [self.page.locator('#cedit button[data-tag="h' + str(level) + '"]').evaluate(
            '(e) => parseFloat(getComputedStyle(e).fontSize)') for level in range(1, 7)]
        sizes.append(self.page.locator('#cedit button[data-tag="p"]').evaluate(
            '(e) => parseFloat(getComputedStyle(e).fontSize)'))
        self.assertTrue(all(a > b for a, b in zip(sizes, sizes[1:])), sizes)
        self.page.set_viewport_size({'width': 390, 'height': 844})
        for level in (1, 6):
            option = self.page.locator('#cedit button[data-tag="h' + str(level) + '"]')
            option.scroll_into_view_if_needed()
            box = option.bounding_box()
            self.assertGreaterEqual(box['x'], 0)
            self.assertLessEqual(box['x'] + box['width'], 390)
            self.assertGreaterEqual(box['y'], 0)
            self.assertLessEqual(box['y'] + box['height'], 844)

    def test_toolbar_reports_current_heading_level_without_changing_text(self):
        labels = ['一级标题', '二级标题', '三级标题', '四级标题', '五级标题', '六级标题']
        original = '\n\n'.join(['普通段落'] + ['#' * level + ' ' + label.replace('标题', '小节')
                                for level, label in enumerate(labels, 1)])
        self.seed_text_quote(original)
        self.open_app()
        self.row_with('普通段落').click()
        self.page.locator('#btnEdit').click()
        editor = self.page.locator('#cedit .vditor-ir .vditor-reset')
        editor.wait_for(state='visible')
        indicator = self.page.locator('#cedit button[data-type="headings"]')
        for tag, expected in [('p', '正文')] + [('h' + str(level), label) for level, label in enumerate(labels, 1)]:
            editor.locator(tag).click()
            self.page.wait_for_function('expected => document.querySelector("#cedit button[data-type=headings]").textContent.includes(expected)', arg=expected)
            self.assertIn(expected, indicator.inner_text())
        editor.locator('h3').click()
        self.page.keyboard.press('ControlOrMeta+a')
        self.page.wait_for_function('() => document.querySelector("#cedit button[data-type=headings]").textContent.includes("混合")')
        self.assertIn('混合', indicator.inner_text())
        editor.locator('h2').click()
        self.page.wait_for_function('() => document.querySelector("#cedit button[data-type=headings]").textContent.includes("二级标题")')
        self.page.locator('#cSave').click()
        self.page.locator('#compose').wait_for(state='hidden')
        saved = next(q for q in self.list_all() if '二级小节' in q['text'])
        self.assertEqual(saved['text'].strip(), original)
        self.page.locator('#btnEdit').click()
        editor.locator('h3').wait_for(state='visible')
        editor.locator('h3').click()
        self.page.wait_for_function('() => document.querySelector("#cedit button[data-type=headings]").textContent.includes("三级标题")')

    def test_escape_closes_heading_menu_without_discarding_draft(self):
        self.seed_text_quote('## 保留草稿的标题')
        self.open_app()
        self.row_with('保留草稿的标题').click()
        self.page.locator('#btnEdit').click()
        editor = self.page.locator('#cedit .vditor-ir .vditor-reset')
        editor.locator('h2').wait_for(state='visible')
        editor.locator('h2').click()
        self.page.keyboard.press('End')
        self.page.keyboard.type('DRAFT-UNSAVED')
        indicator = self.page.locator('#cedit button[data-type="headings"]')
        indicator.focus()
        self.page.keyboard.press('Enter')
        self.page.locator('#cedit button[data-tag="h2"]').wait_for(state='visible')
        self.page.keyboard.press('Escape')
        self.assertTrue(self.page.locator('#compose').is_visible(), '关闭标题菜单不能退出编辑或丢弃草稿')
        self.assertIn('DRAFT-UNSAVED', editor.inner_text())
        self.assertEqual(indicator.get_attribute('aria-expanded'), 'false')
        self.assertTrue(indicator.evaluate('(e) => document.activeElement === e'))

    def test_editor_content_column_is_centered_and_responsive(self):
        quote = self.seed_text_quote('居中编辑内容栏。\n\n1. 第一项内容')
        self.page.set_viewport_size({'width': 1600, 'height': 1000})
        self.open_app()
        self.row_with('居中编辑内容栏').click()
        self.page.locator('#btnEdit').click()
        editor = self.page.locator('#cedit .vditor-ir .vditor-reset')
        editor.wait_for(state='visible')
        box = self.page.locator('#cbox').bounding_box()
        self.assertLessEqual(box['width'], 960, '宽屏编辑内容应限宽，而非横铺整个窗口')
        self.assertAlmostEqual(box['x'], 1600 - box['x'] - box['width'], delta=1)
        self.assertGreater(box['x'], 150, '宽屏两侧应留出内容栏外的留白')
        padding = editor.evaluate('(el) => parseFloat(getComputedStyle(el).paddingLeft)')
        self.assertGreaterEqual(padding, 14, '文字应有合理内边距')
        self.assertIn(editor.evaluate('(el) => getComputedStyle(el).textAlign'), ['start', 'left'])
        editor.locator('li').click()
        self.page.keyboard.press('End')
        self.page.keyboard.type('追加前')
        self.page.set_viewport_size({'width': 375, 'height': 812})
        self.page.keyboard.type('追加后')
        narrow = self.page.locator('#cbox').bounding_box()
        self.assertGreaterEqual(narrow['x'], 0)
        self.assertLessEqual(narrow['x'] + narrow['width'], 375)
        self.assertGreaterEqual(narrow['width'], 320)
        self.assertLessEqual(editor.evaluate('(el) => el.scrollWidth - el.clientWidth'), 1)
        self.assertIn('第一项内容追加前追加后', editor.locator('li').inner_text())
        self.page.locator('#cSave').click()
        self.page.locator('#compose').wait_for(state='hidden')
        self.assertIn('第一项内容追加前追加后', self.find(quote['id'])['text'])

    def test_new_list_item_caret_stays_on_the_text_line(self):
        self.open_app()
        self.page.locator("#btnNew").click()
        editor = self.page.locator("#cedit .vditor-ir .vditor-reset")
        editor.wait_for(state="visible")
        self.page.add_style_tag(content="#cedit .vditor-reset {caret-color: #ff00ff}")

        def caret_top(item):
            for _ in range(5):
                image = base64.b64encode(item.screenshot(caret="initial")).decode("ascii")
                top = self.page.evaluate("""async png => {
                    const image = new Image();
                    image.src = 'data:image/png;base64,' + png;
                    await image.decode();
                    const canvas = document.createElement('canvas');
                    canvas.width = image.width;
                    canvas.height = image.height;
                    const context = canvas.getContext('2d');
                    context.drawImage(image, 0, 0);
                    const pixels = context.getImageData(0, 0, image.width, image.height).data;
                    for (let i = 0; i < pixels.length; i += 4) {
                        if (pixels[i] > 200 && pixels[i + 1] < 80 && pixels[i + 2] > 200) {
                            return Math.floor(i / 4 / image.width);
                        }
                    }
                    return -1;
                }""", image)
                if top >= 0:
                    return top
            self.fail("截图中没有可见的光标")

        self.page.keyboard.type("1. ")
        self.page.keyboard.insert_text("第一项列表文字。")
        editor.locator("ol > li").first.wait_for()
        self.page.keyboard.press("Enter")
        second = editor.locator("ol > li").nth(1)
        blank_top = caret_top(second)
        self.page.keyboard.insert_text("第二项")
        self.assertEqual(second.inner_text(), "第二项")
        typed_top = caret_top(second)
        self.assertAlmostEqual(blank_top, typed_top, delta=2)
        self.page.keyboard.press("Enter")
        self.page.keyboard.press("Enter")
        self.page.keyboard.insert_text("退出列表后的正文")
        self.assertEqual(editor.locator("ol > li").count(), 2)
        self.assertEqual(editor.locator("p").last.inner_text(), "退出列表后的正文")
        self.page.locator("#cSave").click()
        self.page.locator("#compose").wait_for(state="hidden")
        saved = next(quote for quote in self.list_all() if "第一项列表文字" in quote["text"])
        self.assertRegex(saved["text"], r"1\. 第一项列表文字。\n2\. 第二项\n+退出列表后的正文")

    def test_long_body_stays_readable_after_window_resize(self):
        text = '\n\n'.join('第%d段正文需要可以滚动阅读。' % i for i in range(1, 81))
        self.seed_text_quote(text)
        self.open_app()
        self.row_with('第1段正文').click()
        self.page.locator('#app').wait_for(state='visible')
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.page.wait_for_function('() => document.querySelector("#quote").scrollHeight > document.querySelector("#quote").clientHeight')
        self.assertEqual(self.page.locator('#quote').evaluate('(e) => getComputedStyle(e).overflowY'), 'auto')
        self.page.locator('#qt p').last.scroll_into_view_if_needed()
        self.assertTrue(self.page.locator('#qt p').last.is_visible())
        self.assertIn('第80段正文', self.page.locator('#qt p').last.inner_text())
        self.assertLessEqual(self.page.locator('#qt p').first.evaluate('(e) => parseFloat(getComputedStyle(e).fontSize)'), 28)

    def test_choose_heading_level_in_toolbar_and_change_it(self):
        self.open_app()
        self.page.locator('#btnNew').click()
        editor = self.page.locator('#cedit .vditor-ir .vditor-reset')
        editor.wait_for(state='visible')
        editor.click()
        self.page.keyboard.type('通过工具栏设置标题')
        self.page.locator('#cedit button[data-type="headings"]').click()
        self.page.locator('#cedit button[data-tag="h2"]').click()
        editor.locator('h2').wait_for(state='visible')
        self.page.locator('#cSave').click()
        self.page.locator('#compose').wait_for(state='hidden')
        self.row_with('通过工具栏设置标题').click()
        self.page.get_by_role('heading', name='通过工具栏设置标题', level=2).wait_for(state='visible')
        self.page.locator('#btnEdit').click()
        editor.locator('h2').wait_for(state='visible')
        editor.locator('h2').click()
        self.page.wait_for_function('() => document.querySelector("#cedit button[data-type=headings]").classList.contains("vditor-menu--current")')
        self.page.locator('#cedit button[data-type="headings"]').click()
        self.page.locator('#cedit button[data-tag="h3"]').click()
        editor.locator('h3').wait_for(state='visible')
        self.page.locator('#cSave').click()
        self.page.locator('#compose').wait_for(state='hidden')
        self.page.get_by_role('heading', name='通过工具栏设置标题', level=3).wait_for(state='visible')
        saved = next(q for q in self.list_all() if '通过工具栏设置标题' in q['text'])
        self.assertTrue(saved['text'].strip().startswith('### '))
        self.page.locator('#btnEdit').click()
        editor.locator('h3').wait_for(state='visible')
        editor.locator('h3').click()
        self.page.wait_for_function('() => document.querySelector("#cedit button[data-type=headings]").textContent.includes("三级标题")')
        self.page.locator('#cedit button[data-type="headings"]').click()
        self.page.locator('#cedit button[data-tag="p"]').click()
        editor.locator('p').wait_for(state='visible')
        self.page.wait_for_function('() => document.querySelector("#cedit button[data-type=headings]").textContent.includes("正文")')
        self.page.locator('#cSave').click()
        self.page.locator('#compose').wait_for(state='hidden')
        self.assertEqual(self.page.locator('#qt h1,#qt h2,#qt h3,#qt h4,#qt h5,#qt h6').count(), 0)
        self.assertIn('通过工具栏设置标题', self.page.locator('#qt p').inner_text())

    def test_wallpaper_keeps_complete_body_and_heading_hierarchy(self):
        self.seed_text_quote('Wallpaper body first.\n\n## Wallpaper heading\n\nWallpaper body last.')
        self.open_app()
        self.row_with('Wallpaper body first.').click()
        self.page.locator('#app').wait_for(state='visible')
        drawn = self.capture_wallpaper()
        visible = ' '.join(item['text'] for item in drawn)
        for text in ('Wallpaper body first.', 'Wallpaper heading', 'Wallpaper body last.'):
            self.assertIn(text, visible)
        body_font = next(item['font'] for item in drawn if item['text'] == 'Wallpaper body first.')
        heading_font = next(item['font'] for item in drawn if item['text'] == 'Wallpaper heading')
        size = lambda font: float(re.search(r'([0-9.]+)px', font).group(1))
        self.assertGreater(size(heading_font), size(body_font))

    def capture_wallpaper(self):
        self.page.evaluate('''() => {
            window.canvasText = [];
            const draw = CanvasRenderingContext2D.prototype.fillText;
            CanvasRenderingContext2D.prototype.fillText = function(text, ...args) {
                window.canvasText.push({text, font: this.font, x: args[0], y: args[1],
                                       width: this.measureText(text).width,
                                       canvasWidth: this.canvas.width, canvasHeight: this.canvas.height});
                return draw.call(this, text, ...args);
            };
        }''')
        requests = []
        def wallpaper_response(route):
            requests.append(route.request.post_data_json)
            route.fulfill(status=200, content_type='application/json',
                          body='{"ok":true,"applied":false,"path":"/tmp/disposable-wallpaper.png"}')
        self.page.route('**/wallpaper', wallpaper_response)
        self.page.locator('#btnWall').click()
        self.page.wait_for_function('() => document.querySelector("#btnWall").textContent === "壁纸"')
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0]['png'].startswith('data:image/png;base64,'))
        return self.page.evaluate('window.canvasText')

    def test_task_status_and_wallpaper_preserve_all_visible_content(self):
        text = ('# 导出示例\n\n- [ ] TODO\n- [x] DONE\n\n'
                '<div>raw block <strong>strong</strong></div>\n\n'
                '1. parent\n   - child\n     - grandchild\n\n'
                '```\nfirst_line\nsecond_line\nthird_line\n```\n\n' + 'A' * 200)
        self.seed_text_quote(text)
        self.open_app()
        self.row_with('导出示例').click()
        self.page.locator('#app').wait_for(state='visible')
        boxes = self.page.locator('#qt input[type="checkbox"]')
        self.assertEqual(boxes.count(), 2)
        self.assertFalse(boxes.nth(0).is_checked())
        self.assertTrue(boxes.nth(1).is_checked())
        self.assertTrue(boxes.nth(0).is_disabled() and boxes.nth(1).is_disabled())
        drawn = self.capture_wallpaper()
        visible = ' '.join(item['text'] for item in drawn)
        self.assertIn('raw block strong', visible)
        for line in ('first_line', 'second_line', 'third_line'):
            self.assertTrue(any(item['text'].strip() == line for item in drawn), line)
        for item in drawn:
            self.assertNotIn('\n', item['text'])
            self.assertLessEqual(item['x'] + item['width'], item['canvasWidth'] + 1, item)
        self.assertEqual(sum(item['text'].count('A') for item in drawn), 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
