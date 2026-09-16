# P1 桌面与 Curry 脑电接入执行报告

日期：2026-09-15。执行范围：`docs/tasks/P1_DESKTOP_EEG_EXECUTION.md`。本报告只记录本次 P1 实际实现和验证，不表示主线程已更新队列，也不扩展到 P2。

## 交付结果

已完成可启动的 PySide6/PyQtGraph 桌面程序。启动方式：

```sh
uv sync --locked
uv run --locked sleep-stim-controller
```

窗口默认连接 `127.0.0.1:4455`。连接期间地址和端口锁定；界面显示 `disconnected / connecting / streaming / stopping / error`，并单独显示等待握手、等待首个有效块等细节。数据到达前图表为空，不生成演示曲线。模型和电刺激均明确标记为未接入。

## 实现文件

- `src/sleep_stim_controller/app.py`：启动入口和 Qt 信号/轮询接线。
- `src/sleep_stim_controller/ui.py`：正式窗口、固定状态/错误区、可滚动设置/诊断、动态多通道图表。
- `src/sleep_stim_controller/controller.py`：单会话状态机、后台线程、代际保护、错误和关闭生命周期。
- `src/sleep_stim_controller/validation.py`：P1 DataBlock 边界校验。
- `src/sleep_stim_controller/latest.py`：最多一个待展示块的有界交接及替换计数。
- `tests/`：校验、缓冲、离屏 UI 和合成 TCP 集成测试。
- `pyproject.toml`、`README.md`：启动入口、pytest 发现范围和使用说明。

## 合同验收对应关系

| 合同要求 | 实现/证据 |
| --- | --- |
| BasicInfo + ChannelInfo 后才交付 EEG | `curry_netstream.client.CurryClient` 在完整标签就绪后才请求推流并生成 DataBlock；`test_real_loopback_handshake_two_blocks_and_bounded_handoff` 经过真实 TCP 握手。 |
| 不固定通道、采样率、单位 | `SessionInfo` 动态生成图表；Y 轴为“原始值（单位未确认）”；UI 测试使用 2 通道/10 Hz。 |
| 严格完整 30 秒块 | `validation.py` 拒绝二维/非空以外的形状、标签不匹配、非正/非有限采样率、NaN/Inf、非 30 秒样本数和握手不一致；非法块集成测试可见地进入 error。 |
| 时间轴语义 | `ui.py` 使用 `x[k] = k / sample_rate_hz`，显示块起点和样本数，不使用墙钟或同步声明。 |
| 多通道滚动图、共享 X、独立 Y | `PlotPanel` 为每通道创建 PlotItem，后续图表 `setXLink` 到第一条，右侧包在 QScrollArea；离屏截图见 [P1_DESKTOP_EEG_OFFSCREEN.png](P1_DESKTOP_EEG_OFFSCREEN.png)。 |
| 有界最新块交接 | `LatestItemBuffer` 只保存一个 pending item，替换次数进入诊断；两连续块测试确认显示端取得最新第 2 块。 |
| 后台网络、主线程 UI | Curry 工作线程只做连接/握手/解码/校验和 buffer 写入；Qt 定时器在主线程取块并绘图。 |
| 空闲、半包可取消 | Curry 客户端使用短接收轮询，socket timeout 保留半包继续等待；分片测试跨多个超时仍收到两块，idle 测试取消耗时小于 5 秒。 |
| EOF/协议/非法数据错误可见 | 远端 EOF 和非 30 秒块测试均进入 error，保留错误文本；取消产生的连接结束不显示故障。 |
| 断开、重连、关闭 | 重复连接被拒绝；断开后新会话使用新代际；关闭沿用取消路径，未清理时窗口保持可见且为 stopping。 |

## 实际验证

`uv` 在受限沙箱中默认无法访问用户级缓存；使用任务专用临时缓存目录执行，未改动依赖版本。`uv sync --locked` 实际成功。

首版相关测试命令（收口前）：

```sh
UV_CACHE_DIR=/private/tmp/sleep-uv-cache \
QT_QPA_PLATFORM=offscreen \
uv run --locked pytest
```

结果：24 passed（根级新增 P1 测试 13 项，Curry 原有测试 11 项），无失败。覆盖内容包括两个连续 30 秒合成块、真实本机回环握手/分帧、帧头和载荷跨超时分片、静默取消、远端 EOF、非法块、重复连接和重连、离屏布局/图表以及原有协议回归。该结果按要求保留为首版事实。

启动验证也实际执行：

