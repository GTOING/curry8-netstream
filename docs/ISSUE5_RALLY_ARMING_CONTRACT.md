# Issue 5：真实控制启用与停止证据合同

日期：2026-09-22。状态：实现完成；loopback、离屏 GUI 与完整回归已验证，真实 Rally/硬件仍未验收。

依据：[远端 #5 快照](../reports/remote_issues/2026-09-22/issue-5.md)。适用基线 main@6b0f234bf35be9d43f1d8ab5a66c72b279ac996f。

## 替代范围

本合同替代 P4B 合同及旧任务中“启用必发基线 Stop”“未确认 STOPPED 即应 Stop”的规则。保留其资格过滤、年龄保护、单请求、Stop 优先、传输隔离、收尾租约、单写者与回放隔离。历史报告不改写。当前工程只有二值启停，不实施 #6 协议切换。

## 启用与证据

操作者显式确认本次 Rally 已加载并检查协议、当前未进行刺激。有效实时会话、握手、显式 ONNX 与有效配置仍是前置条件。启用只进入 ARMED/IDLE，零 UDP；操作确认不是 API 或物理确认，不能伪造 confirmed_state/confirmed_utc。仅启用之后的新合格结果驱动控制，旧缓存不得启动。

IDLE/STOPPED + 非目标期 no-op；IDLE/STOPPED + 目标期 Start；RUNNING + 目标期 no-op；RUNNING + 非目标期 Stop；Stop 成功后非目标期不重复 Stop，再进入目标期发送 Start。保持用户目标多选，不冻结实验阶段映射。

区分 requested、实际发送、API 确认、物理未验证。用实际发送证据表达本应用是否仍可能留有活动输出：Start 实际发送后即产生待停止责任（包括拒绝/超时/未知），明确 Stop 成功才消除；未发送或取消的 Start 不产生该责任。sendto 与取消/回调交错必须在传输边界协调，不能仅凭运行状态字符串推断。

关闭、故障、EOF、断开、退出共用幂等停止判定：有上述责任或已确认 RUNNING 才请求一次必要 Stop；Start 在途先取消未发送请求，已发送请求等待有界结局后收尾。无 Start 发送证据的 IDLE 会话及已确认 STOPPED 不发 Stop。一次未解决的 Start 对应最多一次保护性/补偿 Stop；已有 Stop 在途不叠加，Stop 失败不自动重试。正常成功 Stop 后下一次新的 Start 可产生新的停止责任。

FAULT/UNKNOWN 解除自动控制并阻止自动 Start；反复勾选不能清空尚未解决责任或复用旧确认。恢复必须显式人工核对并重新确认当前无刺激，且没有在途请求；记录恢复原因及证据来源，不伪装为 API Stop 成功。旧 generation 的结果或回调不得改变新会话；旧会话仍须完成自身有界收尾后才能释放资源。

两个现场拒绝文本（尚未开始刺激、Status_ControlOwned）均不能当作已证明停止；不新增无厂商证据的 already_stopped 分类。API 成功仍不证明物理输出。

## 记录与兼容

沿用 rally_control 与 SessionWriter，允许向后兼容的可选证据字段；旧会话可读，不重写证据。启用/人工确认/无命令决策也可追溯。必要 Stop 仍使用独立控制租约及预留 socket；自然 EOF 在 handoff_network_end 前登记收尾；最后事件与关闭准入保持原子屏障；记录失败仍尽力 Stop 并报告记录缺失。
