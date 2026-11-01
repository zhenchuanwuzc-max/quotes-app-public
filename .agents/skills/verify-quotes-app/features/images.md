# 图片与视频来源

用户可保存图片金句、查看大图，并回到原视频链接。

## Sub-features

- `image-upload-edit` 上传、编辑并移除图片。
- `image-view` 目录缩略图和海报大图。
- `source-link` 展示安全的视频来源链接。
- `upload-retry` 上传失败后保留草稿并重试。

## How to get to it (user POV)

在新增或编辑面板添加图片，或从带图的目录行打开海报并点击图片。

## Driving it with Playwright

Preconditions: 按验证入口使用独立数据和随机端口。使用测试中的合成图片。

- 运行 `PYTHONPATH=tests /usr/bin/python3 -m unittest test_browser.BrowserUsageTest.test_upload_image_only_edit_preserve_and_remove test_browser.BrowserUsageTest.test_video_image_thumbnail_detail_and_return_link test_browser.BrowserUsageTest.test_failed_image_upload_can_retry_without_losing_draft -v`。
- 要取视觉证据时，另行截取上传、保存、放大和关闭大图的界面，并核对隔离数据目录里的附件文件。

## Gotchas

- 示例数据默认没有视频图片，现有测试会创建合成图片和来源。
- 浏览器测试验证应用内展示，不等于真实视频扩展采集或跨设备附件同步。
