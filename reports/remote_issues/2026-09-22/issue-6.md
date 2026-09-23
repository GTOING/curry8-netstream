# Issue #6: 按睡眠分期驱动版本化 Rally 多协议实验范式（立即切换）

来源：https://github.com/GTOING/curry8-netstream/issues/6

获取日期：2026-09-22；远端更新：2026-09-22T14:37:53Z；状态：OPEN；评论：0。

以下为远端正文快照，仅作来源证据；执行范围由本地任务决定。

## 背景

现有 P4-B 只把实时 ONNX 睡眠分期映射为 Rally 当前已加载协议的二值 `Start Stim` / `Stop Stim`。本 Issue 定义后续扩展：根据当前睡眠阶段选择协议、切换协议或停止刺激，并让每次实验使用一个独立、版本化、可回溯的实验范式包。

本方案基于 `main@6b0f234bf35be9d43f1d8ab5a66c72b279ac996f`。它是需求/设计合同，不表示已经实现。现有基线 Stop 问题见 #5；本功能的正式实现应在同一状态机上增量完成，而不是另建第二套控制链。

## 已冻结的范式 A

| 睡眠期 | 动作 |
|---|---|
| W | 启动或切换到 `protocol_A` |
| N1 | 启动或切换到 `protocol_B` |
| N2 | 启动或切换到 `protocol_C` |
| N3 | `Stop Stim` |
| REM | `Stop Stim` |

以后新增范式沿用相同格式，仅增加新的版本化范式配置和协议定义，不复制 UDP、故障收尾或设备状态机。

## 架构原则

采用“一套通用 `ParadigmRuntimeManager` + 每个实验一个不可变范式包”，不为每个范式复制完整 `RealtimeControlManager.py`。

旧 `reference/RealtimeControlManager.py` 仅作参考，不能直接作为正式运行器：

- 它按固定时间线启动/杀死 Demo 子进程，不读取睡眠分期；
- `_kill()` 只结束 Python 进程，不等于 Rally 已停止；
- 多个 Demo 自带循环、sleep 和 socket，不能进入实时分期控制链；
- 正式实现必须复用现有会话、结果资格过滤、单请求传输和单写者记录。

建议目录：

```text
experiments/
└── paradigm_A/
    ├── paradigm.json
    ├── protocols/
    │   ├── protocol_A.json
    │   ├── protocol_B.json
    │   └── protocol_C.json
    └── README.md
```

## 协议存储

运行时使用经过验证的规范 JSON，不直接执行 `RealtimeControlAPIDemo*.py`。

`buildProtocol()` 中的数据可以作为协议来源，但应在实验开始前转换、验证并冻结为 JSON。若将来需要 Python 计算协议，builder 只能作为准备阶段的纯函数运行一次；运行时消费并归档其规范化输出，而不是在每个 epoch 启动脚本。

示例：

```json
{
  "schema_version": 1,
  "name": "protocol_A",
  "protocol_version": "1.0.0",
  "initial_stimulus_channels": ["F5", "AF3", "F1", "FC3"],
  "return_channels": [],
  "payload": {
    "SD": 12000,
    "FR": 30,
    "FD": 30,
    "CHS": [
      {"N": "F5", "T": "tD", "A": 400.0},
      {"N": "AF3", "T": "tD", "A": 400.0},
      {"N": "F1", "T": "tD", "A": 400.0},
      {"N": "FC3", "T": "tD", "A": 400.0}
    ]
  }
}
```

字段遵循供应商示例：

- `SD`：刺激总时长；
- `FR` / `FD`：渐升/渐降；
- `CHS`：待修改通道；
- `N`：通道名；
- `T`：刺激类型；
- `A`：电流幅值；
- `F`：频率；
- `P`：相位；
- `D`：直流偏置。

所有 A/B/C 协议的通道必须兼容同一个 Rally 基础协议；`CHS` 只能是基础刺激通道的子集，返回通道不能修改。本地验证不能替代 Rally/硬件参数限制。

## 范式配置

```json
{
  "schema_version": 1,
  "paradigm_id": "paradigm_A",
  "paradigm_version": "1.0.0",
  "required_rally_base_protocol": "base_protocol_A",
  "stage_actions": {
    "W":   {"action": "stimulate", "protocol": "protocol_A"},
    "N1":  {"action": "stimulate", "protocol": "protocol_B"},
    "N2":  {"action": "stimulate", "protocol": "protocol_C"},
    "N3":  {"action": "stop"},
    "REM": {"action": "stop"}
  },
  "protocol_files": {
    "protocol_A": "protocols/protocol_A.json",
    "protocol_B": "protocols/protocol_B.json",
    "protocol_C": "protocols/protocol_C.json"
  },
  "decision_policy": {
    "mode": "immediate",
    "required_consecutive_epochs": 1,
    "minimum_dwell_seconds": 0,
    "confidence_threshold": null
  }
}
```

## 已冻结的睡眠期决策策略：立即切换

每收到一个新的、合格的实时 ONNX 结果，立即计算期望动作：

- 不做连续 epoch 确认；
- 不做多数投票；
- 不设最短协议维持时间；
- 暂不以置信度阻断，置信度仍须记录；
- 仍要求结果属于当前实时 session/generation、推理成功、标签合法、block 单调、新鲜且不是回放/离线结果。

“立即”是指分期结果产生后立即决策。由于模型按 30 秒窗口输出，它不等于生理阶段发生瞬间响应，实际延迟包含窗口完成、预处理、推理和 API 通信。

