# P2 分期接口、会话记录与离线回放执行报告

日期：2026-09-16。状态：执行线程已完成，待主线程验收；不据此进入 P3。

## 实现结果

- `src/sleep_stim_controller/staging.py` 定义公开接口 `ModelAdapter`、`ModelDescriptor`、`BlockContext`、`StagePrediction`、`ProcessingResult` 和状态枚举。正式默认实现 `NoModelAdapter` 对每个块返回 `unavailable`，不生成 W 或任何伪分期。预测拿到隔离的数据副本；阶段只接受 W/N1/N2/N3/REM，confidence 只接受有限 0–1 数值且必须附带语义说明。
- `src/sleep_stim_controller/controller.py` 在 P1 校验之后、绘图缓冲之前为完整块分配 session/block ID，并采集 UTC 接收时间与本机 `monotonic_ns`。新的独立顺序处理通路不依赖 UI 是否消费最新绘图块。
- `src/sleep_stim_controller/staging.py` 中处理队列最多等待 4 个块，另有至多 1 个正在处理的块；绘图缓冲另有自己的单块替换策略。队列满会拒绝当前块、结束接收并显示错误，不静默覆盖处理任务。4 是工程积压上限，不是实验参数或目标推理延迟；按常规 30 秒块计，最多允许约 2 分钟等待积压，超限即失败。
- `src/sleep_stim_controller/recording.py` 实现 v1 单写者格式：`manifest.json`、有序 `events.jsonl` 和原值 `blocks/<block_id>.npy`。保存先写临时 NPY、`allow_pickle=False`，原子改名后写 `block_saved`，再写关联的 `processing_result`；manifest 原子替换。数据为 Curry 解码后的原值，不是 TCP wire 抓包，单位固定记作 `unknown`。记录默认关闭；启用后每次连接创建不覆盖的独立目录。保存未启用时不创建会话文件。
- `src/sleep_stim_controller/recording.py` 的 `SessionReader` 仅索引有效 `block_saved` 事件，按需读取单块；校验 schema、相对路径边界、shape/dtype、事件序号和处理结果关联。未终结 manifest、失败终态、截断末行、缺文件/缺结果会标记不完整；中间损坏给出位置，不静默跳过。只读打开不会改写或清理原件。
- `src/sleep_stim_controller/replay.py` 将索引和单块文件读取放在后台线程，并只保留一个最新导航请求；`src/sleep_stim_controller/ui.py` / `app.py` 接入记录开关、保存目录、保存状态、分期结果、打开/退出回放和上一块/下一块/块号选择。回放始终标“离线回放”，缺结果显示“未记录/处理未完成”；回放期间连接被禁用，打开回放不会访问 Curry 或调用模型。
- 用户主动断开时先停接收，处理线程仍为已接纳块保存原值，再将未完成预测合作式取消并记录终态。模型单块失败不阻止后续块；存储失败和队列溢出停止实时采集、保留已成功写入前缀并标记 failed/未处理计数。没有杀线程或虚报清理完成；不合作的未来适配器或阻塞 I/O 会保持“停止中”，直到资源实际退出。

## 验收证据索引

| 验收点 | 实现与测试证据 |
| --- | --- |
| 默认无模型、关闭记录、多个块各自 unavailable；模型成功/失败/非法输出/取消和原始数组隔离 | `staging.py` 的 `NoModelAdapter` / `_process_one`；`tests/test_p2_recording.py` 中 `test_no_model_is_explicit_and_recording_off_creates_no_files`、`test_injected_model_status_validation_and_raw_block_isolation`、`test_cooperative_cancel_saves_accepted_prefix_and_records_cancelled`。 |
| P1 校验→处理→保存→reader→回放完整链路；绘图替换不丢处理块；接收时间和样本区间可关联 | `controller.py::_accept_block`、`app.py::pump_latest`；`tests/test_controller_synthetic.py::test_loopback_pipeline_processes_every_accepted_block_independent_of_plot_buffer` 与 `test_loopback_recording_and_window_wired_offline_replay`；`tests/test_p2_recording.py::test_session_v1_roundtrip_raw_blocks_order_and_results`。 |
| 队列上限、积压可见、记录失败前缀、用户取消保存机会 | `staging.py::ProcessingPipeline`、`controller.py::_on_pipeline_fatal_from_worker`；`tests/test_p2_recording.py::test_processing_backlog_is_bounded_and_counted`、`test_storage_failure_preserves_prefix_and_reports_unprocessed`；回环测试 `test_loopback_slow_processor_overflow_is_visible_and_stops_stream`。 |
| 未完成会话、截断末行、缺结果、缺文件、坏 NPY、越界路径、不支持 schema、中间事件损坏 | `recording.py::SessionReader`；`tests/test_p2_recording.py::test_reader_marks_open_prefix_truncation_and_missing_result_incomplete`、`test_reader_reports_missing_corrupt_and_escaping_data_files`、`test_reader_rejects_unsupported_schema_and_malformed_middle_event`。 |
| 会话初始化失败不启动 Curry；采集失败保留已保存前缀；重放不连网 | `controller.py::_on_pipeline_ready`、`replay.py`、`app.py`；回环测试 `test_recording_initialization_failure_does_not_start_curry`、`test_connection_failure_retains_failed_session_and_valid_prefix`、`test_loopback_recording_and_window_wired_offline_replay`。 |
| UI 默认值、最小尺寸和控件布局 | `ui.py::MainWindow`；`tests/test_ui.py` 与上述窗口接线回放测试。800×600 离屏截图：[`P2_STAGING_RECORDING_UI.png`](P2_STAGING_RECORDING_UI.png)。 |

