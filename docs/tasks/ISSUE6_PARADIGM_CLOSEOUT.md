你是 Issue 6 多协议范式收口执行线程。

在 `/Users/xuqinghe/sleep/main-development` 的现有未提交 Issue 5+6 工作区修复 [主线程验收发现](../../reports/ISSUE6_PARADIGM_ACCEPTANCE.md)。保留全部 dirty/untracked、Git index、历史报告和既有 231 passed 记录；不重置、不清理、不进入真实设备测试。权威行为以 [Issue 6 合同](../ISSUE6_PARADIGM_CONTRACT.md)、[Issue 5 合同](../ISSUE5_RALLY_ARMING_CONTRACT.md)为准；只做本缺陷所需局部修改。

## 修复目标与方法

1. 排查 `_submit_real_request` → `_on_real_not_sent` → `_plan_paradigm_request_locked` 的同步路径。未发送 Start/Apply 只有在**因更新的合格期别明确替代旧请求**且 worker 仍接纳、session/generation 与年龄仍有效、未到估计 SD 截止、无 Stop 优先时，才能按最新期别规划一次新请求。不要把 `NOT_SENT` 一律视为可重排；容量不足、worker shutdown、pipeline 租约拒绝、故障和主动停止都应退出自动重排。
2. `sendto` 异常/UNKNOWN 不自动重试 Apply。若本会话有实际发送 Start 的未解责任，走一次独立 Stop（沿用额外 socket 容量和控制租约）；没有 Start 证据则零 Stop。不能显示目标协议已确认。Start 发送异常同样应保持无重试与正确停止责任。
3. 发送前 SD 已到期应进入既定 EXPIRED/UNKNOWN 与一次保护性 Stop 路径，不能不断创建同一 Apply。结果年龄过期、旧 generation、EOF、窗口关闭、录制故障的发送前拒绝也应安全终止，保留事件顺序与 writer 关闭屏障。
4. 可以给传输拒绝增加明确的机器可读原因或简洁状态标记，以区分真正的 superseded 与资源/生命周期故障；不要依赖中文错误信息。保持二值模式、单 worker、动态源端口与 Stop 预留容量不退化，不创建第二个控制队列。

## 必要验证

用可控 worker/fake socket 与随机 localhost 假端覆盖：普通隔离容量为 1 时 Start 成功后 Apply 提交被拒，有限时间内退出、最多一次 Stop、线程和租约释放；Start 首次提交被拒且无实际发送时零 Stop；Apply `sendto` 抛错时不重发、一次 Stop；在 Apply 发送前跨过估计 SD 截止时不重发并进入到期状态；旧 Apply 因 B→C 的新合格期别被取消后只提交最新 C，且不会把一般 `NOT_SENT` 放开重试。补测录制关闭/EOF 路径中必要 Stop 事件先于 `session_finished`，旧会话回放零发送。

保留现有合成 Curry TCP→staging→fake Rally→GUI/SessionWriter 闭环；完成受影响定向测试、最终完整回归、编译和 `git diff --check`。只有逻辑变化时无需重复制作相同 GUI 截图。测试不访问生产 8801、真实 Curry/Rally、COM、电刺激设备或受试者。

把修复、命令和实际结果**追加**到 `reports/ISSUE6_PARADIGM_IMPLEMENTATION_REPORT.md`，保留原 231 passed 记录；不改主线程验收文件、共享队列或原始证据，不提交、推送、合并、评论或再分发任务。完成后回报主线程复核。
