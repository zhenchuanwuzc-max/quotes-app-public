# 多设备同步

数据仓通过 Git 同步金句和图片。用户可在界面手动触发，服务也会安排后台同步。

## Sub-features

- `sync-merge` 不同设备的变更按合并规则处理。
- `sync-attachments` 图片随数据纳入同步。
- `sync-ui` 手动同步显示实际结果。

## How to get to it (user POV)

目录点击同步按钮，或等待后台同步。手动同步会访问数据仓配置的远端。

## Driving it with Playwright

Preconditions: 默认验证实例的数据目录没有 `sync.sh`，不可把它的同步失败当作真实同步结论。

- 运行 `PYTHONPATH=tests /usr/bin/python3 -m unittest test_merge test_data_sync -v`，验证临时 Git 仓中的合并、附件提交和模拟发版边界。
- 如需证明真正的跨设备同步，须另行安排隔离的私有远端与两套独立数据目录，并记录两端数据及图片。现有测试不证明真实设备同步。

## Gotchas

- 不运行真实数据仓的 `sync.sh` 做试验。它会提交、拉取和推送。
- 浏览器内同步按钮和外部网络边界不在默认 helper 覆盖范围。
