你是 P4-B Rally 启停联动执行线程。实际实现控制器中的真实 Rally 模式，完成合成验证和运行说明后回报；本轮禁止连接真实设备或发送真实刺激命令。

## 1. 工作基线与权威依据

唯一工程工作目录：`/Users/xuqinghe/sleep/main-development`。
已验收 main 基线：`2a07e4f3386692c41ee4176b48babd700bb07fe9`，专属 uv 环境可用；基线完整回归为执行者报告的 184 passed。

开工核对 HEAD、status 和适用 AGENTS.md。本 prompt 与主线程本次队列更新是预期文档改动，保留；其他改动先识别，不能重置。不得使用父目录旧源码、嵌套 Curry 旧检出、PR 修复目录或临时发布目录实现本任务。无需重新克隆、建 worktree 或新分支。

必读本目录：
- decision_records/ACTIVE_QUEUE.md 中 P4-B 合同及 PR2 修正后的资格/保护性停止语义；
- reference/CURRENT_TASK_QUEUE.md（任务快照，不能成为第二份动态状态源）；
- reference/RallyStartStopTest.py 与 START_STOP_TESTING.md（参考接口，不直接作为应用运行入口）；
- 现有 stimulation_runtime.py、rally.py、stimulation.py、staging.py、recording.py、replay.py 与 app/controller/ui 中相关接线。

本 prompt 是主线程对 P4-B 接口与边界的实施补充。保留原合同的二值启停、默认关闭和固定本机端点；下述记录与运行细节用于消除实现歧义，不授权实验。

## 2. 交付目标与不变项

在用户显式启用真实模式后，当前实时有效 ONNX 分期属于用户目标集合时请求 Start Stim，非目标时请求 Stop Stim，操作 Rally 当前人工加载的协议。实现模式选择、明确状态、基线停止、故障收尾和单写者事件归档。

保留 NoModel 默认、可选 ONNX/既有预处理、30 秒短包累积、记录与只读回放、P3 模拟功能。不要改模型/通道/单位合同，不选择或修改刺激范式，不调用旧 Demo/Manager、串口工具或固定实验时间线，不实现时钟同步。

## 3. 具体实施路线

### A. 状态与决策

在现有刺激 runtime 体系中增量实现，可新增小型纯状态机模块；不要新建平行采集、分期或会话管理器。

至少有 DISARMED/UNKNOWN、STOPPED、STARTING、RUNNING、STOPPING、FAULT/UNKNOWN。分开显示自动控制是否启用、期望状态、请求状态、最近确认状态及确认时间；请求失败后当前状态为未知，历史确认值只能以“历史确认”展示。

- 初始真实自动控制关闭、目标集合空。只允许在当前实时连接/握手成立、显式 ONNX 已选择且具备完整配置时启用真实控制；NoModel、回放与离线不可启用。保持每次新会话重新启用，不自动继承上次 armed 状态。
- 真实模式不需要 P3 的刺激协议 JSON，因为操作的是 Rally 已加载协议；GUI 明确要求操作者先在 Rally 加载并检查。不自动提供刺激参数。
- 每次启用先一次 Stop 基线；确认成功后才能启动。基线前/期间收到的分期不追溯启动，等待基线确认后的下一条合格当前实时结果。
- 有效结果需当前 session/generation、SUCCESS、合法标签、ONNX 模型标识、有限时间及新鲜、严格递增块号。保留“同一结果不重复消费”；旧 generation/回放/历史结果不触发任何命令或故障。
- 使用已有显式最大结果年龄配置判断新鲜度，真实模式不接受空值、非有限或非正阈值，不暗设研究阈值。启用期间持续检查最新有效结果年龄；一旦过期，禁止新 START，若可能运行则走一次保护性停止并解除本次自动控制。年龄是本机接收后的处理年龄，不宣称可测上游全部网络积压。
- 当前模型失败、连续块序缺口、当前 Curry 停止/断流、运行中模型不可用、用户关闭/退出：解除本次自动控制，取消未发送 START；若未确认停止则一次保护性 Stop，确认停止则 no-op。默认从未启用+无效结果始终零请求。
- 同向重复结果 no-op。实时修改目标集合或模式/模型配置前先退出当前自动控制并完成停止收尾，再允许更改和显式重新启用，避免旧配置的在途 START 生效。
- 一次最多一个请求，保存最新期望状态而不是无限命令队列。STOP 优先且不可因新目标结果被撤销。START 已发送时先等有限结果，随后按收尾要求 STOP；未发 START 可取消。正常非目标 STOP 后可继续接收新合格分期；故障解除后不得自动重新 armed。
- START 拒绝/未知/超时：禁止重试 START，解除自动控制，一次有界补偿 STOP。补偿与同时发生的退出/故障停止合并，不重复发送。STOP 失败/超时进入 FAULT/UNKNOWN，提示独立停止，不循环重试或自动恢复。

