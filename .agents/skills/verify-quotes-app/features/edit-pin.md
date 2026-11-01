# 编辑与置顶

用户在目录或海报中编辑金句、置顶或取消置顶。

## Sub-features

- `edit-quote` 编辑正文、出处和用途；旧版本冲突不能静默覆盖。
- `pin-toggle` 置顶或取消置顶并在重载后保留状态。

## How to get to it (user POV)

从目录行的操作按钮或金句海报的“编辑”“置顶”进入。

## Driving it with Playwright

Preconditions: 按验证入口使用独立数据和随机端口。

- 使用浏览器打开隔离实例。分别点击目录行和海报入口，编辑或置顶后检查页面与隔离 JSON，重启再检查状态。
- 运行 `PYTHONPATH=tests /usr/bin/python3 -m unittest test_mutations -v` 可验证接口写入、冲突和持久化。该命令不证明目录或海报入口都已被点击。

## Gotchas

- 置顶界面会先乐观更新，必须等待服务端响应并检查保存结果。
- 编辑与置顶的全部 UI 入口尚未由本地图的现成脚本覆盖。
