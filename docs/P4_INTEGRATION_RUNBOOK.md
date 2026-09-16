# P4 接入准备操作手册

日期：2026-09-16。适用于 P4 后续单独定界的接入准备；不构成真实采集、模型、Rally、刺激或同步操作授权。当前产品状态仍是 P1/P2/P3：Curry TCP 客户端、NoModel、可选记录/只读回放及本应用本机 UDP 模拟。

## 平台更新

正式部署目标已由用户明确为 Windows，模型接入暂缓。下文 macOS 原生核验步骤保留作历史诊断参考，不再是目标平台验收入口。Windows 环境安装、原生窗口、路径/权限、回环测试与设备接入尚待实际验证；后续执行任务应提供 Windows 命令与证据。

## 当前接口与边界

| 子系统 | 现有入口 | 已支持 | 当前不能据此推出 |
| --- | --- | --- | --- |
| Curry | 主窗口连接配置 → CurrySessionController.connect() → CurryClient.connect()/stream() | 用户配置 IPv4/端口；TCP BasicInfo、ChannelInfo 握手；请求开始推流；解码为 float32、[channel, sample] 的 DataBlock；控制器检查标签、连续性、有限数值和完整 30 秒块 | 不代表连接了真实采集器；不证明硬件配置、数值单位、采样准确度或记录/采集器状态 |
| 块上下文 | CurrySessionController._accept_block() → BlockContext | block_id、样本范围、接收边界 UTC 与本机 monotonic_ns；session 内样本连续性可检查 | 接收时间不是 EEG 精确采样时刻；不同进程的 monotonic 时钟不能直接比较 |
| 处理/保存 | ModelAdapter；ProcessingPipeline；SessionWriter/SessionReader | 默认 NoModel；有界顺序处理；记录默认关闭；启用后保存解码原值和关联事件；回放只读 | 不自动推断预处理、通道映射、单位或分期效果；回放不运行模型或通信 |
| P3 策略/Rally | StimulationRuntime；LoopbackRallySimulator；RallyTransportWorker | 显式配置目标期、策略、间隔、结果年龄及 JSON；仅发往本应用持有的 127.0.0.1 随机端口模拟器；单请求、无自动重试 | 没有真实 Rally 端点、8801 解锁或真实设备控制接口；API 成功不是刺激输出证据 |
| 时间 | BlockContext 与 ProcessingResult 单调时间、UTC 日志时间及样本索引 | 可描述本进程收到/处理/发送/收到响应的顺序与耗时 | 没有 Curry、模型主机、Rally 和刺激输出之间的时钟映射、硬件事件同步或误差保证 |

P2 会话中 block_saved 与 processing_result 由单一写者追加；P3 决策和请求结果复用该写者。SessionReader 回放不会连接 Curry/Rally、不重新推理、不运行策略。保持 NoModel 默认；不在生产 UI/CLI 加入伪分期入口。

## 原生桌面启动与核验

标准命令必须在登录的、可显示窗口的 macOS 桌面会话运行，不设置 QT_QPA_PLATFORM=offscreen：

    cd /Users/xuqinghe/sleep
    uv run --locked sleep-stim-controller
    printf 'exit=%s\n' "$?"

本机当前 P4-A 执行 shell 使用 Cocoa Qt 平台但报告 0 个 screen、无 primary screen；Computer Use 对 Terminal 的访问也被安全策略拒绝。因此当前没有原生窗口截图、800×600 可访问性或窗口关闭证据。普通桌面复核时先保持 Curry 未连接、记录关闭、NoModel、五个目标期未选、自动决策关闭、模拟端关闭；再将窗口调整至 800×600，确认连接与错误区可见、滚动区可访问。正常关闭窗口后记录该命令退出码，并确认没有残留的本任务进程/线程。不要把 offscreen 测试当作原生窗口通过。

若在普通桌面仍复现崩溃，终端中同时可见 Qt/macOS 输出；紧接着只检索最近的 Python 崩溃记录：

    find ~/Library/Logs/DiagnosticReports -maxdepth 1 -type f -name 'python3*.ips' -mmin -10 -print

仅挑选与本次启动时间匹配的文件，保留原件；报告异常类型、faultingThread 栈顶、Qt/platform、退出码和窗口是否出现，不导出整个环境或无关系统日志。正常启动并关闭时记 0 退出码、截图及默认状态。不要连接设备来验证启动。

## Curry 接入步骤（须由后续任务单独授权）

