# Issue 6 版本化多协议范式实现报告

日期：2026-09-23。状态：实现与本地合成验证完成，待主线程验收。

## 交付

- 新增版本化范式加载与不可变 session 快照：`src/sleep_stim_controller/paradigm.py`。加载器严格检查字段/版本、重复 JSON 字段、路径越界与符号链接逃逸、有限数值、协议/基础刺激/返回通道兼容、五期映射，并生成规范 JSON 与包、协议、payload SHA-256。session 使用内存快照，不依赖后续磁盘读取。
- 在现有 Rally request/worker 上加入 Apply：`src/sleep_stim_controller/rally.py`。复用单 worker、单请求在途、动态回复端口、隔离 socket、deadline 与 Stop 保留容量；没有第二个 UDP worker。只把精确 `RALLY_ERROR_SUCCESS` 视为 Apply 成功；已知 Rally 错误码为拒绝，未知回复为 unknown。报文结构依据只读 `reference/RealtimeControlAPIDemo5.py`：`RealTimeControl`、一个 ASCII 空格、JSON，再以 UTF-8 编码。本实现发送紧凑规范 JSON；没有复制 Demo 的参数、固定端口调用或循环。
- 扩展 `src/sleep_stim_controller/stimulation_runtime.py`：W→A、N1→B、N2→C、N3/REM→Stop；IDLE 激活先 Start、精确成功后重新检查最新结果并 Apply 最新期望；RUNNING(X)→Y 直接 Apply。Start/Apply 两步完成前不确认目标协议。在途期间仅留最新 desired，Stop 优先；失败不重试，有 Start 发送责任时最多一次必要 Stop。按包内 SD 换算记录估计到期并撤销软件确认，不把估计解释为硬件停止。
- 新增 `paradigm_control` schema，并扩展 `recording.py`、`staging.py` 现有 P2 writer准入与 reader：保存完整范式及 A/B/C 规范内容、版本/哈希、阶段决策、发送/回应、来源端口与时间；必要 Stop 事件位于 `session_finished` 之前。修复端到端测试发现的准入缺口：P2 pipeline 原先只验证 `rally_control`，现也按范式 schema 验证 `paradigm_control`。旧 `rally_control` 记录仍可读。
- 扩展 `app.py` 与 `ui.py`：可显式选择二值/范式模式和范式包，显示包摘要、latest desired、API confirmed、到期估计及物理输出未确认状态；真实启用仍要求实时会话、显式 ONNX、操作者确认和有效配置。回放只展示，不重发。
- 合成包仅位于 `tests/fixtures/issue6_paradigm_test_only/`，名称 `Issue 6 synthetic paradigm@test-only-1`，分类 `synthetic/test-only`，基础协议标记为 `TEST_BASE_PROTOCOL_ONLY`，模型输入通道为 `TEST_EEG_A`。包 SHA-256：`d64eec52ce412b20b660d25329fd34749d81fa9363594b5a5cdd2146708ec50f`。A/B/C payload、SD 转换与通道全部是测试值；正常应用不能用该包真实 arming。仓库没有生产范式包。
- 文档更新：README、Windows 设置说明、P4 接入 runbook；新增 [Issue 6 GUI 与范式包指南](../docs/ISSUE6_PARADIGM_GUI_GUIDE.md)。工程合同、共享队列、历史报告及远端快照未修改。

## 状态与记录行为

| 条件 | 软件行为 |
| --- | --- |
| 合格 W/N1/N2，当前 IDLE | Start 精确成功后，再次验证 session/年龄/最新期别，然后 Apply 最新期望协议；两步成功后才显示 `RUNNING(A/B/C)` |
| 确认运行 X，期别期望 Y | 直接 Apply(Y)，显示切换状态；API 成功后才更新已确认协议 |
| 在途时期别再次变化 | 仅更新 latest desired；过时中间协议不排队补发 |
| N3/REM、退出、EOF、模型/年龄故障 | Stop 意图优先；取消未发送请求，等待已发送请求的有界结果，再按 Start 发送责任最多一次 Stop |
| Start/Apply 拒绝、超时或未知 | 不重试、不显示目标协议已运行；存在 Start 责任时尽力一次保护性 Stop；Stop 未确认保持 UNKNOWN/FAULT |
| 包内 SD 估计到期 | 撤销软件运行确认并进入到期/未知状态；不因同阶段新 epoch 续期，估计不代表物理输出停止 |

记录中的协议回应仅是 Rally API 确认。软件没有独立物理输出传感器，事件明确保持 `physical_output_confirmed=false`。P2 记录关闭时不创建会话事件。

## 验证结果

