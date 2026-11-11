# 浏览与搜索

用户从目录打开金句海报、翻页，并按内容、出处或用途搜索。

## Sub-features

- `browse-list` 目录显示示例金句。
- `browse-poster` 进入海报、翻下一条并返回目录。
- `search-match-empty-clear` 搜索有结果、无结果并清空。

## How to get to it (user POV)

打开首页后使用左栏搜索和分类、右侧目录行以及海报翻页。

## Driving it with Playwright

Preconditions: 按验证入口使用独立数据和随机端口。

- 运行 `prove-browser.py`。`01-list.png` 显示示例目录；`02-search.png` 显示搜索 `Drucker` 的结果。
- 覆盖海报和无结果时，从仓库根运行 `PYTHONPATH=tests /usr/bin/python3 -m unittest test_browser.BrowserUsageTest.test_browse_poster_back_and_search -v`。测试点击目录行和 `#btnNext`、`#btnBack`，再检查搜索命中、空结果和清空。
- `test_directory_controls_stay_to_the_left_of_the_quotes` 核对宽窄窗口的左栏位置，并操作置顶筛选、全部和搜索。

## Gotchas

- 前端在载入列表后筛选，操作前等待 `#list .row` 出现。
- 单次 helper 只证明目录和搜索命中。海报和无结果需运行另一个测试或单独取证。