1. 接入前由主线程取得并确认目标 Curry/NetStreaming Server 软硬件版本、目标机器和授权端点、实际会话配置、可用 TCP 地址/端口、采集与数据保存权限，以及经设备资料核实的 EEG 通道标签、顺序、采样率和数值单位。不得套用 fixture 的通道数/采样率，也不得根据 additional_scale 等字段自行推断 μV。
2. 在约定的本地或隔离环境连接前记录应用、uv/Python/Curry 版本和目标配置。完成 BasicInfo 与 ChannelInfo 握手，逐项核对服务端 EEG 通道数、标签顺序、采样率和批准的配置；不要仅以 TCP connected/streaming 状态当作有数据。
3. 在新批准的采集任务中，至少观察两个连续完整 30 秒块：记录每块 shape、dtype、start_sample/end_sample_exclusive、labels、sample_rate_hz、接收 UTC/monotonic 时间；检查有限值、每块长度与 30 × 采样率相符、块间样本连续。任何缺口、重复、长度或元信息冲突均停止该次验证并保留错误证据，不裁剪、补零、重采样或静默缩放。
4. 仅在授权记录时主动开启会话保存并选择新的输出父目录。关闭后用只读 SessionReader 核对 manifest、block_saved、processing_result、样本范围及 units=unknown；保留原始记录，回放不重连设备。
5. 明确 Curry 的开始/停止采集、放大器、录制与本应用停止推流的关系。现有 client.close() 尽力停止本客户端推流并关闭 TCP；这不是设备采集或设备录制停止确认。

## 可选模型接入（不含模型本体或准确性验收）

当前公开协议见 [staging.py](../src/sleep_stim_controller/staging.py)：ModelAdapter 提供 descriptor、prepare(cancel_event)、predict(block: BlockContext, data: DataBlock, cancel_event) 和 close(cancel_event)。预测可返回 StagePrediction(stage, confidence)；有效期别须精确属于 W/N1/N2/N3/REM，confidence 仅可由模型明确提供为有限的 0–1 数值，含义必须随模型描述。默认实现是 NoModelAdapter，输出 unavailable。

后续接入前须由用户提供模型包及版本/许可、模型输入输出说明、通道与顺序、单位、采样率、预处理和窗口语义、设备/算力依赖、取消/关闭要求、代表性测试样例及允许的运行范围。适配器从 BlockContext 和独立数据副本运行，不能改写保存的原始 DataBlock；必须验证加载/失败/取消/非法输出/耗时与结果关联。不得凭工程推理成功宣称睡眠分期准确；准确性需单独的研究设计、独立标签和验收授权。

## Rally/刺激接入缺口

当前实现只包含 P3 JSON 结构校验及 app-owned 随机 loopback 模拟器；Rally 协议适配不能被当作真实连接器。解锁任何真实请求前，主线程必须取得并冻结：

- Rally 软件/固件/服务进程版本、部署机器和经授权的网络端点；官方 API/SDK 文档、初始请求格式、字段类型、单位、合法响应码和协议版本。
- 用户核准的具体刺激协议及工程/设备限制：通道映射、极性、幅度、电流/电荷/频率/脉宽/占空等参数含义与上下限。不能从 Demo、PPT 或 P3 JSON schema 推导安全阈值。
- 实际开始、持续、修改、停止命令语义；独立设备状态/输出观察接口；请求关联、超时/未知结果处理、幂等规则和错误恢复。必须说明由谁以及如何独立确认实际输出和停止。
- 独立紧急停止路径、风险评估、设备/人员安全程序、正式联调授权及限定测试条件。

P3 的成功响应只证明模拟 API 返回成功。unknown、拒绝不重发；关闭自动决策只阻止新请求，不停止设备；关闭 app 或 Python 进程也不等于设备停止。当前产品不提供真实地址或真实发送开关。

## 同步接入缺口

当前能关联的时间包括块样本索引、块接收 UTC/本机 monotonic_ns、模型处理单调时间和模拟请求/响应的本机顺序。它们不是统一硬件时基；Curry INFO_TIME 即使收到也不构成本地时钟校准。后续任务须先明确参考时钟、EEG 与刺激输出的独立硬件事件/标记来源、事件时间戳接口、每次校准及漂移方法、跨设备时钟映射，以及主线程/研究负责人批准的最大误差和测量判据。以独立采集的硬件事件验证映射并报告不确定度；缺乏可观测硬件事件时只能报告软件事件顺序/延迟，不能声明精确同步。

## 后续批次交付证据

每项真实接入任务应明确设备/模型版本、端点及授权、配置文件身份、所用数据范围、命令与退出状态、原始失败和复测结果、线程/连接关闭证据、独立观察来源及不能支持的结论。先以合成 TCP 与 app-owned UDP 模拟验证软件路径，再按单独冻结的合同执行设备验证。真实采集、真实 Rally/刺激、模型本体/训练、受试者实验、紧急停止和时钟同步均不属于 P4-A。
