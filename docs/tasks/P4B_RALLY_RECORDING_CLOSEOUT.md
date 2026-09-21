你是 P4-B 第二轮收口执行线程。继续在 `/Users/xuqinghe/sleep/main-development` 现有未提交实现修复剩余记录收尾竞态，不重置其他内容。

依据 reports/P4B_RALLY_START_STOP_ACCEPTANCE.md 末节。年龄与容量修复保留，重点完成以下有限范围：

1. 自然EOF/网络异常时，在 pipeline.handoff_network_end 放行结束之前完成真实控制收尾登记。不要让保护性Stop依赖稍后Qt回调才能建立租约；可由网络finally调用线程安全、幂等的runtime收尾方法，或在会话开始登记显式控制生命周期门槛并在控制收尾结束释放。避免重复Stop、锁内join及循环等待。
2. ProcessingPipeline的“允许新控制租约→关闭写入”应是同一condition锁内的原子状态转换。不能等writer.finish之后才设置_closed；越过最终等待/排空点后必须拒绝新租约，但必要Stop仍可在写入已故障时尽力执行，并明确记录缺失。
3. 确认finally等待控制工作时到达的session_events全部在session_finished之前由原单写者落盘；不能只等待计数归零然后直接finish，遗漏最后sent/outcome。已接纳事件、外部工作、事件预留及关闭门槛要一致。磁盘故障不能无限等待或阻止Stop。
4. 新增真正使用ProcessingPipeline/SessionWriter的自然TCP EOF测试：先运行至RUNNING且无请求在途，主动由假Curry关闭TCP，控制Qt事件处理时序，证明Stop及outcome先于session_finished且资源释放。另用屏障固定writer关闭临界窗口，验证迟到租约不会被错误接纳，正常已接纳控制事件不会遗漏。不要再用点击断开或仅_PipelineFixture替代该证据。
5. 保留已有主动断开、记录失败、在途Start补偿、关闭、年龄与socket边界回归。完成最终完整测试与diff --check，追加报告保留201 passed记录，说明新增可复现证据和真实平台限制。

不提交、不推送、不修改共享队列、不连接生产8801/COM或设备，其他边界沿用原P4-B任务。完成后回报实际测试与路径供主线程验收。
