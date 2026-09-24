你是 Issue 7 会话分期 CSV 收口执行线程。

在 `/Users/xuqinghe/sleep/main-development` 的现有未提交 Issue 7 工作区修复[主线程验收发现](../../reports/ISSUE7_SESSION_CSV_ACCEPTANCE.md)。保留全部 dirty/untracked、Git index、原 246 passed 报告和历史证据；不重置或清理。权威需求为[远端 Issue #7](https://github.com/GTOING/curry8-netstream/issues/7)及[原执行任务](ISSUE7_SESSION_CSV_EXECUTION.md)；远端暂不可访问时，验收记录已写明本轮两项精确差异。只做这两项局部修复，不实施 Issue #8。

## 修复目标

1. 将 CSV 的单列 `relative_seconds` 改为 `relative_start_s` 和 `relative_end_s`。自动续写与从有效前缀重建使用同一列定义和计算：以本 session 首个完整窗口起始采样点为零点，按原始采样率计算起点及排他终点；例如 10 Hz、`[9000,9300)` 和 `[9300,9600)` 应分别给出 `0–30` 秒、`30–60` 秒。保持 `start_sample`、`end_sample_exclusive` 原样，并同步更新操作指南、执行报告和有关断言；不要将本机接收时刻或模型输入的 100 Hz 当作采样时间原点。
2. CSV 追加失败后不再为后续窗口积存 `_stage_csv_contexts`，也不让后续结果反复尝试已禁用的派生写入。正常成功路径仍逐块释放临时元数据；保持 JSONL/NPY 单写者、显示的 CSV 失败状态、NoModel/失败行语义和必要 Rally Stop 收尾。用故障发生后继续处理多个窗口的测试证明缓存不会随会话增长，同时权威结果和 Stop 事件照常完成。

## 验证与回报

先运行受影响测试，并覆盖自动续写与旧会话重建的两列时间区间、非零起始样本、CSV 故障后的多块处理。修复后完成最终完整回归、必要编译和 `git diff --check`；原 246 passed 证据保留，按新实际结果追加到 `reports/ISSUE7_SESSION_CSV_REPORT.md`。若 GUI 布局未变，不必重复截图。测试只用合成数据和随机本机假端；不连接真实 Curry/Rally、生产 8801、COM、刺激设备或受试者，不以离屏验证宣称 Windows Excel 通过。

不修改主线程验收记录或共享队列，不提交、推送、合并、评论 issue 或再分发任务。完成后回报主线程复核。
