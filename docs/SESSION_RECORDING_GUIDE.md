# 会话记录与分期 CSV

在桌面端展开“会话记录与离线回放”，先选择保存父目录，再勾选“启用本次会话记录”。默认只保存 v1 权威会话归档；如需表格，可另勾选“同时自动生成分期 CSV”。CSV 选项默认关闭，且只有会话记录启用时可选。连接后两项设置都会锁定。

每次连接都会使用新的 session 标识和目录，不会向旧 session 追加：

```text
<保存父目录>/session_<session_id>/
  manifest.json
  events.jsonl
  blocks/*.npy
  stage_labels.csv       # 仅显式启用自动 CSV 时创建
```

CSV 由原会话写线程在每个 `processing_result` JSONL 事件持久化后追加并 flush 一行，不等待采集结束。它是便于检查与导入的派生表；`events.jsonl` 和 `blocks/*.npy` 仍是权威记录。文件使用标准 CSV 转义和 UTF-8 BOM，可直接用 Windows Excel 打开。NoModel、失败和取消结果也有对应行；没有真实成功输出时，`stage` 和 `confidence` 为空。

`stage_labels.csv` 的列包括：

- `session_id`、`block_id`：会话与分析窗口身份。
- `start_sample`、`end_sample_exclusive`、`sample_rate_hz`：原始采样范围和采样率；结束边界不包含在窗口内。
- `relative_start_s`、`relative_end_s`：相对本 session 首个完整分析窗口起始采样点的起点与排他终点秒数，按原始采样率计算，不是墙上时钟。例如原始采样率为 10 Hz 时，`[9000,9300)` 对应 `0–30` 秒，`[9300,9600)` 对应 `30–60` 秒。
- `window_received_local_iso`、`received_utc`：最后贡献网络包在本机接收边界记录的一对本地偏移时间和 UTC 时间。
- `local_time_source`：`captured_at_receive` 表示本机时间当时已记录；`unavailable` 表示旧会话没有该字段。
- `status`、`stage`、`confidence`、`reason`、`model_id`：原处理状态与模型输出/原因。

本机时间是应用处理网络包时记录的墙上时间，不是 Curry EEG 采样的精确采集时刻、设备间同步时钟、模型完成时间、CSV 写入时间，也不是 Rally/硬件输出时间。一个网络包可能完成多个分析窗口，它们会共享这个接收时刻。请勿用该字段推断 Curry 与 Rally 的同步或刺激硬件时序。

对于没有本机时间字段的旧 v1 会话，重新导出时 `window_received_local_iso` 留空、`local_time_source` 为 `unavailable`，原有 `received_utc` 保留；程序不会用当前电脑时区伪造历史本机时间。

## 重新导出与故障

结束实时会话并确认写者已退出后，打开该会话的只读回放，点击“导出/重新生成 CSV”。导出按 `block_saved`、`processing_result` 和对应 `.npy` 可验证的连续前缀构建；缺行、重复行或末尾半行都会被替换。程序在 session 目录内写临时文件并原子替换 `stage_labels.csv`，不修改 `events.jsonl` 或 `.npy`。在 Windows 上若 CSV 正被 Excel 打开，请先关闭 Excel 中的文件再重建；替换失败会在界面报告，原 CSV 保持不动。中断或失败会话只输出可验证前缀，回放状态会标明不完整。

实时写者运行期间不能手动重建。若启用自动 CSV 时新文件无法创建，连接前初始化失败，Curry 网络采集不会开始。若采集中追加 CSV 失败，界面显示具体失败/不完整状态；应用继续尽力写入权威 JSONL/NPY 和处理结果，CSV 不会被报告为成功，必要的 Rally Stop/控制事件也继续按原收尾路径执行。结束后可在回放中从权威记录重新生成表格。

当前实现与验证记录见 [Issue 7 执行报告](../reports/ISSUE7_SESSION_CSV_REPORT.md)。Windows 原生窗口和 Excel 的实际打开检查需要 Windows 环境；macOS 离屏 GUI 或合成测试不能替代该项验证。