## 实际验证

- `UV_CACHE_DIR=/private/tmp/sleep-uv-cache uv sync --locked`：成功，14 个包已解析/检查；无新增依赖，未改 `pyproject.toml` 或 `uv.lock`。
- `UV_CACHE_DIR=/private/tmp/sleep-uv-cache QT_QPA_PLATFORM=offscreen uv run --locked pytest`：最终 **49 passed**，包含根级 P1、P2、Curry 客户端和协议测试；最终运行耗时 6.40 秒。
- `QT_QPA_PLATFORM=offscreen UV_CACHE_DIR=/private/tmp/sleep-uv-cache uv run --locked python -c 'from PySide6.QtCore import QTimer; from PySide6.QtWidgets import QApplication; from sleep_stim_controller.app import main; app = QApplication([]); QTimer.singleShot(500, app.quit); raise SystemExit(main([]))'`：退出码 0，验证正式 app wiring 能启动窗口事件循环并正常退出。
- `uv run --locked sleep-stim-controller` 在原生 GUI 路径实际尝试后以退出码 139 结束，输出含 PasteBoard、Input Source 与 macOS XPC `Connection invalid` 错误。`computer-use` 的应用列表中没有已注册的 `sleep-stim-controller` app，按该名字读取窗口状态也返回 Invalid app。该结果不计为通过；当前可证明的是受控 offscreen Qt 窗口与完整控件接线，原生 macOS 窗口仍未验收，不能据此断定崩溃根因。
- 首轮默认沙箱回归因禁止绑定本机 `127.0.0.1` 而使既有合成 TCP 用例报 `PermissionError`；按合同在获准本机回环执行后，全套最终回归通过。首轮积压测试还曾假设 worker 必须先进入模型预测才会溢出；回环发送速度下队列可能先满，已改为验证容量/显式终止，另由受控 worker 测试覆盖活跃预测取消。

## 未执行与边界

- 未连接 Curry 真机、Rally 或刺激设备；未运行真实采集、受试者实验、训练或模型推理。分期接口和测试替身只验证工程语义，不证明睡眠分期准确性。
- 原生 macOS GUI 窗口检查因当前 shell/UI 执行环境不可用而未完成；已完成 800×600 离屏布局与窗口级保存/回放/退出集成验证。建议主线程验收时确认本报告、截图与源码映射；若需要原生窗口证据，应在可正常访问 macOS GUI 服务的桌面会话重试。
- 不修改 P2 合同、ACTIVE_QUEUE、P1 历史报告或 Curry 独立仓库；没有提交/推送、创建分支/工作树或继续进入 P3。

## 主线程追加修复（2026-09-16）

- 修复范围：`src/sleep_stim_controller/controller.py` 在实时/回放模式切换边界清空实时绘图交接，并在回放活跃时拒绝绘图泵消费实时块；正常断开后、尚未进入回放时，P1 仍保留最后一块供历史查看。退出回放时也清空可能残留的实时交接，防止旧块延迟出现。
- 新增 `tests/test_controller_synthetic.py::test_replay_mode_isolates_disconnected_live_pending_block` 的三种确定性正式 UI 接线路径：暂停绘图轮询，接收并断开仍有 pending 的实时会话；再分别打开另一会话（实时待显示范围 `[400, 700)`、回放首块 `[900, 1200)`）、有效空会话和读取失败路径。验证打开后与加载后手动触发绘图泵均不消费旧块；有数据回放的曲线、block_id、样本区间及已记录结果保持一致；退出回放后旧实时块仍不出现。既有 P1 EOF/断开后 pending 块历史查看测试一并通过。
- 定向验证：`UV_CACHE_DIR=/private/tmp/sleep-uv-cache QT_QPA_PLATFORM=offscreen uv run --locked pytest tests/test_controller_synthetic.py -k replay_mode_isolates_disconnected_live_pending_block -q`：**3 passed, 21 deselected**，2.03 秒。
- 最终完整回归：`UV_CACHE_DIR=/private/tmp/sleep-uv-cache QT_QPA_PLATFORM=offscreen uv run --locked pytest`：**52 passed**，8.01 秒，包含根级 P1/P2、Curry 客户端与协议测试。
- 原生 `uv run --locked sleep-stim-controller` 的退出码 **139** 及 PasteBoard/Input Source/macOS XPC `Connection invalid` 错误继续按原报告保留；本次未重试原生 GUI，也未推断根因或宣称原生启动通过。没有真机、Rally、刺激或模型验证。
