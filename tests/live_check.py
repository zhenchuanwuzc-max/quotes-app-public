#!/usr/bin/env python3
"""上线后对线上服务做的只读浏览器检查（scripts/release.sh 调用；不是 unittest，不会被 discover 收进去）。

只看不改：打开首页 → 目录行数 = 接口返回的总数 → 点第一条进海报（地址带上它的 id）→ 回目录。
页面 JS 报错、发往 127.0.0.1 以外的请求（检查更新等）都会被拦下；报错算失败。
输出只有数字和步骤名，不打印任何金句内容。

  /usr/bin/python3 tests/live_check.py http://127.0.0.1:8767
退出码：0 通过，1 不通过。
"""
import json
import re
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

TIMEOUT_MS = 15_000


def main(base: str) -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(base + "/quotes?limit=1", timeout=5) as r:
        total = json.load(r)["total"]

    errors = []
    step = "启动浏览器"
    t0 = time.time()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            ctx = browser.new_context(viewport={"width": 1280, "height": 860})
            ctx.set_default_timeout(TIMEOUT_MS)
            ctx.route(re.compile(r"^https?://(?!127\.0\.0\.1[:/])"), lambda route: route.abort())
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(str(e).splitlines()[0]))

            step = "打开目录"
            page.goto(base + "/")
            if total:
                page.wait_for_function("n => document.querySelectorAll('#list .row').length === n", arg=total)
                step = "点第一条进海报"
                first = page.locator("#list .row").first
                qid = first.get_attribute("data-id")
                first.click()
                page.locator("#app").wait_for(state="visible")
                page.wait_for_function("h => location.hash === h", arg="#/q/" + qid)
                if not (page.locator("#qt").text_content() or "").strip():
                    raise AssertionError("海报是空的")
                step = "回目录"
                page.locator("#btnBack").click()
                page.locator("#index").wait_for(state="visible")
            else:
                page.locator("#list .istate").wait_for()
            if errors:
                raise AssertionError("页面 JS 报错：" + "；".join(errors))
        except Exception as e:
            print("✗ 线上浏览器检查没过（%s）：%s" % (step, str(e).splitlines()[0]))
            return 1
        finally:
            browser.close()
    print("✓ 线上浏览器检查通过：目录 %d 条全部渲染，海报可进可回（%.1f 秒）" % (total, time.time() - t0))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1].rstrip("/")))
