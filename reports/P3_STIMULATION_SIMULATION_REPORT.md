# P3 睡眠期决策与 Rally 本机模拟执行报告

日期：2026-09-16。范围：P3 合同规定的本机模拟实现与离屏/回环工程验证；未进行真机、受试者或真实刺激操作。

## 实际实现

- `src/sleep_stim_controller/stimulation.py` 提供 `StimulationConfig`、`TriggerMode`、`RequestStatus`、`StimulationDecisionEngine`、`parse_protocol_scheme()`、`load_protocol_scheme()`、`build_rally_message()` 和 `parse_rally_response()`。配置无预置目标期、策略、实验间隔、最大结果年龄或协议；本机模拟通信超时按合同默认为 1 秒。
- `src/sleep_stim_controller/rally.py` 提供 `LoopbackRallySimulator`、`SimulatedRallyEndpoint`、`SimulatedReply` 和 `RallyTransportWorker`。模拟端仅绑定 `127.0.0.1:0`；发送端点还须是当前 runtime 所持模拟器创建的同一 endpoint 对象。应用没有外部 Rally 地址或 `8801` 解锁入口。
- `src/sleep_stim_controller/stimulation_runtime.py` 提供 `StimulationRuntime`，协调 live session、策略、配置、模拟端和异步结果，不在回放路径调用策略或通信。
- `src/sleep_stim_controller/staging.py` 扩展现有 P2 `ProcessingPipeline`：新增 `reserve_session_event()`、`commit_session_event_reservation()`、`enqueue_session_event()` 与外部工作租约。P3 事件队列及预留槽位合计有界为 64；队列/落盘错误触发现有 fatal/断开收尾路径。
- `src/sleep_stim_controller/recording.py` 的 `SessionWriter.append_extension_event()` 由原 P2 单写者追加 schema v1 可选事件；`SessionReader` 校验配置版本、block/session/request 关联、测试替身标记和结果状态，并将每块相关事件作为 `ReplayBlockEntry.stimulation_events` 提供只读回放。
- `src/sleep_stim_controller/app.py`、`ui.py` 接入中文可折叠 P3 设置：W/N1/N2/N3/REM 多选、显式策略/间隔/结果年龄、用户选择的协议 JSON、模拟超时、模拟端启停、自动决策、近期决策/响应。保持 P2 默认 `NoModelAdapter`；正式 UI 没有假分期或手动期别入口。
- 根 `README.md` 增加 P3 运行约束与协议字段说明；截图：[P3 默认 UI](P3_STIMULATION_SIMULATION_UI.png)。

## 策略、通信与事件收口

`each_matching_block` 对每个新到达的有效目标期块形成一次候选；`enter_target_set` 将首个有效目标块视为进入，目标集合内部切换不重复触发。失败/缺模型/取消不会重置上个有效目标集合成员状态。新 session 与显式配置变更开始新的进入周期；配置变更不会重置已处理 block ID 或上次实际发送时刻。重复/倒序块不改变策略前态；缺少单调接收时间、年龄超限、最小间隔、在途请求、NoModel/非法结果均不发送，抑制候选不排队补发。

UDP 报文为 UTF-8 `RealTimeControl ` 加 JSON，不插入本地 `request_id`。同时至多一个 transport 请求。每次请求使用独立随机来源端口；超时后端口隔离保留到所属模拟端关闭，最多 128 个，达到上限后拒绝新发送。旧请求的迟到/重复数据报仍定向到旧端口；响应还必须来自所持模拟 endpoint 的精确地址/端口。模拟端释放其端点时，传输工作线程自行回收关联 socket。`RALLY_ERROR_SUCCESS` 只记作 API 报告成功；拒绝、未知码、非法 UTF-8、超时及已发送后关闭均不会重试；未知或拒绝关闭自动决策。关闭自动决策不表达设备停止。

记录开启时，每块的 `block_saved`、`processing_result` 之后由处理/写者线程追加配置快照与 `decision`；异步 `request_sent` 和 `request_outcome` 经有界队列返回同一写者。session 收尾等待在途租约和事件预留归零、队列清空后再写 `session_finished`。发送前预留 `request_sent` 容量，事件不能接纳时阻止发送并按 P2 fatal 路径停止接收。旧 P1/P2 schema v1 事件及字段未改写。

## 验证与实际结果

已实际执行依赖同步：

```text
UV_CACHE_DIR=/private/tmp/sleep-uv-cache uv sync --locked
Resolved 14 packages in 11ms
Checked 14 packages in 3ms
```

已执行任务要求的最终完整回归（offscreen UI；网络仅合成 TCP/本应用 loopback UDP）：

```text
UV_CACHE_DIR=/private/tmp/sleep-uv-cache QT_QPA_PLATFORM=offscreen uv run --locked pytest
88 passed in 10.11s
```

还执行了 P3 传输、合成 Curry、决策、NoModel 与 UI 关联定向回归：

```text
UV_CACHE_DIR=/private/tmp/sleep-uv-cache QT_QPA_PLATFORM=offscreen uv run --locked pytest -q tests/test_rally.py tests/test_controller_synthetic.py -k 'p3 or transport or timeout_wrong_source or late_reply or duplicate_late_reply or simulator_owns or cancel_before_send'
13 passed, 24 deselected in 2.31s
```

该回归覆盖两种策略、边界/非法协议、API 成功/拒绝/未知码、错误来源、超时、迟到/重复响应隔离和取消未发送请求；合成 TCP → 注入的测试模型 → P2 pipeline → P3 决策 → 实际本机 UDP → P2 事件 → 只读回放也已贯通。集成记录验证 `test_double=true` 被保留，回放前后模拟 UDP 请求数不变。另验证正式 `NoModel` 即使配置完整并显式开启自动决策仍零 UDP 请求；API 拒绝后只发送一次且自动决策关闭；窗口退出时已发送未确认请求记为 `unknown`，其 `request_outcome` 序号早于 `session_finished`。事件队列预留溢出测试确认会话以失败/不完整收尾且写者不挂起。

由于正常沙箱首次拒绝本机 socket bind，以上网络回归在获准的 loopback 测试权限下运行；没有连接外网、真实 Rally、设备或固定端口。另以 offscreen Qt 构造应用、显示并关闭窗口，实测 `exit=0` 且 P3 transport worker 正常退出。原生 macOS GUI 会话未在本任务中启动或复测。

## 失败修复与边界

端到端首轮联调发现事件槽位预留路径缺少 `uuid` 导入，已补齐并重跑相关测试；一处测试断言把 UDP `bytes` 按 `str` 查找，已修正为字节断言。传输关闭语义复核后，改为由唯一 transport worker 回收它拥有的 socket；定向及最终完整回归均通过。无已知 P3 工程测试失败。

真实模型与模型效果、Curry 真机、真实 Rally、刺激输出及停止、设备参数阈值/极性、跨设备时钟同步、受试者/正式实验和 P4 均未执行，也未由模拟结果支持。原生 macOS 启动 139 的既有根因仍未确定；offscreen 启动/关闭通过不构成原生桌面或真机通过。未修改 P1/P2 历史报告、P3 合同、ACTIVE_QUEUE 或 reference 原件；未改依赖/锁文件，未初始化根 Git、提交、推送或启动后续阶段。