- `env UV_CACHE_DIR=/private/tmp/issue6-uv-cache uv sync --locked`：成功，锁定依赖已满足。
- `env UV_CACHE_DIR=/private/tmp/issue6-uv-cache QT_QPA_PLATFORM=offscreen uv run --locked pytest -q tests/test_p4b_rally_control.py tests/test_controller_synthetic.py::test_issue6_curry_tcp_paradigm_apply_gui_recording_and_readonly_replay`：**32 passed**。覆盖二值兼容、Apply wire/解析、范式映射与在途 latest desired、拒绝与到期，以及新增的合成 Curry TCP→两个 30 秒 epoch→测试分期→随机 localhost Rally 假端→GUI→SessionWriter/Reader→只读回放闭环。闭环核对 Start、Apply A、Apply B、Stop 顺序，快照完整，必要 Stop 先于 `session_finished`，回放期间请求数不增加。
- `env UV_CACHE_DIR=/private/tmp/issue6-uv-cache QT_QPA_PLATFORM=offscreen uv run --locked pytest -q`：**231 passed in 23.80s**。包含合成 TCP/UDP loopback；测试没有连接 `127.0.0.1:8801`、COM、真实设备或受试者。
- `env UV_CACHE_DIR=/private/tmp/issue6-uv-cache uv run --locked python -m compileall -q src/sleep_stim_controller tests reports/ISSUE6_PARADIGM_CAPTURE.py`：通过。
- 离屏 GUI 捕获脚本 `reports/ISSUE6_PARADIGM_CAPTURE.py` 成功生成 [800×600 截图](ISSUE6_PARADIGM_800X600.png)，调用正常关闭窗口并等待控制 worker 退出；图像尺寸已核对为 800×600。
- 最终 `git diff --check`：通过。未暂存、提交、推送或合并。

## 未验证与限制

真实 A/B/C 参数、生产基础协议、通道/设备限制和生产 SD 单位换算未批准；包里的测试换算不是设备时间。FR=30 与 Start 后基础输出窗口仍只是 Issue 假设，未作为实现默认值或硬件时间上限。未执行 Windows 原生 GUI、真实 Rally、Curry、固定端口 8801、COM、电刺激硬件、无受试者时间窗测量或受试者实验。测试假端结果不支持物理刺激、安全性或研究效果结论。

实施在包含既有 Issue 5 dirty/untracked 产物的工作区上完成；这些材料与 Git index 均保留。本报告不替代主线程对合同边界和验收证据的复核。

## 2026-09-23 收口修复

- `rally.py` 的 not-sent 回调现在传递向后兼容的字符串型机器原因码，明确区分 `superseded`、容量/忙碌拒绝、发送前生命周期拒绝、取消和 `sendto` 异常；before-send 保留二元回调兼容，也允许返回原因码。增加按请求注入 socket 的测试缝，不改变默认 socket、单 worker、动态源端口或 Stop 预留容量。
- `stimulation_runtime.py` 不再因一般 `NOT_SENT` 重排 Start/Apply。只有尚未发送、被新合格期别明确替代的 Apply，且 session/generation、结果年龄、SD deadline、自动控制、窗口和 Stop 优先检查均通过时，才尝试按最新期别重规划一次；替代请求若被 worker 拒绝便直接故障收尾，不递归重试。发送未知、普通容量不足、租约拒绝、生命周期拒绝均走有限失败路径；按既有 Start 发送责任决定零次或一次 Stop。发送前检测到 SD 到期进入 `EXPIRED/UNKNOWN`，必要 Stop 复用现有控制租约。
- 新增六个回归用例：普通隔离容量为 1 时 Start 成功、Apply 被拒、只发一次预留 Stop 且租约归零；初始 Start 提交被拒时零 Stop；Apply `sendto` 异常时不重发并只 Stop 一次；Start `sendto` 异常且无发送证据时零 Stop；Apply 发送前越过 SD deadline 时进入到期状态并不发旧 Apply；Apply B 被更新合格期别 C 取代后只发送 C，普通 `NOT_SENT` 不触发重排。
- Issue 6 Curry→staging→fake Rally→GUI→SessionWriter/Reader 测试改为自然 TCP EOF 收尾；验证必要 Stop 记录严格早于 `session_finished`，既有回放期间请求数不增加检查仍通过。

### 收口验证

- `env UV_CACHE_DIR=/private/tmp/issue6-uv-cache QT_QPA_PLATFORM=offscreen uv run --locked pytest -q tests/test_p4b_rally_control.py tests/test_controller_synthetic.py::test_issue6_curry_tcp_paradigm_apply_gui_recording_and_readonly_replay`：**38 passed in 11.04s**。
- `env UV_CACHE_DIR=/private/tmp/issue6-uv-cache QT_QPA_PLATFORM=offscreen uv run --locked pytest -q`：**237 passed in 24.46s**。原有 231 passed 历史记录保留；本轮新增六项用例。
- `env UV_CACHE_DIR=/private/tmp/issue6-uv-cache uv run --locked python -m compileall -q src/sleep_stim_controller tests reports/ISSUE6_PARADIGM_CAPTURE.py`：通过。
- `git diff --check`：通过。
- 普通 sandbox 首次运行 loopback 测试时，系统拒绝绑定随机 localhost 端口；按授权的本机 loopback 测试权限重跑后定向及完整回归均通过。没有连接生产 `127.0.0.1:8801`、外部设备或真实 Curry/Rally。
- 未重制 GUI 截图；本轮只有传输/控制逻辑及测试变化。未改共享队列、主线程验收报告或历史 231 passed 记录；未暂存、提交、推送或合并。
