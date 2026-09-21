# P4-B Rally 启停控制合同

版本：1（2026-09-21）

本合同描述控制器对 Rally 当前已加载协议的二值启停联动。它不选择、上传或修改刺激范式，不声明模型准确性、物理输出或受试者安全性。真实模式默认关闭；P4-B 自动化证据只使用随机 loopback 假端点。

## 1. 模式和资格

控制面板有两个互斥模式：

- `simulation`：P3 本机拥有的随机 `127.0.0.1` UDP 端口，发送既有 JSON 协议；它不使用 `8801`。
- `real`：只允许生产固定端点 `127.0.0.1:8801`，发送二值 Rally 命令；GUI 不开放任意地址。测试可以通过构造器注入另一个随机 loopback 端口，但这不是生产配置。

真实自动控制初始关闭、目标集合为空。只有下列条件同时满足时，显式启用才会成功：当前实时 Curry 会话已建立并完成握手；本次连接前显式选择 ONNX；目标期、触发策略、最小请求间隔和最大结果年龄有效；应用未进入收尾。`NoModel`、回放、离线数据、历史块和其他 session/generation 不能启用真实控制。每个新实时会话都重新要求确认。

真实模式操作操作者已经在 Rally 中加载并检查的协议，软件不读取或修改刺激参数。GUI 的确认框只确认这个操作前提，不是硬件急停。

## 2. 状态和转换

状态字段彼此分开：

- `enabled`：本次真实自动控制是否 armed；
- `expected_state` / `desired_state`：保护性策略当前希望 Rally 处于 `RUNNING` 或 `STOPPED`；
- `runtime_state`：`DISARMED/UNKNOWN`、`STOPPED`、`STARTING`、`RUNNING`、`STOPPING`、`FAULT/UNKNOWN`；
- `confirmed_state`：只有精确成功回复可以更新的最近 Rally 协议确认；失败或超时后不会沿用它冒充当前确认，GUI 会同时显示 `FAULT/UNKNOWN`；
- `confirmed_utc`、请求/回复时间、回复来源、关联 block/model：用于追溯，不是统一硬件时钟。

显式启用的第一步永远是一次 `Stop Stim` 基线。基线成功后进入 `STOPPED`，并丢弃基线在途期间收到的结果；只有之后新的合格结果才可控制。基线失败/超时不允许 `Start Stim`。

当前实时、当前 generation、`SUCCESS`、标签属于 `W/N1/N2/N3/REM`、模型标识为非测试 ONNX、接收/处理时间有限、结果未超过已配置最大年龄且块号严格递增，才是合格结果。目标期集合内映射到 `RUNNING`/`Start Stim`，非目标期映射到 `STOPPED`/`Stop Stim`。重复同向结果、重复 block 和倒序 block 是 no-op；一个请求在途时只保留最新期望状态，不建立无界队列。Stop 优先于 Start；未发送的 Start 可取消，已发送的 Start 必须等有限结果后再进行一次 Stop 收尾。现有 GUI 周期调度会调用显式 `tick()` 检查“没有新结果”的年龄；真正 `sendto` 前 worker 还会重新核对当前 session/generation、armed、基线和缓存年龄，过期缓存不能在等待 Stop 后再次启动。

当前会话模型失败/不可用、块序缺口、结果过期、Curry 停止/断流、用户关闭自动控制、窗口关闭或应用退出会解除 armed 状态。若没有明确 `STOPPED` 确认，则最多发一次保护性 `Stop Stim`。Start 拒绝、未知或超时不重试 Start，只发一次有界补偿 Stop。Stop 拒绝、未知或超时进入 `FAULT/UNKNOWN`，不循环重试，提示操作者在 Rally/硬件侧独立停止。操作系统异常、强制杀进程和设备自身故障不由软件保证停止。

## 3. UDP wire 和隔离

生产命令是严格 UTF-8 字节：

```text
Start Stim
Stop Stim
```

启动只有 `启动刺激成功` 或兼容字符串 `RALLY_ERROR_SUCCESS` 判为 API 成功；停止只有 `停止刺激成功` 或同一兼容字符串判为 API 成功。空、乱码、截断、未知文本和与命令不匹配的中文成功文本不能判成功。拒绝码记录为 `api_rejected`，无法解释/超时/来源异常最终记录为 `unknown`。

Rally 不回显 request id，因此 request id 只用于本地事件关联，不能用于证明回复属于哪条 vendor 命令。每条命令由单一 worker 线程创建独立 UDP socket 和动态源端口；发送后该 socket 在本次 worker 生命周期内隔离，迟到或重复回复不会被下一条命令读取。接收只接受主机 `127.0.0.1`，允许 Rally 的动态源端口，并记录实际主机/端口。等待使用有限总 deadline，无关报文不会重置 deadline；隔离 socket 数量有界，关闭时统一释放。`max_quarantined_sockets` 是普通 Start 的准入上限，并额外保留一个有界 Stop/不确定 Start 补偿槽；容量不足时拒绝新的 Start，不关闭旧 socket 复用端口掩盖迟到回复。

