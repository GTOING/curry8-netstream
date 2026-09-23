# Issue 6 主线程工程验收

日期：2026-09-23。结论：**暂不通过，需完成未发送请求收口**。此结论只针对工程逻辑；真实刺激参数、Windows 和 Rally 物理输出仍未验证。

执行者报告 `uv sync --locked` 成功、定向闭环 32 passed、完整回归 231 passed、编译和离屏 800×600 启停通过。主线程复核了范式加载、Rally wire、Start→Apply、Stop 优先、pipeline/SessionWriter/Reader 准入和对应测试，确认 `paradigm_control` 已进入 P2 单写者链路，合成包仅在测试目录且默认不能真实 arming。主线程未重跑完整测试；实际 `git diff --check` 通过。既有 `:memory:.ses` 未跟踪文件保持原状。

## 必须收口：未发送 Start/Apply 的同步重排

`stimulation_runtime.py::_on_real_not_sent` 对 Apply 在最新结果仍合格时不检查拒绝类别，直接创建下一条 Apply（约 1805–1823 行）；Start 的范式分支也会在同样条件下重新规划 Start（约 1871–1888 行）。`_submit_real_request` 的 `transport.submit(False)` 会**同步**回调 `_on_real_not_sent`（约 733–740 行），而 worker 在 socket 隔离容量用尽、shutdown 或忙碌时会拒绝提交（`rally.py:895–911`）。持续拒绝使回调递归提交同类命令，不能到达必要 Stop 和租约释放。

可达例子：将普通 socket 容量设为 1；Start 成功后该 socket 留在隔离区，后续 Apply 被普通容量拒绝。Apply 的同步 `not_sent` 回调仍把最新 W 视为合格，立即再提交 Apply，重复上述过程。Stop 虽有额外容量，也要在这条重排链退出后才有机会发送。

`rally.py::_run_command` 在 `sendto` 抛 `OSError` 时以 `UNKNOWN` 调用 `on_not_sent`（约 1030–1045 行）；当前 Apply 分支仍可能重排，违反合同的未知结果不自动重试。发送前因估计 SD 到期被拒绝（`stimulation_runtime.py:978–980`）也只返回 `NOT_SENT`，而回调的“最新结果合格”检查没有再次检查 SD 到期，可能反复重排。

需要区分“**新合格期别替代旧未发请求**”与容量/关闭/发送异常/到期等不可重排情况；只在明确的替代事件且最新结果、session、年龄和到期条件都重新通过时规划一次新请求。其他情况解除自动控制，按 Issue 5 实际 Start 发送责任执行最多一次必要 Stop，记录故障及归档结果。避免以错误文本字符串匹配作为长期分流依据。

见[Issue 6 收口任务](../docs/tasks/ISSUE6_PARADIGM_CLOSEOUT.md)。本轮未修改执行源码、未提交推送、未连接真实设备。

## 收口复核（2026-09-23，替代上方暂不通过结论）

**Issue 6 工程范围验收通过。** 上方暂不通过是初次回报的历史结论。

- `rally.py` 向未发送回调传递机器可读原因；普通 worker 容量/退出拒绝、发送异常、取消和发送前资格拒绝可区分。`_submit_real_request` 同步拒绝不再使 Start/Apply 自动重排。
- `stimulation_runtime.py::_on_real_not_sent` 仅在旧 Apply 被新合格期别明确替代、请求未发送、当前 session/generation 和年龄仍有效、SD 未到期且无 Stop 优先时规划最新协议。其余拒绝进入有界失败/停止路径；`UNKNOWN` 不重试。到期路径复用控制租约并最多一次必要 Stop。
- 新增六项针对性回归覆盖普通 socket 容量为 1、Start 初始提交拒绝、Apply/Start `sendto` 异常、发送前 SD 到期和 B→C 取代；断言请求数量、状态、Stop 责任和租约释放。Curry→staging→假 Rally→GUI→SessionWriter/Reader 闭环改为自然 EOF，检查 Stop 事件在 `session_finished` 前、回放零发包。
- 执行者最终定向 **38 passed**、完整 **237 passed**，编译与 `git diff --check` 通过；原 **231 passed** 记录保留。主线程按关键代码和测试复核，未重复运行全量测试；本轮实际 `git diff --check` 通过。

交付为合成环境的软件工程能力。仓库尚无生产范式包或已批准的 A/B/C 参数；Windows、Rally 8801、硬件与物理输出均未验证。Issue 5+6 本地改动尚未提交/推送；既有未跟踪 `:memory:.ses` 保持原状。