- `uv run --locked sleep-stim-controller` 在 `QT_QPA_PLATFORM=offscreen` 下成功进入事件循环；由受控测试父进程发送 SIGTERM 后返回 143，证明入口可启动。
- 直接创建窗口并用 Qt 定时器调用 `close()` 返回 0，且 `busy=False`，证明无连接启动/关闭路径正常。
- 已生成并检查离屏界面截图：[P1_DESKTOP_EEG_OFFSCREEN.png](P1_DESKTOP_EEG_OFFSCREEN.png)。
- 尝试使用 macOS 原生 CUA 窗口检查时，系统拒绝 CUA 访问 Terminal（`com.apple.Terminal`）；因此没有声称完成原生窗口人工验收。离屏检查和自动化证据已完成。

## Curry 上游兼容改动

`curry8-netstream/` 是独立 Git 仓库，未初始化根仓库、未提交或推送。仅修改：

- `client.py`：默认连接超时改为 3 秒；增加短接收轮询、`cancel_event`/`cancel()` 合作式取消、`StreamResult`、状态/会话/错误回调；保留半包跨 timeout；错误 EOF 和协议错误可传播；握手标签就绪前不交付 EEG；关闭时尽力发送 `REQUEST_STREAMING_STOP`。
- `__init__.py`：导出新增取消异常和结果类型。
- `README.md`：补充生命周期接口说明。

未改变线协议字节布局、数据缩放、CLI 的原有调用方式或控制请求语义。P1 控制器不调用真实 Curry、Rally 或电刺激脚本；合成服务仅绑定本机回环端口。

## 未执行项目与剩余限制

- 未连接真实 Curry 8，合成回环通过不等于真机验证；仍需目标设备地址、配置和至少两个真实 30 秒块。
- 未加载睡眠分期模型，未生成任何分期结果。
- 未实现电刺激策略、Rally 通信、正式实验、记录/回放和跨设备时钟同步。
- 未进行 macOS 原生窗口人工检查，原因见上文；已完成离屏 UI 验证。
- 未修改共享任务队列或声称主线程验收；后续应由主线程依据本报告决定是否进入 P2。

## 主线程验收补充收口（2026-09-15）

针对 [P1_DESKTOP_EEG_ACCEPTANCE.md](P1_DESKTOP_EEG_ACCEPTANCE.md) 的补充要求，继续在原 P1 范围内完成了以下局部修复和验证。

### 修复

- `QueuedBlock` 现在携带会话代际、历史标记和终态原因。控制器在会话终态后消费 pending 块时，会先将其标记为历史数据；因此 EOF、主动断开以及“终态通知先于下一次 poll_latest”都不会把历史块重新显示成当前最新数据。
- 新会话建立时清空旧会话 pending，并重置会话计数/终态信息。旧 worker 的迟到回调仍受 generation 检查保护，不能覆盖新会话。
- `resources_released()` 只有在实际 worker thread 已退出后才返回真；终态处理不在 Qt 主线程阻塞等待，而是短延迟重查线程退出。

### 新增确定性证据

- `tests/test_controller_synthetic.py` 增加等待 BasicInfo、等待 ChannelInfo、帧头半包和载荷半包的主动取消测试；每项均确认 5 秒内完成、worker 不存活、客户端资源释放、合成服务连接结束。
- 增加活跃连接和活跃载荷半包期间调用 `window.close()` 的测试，确认窗口在清理完成前保持可见，随后实际关闭，合成服务结束，worker 不存活且控制器资源释放。
- 增加 EOF 前已有 pending、主动断开后已有 pending、重连清除旧 pending 并接收新代际块的应用接线测试。测试同时覆盖“会话结束先于 `pump_latest()`”的顺序。
- 增加不连续 `start_sample` 和 NaN 数据经真实合成 TCP → Curry 解码 → 控制器 → UI 的可见错误测试；错误分别包含“采样不连续”和“NaN”。
- 最小尺寸测试实际 `show()` 了 `800×600` 窗口，确认连接/断开按钮和持久错误提示可见；证据截图见 [P1_DESKTOP_EEG_MINIMUM_OFFSCREEN.png](P1_DESKTOP_EEG_MINIMUM_OFFSCREEN.png)。

### 补充运行结果

第一次收口定向运行发现重连测试的合成服务发送速度过快，测试在断言“无 pending”前已经收到新块；该失败是测试时序问题，未改变实现语义。将 fixture 改为可控释放后重新运行：

```sh
UV_CACHE_DIR=/private/tmp/sleep-uv-cache \
QT_QPA_PLATFORM=offscreen \
uv run --locked pytest tests/test_controller_synthetic.py tests/test_ui.py
```

结果：17 passed（无失败）。随后已执行包含 Curry 原有测试的最终完整回归：

```sh
UV_CACHE_DIR=/private/tmp/sleep-uv-cache \
QT_QPA_PLATFORM=offscreen \
uv run --locked pytest
```

结果：35 passed（根级 P1 收口及原有 Curry 测试，全部通过）。此前首版结果 24 passed 仍保留，不覆盖历史事实。
