# P4-B 主线程复核

日期：2026-09-21。结论：暂不通过整轮工程验收，需收口。未运行真实设备或测试；193 passed 是执行者报告结果，本次实际 git diff --check 通过。

已核对报告、runtime/worker 的关键实现、pipeline 收尾接口及新增九项测试。真实模式、命令相关成功回复、事件类型等主路径已实现，但以下阻塞项未收口：

## P1：无新结果时没有年龄保护，旧结果可在等待后启动

stimulation_runtime.py:625 仅在 _process_real_result 收到新结果时计算年龄，没有独立周期检查。Curry TCP 保持打开但不再发数据时，没有新结果触发该分支，RUNNING 可以无限保持。_real_latest_valid_result_locked（约1156行）直接返回缓存，Stop 回复成功后也可能依据已过期缓存发 Start。原任务明确要求持续检查年龄及发送前资格。

修复：增加可控时钟驱动的周期检查，当前 armed 会话年龄超过显式阈值时解除并一次停止；每次新/后续 Start 真正发送前重新检查当前会话、armed、停止优先和年龄，不能仅相信队列入口。使用假时钟覆盖不再来结果及在途 Stop 后旧缓存过期。

## P1：收尾和记录故障会阻止必要 Stop，事件租约释放过早

_submit_real_request（约526行）在 pipeline.acquire_external_work 返回 False 时直接不发送。staging.py:292 在 _finish_requested 或 _external_error 后拒绝租约。controller 网络线程 handoff_network_end 会先设置 finish，再由 Qt _on_network_finished 通知 runtime 停止，因此自然 EOF/错误可能让保护性 Stop 被记录收尾门槛阻止；记录故障同样如此。

_on_real_outcome（约972行）先 release_external_work，再做后续 Stop 和事件入队，writer 可在两者之间关闭。必须把一次控制生命周期的收尾租约保持至最终 outcome 入队及补偿停止完成；不能靠 Qt 回调先后或稍后再补写 JSONL。记录不可用也必须继续尝试必要 Stop，同时报告记录失败。需协调明确的控制收尾交接，保留网络摘要原有交接，避免锁内等待/相互等待。

## P1：隔离 socket 上限可让已成功启动后无法停止

rally.py:828 对所有请求统一拒绝 len(_quarantined)>=128；每个已发送请求的 socket 都保留到 worker shutdown。默认基线为第1个 Stop，交替执行至第128个请求可恰为 Start，成功后第129个保护性 Stop 因容量被拒绝。不能仅以 UNKNOWN 提示代替本可发送的停止。

修复：预留停止/启动不确定补偿所需资源，在不足以完成启动及后续停止时禁止新 Start，并安全退出本次自动控制；不关闭旧 socket 立即复用端口来规避迟到回复。使用很小容量的测试验证边界，容量耗尽不得出现 Start 成功却没有 Stop 发送额度。

## 验证缺口

新增9项覆盖基本 runtime 与 worker，但没有原任务要求的真实合成 Curry TCP→分期→假 Rally→GUI/单写者整体接线，也没有上述周期年龄、EOF关闭、迟到通用成功回复冒充 Stop、容量耗尽和写入失败的完整覆盖。补有确定性屏障/假时钟的测试；测试数量本身不是门槛。最终完整回归通过后再回报，Windows/真实 Rally 仍独立待验收。

## 第二轮收口复核（2026-09-21）

执行者报告最终201 passed，保留193 passed历史。主线程核对新增 tick、发送前复核、Stop专用容量、租约转移及整线测试，前述年龄保护和容量阻塞项已针对性修改。但整轮仍暂不通过，记录收尾门槛存在以下确定调用顺序缺口：

controller._run_session finally 仍先调用 pipeline.handoff_network_end，再 emit _network_finished_signal；必要停止直到 Qt _on_network_finished 才调用 on_session_stopping。自然EOF且无在途请求时，pipeline可以在Qt回调前完成writer.finish。新 acquire_control_work 只检查 _closed，且 _closed 在writer.finish之后才置True，因此还有“writer已越过等待点，正在关闭时仍接纳租约”的窗口。结果为必要Stop虽然现在可以尽力发送，但可写记录仍可能遗漏停止sent/outcome；已接纳租约不能保证事件先于session_finished。

另一个相关顺序：pipeline finally在等待external work结束后直接writer.finish，不重新排空等待期间到达的session_events；应一并核对最终控制事件的实际写入，而不是仅检查租约计数。

新增整线测试使用 window.disconnect_button.click()，主动断开先通知runtime，不能复现自然EOF的反向顺序。test_eof_close_and_record_failure_keep_required_stop_alive_until_outcome使用_PipelineFixture，无真实writer关闭竞争，也不能证明自然EOF归档顺序。

下一步只针对该剩余生命周期问题收口：控制收尾必须在网络结束放行writer之前登记，writer最终关闭与拒绝新租约必须在同一锁内完成状态转换，已接纳事件在最终关闭前排空；必要停止继续独立于写入失败。增加真实ProcessingPipeline/SessionWriter、自然TCP EOF、可控Qt延迟和writer关闭屏障的确定性测试。未运行全套或设备验证。

## 最终收口验收通过（2026-09-21）

主线程已复核最后一轮实际代码及新增测试，P4-B 本轮工程范围验收通过。

- 网络 finally 在 handoff_network_end 之前登记 runtime 停止收尾，消除依赖后续 Qt 回调才申请租约的自然EOF窗口。
- 正常 pipeline 循环优先排空已接纳事件，在同一 condition 锁内检查外部工作/预留/网络交接并关闭准入；writer.finish 前已拒绝迟到控制租约。异常路径标记证据不可写，拒绝无人处理的事件，而必要Stop仍可尽力执行。
- 新增自然TCP EOF整线测试从RUNNING由假Curry关socket，核对Stop/Start/Stop、全部控制事件早于session_finished，EOF如实为failed；真实writer屏障测试覆盖关闭临界区的迟到租约和事件拒绝。

执行者最终完整回归203 passed、P4B/Curry专项54 passed、编译检查通过。主线程未重复全套或专项测试，本轮实际git diff --check通过。193/201 passed和此前失败复核保留为历史。

验收只覆盖工程及合成验证；未取得Windows原生、真实Rally回复、物理输出、模型效果或受试者实验证据。工作区尚未提交/推送，未经新授权不发布或操作设备。
