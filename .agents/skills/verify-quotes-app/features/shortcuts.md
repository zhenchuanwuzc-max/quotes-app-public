# 快捷键设置与操作

## Sub-features

- 列表 A 新增，N 保留兼容；E 编辑选中金句；编辑器 Mod+S 保存，Mod+Enter 保留兼容。
- 快捷键菜单显示动作、主键、适用界面，可修改、取消、恢复默认；冲突不可保存。
- 草稿仅保存成功后生效；错误留在面板内，允许重试；本机设置重启后保留。
- 输入框、编辑器、下拉控件和中文输入法不触发背景单字符操作。

## How to get to it (user POV)

目录或展示页点击“快捷键”，修改下拉框后点击“保存设置”。恢复默认后仍需保存。固定导航及兼容键在面板内查看。

## Driving it with Playwright

使用临时 HOME、数据目录和随机端口；从根目录运行：

`PYTHONPATH=tests /usr/bin/python3 -m unittest test_shortcuts test_browser.BrowserUsageTest.test_add_edit_save_shortcuts_respect_typing_fields test_browser.BrowserUsageTest.test_shortcut_menu_change_cancel_restore_and_restart test_browser.BrowserUsageTest.test_shortcut_menu_conflict_failed_save_and_escape -v`

检查取消、冲突、503失败、修改前后实际按键、新浏览器与服务重启、恢复默认和焦点范围。

## Gotchas

- Mod 指 Mac 的 Command 或其他平台的 Ctrl。
- 设置属于本机；不修改金句内容，也不随金句同步到其他电脑。
- 保存主键支持 Mod+S 或 Mod+Shift+S；不拦截系统编辑组合键。
- 浏览器验收不替代原生窗口焦点与性能验收。