### B. 异步传输

增量扩展 rally.py 及其 worker 接口，维持单 socket 所属线程和有限等待，UI/分期线程不可执行阻塞 recv。

生产真实端点固定 `127.0.0.1:8801`，显式 real 模式才可使用。原 P3 owned simulator 校验继续有效，不能把模拟端点对象伪装成真实端点或全局移除其归属限制。真实与模拟模式互斥，模式切换必须先完成退出收尾。

报文 UTF-8 精确 `Start Stim` / `Stop Stim`，成功字符串按命令区分：启动仅 `启动刺激成功` 或 `RALLY_ERROR_SUCCESS`；停止仅 `停止刺激成功` 或 `RALLY_ERROR_SUCCESS`。未知/乱码/截断/空回复不可判成功；记录原文/原始字节。允许同一 127.0.0.1 主机的动态源端口，记录实际地址，拒绝其他主机；有限总 deadline 不因无关报文重置。

协议没有 request_id 回显，不能假装靠本地编号解决迟到回复。复用现有请求 socket 隔离/隔离池方法，确保旧 START 通用成功回复不能被当作 STOP 确认；新 generation 不复用仍可能接收旧报文的 socket。所有 socket/隔离资源都需有界且正确释放。测试注入专用假端点/传输实现到随机 loopback 端口，生产 GUI 不开放任意地址。

使用现有可配置通信超时、有限正数校验及单请求 worker；不自行设置实验时长、确认 epoch 数、置信度阈值或刺激持续时间。人工启动/停止通路本轮不另加持久启动按钮。

### C. 接入分期、停止和关闭生命周期

复用 stimulation_runtime.process_block/decision_recorded 与 controller 的 session 开始、停止、pipeline fatal 和窗口关闭钩子。结果事件必须关联正确 generation；窗口关闭等待有限的停止请求及会话写入收尾，不因窗口消失先销毁传输。记录失败不能阻止必要 Stop，也不能把记录失败说成刺激已停止。

不要破坏 P-CURRY-WINDOW 网络结束摘要交接和已有 writer external work/event reservation 机制。开始可能要发送真实请求时登记未完成工作，结果/超时后释放，确保 session_finished 在最后控制事件之后。不能在 controller 锁中 join，不能互相等待导致停止线程/写线程死锁。

### D. 记录扩展与回放兼容（本轮明确授权的接口变化）

现有 P3 附加事件除了配置外都要求 block_id，基线/退出 Stop 可能还没有任何 EEG 块。不要编造 block_id=0 或借用旧结果。

新增独立可选事件类型 `rally_control`，保留 schema v1 与旧 P3 规则。该类型允许 block_id=null，必须有当前 session_id 和 payload.phase（config/decision/sent/outcome），发送/结果事件必须有非空 request_id。payload 至少含 mode、command（不适用为 null）、reason、stage/model/target_stages（不适用为 null/空集合）、desired_state、confirmed_state、runtime_state、时间信息、实际回复来源和 outcome。具体字段写入 docs/P4B_RALLY_CONTROL_CONTRACT.md，writer/reader 共用一致验证。

