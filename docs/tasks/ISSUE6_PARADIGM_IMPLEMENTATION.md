你是 Issue 6 版本化 Rally 多协议范式执行线程。

## 目标、基线与权威依据

在正式工作区 `/Users/xuqinghe/sleep/main-development`，实现分期驱动的通用多协议控制：W→A、N1→B、N2→C、N3/REM→Stop。实现基于**当前未提交的 Issue 5 修复**（主线程已验收，执行者报告 210 passed），不能仅从 Git HEAD `6b0f234bf35be9d43f1d8ab5a66c72b279ac996f` 新建干净分支而丢掉它。开始先核对 dirty/index 并保护已有产物。用户未授权本任务提交、推送、合并、真机命令或受试者实验。

必读：[Issue 6 工程合同](../ISSUE6_PARADIGM_CONTRACT.md)、[远端 Issue 6 快照](../../reports/remote_issues/2026-09-22/issue-6.md)、[Issue 5 合同](../ISSUE5_RALLY_ARMING_CONTRACT.md)、[Issue 5 验收](../../reports/ISSUE5_RALLY_ARMING_ACCEPTANCE.md)、[P4B 传输/记录合同](../P4B_RALLY_CONTROL_CONTRACT.md)。不自行修改上述合同或共享队列；如遇真正改变实验语义的冲突，保留证据并提出最小待决项。局部代码组织可自行决定。

## 实施顺序与具体方法

1. **范式包**：新增独立的加载/验证对象（建议 `ParadigmRuntimeManager` 对应通用决策状态，数据类型按现有代码风格实现）。解析版本化 `paradigm.json` 和全部被引用协议 JSON；在启用前完成字段/路径/数值/通道验证，规范化为稳定 JSON 并计算内容 SHA-256。一个 session 固定同一快照，文件后来变化不改变在途控制。只提供清楚标记 `synthetic/test-only` 的 A/B/C 测试包，不把 Issue 示例中的电流等数值伪装为已批准生产参数。真实模式无默认生产包，未明确选择并核对完整包时不能 armed。
2. **传输**：扩展现有 `rally.py` 请求/worker 支持 `RealTimeControl ` + 规范 JSON UTF-8 报文。不要启动 Demo、写第二条 UDP worker 或复用不同请求的 socket。`RALLY_ERROR_SUCCESS` 是 Apply 的唯一已知精确成功；已知 `RALLY_ERROR_*` 归类拒绝，其余无法解释的回复归 unknown。原有 Start/Stop 中文成功解析、动态源端口、有界总 deadline、socket 隔离、Stop 专用容量与发送边界责任保持。参照 `reference/RealtimeControlAPIDemo*.py` 核对字段与一个空格；这些 Demo 自带的数值和循环不进正式运行器。
3. **同一控制链**：扩展 `stimulation_runtime.py` 当前会话/generation/年龄/单调块过滤，支持二值模式与显式范式模式。激活从 IDLE 发送 Start，成功后复核最新结果并应用**最新期望**协议；两条成功才显示 RUNNING(X)。运行中 X→Y 直接 Apply(Y)，不先 Stop。控制事件可增加兼容字段或新版本/事件类型，但必须经现有单写者、reader 与回放显示，旧 `rally_control` 会话仍可读。真实模式和 P3 模拟互斥；NoModel、回放和旧 generation 不发请求。
4. **在途状态与停止优先**：保留一个请求在途和一个最新 desired；A 处理时依次出现 B、C，则跳过过时 B。N3/REM、用户关闭、年龄/模型故障、Curry EOF、录制失败和退出时，取消未发命令，已发命令待有界结果后最多一次必要 Stop。Start/Apply 任一步拒绝或未知不可显示 RUNNING(X)，不自动重试。沿用 Issue 5 实际 Start 发送责任：无 Start 证据的 IDLE 不发 Stop；Stop 失败保留 FAULT/UNKNOWN。需要停止的 Apply 链不能因为 pipeline 普通事件准入关闭而遗失控制租约；保留 EOF handoff 与 writer 关闭屏障。
5. **到期**：若包明示并可验证 SD 换算，记录每次协议确认与估计到期；到期撤销 RUNNING 确认，进入 EXPIRED/UNKNOWN、解除自动控制，有 Start 责任时至多一次 Stop，等待操作者独立核对后重新启用。同阶段结果不得自动续期或重启。不能证明 SD 单位的包禁止真实自动控制。模拟时钟验证，不将估计时间写成物理停止事实。
6. **GUI 与归档**：界面显式选择二值或范式模式、包路径/版本和人工确认，显示 latest desired、API 确认协议、激活/切换/到期/故障。尽量在 800×600 可用。记录完整规范范式及全部协议内容/版本/哈希、基础协议、模型通道、epoch 起止/完成、stage/confidence、决策/no-op、每次发送/回应的 UTC 与 monotonic 时间、源端口、切换/维持间隔、API 与物理状态区分；不要只存绝对路径或依赖日后读取外部包。回放只展示，绝不再次发送控制命令。
7. **文档**：更新 README、Windows 设置/接入手册与 GUI 说明，明确无生产范式包、参数尚未批准、FR=30/基础输出时窗仅为 Issue 假设、Windows 和真机验证未执行。历史报告只读。不修改共享 `decision_records/ACTIVE_QUEUE.md` 或主线程验收记录。

## 核心验证矩阵

用 mock 传输、可控时钟和随机本机 loopback 假 Rally，**不访问生产 `127.0.0.1:8801`**。至少覆盖：

- 包缺文件/越界引用/重复通道/非有限数/返回通道修改/错误版本/缺 SD 单位、规范化哈希及加载后磁盘变化；真实启用零 UDP、无包和合成包在真实模式不可启用。
- W→A、W→W no-op、W→N1 B 直接切换、N1→N2 C、N2→N3 Stop、N3→REM no-op、REM→W Start+Apply A；模型失败、旧结果、回放不驱动控制。
- 激活两个步骤的成功、拒绝、超时、错回复；只有两步成功才显示 RUNNING。Start 在途时 B→C 只应用 C；Apply A 在途时 B→C 跳 B；任意在途出现 N3/REM 只做必要 Stop；无未解决 Start 的关闭零 Stop。
- 发送/取消竞态、Stop 优先、Stop 失败不自动重试、socket 容量预留与动态回复端口；若估计 SD 到期则撤销确认、最多一次 Stop、同阶段不续期，重新启用前人工确认。
- Curry 合成 TCP→30 秒窗口→合格分期→fake Rally→GUI→SessionWriter/Reader 集成：归档完整快照与时间序列，必要 Stop 事件先于 `session_finished`，录制故障仍尽力 Stop，回放零发送。保持现有二值模式测试通过。

执行 `uv sync --locked`、相应定向/集成测试、最终完整回归、编译和 `git diff --check`；完成离屏 GUI 启停及 800×600 截图。任何测试不得连接真实 Rally、8801、COM、电刺激设备或受试者；本任务也不执行无受试者真机时间窗测量。

## 交付

报告写 `reports/ISSUE6_PARADIGM_IMPLEMENTATION_REPORT.md`，说明实际文件、状态转换、wire 依据、合成包身份、命令序列、测试结果和已知限制；截图写 `reports/ISSUE6_PARADIGM_800X600.png`。报告分别陈述软件测试、假端 API 行为和未验证的真实物理输出，不宣称范式 A 真实刺激参数已确定。保留现有 dirty/untracked、Git index 和历史证据；不提交、推送、合并或在远端评论，不分包给其他执行线程。完成后回报主线程验收。
