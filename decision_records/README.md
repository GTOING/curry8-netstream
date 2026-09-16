# 决策与任务入口

## 当前入口

- [项目开发入口](../README.md)

- [当前任务队列](ACTIVE_QUEUE.md)
- [参考材料归档](../reference/README.md)

## 有效合同与计划

- [睡眠分期电刺激控制器需求整理](../docs/SLEEP_STIM_REQUIREMENTS.md)：记录用户已明确目标与待定项，尚未冻结详细接口、刺激参数或同步合同。
- [开发流程规划](plans/SLEEP_STIM_DEVELOPMENT_PLAN.md)：分阶段范围、依赖与验收；P1 工程验收通过，P2 工程验收通过，P3 模拟工程验收通过，P4-A 诊断/文档已验收，原生桌面验证待完成，真机/同步后续批次未发布。

- [P1 桌面与 EEG 合同](../docs/P1_DESKTOP_EEG_CONTRACT.md)
- [P1 执行 prompt](../docs/tasks/P1_DESKTOP_EEG_EXECUTION.md)：已实施并完成验收补充，P1 工程范围通过。

- [P2 合同](../docs/P2_STAGING_RECORDING_CONTRACT.md)
- [P2 执行 prompt](../docs/tasks/P2_STAGING_RECORDING_EXECUTION.md)：已完成并通过工程验收，文末收口要求保留为历史。

- [P3 合同](../docs/P3_STIMULATION_SIMULATION_CONTRACT.md)
- [P3 执行 prompt](../docs/tasks/P3_STIMULATION_SIMULATION_EXECUTION.md)：已实施并通过模拟工程验收。

- [P4 接入准备合同](../docs/P4_INTEGRATION_READINESS_CONTRACT.md)
- [P4-A 执行 prompt](../docs/tasks/P4A_NATIVE_READINESS_EXECUTION.md)：诊断/文档已交付，原生桌面验证待完成。

- [P-GUI 合同](../docs/P_GUI_CONTRACT.md)：取消波形显示，重组控制台，后台数据合同保持。
- [P-GUI 执行 prompt](../docs/tasks/P_GUI_EXECUTION.md)：已实施并通过工程验收。

- [ONNX 睡眠分期合同](../docs/P_MODEL_ONNX_CONTRACT.md)：单通道选择、µV、50 Hz 工频处理、0.3–35 Hz 带通、100 Hz、无逐样本 z-score，以及按模型自动读取时间长度。

## 完成任务与执行证据

- [ONNX 睡眠分期接入报告](../reports/2026-09-16_ONNX_STAGING_INTEGRATION.md)：真实 ONNX GUI 合成回环、97 项完整回归、wheel 模型打包和 Windows 原生窗口复验通过；未真机。
- [Windows 安装与回环验证](../reports/2026-09-16_WINDOWS_VERIFICATION.md)：锁定安装、原生窗口、中文/空格路径和关键 TCP/UDP 回环通过；该报告形成时模型仍为 NoModel，后续状态以上一条为准。

- [P-GUI 执行报告](../reports/P_GUI_REPORT.md)
- [P-GUI 主线程验收](../reports/P_GUI_ACCEPTANCE.md)：去图与控制台整理通过；89 passed 为执行者结果，原生零屏幕限制保留。

- [P4-A 排障报告](../reports/P4A_NATIVE_READINESS_REPORT.md)
- [P4 接入手册](../docs/P4_INTEGRATION_RUNBOOK.md)
- [P4-A 主线程验收](../reports/P4A_NATIVE_READINESS_ACCEPTANCE.md)：诊断/文档通过；原生启动 139 未判为已修复。

- [P3 执行报告](../reports/P3_STIMULATION_SIMULATION_REPORT.md)
- [P3 主线程验收](../reports/P3_STIMULATION_SIMULATION_ACCEPTANCE.md)：模拟工程通过，执行者回归 88 passed；原生 GUI 与真机未验收。

- [P2 执行报告](../reports/P2_STAGING_RECORDING_REPORT.md)
- [P2 主线程验收](../reports/P2_STAGING_RECORDING_ACCEPTANCE.md)：末节记录工程验收通过；回放隔离已收口，原生启动 139 根因未定。

- [P1 执行报告](../reports/P1_DESKTOP_EEG_REPORT.md)
- [P1 主线程验收](../reports/P1_DESKTOP_EEG_ACCEPTANCE.md)：末节记录 P1 工程验收通过，保留首次验收及未真机/未原生窗口限制。

- [2026-09-15 参考代码与文档分析](../reports/2026-09-15_reference_review.md)：本地归档、静态阅读及既有环境验证状态。

- [2026-09-15 开发目录整理](../reports/2026-09-15_development_layout.md)：根级 uv 项目和 src 布局、依赖锁定、11 项既有测试通过。

## 历史决定与取证

暂无历史决定条目。原始参考资料作为来源保留，不作为执行指令。

历史失败与当时执行约束保留；当前是否完成、待授权或执行，以活动队列和最新明确决定为准。
