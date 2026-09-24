# Issue 7 主线程工程验收

日期：2026-09-24。结论：**暂不通过，需完成两项局部收口**。本结论只涉及会话 CSV 工程合同；Windows Excel、真实 Curry/Rally、时钟同步与物理输出仍未验证。

执行者报告 `uv sync --locked`、完整回归 **246 passed**、编译和 `git diff --check` 通过，并交付[执行报告](ISSUE7_SESSION_CSV_REPORT.md)、[操作指南](../docs/SESSION_RECORDING_GUIDE.md)和 800×600 离屏截图。主线程按代码和关键测试复核，没有重跑全量；本轮实际 `git diff --check` 通过。可选 GUI、同一 session 单写者逐结果追加、同一瞬间生成带偏移本机时间与 UTC、旧 v1 回放重建，以及 CSV 故障不阻断 JSONL 与必要 Rally Stop 的主路径均有实现和合成证据。

## 待收口

1. [Issue #7](https://github.com/GTOING/curry8-netstream/issues/7)明确要求 `relative_start_s` 与 `relative_end_s`，让用户直接看到该标签对应窗口的相对时间区间。目前 [CSV 表头](../src/sleep_stim_controller/recording.py)只有 `relative_seconds`，写入与重建均只计算起点；[测试](../tests/test_issue7_session_csv.py)也只断言 `0, 30`，没有校验第一个窗口的终点为 30 秒、第二个为 60 秒。虽然采样点和采样率允许事后计算，交付表本身尚未满足约定列。
2. 在 CSV 首次追加失败后，`_mark_stage_csv_failed()` 清空缓存，但后续 `save_block()`仍因 `stage_csv_enabled` 为真不断把新窗口加入 `_stage_csv_contexts`；`_append_stage_csv_result()`见到 `stage_csv_error` 就直接返回，不会弹出这些窗口。长时间会话在派生 CSV 已禁用后会持续增长无用内存。应在失败状态停止收集或保证逐块释放，继续保留权威记录与必要 Stop 的现有故障隔离行为。

限界修复见 [Issue 7 收口任务](../docs/tasks/ISSUE7_SESSION_CSV_CLOSEOUT.md)。保留执行者原 246 passed 记录与本次未验证边界；本轮不改实现、不提交推送、不关闭远端 issue。

## 收口复核（2026-09-24，替代上方暂不通过结论）

**Issue 7 工程范围验收通过。** 上方暂不通过是首次回报的历史结论。

- `recording.py` 的表头、实时写入与回放重建统一使用 `relative_start_s` 和 `relative_end_s`。原始 10 Hz、非零起点 `[9000,9300)` 与 `[9300,9600)` 在测试中分别对应 `0–30` 秒、`30–60` 秒；指南与执行报告已同步。
- `save_block()` 在 `stage_csv_error` 后不再登记派生 CSV 上下文；`save_processing_result()` 不再尝试失败后的追加。八块故障测试断言首次写入失败后后续上下文数为零、仅一次追加尝试，同时全部权威处理结果和必要 Rally Start/Stop 控制事件仍可读。
- 执行者收口后定向 **5 passed**、完整回归 **246 passed**，编译及 `git diff --check` 通过；原 246 passed 记录保留。主线程复核关键实现、测试和报告，未重复运行全量；本轮实际 `git diff --check` 通过。

该结论只确认合成环境中的软件工程能力。Windows 原生窗口/Excel、真实 Curry/Rally、生产端口及物理输出仍未验证；本次未提交、推送或关闭远端 Issue #7。
