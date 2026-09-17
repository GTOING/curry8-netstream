# P-CURRY-WINDOW 执行报告

日期：2026-09-17。目标克隆：`/private/tmp/sleep-github-publish-20260916`。实际基线为 `main@19702e00da653a48aca0a892b1a32fe64e92e0c0`（包含已合并 ONNX 修复和 Windows 环境脚本）。本轮未提交、未推送、未合并、未修改 Git index；既有未跟踪 `:memory:.ses` 保持原状。

## 结果

已将 Curry 接收路径从“每个网络包必须为 30 秒”改为“网络包可变长，按连续采样点组装不重叠的 30 秒分析窗口”。默认 `NoModel` 仍是完整链路的默认入口。网络收包、窗口组装、处理队列、记录和回放仍由原有生命周期管理；没有连接真实 Curry、Rally、设备或受试者，也没有执行真实采集/刺激。

## 实现与调用路径

- `validation.py` 新增 `validate_stream_block()`，只检查 DataBlock 类型、二维非空数据、标签/通道、有限值、正采样率、非负整数 `start_sample` 及握手元信息；`validate_data_block()` 保留 30 秒完整窗口校验并复用公共检查。
- 新增 `epoching.py::ThirtySecondEpochAssembler`。构造时按握手采样率计算 `N=round(Fs*30)`，要求 `Fs*30` 是正整数；首个合法包可以从非零样本号开始。每个包先完整校验，再按绝对样本号检查连续性，用至多一个未完成窗口的 float32 缓冲跨包填充；一个包可以产出多个窗口，完整窗口交付后换用新缓冲，后续写入不会修改已交付数组。事件列表非空时明确报“实时流事件语义尚未支持”。
- `controller.py` 在握手成功后为当前 generation 创建累积器；网络线程在 `_accept_block()` 入口捕获 UTC/monotonic 到达时间，累积产物经完整窗口校验后才生成 `BlockContext`、进入既有有界 pipeline 和界面最新摘要。`block_id`、validated/accepted 统计现在代表完整分析窗口；另有 received packet/sample、completed/accepted window 和 partial 尾段统计。
- 进度通过线程安全快照和 200 ms Qt 合并刷新（最多约 5 Hz）送入 UI，不按每个网络包排队信号。握手后状态立即显示已连接/正在接收，摘要显示收包、窗口和 `pending_samples / Fs` 秒；回放模式和实时终态不会被迟到实时进度覆盖。
- `staging.py` 在 pipeline 完成前接收冻结的组装摘要；`recording.py` 仍由原单写者在 `session_finished` payload 中可选写入 `stream_assembly`。关闭保存时只显示诊断、不创建文件；旧 schema v1 会话仍可读，新增字段缺失不影响回放。
- `README.md`、Windows 配置说明、P4 接入手册及当前 P1/模型/需求说明已改为区分 Curry 发包频率与 30 秒分析窗口，并说明非压缩 Raw/Processed 配置、连续性保护和现场边界。

### 时间、取消与结束

一个窗口的 `received_utc`/`received_monotonic_ns` 取使该窗口完整的最后一个网络包进入回调时的时间；不是第一包时间，也不是处理线程开始时间。EOF、主动断开、异常和关闭会冻结摘要并释放累积缓冲；不足 `N` 的尾段不进入模型、策略或 `blocks/*.npy`。取消、队列满、重连和旧 generation 回调沿用原有协作取消/资源回收路径，旧累积器不能修改新会话。

`stream_assembly` 字段为：`window_seconds`、`window_samples`、`received_packets`、`received_samples`、`completed_windows`、`accepted_windows`、`partial_samples`、`partial_start_sample`。其中 received 只计通过网络包校验的包，completed 是累积完成数，accepted 是成功进入有界处理队列数；队列满或取消时二者可以不同。`partial_*` 只描述结束时实际留在累积缓冲中的不足窗尾段，不宣称等于所有未保存样本。

## 明确样例

合成会话使用 `Fs=10 Hz`，所以每个分析窗口为 `N=300` 点；首包 `start_sample=100`，网络包样本数依次为 `[73, 227, 111, 189, 200]`，总计 800 点。组装结果为：

| 产物 | 样本区间 | 结果 |
| --- | --- | --- |
| 窗口 1 | `[100, 400)` | 300 点，进入 pipeline |
| 窗口 2 | `[400, 700)` | 300 点，进入 pipeline |
| 尾段 | `[700, 900)` | 200 点，结束时不用于分期 |

测试逐样本比较记录回读数组，确认没有重叠、遗漏、缩放或后续缓冲覆盖；同一窗口的接收时间分别来自第 2 和第 4 个网络包。

## 实际验证

平台是当前 macOS 开发环境；命令均在目标克隆执行：

```sh
UV_CACHE_DIR=/private/tmp/sleep-uv-cache uv sync --locked
QT_QPA_PLATFORM=offscreen UV_CACHE_DIR=/private/tmp/sleep-uv-cache uv run --locked pytest -q
QT_QPA_PLATFORM=offscreen UV_CACHE_DIR=/private/tmp/sleep-uv-cache uv run --locked python reports/P_CURRY_WINDOW_capture.py
```

