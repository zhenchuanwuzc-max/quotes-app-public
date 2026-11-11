# 编辑内容栏布局

## Sub-features

- 宽屏编辑栏水平居中且限宽；文字保持左对齐，具有合理内边距。
- 工具栏、编辑框、添加图片和附件同列；窄屏自然铺开，不造成横向溢出。
- 缩放窗口后保留输入位置，继续输入和保存。
- 编号列表按回车后，空列表项与输入文字后的光标高度对齐。连续回车可退出列表。

## How to get to it (user POV)

目录新增或打开金句编辑，在宽窗口观察内容栏两侧留白；缩窄窗口后继续输入并保存。

## Driving it with Playwright

使用临时 HOME、数据与随机端口，从根目录运行：

`PYTHONPATH=tests /usr/bin/python3 -m unittest test_browser.BrowserUsageTest.test_editor_content_column_is_centered_and_responsive -v`

检查1600px窗口中的限宽和等距留白、正文内边距及左对齐；切换375px后无溢出、输入连续且保存结果一致。视觉改变同时保留实际截图，不能只验源码样式。

用 `QUOTES_TEST_BROWSER=webkit` 运行 `test_new_list_item_caret_stays_on_the_text_line`，比较回车后与输入后的实际光标像素，并核对保存的列表和正文。

## Gotchas

- 居中的是编辑内容区域；正文段落、编号仍按正常左对齐排版。
- 浏览器截图与原生窗口验收分开；原生已有草稿时不要为刷新布局丢弃草稿。
