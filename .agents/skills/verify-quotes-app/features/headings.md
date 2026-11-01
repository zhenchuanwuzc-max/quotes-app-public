# 标题与正文层级

普通段落按正文展示，H1–H6 按实际 Markdown 级别展示。首段位置和内容长短不改变标题级别。

## Sub-features

- `heading-body` 普通多段正文使用相同字号并保留句尾标点。
- `heading-levels` H1–H6 有语义标签及递减字号，编辑保存与重开后保持级别。
- `heading-size-preview` 正文与六级标题字号不同，编辑和展示按相同层级比例区分，现有标题菜单各项以不同字号预览。
- `heading-toolbar` 在编辑器选择或修改标题级别。
- `heading-syntax` Setext 标题、转义井号、代码块与安全 HTML 边界。
- `heading-scroll` 窄窗口长文可以滚动阅读。
- `heading-wallpaper` 壁纸图像保留完整段落和标题级别。

## How to get to it (user POV)

目录打开一条金句，再点击“编辑”。光标位于普通段落时，点击“H”选择一级至六级标题。光标位于已有标题时，点击“H”先取消标题，再点击“H”可选择新级别。保存后查看金句展示。

## Driving it with Playwright

Preconditions: 使用验证入口创建的隔离样本和随机端口，禁止连接真实数据。

- 运行 `prove-headings.py --evidence <新目录>`，实际脚本位置及完整命令见上层 SKILL.md。脚本复用 tests/test_browser.py 中相关真实浏览器路径，截图和输出保存在指定目录。
- 检查正文同字号、H1–H6 层级、工具栏从 H2 改为 H3、保存后数据及刷新后的展示。
- 检查正文与 H6 仍有字号差异；标题菜单的 H1–H6 大小递减，窄窗口中首尾选项都可完整访问。
- 壁纸路径观察真实 canvas 绘制和发出的 PNG，在浏览器网络边界拦截 `/wallpaper`。不能据此宣称 macOS 已成功设置桌面。

## Gotchas

- 原始 Markdown 保持存储格式。正文展示复用本地 Lute，目录、搜索和复制仍使用纯文本派生值。
- 原生 macOS 包装窗口的使用体验需另行验收。隔离 Chromium 不能证明原生窗口行为或跟手性。
- 地图不要求改写已有金句内容。已有 `#` 会按对应标题显示，无标题内容使用正文。