最终完整回归覆盖根级控制器、累积器、校验、记录/回放、ONNX、P3 模拟策略、UI 以及 `curry8-netstream/tests`；最终结果为 **123 passed**。新增真实 loopback TCP 场景覆盖 BasicInfo/ChannelInfo 握手、TCP 分段/粘包、五个短网络包、两个完整窗口、200 点尾段、记录/reader 数组准确性、最后贡献包时间、NoModel 和资源收尾。另有累积器边界测试覆盖固定/不等长包、跨窗、单包多窗、非零首样本、缺样/重复/乱序、NaN/Inf、元信息变化、事件、非法 `Fs×30`、finalize 清理和已产出数组隔离。

一次前置回归仅失败于旧测试仍把 299 点网络包当作“必须 30 秒”的错误场景；已将该场景改为真正的空 EEG payload 协议错误，并在最终回归中通过。测试中的 localhost socket 只用于合成服务；合成快速传输验证样本分窗和协议边界，不证明真实采样时钟、网络积压或端到端实时性。

800×600 进度界面截图（`QT_QPA_PLATFORM=offscreen`，不是原生桌面截图）：[P_CURRY_WINDOW_800X600.png](P_CURRY_WINDOW_800X600.png)。截图显示 NoModel、已连接/正在接收以及 48 包、4125 点、16.5/30 秒累计进度。

## 尚未完成的 Windows+Curry 真机验收

本轮没有 Windows 主机、Curry 实例或现场授权，因此不能宣称 Windows 原生窗口或真机通过。现场复核应在用户提供的 Windows 机器上按以下顺序执行，并保留版本和配置：

1. 用 `setup_windows.ps1` 完成 `uv sync --locked`，启动控制器，保持模型勾选关闭（NoModel）、记录按现场授权决定。
2. 在 Curry 8 NetStreaming Server 配置与控制器一致的 IPv4/端口，选择非压缩 float32；先记录 Curry/放大器/控制器版本、Raw 或 Processed、montage、通道顺序、采样率、Blocks Per Second/Auto 和实际每包样本数。不要为凑 30 秒修改采样率。
3. 启动推流，确认握手后立即显示已连接/正在接收；观察收包计数和 `pending / 30 秒` 进度，持续到至少两个完整窗口，核对每个窗口的起止样本号、通道、采样率、有限值和连续性。
4. 经批准后开启记录，断开并用 reader 核对两个完整窗口和 `session_finished.stream_assembly`；主动断开时确认不足窗尾段被计数但未进入分期/记录窗口，窗口关闭后线程和 socket 退出。

现场若出现错误，需保留完整错误文本、Curry 版本、采样率、包频率/每包点数、Raw/Processed/压缩设置和起始样本号。当前实时事件列表语义尚未校准，带事件网络流应先作为明确失败处理，不应关闭连续性保护或静默补点。

## 收口修复（2026-09-17）

本节追加记录本次复核后的修复；上面的原始执行结果与 **123 passed** 保持不变。

### 摘要交接与结束时序

- `ProcessingPipeline.mark_network_started()` 只在控制器真正创建网络线程前启用网络结束门槛；未启动网络、管线准备失败和启动异常直接提交空摘要/结束状态，不会等待不存在的交接。
- 网络 worker 的 `finally` 在关闭 socket、冻结当前 generation 的累积器之后，直接调用一次性的 `handoff_network_end()`。该接口同时提交冻结摘要、取消/错误状态并唤醒处理线程；后续 Qt `_on_network_finished` 只负责生命周期和幂等兜底，不写 `events.jsonl`。
- 处理/记录异常先取消网络，处理线程在单写者 `SessionWriter.finish()` 前等待已启动网络的交接，因此 `session_finished.stream_assembly` 与 `PipelineOutcome.stream_assembly` 来自同一个冻结快照。控制器没有在锁内 join 或等待 worker。
- UI 会话初始文案为“正在连接/等待握手”；只有收到完整 BasicInfo / ChannelInfo 并进入真实推流状态后才显示“已连接”。失败、主动断开和终态均清除连接成功语义。

### 新增确定性覆盖

追加测试覆盖：已有 200 点尾段在 EOF 与主动取消后不会进入下一 generation；重连从空累积器开始；旧 generation 的迟到回调不能替换新进度；处理线程在网络尚未结束时注入一次 `save_processing_result` 失败，仍能写出失败归档的 `session_finished`，且其摘要与 `PipelineOutcome` 一致；显式覆盖 Inf、重复和乱序包；UI 握手前后的文案和终态文案。

`reports/P_CURRY_WINDOW_capture.py` 已改为启动真实合成 TCP Curry server，经过控制器的 BasicInfo/ChannelInfo、短包解析、窗口累积和 Qt 信号后再抓取 800×600 画面；不再手动注入状态或快照。当前截图实际显示 5 个网络包、800 个样本、2 个完整窗口和 200 点尾段：[P_CURRY_WINDOW_800X600.png](P_CURRY_WINDOW_800X600.png)。

收口验证实际执行：`uv sync --locked` 成功；目标克隆中的最终 `QT_QPA_PLATFORM=offscreen UV_CACHE_DIR=/private/tmp/sleep-uv-cache uv run --locked pytest -q` 为 **131 passed in 13.14s**；`git diff --check` 通过。全量回归包含新增 race、EOF/取消/重连、旧 generation、Inf/重复/乱序和 UI 握手测试。

Windows+Curry 真机、真实设备、Rally 和受试者实验仍未执行；这些限制与上面的原始报告一致。
