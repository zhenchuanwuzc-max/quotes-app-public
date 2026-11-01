# 金句 App 功能验证地图

基线：代码仓当前工作树。每次使用时先核对 `git status` 与 `git rev-parse --short HEAD`，再按 [验证入口](../SKILL.md) 启动隔离实例。测试只使用 `quotes.example.json` 或合成数据。

每个入口都要分别验收。浏览器操作要观察界面变化，涉及写入时再核对隔离的 JSON 或附件文件。记录未走过的入口和跳过原因；已有测试名称不等于本轮已经执行。

| 功能 | 主要入口 | 路径 |
| --- | --- | --- |
| 浏览与搜索 | 目录、海报、搜索框 | [browse-search.md](browse-search.md) |
| 新增与删除 | 目录新增、海报删除 | [create-delete.md](create-delete.md) |
| 编辑与置顶 | 目录行和海报 | [edit-pin.md](edit-pin.md) |
| 标题与正文层级 | 编辑器标题工具、金句展示 | [headings.md](headings.md) |
| 图片与视频来源 | 新增或编辑、缩略图、海报 | [images.md](images.md) |
| 多设备同步 | 同步按钮及后台同步 | [sync.md](sync.md) |

图片、同步、壁纸、原生窗口和真实设备的验证范围各自独立。隔离环境不代表系统壁纸或更新操作也被隔离。
