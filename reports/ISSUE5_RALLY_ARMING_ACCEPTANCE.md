# Issue 5 主线程验收

日期：2026-09-22。结论：暂不通过，需完成两项收口。只读复核当前代码与执行报告；执行者报告完整 206 passed、专项 20 passed、集成 2 passed，主线程未重复测试。主线程实际运行 git diff --check 通过。

已确认启用不再发送基线 Stop，新增真实发送责任及未发送取消分支，保留保护性停止租约。以下发现不能由现有通过数量排除。

## 1. 高优先：取消回调在 worker 锁内，形成反向锁顺序

`rally.py:956-959` 持有 `_condition` 调用 `_on_not_sent`，后者在 `stimulation_runtime.py:1131` 获取 runtime `_lock`。反向路径 `_disable_real_control`（484–501）、`_handle_real_fault` 和 `_plan_real_request_locked` 持有 runtime 锁调用 `cancel_unsent()`，需要 worker condition。

在请求已被取消或 shutdown 的窗口，worker 可持有 condition 等 runtime，而另一个关闭/故障线程持有 runtime 等 condition，构成 ABBA 死锁。RLock 只允许同线程重入，不能解除跨线程循环等待。结果可能是 GUI/网络收尾阻塞及控制租约无法释放。

要求：worker 锁内只作取消判定及内部状态更新；释放锁后再 close/callback。系统性检查本轮控制 worker 的外部回调与 runtime→worker 调用锁顺序。用可控屏障覆盖重复取消/关闭交错，证明线程和租约有界退出，不用长 sleep 或反复跑碰运气。

## 2. 高优先：重新启用重置块号，旧结果可重新触发 Start

`stimulation_runtime.py:458-460` 每次启用把 seen/last_valid 清零并删除缓存；`_process_real_result:778` 的去重只依据这个 seen 值。没有启用时间边界检查。

例如同会话 block 10 目标期已执行 Start，用户关闭且 Stop 成功，再次启用；仍新鲜的同一 block 10 被重复交付时会再次通过块号资格并请求 Start。启用前已完成但尚待交付的结果也没有时间屏障。与合同“仅启用后的新结果，旧缓存不得启动”不一致。

要求：保留会话级已观察块号高水位，启用不能重置；建立明确的 armed 结果边界（如结果完成单调时间及已观察块号），实时未启用阶段也须能识别旧结果。新 session 才重置会话高水位；不要把启用前的结果伪装为新结果，也不要因合法 disabled 阶段的块号推进制造误报缺口。补测重新启用重复块、启用前完成但迟到交付、新合格结果与新 session。

## 后续

执行 [收口任务](../docs/tasks/ISSUE5_RALLY_ARMING_CLOSEOUT.md)。上述为代码路径推导，未运行死锁复现或新增测试。未提交/推送，未进行真机验证。Issue 6 仍待本轮验收。

## 收口复核（2026-09-23，替代上方暂不通过结论）

**工程范围验收通过。** 上方两项发现已修复；前次暂不通过为历史结论。

- `rally.py` 控制 worker 的发送前取消分支现在只在 `_condition` 内决定是否取消，释放锁后关闭 socket 并调用 `on_not_sent`。复核 worker 其余控制回调路径，没有发现持有该 condition 调用 runtime 回调。新增确定性并发测试 `test_cancelled_start_close_fault_and_shutdown_do_not_deadlock_or_send` 用屏障/事件交错取消、关闭、故障与 shutdown，断言线程退出、零 UDP、租约归零。
- `stimulation_runtime.py` 的 `_real_seen_block_id` 在同一会话重新启用时保留，停用期间也推进；新会话才清零。启用记录单调时间，旧块重复直接忽略，启用前完成而迟到的块只推进高水位；启用后完成的合格结果可触发控制。测试覆盖重新启用、停用期间块进展与新会话编号重起。
- 执行者追加报告：新增专项 4 passed，最终完整 210 passed，编译与 `git diff --check` 通过；保留原始 206 passed 记录。主线程按代码与关键测试复核，未重复运行全量测试；本轮实际 `git diff --check` 通过。

工程通过不表示 Windows 原生、真实 Rally API、物理输出或受试者实验通过。当前改动仍未提交或推送；Issue 6 多协议扩展尚未实施。