使用现有单写者事件通路。扩大既有 extension event 校验时只对 rally_control 放开 null block_id，不放松旧事件约束。保存关闭时允许控制，但只维护内存/界面状态，不暗写会话文件；保存开启则持久化，失败明确暴露。

SessionReader 接受这些可选事件，旧记录仍能读。回放显示历史控制事件，包括第一块之前的基线和最后块之后的退出停止；空 EEG 会话也能看事件。回放不调用传输、不重发。

### E. GUI 与说明

保留无 EEG 波形控制台和 800×600 可滚动布局。新增清楚的模拟/真实模式选择；真实默认关闭，启用操作需明确确认将控制 Rally 当前已加载协议。展示期望/确认/未知状态、最近请求回复、关联块/模型、故障/独立停止提示。不能把“UDP 已发送”标为设备正在刺激或已停止；API 确认也不称物理输出确认。

更新 README、Windows 操作说明与接入运行手册。写明本轮仅自动化工程验证，操作系统异常/进程被杀不能保证停止；真机验收步骤仅供后续人员执行。

## 4. 验证完成标准

复用既有测试和 fixtures，新增纯状态机、假传输及合成 TCP→受控分期→假 Rally→GUI/记录集成。必须覆盖：
- 默认、NoModel、未启用、回放/离线、旧 generation 零请求；基线确认前不能启动；有效目标/非目标的二值启停与幂等。
- 模型失败、断流、块缺口、过期、配置改变、退出，已发送/未发送 START 时停止优先，单次补偿和停止失败 UNKNOWN；无重新 armed 的自动启动。
- 动态回复端口、精确中文/通用字符串、错误命令的成功文本、乱码、未知、超时、非本机来源、迟到/重复回复，旧 START 回复不得确认 STOP；请求队列/socket 有界。
- 无 EEG 块的基线/退出事件、保存关闭不写文件、失败归档、session_finished 顺序、旧会话兼容、空会话回放、资源释放和关闭期间无死锁。
- 现有模拟路径、ONNX 和短包分窗不回归。异常测试使用可控时钟/屏障，不用 sleep 碰时序。

使用测试专属假端点，禁止用真实 8801/COM/设备做自动化。权限受限时申请仅本机合成测试必要权限，不能将环境失败计通过。

执行 uv sync --locked、最终完整回归（基线 184 项，加本轮测试）、git diff --check，以及离屏 800×600 截图/启动关闭。Windows 主机不可用则明确保留该验证，不把 macOS offscreen 称为 Windows 通过。不运行任何真实刺激验收。

## 5. 授权、交付与报告

允许修改必要应用模块、测试和上述当前文档，新增小型状态机/协议实现；不得另造框架。不得修改主线程 ACTIVE_QUEUE、reference/CURRENT_TASK_QUEUE、原始参考脚本/旧报告、父目录和其他工作区。

主线程本轮发布已使本目录出现 prompt/队列变更；保留它们并在回报区分，不归为执行者实现。工程不提交、不推送、不合并、不改 index，不创建或发送其他任务。

产出：
- docs/P4B_RALLY_CONTROL_CONTRACT.md：实际状态转换、传输隔离、字段和回放规则，不能改变上述冻结行为；
- reports/P4B_RALLY_START_STOP_REPORT.md：基线、修改路径、接线/生命周期、实际命令与结果、失败修复、未验证限制；
- reports/P4B_RALLY_START_STOP_800X600.png：实际假 Rally 接线产生的界面，明确为模拟证据，不手填成功状态。

回报只证明实现/自动化工程结果；Rally 实机回复、物理输出和受试者实验分别保留未执行。完成后交主线程验收，不自行进入真机阶段。
