#!/usr/bin/env python3
"""
quotes-app 桌面 App 主入口（py2app 打包入口）
范式照抄 daily-todo：
- 后台线程跑 HTTP server
- 主线程跑 pywebview 原生 WKWebView 窗口
- macOS 菜单栏「检查更新…」（PyObjC NSMenuItem，主线程操作）
- py2app 打包成 .app bundle 装到 ~/Applications/
"""
import os
import threading
import time
import urllib.request

import server  # 同包内


URL = f"http://localhost:{server.PORT}"


def server_up() -> bool:
    try:
        urllib.request.urlopen(f"{URL}/health", timeout=1)
        return True
    except Exception:
        return False


def start_server_thread() -> None:
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    for _ in range(20):
        if server_up():
            return
        time.sleep(0.15)


_window_ref = [None]
_menu_helper = [None]


def _menu_log(msg: str) -> None:
    try:
        with open("/tmp/quotes-menu.log", "a", encoding="utf-8") as f:
            f.write(f"{msg}\n")
    except Exception:
        pass


def setup_app_menu() -> None:
    """启动后在 macOS App 菜单（最左侧 'quotes' 菜单）里加「检查更新…」。
    NSApp.mainMenu() 操作必须在主线程，否则 silent fail；pywebview 在 worker thread
    跑此 callback，故用 NSObject + performSelectorOnMainThread 切回主线程。
    （完全照抄 daily-todo desktop_app.py 的 MenuSetupHelper 模式）"""
    _menu_log("setup_app_menu called")
    try:
        from AppKit import NSApp, NSMenuItem  # type: ignore
        from Foundation import NSObject  # type: ignore
    except Exception as e:
        _menu_log(f"import error: {e}")
        return

    class MenuSetupHelper(NSObject):
        def doSetup_(self, _):  # noqa: N802 — runs on main thread
            try:
                main_menu = NSApp.mainMenu()
                if not main_menu or main_menu.numberOfItems() < 1:
                    return
                app_menu = main_menu.itemAtIndex_(0).submenu()
                if not app_menu:
                    return
                for i in range(app_menu.numberOfItems()):
                    if app_menu.itemAtIndex_(i).title() == "检查更新…":
                        return  # 已插入
                item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    "检查更新…", "checkForUpdate:", "")
                item.setTarget_(self)
                app_menu.insertItem_atIndex_(item, 1)
                app_menu.insertItem_atIndex_(NSMenuItem.separatorItem(), 2)
                self._inserted_item = item  # 保留引用防 GC
                _menu_log("INSERTED 检查更新…")
            except Exception as e:
                _menu_log(f"doSetup error: {e}")

        def checkForUpdate_(self, sender):  # noqa: N802 — menu callback (main thread)
            # evaluate_js 必须在子线程调用，否则主线程死锁（主线程等 WKWebView
            # completionHandler，而它也要回主线程 → 自锁）
            def _do():
                try:
                    w = _window_ref[0]
                    if w is not None:
                        w.evaluate_js("if (typeof checkUpdate === 'function') checkUpdate(true);")
                    _menu_log("menu clicked → checkUpdate(true)")
                except Exception as e:
                    _menu_log(f"checkForUpdate error: {e}")
            threading.Thread(target=_do, daemon=True).start()

        def scheduleSetup(self):  # noqa: N802
            self.performSelectorOnMainThread_withObject_waitUntilDone_("doSetup:", None, False)

    helper = MenuSetupHelper.alloc().init()
    _menu_helper[0] = helper  # 全局 retain 防 GC
    for delay in (0.5, 1.5, 3.0):  # 多次尝试，pywebview 菜单创建时机不固定
        threading.Timer(delay, helper.scheduleSetup).start()


def main() -> None:
    start_server_thread()
    import webview
    window = webview.create_window(
        "quotes · 金句库",
        URL,
        width=720,
        height=820,
        resizable=True,
        min_size=(480, 540),
    )
    _window_ref[0] = window
    webview.start(setup_app_menu)


if __name__ == "__main__":
    main()
