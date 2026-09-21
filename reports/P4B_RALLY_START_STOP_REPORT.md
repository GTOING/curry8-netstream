# P4-B Rally 启停联动执行报告

日期：2026-09-21
工作目录：`/Users/xuqinghe/sleep/main-development`
基线：`main@2a07e4f3386692c41ee4176b48babd700bb07fe9`

## 结论

P4-B 的真实 Rally 控制路径已在现有 runtime、Curry 会话生命周期和 P2 单写者记录通路上完成。真实模式默认关闭；生产代码固定使用 `127.0.0.1:8801`，GUI 不提供任意地址输入。本轮所有自动化通信都使用随机 `127.0.0.1` 假端点，没有连接真实 Rally、Curry 硬件、刺激设备或发送真实刺激。

最终工程回归通过：`193 passed`。其中任务基线为执行 prompt 记录的 `184 passed`，本轮新增 P4-B 专项测试 9 项。

## 实现范围

- `rally.py` 增加独立的二值 Rally worker：严格 UTF-8 `Start Stim` / `Stop Stim`，命令相关的中文成功回复与 `RALLY_ERROR_SUCCESS`，拒绝/未知/乱码/空回复分类，动态 loopback 回复源端口记录，单请求、独立 socket、迟到回复隔离、有界超时和关闭回收。
- `stimulation_runtime.py` 增加真实模式状态与资格控制：当前 session/generation、握手、显式 ONNX、完整参数配置、基线 Stop、目标/非目标二值状态、幂等、停止优先、启动失败单次补偿 Stop、模型/块序/年龄/断流/退出保护性停止及 `FAULT/UNKNOWN`。
- `controller.py` 传递当前 generation 和是否显式配置模型；`app.py` 接通真实模式和状态信号。P3 本机模拟 JSON 协议保持独立。
- 新增 `rally_control` schema v1 共用校验；保存开启时经 `ProcessingPipeline` 的 external-work/event queue 交给既有 `SessionWriter`，保存关闭时只保留 runtime/UI 内存状态。`SessionReader` 支持无 EEG block 的基线、补偿和退出控制事件，回放不发送命令。
- GUI 增加模拟/真实模式、协议前提确认、期望/确认/运行状态、最近请求/回复、端点和独立停止提示；真实模式隐藏本机模拟协议/模拟端操作项。真实 API 确认没有被表述为物理输出确认。
- 更新 `README.md`、`docs/WINDOWS_SETUP.md`、`docs/P4_INTEGRATION_RUNBOOK.md`，并新增 [P4B_RALLY_CONTROL_CONTRACT.md](../docs/P4B_RALLY_CONTROL_CONTRACT.md)。

一次回调重入竞态在专项失败测试中发现并修复：启动拒绝后的补偿 Stop 可能发生在 worker 清理旧请求之前；同一 worker 回调现在可排入唯一后续生命周期命令，其他线程仍遵守单请求限制。

## 验证命令与结果

```text
uv sync --locked
→ Resolved 17 packages in 16ms
  Checked 16 packages in 3ms

QT_QPA_PLATFORM=offscreen uv run --locked --no-sync pytest -q tests/test_p4b_rally_control.py
→ 9 passed in 3.93s

QT_QPA_PLATFORM=offscreen uv run --locked --no-sync pytest -q
→ 193 passed in 23.21s

git diff --check
→ 通过，无输出

python -m py_compile reports/P4B_capture.py src/sleep_stim_controller/*.py
→ 通过
```

专项测试覆盖精确回复、动态回复端口和 socket 隔离、基线后启停及幂等、启动拒绝单次补偿、停止失败与独立停止提示、旧 generation/非 ONNX 零启动请求、无 EEG 控制事件回读，以及保存开启时单写者落盘顺序。

## 800×600 合成证据

使用 [P4B_capture.py](P4B_capture.py) 运行：

```text
QT_QPA_PLATFORM=offscreen uv run --locked --no-sync python reports/P4B_capture.py
→ saved /Users/xuqinghe/sleep/main-development/reports/P4B_RALLY_START_STOP_800X600.png
```

产物为 800×600 PNG，画面由随机 loopback 假 Rally 实际完成 `Stop Stim` 基线和合格 ONNX `N2` 后的 `Start Stim` 回复，再通过 runtime 信号渲染。截图中显示随机测试端点、明确确认、目标期、最近 API 回复和“API 确认不等于物理输出确认”。

## 未执行与限制

- 未连接或发送到生产 `127.0.0.1:8801`，未使用 COM、Rally/刺激设备或真实 EEG；因此没有 Rally 实机回复、物理刺激输出、安全性、模型效果或受试者实验证据。
- 未做 Windows 原生窗口验收；本轮 800×600 是 macOS offscreen 合成 GUI 证据，不宣称 Windows 原生通过。
- 操作系统异常、强制杀进程或设备自身故障不能由本软件保证停止；停止未知/失败时界面要求操作者在 Rally/硬件侧独立停止。
- 未提交、推送、合并或修改 Git index。主线程预先产生的 `decision_records/ACTIVE_QUEUE.md` 与本执行 prompt 改动按任务要求保留。