立即切换不等于每个 epoch 重发：

- `W → W` 且已经运行 A：no-op；
- `W → N1`：切换 B；
- `N1 → N2`：切换 C；
- `N2 → N3`：Stop；
- `N3 → REM` 且已经停止：no-op；
- `REM → W`：启动并应用 A。

## 状态机

至少区分：

- `DISARMED/UNKNOWN`
- `IDLE/STOPPED`
- `ACTIVATING(protocol)`
- `RUNNING(protocol)`
- `SWITCHING(from, to)`
- `STOPPING`
- `FAULT/UNKNOWN`

### 从停止状态启动指定协议

现有资料表明 `RealTimeControl {JSON}` 要求 Rally 已在刺激状态，因此从停止状态进入 W/N1/N2 时使用复合操作：

```text
ACTIVATE(X)
  Start Stim
  → 等待精确启动成功
  → 立即发送 RealTimeControl protocol_X
  → 等待 RALLY_ERROR_SUCCESS
  → RUNNING(protocol_X)
```

这是软件层“逻辑原子操作”，不是真正的 UDP 或硬件原子事务。

已接受的实验假设：

- Rally 基础协议启动时有 `FR=30` 秒渐升；
- 在 Start 成功后立即下发目标协议；
- 短暂基础协议输出处于渐升早期，在本范式中视为可接受；
- 无受试者验收时必须测量并记录 Start 到目标协议确认的实际窗口，之后再冻结允许上限。

失败处理：

- Start 失败、拒绝、未知或超时：一次补偿 Stop，进入 `FAULT/UNKNOWN`；
- Start 成功但目标协议失败、拒绝、未知或超时：立即 Stop，进入 `FAULT/UNKNOWN`；
- 只有两步都明确成功，才显示 `RUNNING(protocol_X)`；
- 不自动循环重试。

### 运行中切换

`RUNNING(A) → protocol_B` 时直接发送 `RealTimeControl B`，不要先显式 `Stop Stim`。供应商资料表明 Rally 会在调参时内部停止旧刺激、下发新协议并重新开始，所以每次切换可能重新经历目标协议的 FR 渐升；这属于立即切换策略的实验后果，必须记录。

### 单请求与最新期望

任一时刻最多一个 Rally 请求在途，不创建 A/B/C 命令队列。

若 A 请求在途时结果依次变成 N1、N2：

- 当前请求等待有界结果；
- 只保留最新期望 C；
- 当前请求结束后重新计算；
- 跳过已经过时的 B。

Stop 优先。启动/切换在途时若最新结果变成 N3/REM，则不再继续旧协议链，并在当前请求得到结果或进入未知后执行一次有界停止。

## SD 到期

`SD` 是一次协议的刺激时长上限。默认行为：

- 记录每次协议确认时间与预计到期时间；
- 到期后不能继续显示为已确认 `RUNNING`；
- 默认不因相同分期自动续期或重新启动；
- 是否允许同一睡眠期自动续期属于以后单独的范式字段，不能隐含实现；
- 如果需要覆盖整个实验，应由实验设计设置合适且合规的 SD，而不是由软件绕过时长上限。

## 会话回溯

每个 session 必须保存而非仅引用：

- `paradigm.json` 的规范快照、版本与哈希；
- 所有被引用协议的规范 JSON、版本与哈希；
- Rally 基础协议名称及操作者确认；
- ONNX 模型标识、输入通道和每个 epoch 的分期/置信度；
- 原始期望动作、幂等 no-op、实际请求、回复及动态源端口；
- EEG epoch 起止、分期完成、决策、命令发送、回复时间；
- A/B/C 切换历史和每段协议维持时长；
- API 确认与物理输出确认分开记录。

回放只能展示这些历史事件，不能重新发送控制命令。

## 验收标准

- [ ] 加载范式时一次性解析并验证全部协议；任一引用缺失或无效则禁止启用。
- [ ] 新增范式只需新增版本化范式包，不复制通用状态机。
- [ ] W/N1/N2 分别映射 A/B/C，N3/REM 映射 Stop。
- [ ] 一个合格 epoch 即触发新的期望动作，不使用平滑或驻留时间。
- [ ] 相同协议的重复分期为 no-op，不重复下发。
- [ ] 停止状态进入 W/N1/N2 执行有界 `Start + ApplyProtocol` 复合操作。
- [ ] 运行中 A/B/C 切换只发送目标 `RealTimeControl`，不先发送 Stop。
- [ ] 任一时刻只有一个请求在途，只保留最新期望，Stop 优先。
- [ ] Start 或协议下发结果未知时不重试，执行一次保护性 Stop。
- [ ] N3/REM 已停止时不重复 Stop。
- [ ] SD 到期不会被继续显示为确认运行，也不会隐式续期。
- [ ] session 保存完整范式/协议快照和时序证据。
- [ ] 模拟/mock 自动化覆盖全部状态转换后，才进行无受试者真机验收。
- [ ] 真机报告分别陈述代码测试、Rally API 回复和物理输出证据。

## 非目标

- 本 Issue 不执行真实刺激或受试者实验。
- 不修改参考目录中的原始 Demo/Manager。
- 不使用低电流或低频率“静默协议”代替 Stop。
- 不把 API 成功回复解释为已经证明物理输出或科研效果。
- 不在本 Issue 中冻结 A/B/C 的实际电流、频率、相位、通道或安全上限；这些由对应协议文件和实验审批决定。
