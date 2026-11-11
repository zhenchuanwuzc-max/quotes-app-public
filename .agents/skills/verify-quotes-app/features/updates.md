# 检查更新与版本提示

## Sub-features

检查当前版本与最新公开发布。匿名 GitHub API 限流时回退到公开发布页；刷新请求避免中间缓存隐藏新版本。发现新版时展示版本和查看入口。检查失败显示原因并结束进度状态。

下载、安装、服务重启和原生窗口分别验收。检查成功不证明安装成功。

## How to get to it (user POV)

原生 App 菜单选择“检查更新…”。它调用当前页面的 `checkUpdate(true)`。页面启动后也会后台检查。发现新版时展示更新提示条，用户可查看、忽略或选择立即更新。

## Driving it with Playwright and isolated Python tests

先按 `../SKILL.md` 的隔离启动和 Doctor 检查。通用 `prove-browser.py` 会阻断更新、同步和壁纸路由，所以其通过不表示本功能已验证。

后端回归入口为 `/usr/bin/python3 -m unittest discover -s tests -p test_updates.py -v`。它验证 API 限流回退、无发布错误、原样保留发布元数据、公开下载不发送令牌、以及缓存不能隐藏新版本。外部网络边界由合成响应隔离。

UI 回归入口为 `PYTHONPATH=tests /usr/bin/python3 -m unittest test_browser.BrowserUsageTest.test_update_check_does_not_require_a_token_for_public_releases -v`。它使用本机示例服务，在 Playwright 的 `**/check-update` 路由注入合成当前/最新版本，再调用 `checkUpdate(true)`，观察 `#ubar` 与 `#ubarVer`。这证明页面提示，不证明真实网络或原生菜单点击。

状态补查可在同一隔离页面通过路由响应分别模拟当前已最新、最新字段为空、`error` 和请求失败。检查 `#statusText` 文案与 `#statusBar` 进度结束。等待中的请求应保留检查状态。先拦截路由再触发页面函数，避免自动后台检查混入真实网络。

真实发布验收由协调者在独立授权下读取 `/version`、`/check-update`，核对当前和最新版本；下载公开 ZIP 到临时目录，核 SHA-256、Info.plist 和打包资源。仅运行源检查或 UI mock 不足以完成该项。

## Gotchas

- 不从隔离验证实例调用 `/install-update`。该接口会下载、替换真实 `~/Applications/quotes.app` 并重启。
- 不为匿名公开发布检查临时复制用户令牌。公开下载地址不应携带 API 认证头。
- 完成浏览器函数调用不等于点击了 macOS 原生菜单；菜单、原生启动和性能结果单列。
- 记录实际源提交、安装包版本、发布地址、下载摘要与未测项。真实多机同步和完整自动安装链路不由这些测试证明。