GUI/分期线程不执行阻塞 `recv`。worker 一次最多持有一个 pending/current 请求，`submit` 在忙碌、退出或隔离上限时失败；runtime 保存最新 desired state，并由完成回调决定下一步。普通分期 external-work 在 pipeline 开始收尾或记录故障后仍被拒绝；必要 Stop 使用独立控制收尾租约，租约覆盖最终 outcome 入队和后续补偿 Stop，记录不可用时仍尽力发送并明确报告缺失证据。Curry 网络 worker 在自然 EOF/网络异常的 `finally` 中先调用线程安全、幂等的 runtime 收尾，再执行 `handoff_network_end`；稍后的 Qt 生命周期回调只作幂等兜底。pipeline 在同一 condition 锁内完成“最终事件/租约排空→关闭新控制租约准入”的原子转换，之后的必要 Stop 仍可 best-effort 发送但不得伪称已持久化。`session_finished` 只有在可写会话的最后控制事件和所有控制租约释放后才写入。P3 simulator 的 endpoint ownership、JSON 报文和 transport 规则保持独立。

## 4. `rally_control` session event schema v1

控制事件复用现有 session 单写者，根结构仍由 writer 追加 `sequence`、`session_id`、UTC 和本机 monotonic 时间。扩展事件固定为：

```json
{
  "event_type": "rally_control",
  "session_id": "<current session>",
  "block_id": null,
  "request_id": "<required for sent/outcome>",
  "payload": {
    "schema_version": 1,
    "phase": "config|decision|sent|outcome",
    "mode": "real",
    "command": null,
    "reason": "...",
    "stage": null,
    "model": null,
    "target_stages": [],
    "desired_state": "STOPPED",
    "confirmed_state": null,
    "runtime_state": "STOPPING",
    "created_utc": "...",
    "created_monotonic_ns": 1,
    "sent_monotonic_ns": null,
    "received_monotonic_ns": null,
    "response": null,
    "outcome": "configured"
  }
}
```

`phase=config` 使用 `command=null`，通常没有 `request_id`；`phase=decision` 可表示 no-op，因此 command/request id 可以为 null；`phase=sent` 和 `phase=outcome` 必须有非空 request id 和 `Start Stim`/`Stop Stim` command。`block_id` 可以为 null，尤其是基线、补偿停止、保护性停止、退出和配置事件；若有关联结果则使用真实正整数 block id，不制造 `0`。`stage` 是合法睡眠期或 null；`model` 为模型 descriptor 或 null；`target_stages` 是合法标签数组，可为空。

`response` 在有回复时包含：

```json
{
  "text": "停止刺激成功",
  "bytes_b64": "<原始回复字节的 base64>",
  "source": {"host": "127.0.0.1", "port": 54321}
}
```

原始字节使用 base64 以保持 JSON 可编码，原文在可解码时保留。时间必须是有限、正的本机 monotonic 整数；writer 和 reader 通过同一个 schema validator 检查 phase、命令、状态、来源和时间。旧 P3 `stimulation_config`、`decision`、`request_sent`、`request_outcome` 的 block 约束不放宽。

保存关闭时控制仍可运行，事件只进入 runtime memory/UI，不创建文件。保存开启时事件通过 `ProcessingPipeline` 的 external work/event queue 交给同一个 SessionWriter；正常已接纳事件在最终关闭前排空，`session_finished` 永远位于其后。记录失败会明确进入诊断/会话失败路径，但不会阻止必要的 Stop 收尾；越过 writer 关闭屏障后到达的控制事件只可报告证据缺失，不能重新打开写入。

## 5. 回放和验收边界

`SessionReader` 接受可选 `rally_control` 事件，并将无 block 的事件保存在 session-level `control_events`，有关联 block 的事件同时挂在该 `ReplayBlockEntry`。因此第一块前的基线、块间决策/回复和最后一块后的退出停止都能展示；没有 EEG block 的空会话也能展示。回放 worker 只读文件，绝不创建 Rally transport、发送命令、重跑模型或运行策略。

P4-B 自动化只证明代码路径、随机 loopback wire、状态机、记录/回放顺序和 GUI 状态。它不证明 Rally 实机回复、刺激物理输出、设备安全、模型效果、跨设备时钟同步或受试者实验；这些必须以后在单独授权和独立停止路径可用的条件下进行。
