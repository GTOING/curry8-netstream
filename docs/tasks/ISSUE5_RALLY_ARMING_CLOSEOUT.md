你是 Issue 5 Rally Arming 收口执行线程。

在 `/Users/xuqinghe/sleep/main-development` 现有未提交实现上完成修复，保留所有已有改动和主线程文档，不重置、不改 index、不提交推送。依据 [主线程验收](../../reports/ISSUE5_RALLY_ARMING_ACCEPTANCE.md) 与 [Issue 5 合同](../ISSUE5_RALLY_ARMING_CONTRACT.md)。不进入 Issue 6。

## 必须修复

1. 去除 worker condition 内调用 runtime 外部回调的路径，特别是 `rally.py::_run_command` 新增发送边界的取消分支。锁内记录决定，锁外执行回调；保证取消与实际发送责任语义不退化。审查其余控制 worker 回调锁顺序，避免 runtime→worker 与 worker→runtime 交叉持锁。新增使用 Event/Barrier 的确定性测试，覆盖取消已标记后重复关闭/故障并发，断言线程退出、租约归零、零误发 Start/Stop。
2. 为 armed 建立真正的“新结果”边界。保留 session 内已观察 block 高水位，不能在每次启用时清零；记录启用单调时间，排除启用前已完成而迟交付的结果。按结果完成时间界定新结果，不要求整个 30 秒 EEG 窗口必须从启用后才开始。disabled 期间合法块号前进不能让第一次新合格结果误触发缺口故障。重复旧块应 no-op，不用故障 Stop 处理它。保持新 session/generation 隔离、年龄检查及最新期望语义。

## 验证与回报

补测同会话 Start→关闭→Stop 成功→重新启用→重复旧目标块（零 Start）；启用前完成而迟到的结果（零 Start）；启用后新合格结果（一次 Start）；disabled 期间多个块后重新启用；新会话块号重新起算；原有取消/发送竞态与收尾集成仍通过。

完成必要定向验证、最终完整回归、编译与 git diff --check；只用 fake 和随机 loopback，不接生产 8801、真实 Curry/Rally、COM 或设备。追加 `reports/ISSUE5_RALLY_ARMING_FIX_REPORT.md` 保留原 206 passed 证据，记录新增测试和实际结果。仅逻辑修改无需重做同一静态 GUI 截图；若 UI 有变化再更新。不得修改共享队列或主线程验收结论，由主线程复核后更新。
