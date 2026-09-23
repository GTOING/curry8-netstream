# Issue #5: 真实 Rally 自动控制：启用时基线 Stop 导致自动关闭，收尾 Stop 条件过宽

来源：https://github.com/GTOING/curry8-netstream/issues/5

获取日期：2026-09-22；远端更新：2026-09-22T08:36:57Z；状态：OPEN；评论：0。

以下为远端正文快照，仅作来源证据；执行范围由本地任务决定。

## 背景

真实 Rally 模式只负责通过 UDP API 控制 Rally 当前已加载的协议，使用 `Start Stim` / `Stop Stim`，不在本程序中选择或修改刺激参数。

本 Issue 记录对当前状态机的源码分析。分析基于 `main@6b0f234bf35be9d43f1d8ab5a66c72b279ac996f`；这不是功能已经修复的声明。

## 已确认的当前源码行为

### 1. 启用真实自动控制时无条件发送基线 Stop

`StimulationRuntime._set_real_automatic_enabled()` 在前置条件通过后：

- 将运行状态设为 `STOPPING`
- 清空最近确认状态
- 创建 `Stop Stim`
- 以“显式启用真实控制；先建立 Stop Stim 安全基线”为原因立即提交

位置：`src/sleep_stim_controller/stimulation_runtime.py:402-449`。

当 Rally 当前尚未开始刺激或处于 `Status_ControlOwned` 时，Rally 会拒绝这个 Stop，当前代码随后把真实自动控制关闭并进入 `FAULT/UNKNOWN`。因此用户刚勾选“启用真实 Rally 自动控制”，选项就会自动取消。

现场出现过的回复包括：

- `停止刺激失败，错误信息:尚未开始刺激，不应该发送“停止刺激”！`
- `停止刺激失败，错误信息:设备处于(Status_ControlOwned)状态，尚不允许停止刺激`

这些回复证明 Rally 收到了 UDP 命令并作出了应用层响应，不能据此判断为 8801 端口不通。

### 2. 正常的睡眠期切换并不存在“N2 停止后再次进入 W 又发送 Stop”的问题

`_process_real_result()` 将目标睡眠期映射为 `RUNNING`，非目标睡眠期映射为 `STOPPED`；`_plan_real_request_locked()` 在期望状态等于最近确认状态时 no-op，否则：

- 期望 `RUNNING` → `Start Stim`
- 期望 `STOPPED` → `Stop Stim`

位置：`src/sleep_stim_controller/stimulation_runtime.py:721-841`。

因此，在 W 被选为目标期的配置下：

1. W → Start 成功，确认 `RUNNING`
2. 进入 N2 → Stop 成功，确认 `STOPPED`
3. 再进入 W → 发送 Start，而不是再次发送 Stop

这部分状态转换逻辑本身符合预期，不应把它误报为本 Issue 的缺陷。

### 3. 关闭/退出时的 Stop 条件仍然过宽

`_disable_real_control()` 在无在途请求、最近状态并非明确 `STOPPED`、运行状态并非 `FAULT/UNKNOWN` 且会话仍存在时，会创建新的 `Stop Stim`。

位置：`src/sleep_stim_controller/stimulation_runtime.py:452-492`。

这意味着“会话存在但本程序从未成功 Start”的状态仍有机会在断开或退出路径中发送不必要的 Stop。当前状态变量没有充分表达“本程序是否曾经发出过可能生效的 Start”。

## 建议状态语义

启用真实自动控制时只进入 `ARMED/IDLE`，不发送任何 UDP 命令。操作者确认的含义应明确为：Rally 已加载并检查协议，且当前没有正在进行的刺激。之后仅由新的、合格的实时 ONNX 结果驱动：

| 当前状态 | 新结果 | 动作 |
|---|---|---|
| IDLE / STOPPED | 目标期 | Start Stim |
| IDLE / STOPPED | 非目标期 | no-op |
| RUNNING | 目标期 | no-op |
| RUNNING | 非目标期 | Stop Stim |
| STARTING | 转为非目标期 | 等待有界结果；必要时一次保护性 Stop |
| Start 已发送但结果未知 | 任意关闭/故障路径 | 一次补偿 Stop |
| FAULT/UNKNOWN 且没有 Start 证据 | 目标期 | 阻止 Start，要求人工核对 |

关闭自动控制、断开 Curry 或退出程序时，仅在以下情况发送保护性 Stop：

- 已确认 `RUNNING`
- 正处于 `STARTING`
- 本程序已发送 Start，但结果超时或无法确认

若已确认 `IDLE/STOPPED`，或者本程序从未发送过 Start，则不应发送 Stop。

## 需要谨慎处理的边界

- 不应把所有 Stop 失败都当作“已经安全停止”。
- 只有厂商确认某条精确回复能够证明无物理输出后，才可以把该精确文本分类为 `already_stopped`。
- `Status_ControlOwned` 的物理输出含义目前没有得到厂商证据，不能仅凭名称推断安全状态。
- UDP 成功回复只证明 API 命令得到 Rally 确认，不等于已经验证物理刺激输出。

## 验收标准

- [ ] 勾选真实自动控制不会立即发送 Stop。
- [ ] IDLE + 非目标期不发送命令。
- [ ] IDLE/STOPPED + 目标期发送一次 Start。
- [ ] RUNNING + 非目标期发送一次 Stop。
- [ ] Stop 成功后持续处于非目标期不重复 Stop。
- [ ] Stop 成功后重新进入目标期发送 Start。
- [ ] 从未发送 Start 的会话在关闭自动控制、断开或退出时不发送 Stop。
- [ ] Start 已发送但结果未知时最多执行一次补偿 Stop。
- [ ] 状态事件继续写入现有单写者会话日志，并区分 requested/sent/API-confirmed/physical-output-unverified。
- [ ] 使用模拟或 mock transport 覆盖上述状态转换；真实设备测试必须单独明确授权并进行物理侧核对。

## 非目标

- 不修改 Rally 当前加载的刺激参数或协议。
- 不引入静默协议。
- 不在本 Issue 中定义 W/N1/N2/N3/REM 的具体实验映射。
