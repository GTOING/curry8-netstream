# P3 主线程验收

日期：2026-09-16。角色：决策主线程。

## 结论

P3 刺激策略与本机模拟联调的工程范围通过验收。依据为 [P3 合同](../docs/P3_STIMULATION_SIMULATION_CONTRACT.md)、[执行报告](P3_STIMULATION_SIMULATION_REPORT.md)、当前关键实现、对应测试和[默认界面截图](P3_STIMULATION_SIMULATION_UI.png)。本次为静态复核及交付证据审查，主线程未重跑 uv sync、pytest、网络联调或 GUI。

执行者报告 uv sync --locked 成功、完整回归 88 passed、定向回归 13 passed，以及 offscreen 启动/关闭与传输线程退出成功。这些是执行报告中的实际运行结果，不表述为主线程重新实测。

## 核对要点

- `stimulation.py`：两种显式策略、目标期集合、重复/倒序抑制、失败结果不重置有效期别记忆、接收时间年龄、请求间隔与忙碌抑制；配置修改不重放旧块。无模型默认不产生候选。
- `stimulation_runtime.py` 与 `rally.py`：只接受应用持有的随机回环模拟端；发送前复核配置/年龄，单个在途请求，无自动重试；来源校验与独立 socket/端口保留隔离迟到和重复回复。拒绝/未知关闭自动决策，关闭决策不声称设备停止。
- `staging.py` 与 `recording.py`：复用 P2 单写者和 schema v1 可选扩展；先记录块及处理结果，再记录决策；发送前预留有界事件容量，外部工作租约用于会话收尾。记录失败不冒充完整会话。
- `tests/test_controller_synthetic.py`：合成 TCP 经测试模型、UDP、会话记录到只读回放；测试替身标记保留；NoModel 零发送；API 拒绝后不重试；窗口退出时请求结果 unknown，且 request_outcome 在 session_finished 之前。
- `tests/test_rally.py`、`tests/test_stimulation.py`、`tests/test_p2_recording.py`：协议/策略、错误来源、迟到回复、取消与事件预留溢出等边界证据。`tests/test_ui.py` 包含 800×600 布局、固定错误区域和配置锁定检查；默认截图显示空目标、未选策略、空参数、模拟端及自动决策关闭。截图自身不是 800×600 原生窗口证据。

本次抽查未发现阻止 P3 模拟工程验收的问题；验收不等于所有并发调度或故障情形均已穷尽验证。

## 保留限制与后续

原生 macOS 启动退出码 139 根因仍未确定，P3 未复测；离屏通过不能代替原生桌面验收。真实模型、Curry 真机、Rally/设备实际输出及独立停止、刺激参数限制、时钟同步和受试者实验均未验证。

P4 尚未发布或执行。后续真机阶段需明确目标 Rally/设备版本、实际刺激配置、启停与状态接口、实验触发策略及授权；同步需另行明确事件来源和误差要求。当前验收不扩大真实刺激或实验授权。P1/P2/P3 历史执行报告保持原样。