## 收口修复追加报告（2026-09-21）

主线程复核指出的三项 P1 已收口；上面的 `193 passed` 保留为修复前历史基线，不被本追加结果覆盖。

- `StimulationRuntime.tick()` 接入现有 GUI 进度调度，并支持注入 monotonic 时钟；静默流超过既有最大结果年龄时解除 armed 并走一次保护性 Stop。真实 Rally worker 在实际 `sendto` 前再次复核当前 session/generation、armed、基线、停止优先和缓存年龄，Stop 等待期间变旧的缓存不会重新 Start。
- `ProcessingPipeline` 增加只供必要控制停止使用的收尾租约。普通 external-work 仍在 finish/记录错误后拒绝；必要 Stop 可继续尽力发送，租约覆盖最终 outcome 入队和后续补偿停止。可写会话的 writer 在最后控制事件之后才写 `session_finished`；记录故障时保留内存状态并明确控制事件无法持久化，不把失败报告为成功。
- Rally 隔离容量对普通 Start 使用正常上限，并额外保留一个有界 Stop/启动不确定补偿槽；旧 socket 仍保持隔离，不通过立即端口复用接收迟到回复。

本轮新增合成证据包括：假时钟静默过期、Stop 在途后缓存过期、在途 Start 停止优先、迟到 Start 成功/错命令成功不能确认 Stop、小容量耗尽、启动超时补偿、EOF/记录失败收尾，以及 Curry TCP→受控分期→假 Rally→GUI/SessionWriter 的整线测试。所有 Rally/Curry 端点均为随机 loopback，未连接生产 `127.0.0.1:8801`、真实设备或 COM。

最终验证命令与结果：

```text
QT_QPA_PLATFORM=offscreen uv run --locked --no-sync pytest -q tests/test_p4b_rally_control.py
→ 16 passed

QT_QPA_PLATFORM=offscreen uv run --locked --no-sync pytest -q tests/test_controller_synthetic.py -k p4b_curry_tcp_staging_fake_rally_gui_and_recording_closeout
→ 1 passed
```

最终验证已完成：

```text
QT_QPA_PLATFORM=offscreen uv run --locked --no-sync pytest -q
→ 201 passed in 24.30s

git diff --check
→ 通过，无输出

python -m py_compile src/sleep_stim_controller/*.py reports/P4B_capture.py tests/test_p4b_rally_control.py tests/test_controller_synthetic.py
→ 通过
```

Windows 原生窗口、真实 Rally 回复、物理刺激、安全性和受试者实验仍未执行。

## 记录收尾补充报告（2026-09-21）

本轮针对主线程第二轮复核指出的剩余竞态完成收口；此前追加报告中的 `201 passed` 保留为历史验证记录。

- Curry 网络 worker 在自然 EOF/网络异常的 `finally` 中，先调用线程安全、幂等的 `StimulationRuntime.on_session_stopping()` 登记真实控制收尾，再调用 `ProcessingPipeline.handoff_network_end()`；稍后的 Qt 网络完成回调只作幂等兜底。因此必要 Stop 不再依赖 Qt 回调先后才能建立租约。
- `ProcessingPipeline` 在同一 condition 锁内完成最终控制事件、外部工作和事件预留排空，并在调用 `SessionWriter.finish()` 前关闭新控制租约准入。关闭屏障后的必要 Stop 仍可 best-effort 发送，但事件无法持久化时会报告证据缺失；异常清理路径也不会把迟到事件放入无人排空的队列。
- 正常已接纳的控制事件继续由原单写者排空后才写 `session_finished`；录制失败或 Curry 对端主动关闭时保留真实的 failed/incomplete 状态，不伪造 closed 成功。

新增可复现证据：

- `test_p4b_natural_tcp_eof_registers_stop_before_writer_close`：真实合成 Curry TCP 运行至 `RUNNING` 后由假端主动关闭 socket，不点击 GUI 断开；假 Rally 收到 `[Stop, Start, Stop]`，真实 `SessionReader` 读到全部 sent/outcome，且均在 `session_finished` 前。对端 EOF 的最终状态如实为 `failed`。
- `test_pipeline_closes_lease_admission_before_writer_finish_and_drains_events`：用真实 `ProcessingPipeline`/`SessionWriter` 和 `finish()` 屏障固定关闭临界区；已接纳控制事件被写入，屏障内迟到租约、事件预留和事件入队均被拒绝。

最终验证：

```text
QT_QPA_PLATFORM=offscreen uv run --locked --no-sync pytest -q tests/test_p4b_rally_control.py tests/test_controller_synthetic.py
→ 54 passed in 23.86s

QT_QPA_PLATFORM=offscreen uv run --locked --no-sync pytest -q
→ 203 passed in 24.77s
```

本轮未连接生产 `127.0.0.1:8801`、真实 Curry/Rally、COM 或刺激设备；未执行 Windows 原生验收、物理输出、安全性或受试者实验。未提交、推送、修改 Git index 或共享队列。
