# Issue 5 Rally arming 修复执行报告

日期：2026-09-22

工作目录：`/Users/xuqinghe/sleep/main-development`

依据：`docs/ISSUE5_RALLY_ARMING_CONTRACT.md`、`reports/remote_issues/2026-09-22/issue-5.md`。
基线：`main@6b0f234bf35be9d43f1d8ab5a66c72b279ac996f`。

## 结论

Issue 5 已完成实现和本地验证。真实控制启用不再创建或发送基线 `Stop Stim`，而是记录操作者确认、清除旧结果资格并进入 `ARMED/IDLE`。只有 Start 到达协调后的发送边界并成功 `sendto` 后才登记停止责任；未发送、取消或 `sendto` 失败的 Start 不产生补偿 Stop。

本轮没有连接生产 `127.0.0.1:8801`、真实 Rally/Curry、COM 或电刺激设备；API/loopback 结果不构成物理输出、安全性、模型效果或受试者实验结论。

## 实现范围

- `stimulation_runtime.py`：移除 `_real_baseline_ready` 及启用基线请求，新增 `ARMED/IDLE`、操作者确认、Start 实际发送责任和发送尝试状态；启用时清空实时结果资格，不伪造 `confirmed_state/confirmed_utc`。
- `rally.py`：为控制 worker 增加 `send_started` 边界和 `on_send_started` 回调；取消/退出不能跨过已进入 `sendto` 的边界把 Start 重新分类为未发送。
- 关闭、故障、EOF、断开和退出共用责任判定：已确认 `RUNNING` 或存在已发送 Start 责任才发送必要 Stop；Start 拒绝/超时/未知最多一次补偿 Stop；Stop 失败进入 `FAULT/UNKNOWN` 且不自动重试；旧 generation 回调不清掉新在途请求。
- GUI 显示操作者确认、API 最近确认、Start 收尾责任和 `ARMED/IDLE`；更新 `README.md`、P4 接入手册、Windows 说明及当前/替代合同链接。历史 P4B 报告未改写。
- 增加/迁移 Rally 状态机、发送竞态、SessionWriter、Curry EOF 与 GUI 集成测试，并生成 [Issue 5 800×600 UI 合成截图](ISSUE5_RALLY_ARMING_800X600.png)。

## 实际验证

```text
UV_CACHE_DIR=/private/tmp/sleep-uv-cache uv sync --locked
→ Resolved 17 packages; Checked 16 packages

UV_CACHE_DIR=/private/tmp/sleep-uv-cache uv run --locked pytest -q tests/test_p4b_rally_control.py
→ 20 passed

UV_CACHE_DIR=/private/tmp/sleep-uv-cache uv run --locked pytest -q tests/test_controller_synthetic.py -k 'p4b_'
→ 2 passed, 35 deselected

UV_CACHE_DIR=/private/tmp/sleep-uv-cache uv run --locked pytest -q
→ 206 passed in 19.57s

UV_CACHE_DIR=/private/tmp/sleep-uv-cache uv run --locked python -m compileall -q src tests reports/ISSUE5_RALLY_ARMING_CAPTURE.py
→ 通过

git diff --check
→ 通过，无输出
```

离屏入口验证：`QT_QPA_PLATFORM=offscreen ... python -c ... build_application(); QTimer.singleShot(...); app.exec(); runtime.shutdown()`，退出码 0，线程正常 join。截图脚本运行成功并保存 800×600 PNG；画面显示 `ARMED/IDLE`、操作者确认、`Start 收尾责任=无` 和“启用零 UDP”，仅作为离屏 UI 合成证据。

专项覆盖包括：启用零 UDP、IDLE 非目标 no-op、目标 Start、重复目标/非目标 no-op、Stop 后再次 Start；未发送 Start 的发送前拒绝和关闭取消不补 Stop；已发送 Start 的拒绝/超时单次补偿；Stop 失败不重试；sendto/取消边界、年龄保护、generation、租约、SessionWriter 顺序、Curry TCP EOF 和 GUI 状态。

## 限制与工作区边界

- 未执行真实 Rally API、物理刺激输出、硬件独立停止、Windows 原生窗口、COM、同步校准、模型准确性或受试者实验。
- 未提交、推送、合并或修改 Git index；没有修改共享 `decision_records/ACTIVE_QUEUE.md`。该文件、`decision_records/README.md`、Issue 5/Issue 6 任务材料及 `reports/remote_issues/` 的既有 dirty/untracked 状态均保留。
- 本轮离屏/loopback 生成的 51 字节临时 `:memory:.ses` 已确认是本轮运行产物并移除；Issue 5 截图和报告作为新产物保留。

## Issue 5 closeout 修复（2026-09-23）

主线程验收提出的两项收口已修复：

- Rally 控制 worker 在 condition 锁内只记录发送边界取消判定；socket 关闭和 `on_not_sent` 回调均在锁外执行。复核确认控制 worker 的其他 runtime 回调同样不在 condition 锁内调用，runtime→worker 的取消/关闭路径不再与 worker→runtime 回调形成反向持锁。
- block 高水位现跨越同一实时会话的启用/关闭周期持续推进，仅新会话重置。每次启用记录单调时间；启用前已完成、之后迟到的结果只推进高水位并被忽略，重复旧 block 为 no-op。结果在启用后完成即可参与控制，即便处理窗口在启用前已开始。
- 新增并发取消/重复关闭/故障/shutdown 租约测试；新增重启用旧块与迟到结果测试、disabled 期间多块推进测试，以及新会话 block 编号重新起算测试。

新增验证实际结果：

```text
UV_CACHE_DIR=/private/tmp/sleep-uv-cache uv run --locked pytest -q tests/test_p4b_rally_control.py -k 'cancelled_start_close_fault or rearming_ignores or disarmed_blocks or new_session_resets'
→ 4 passed, 20 deselected in 3.41s

UV_CACHE_DIR=/private/tmp/sleep-uv-cache uv run --locked pytest -q
→ 210 passed in 22.49s

UV_CACHE_DIR=/private/tmp/sleep-uv-cache uv run --locked python -m compileall -q src tests
→ 通过

git diff --check
→ 通过，无输出
```

loopback 测试仅使用随机本机端口和 fake Rally；未访问生产 `127.0.0.1:8801`、真实 Curry/Rally、COM 或设备。修复只涉及逻辑和测试，保留前述离屏 UI 截图及原始 `206 passed` 验收证据。
