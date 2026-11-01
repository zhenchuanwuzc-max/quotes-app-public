# 记住出处与用途

新增金句默认带入上次成功新增或编辑保存的出处与用途。编辑已有条目保留自身字段；取消、失败和版本冲突不改变默认值。

## Sub-features

- 新增和编辑成功后更新默认值，清空后成功保存也记为空。
- 重新打开 App 后保留；原生窗口私密模式不影响数据侧保存。
- 老数据没有默认值记录时，从最近编辑或新增的金句推导。
- 合并选择较新的默认值整组记录，保留清空操作。

## How to get to it (user POV)

保存一条金句后，返回目录点“新增”，查看出处和用途。编辑已有金句时，表单显示该条原有内容。

## Driving it with Playwright

Preconditions: 使用项目验证入口的隔离示例数据，不操作真实金句。

从仓库根运行 `PYTHONPATH=tests /usr/bin/python3 -m unittest test_browser.BrowserUsageTest.test_last_saved_metadata_defaults_survive_edit_cancel_and_restart test_browser.BrowserUsageTest.test_failed_save_does_not_replace_defaults_and_empty_save_clears_them test_merge.MergeDriverTest.test_compose_defaults_merge_latest_in_both_directions_including_clear -v`。

检查新增、编辑、取消、409、503、清空保存，以及服务重启后用全新浏览器环境打开的表单值。

## Gotchas

- 默认只包含出处与用途，正文、链接和图片不跟随上次条目。
- 服务端与金句使用同一原子写入保存默认值，不依赖浏览器 localStorage。
- 临时双向合并测试不等于真实跨设备同步验收。
