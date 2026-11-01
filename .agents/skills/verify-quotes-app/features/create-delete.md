# 新增与删除

用户在目录新增金句，在海报中删除金句。

## Sub-features

- `create-quote` 输入正文和出处并存入。
- `delete-quote` 确认删除后，目录和隔离数据不再包含该条。

## How to get to it (user POV)

目录点击“新增”。打开一条金句后在海报点击“删除”。

## Driving it with Playwright

Preconditions: 按验证入口使用独立数据和随机端口。

- 运行 `prove-browser.py`。`03-draft.png`、`04-saved.png` 和 `result.txt` 分别记录输入、保存和数据文件核对。
- 覆盖删除时运行 `PYTHONPATH=tests /usr/bin/python3 -m unittest test_browser.BrowserUsageTest.test_add_then_delete_through_the_ui -v`。测试在浏览器新增、打开并确认删除，并检查列表 API 的变化。

## Gotchas

- 删除测试自动接受确认对话框；只允许在隔离数据上运行。
- helper 验证保存，不验证删除。
