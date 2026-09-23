你是 Issue 5 Rally 启用与收尾修复执行线程。

## 目标与依据

在 `/Users/xuqinghe/sleep/main-development` 修复真实自动控制勾选即 Stop 被拒绝，以及未启动会话仍发送收尾 Stop 的问题。当前发布基线 `main@6b0f234bf35be9d43f1d8ab5a66c72b279ac996f`；先核对 HEAD/dirty，保留主线程本次任务文档等现有改动。

必读：[本轮权威合同](../ISSUE5_RALLY_ARMING_CONTRACT.md)、[#5 原文快照](../../reports/remote_issues/2026-09-22/issue-5.md)、[P4B 保留合同](../P4B_RALLY_CONTROL_CONTRACT.md)。冲突处本轮合同优先。无需重读全部历史。不实施 #6。

## 具体修改方法

1. `stimulation_runtime.py::_set_real_automatic_enabled` 删除基线 Stop 的创建/提交，设 armed/idle 并记录操作者确认，清掉启用前结果资格，等待新的合格结果。不要用伪造 Stop API 成功使旧分支通过；梳理 `_real_baseline_ready` 的剩余依赖，改为准确的人工准备语义。
2. 明确增加或复用“Start 已实际发送且未被成功 Stop 消解”的状态。与 `rally.py` worker 的 sendto、before_send、sent/not_sent/outcome 路径配合，以真实传输证据驱动，取消与发送竞态不能漏 Stop 或多发 Stop。
3. 统一 `_disable_real_control`、`_handle_real_fault`、`_on_real_not_sent`、`_finish_real_failure` 的必要 Stop 判定。尤其现有 `_on_real_not_sent` 在取消 Start 后仍创建 Stop，`_finish_real_failure` 对所有 Start 失败都补偿：必须区分“从未发送”与“已发送未确认”。不要只修改启用函数。
4. 梳理 `_plan_real_request_locked`、`_finish_real_success`、`_before_real_send`、tick、begin/finish_session、shutdown，保持正常目标→非目标→目标转换与年龄保护。恢复确认和 generation 切换不能洗掉未解决输出状态。
5. `ui.py` 更新确认文案、IDLE/操作确认/API 确认显示；保持默认 NoModel、真实控制默认关闭及现有模拟模式。记录相应证据，旧 session 回放兼容。
6. 更新 README、当前运行合同/手册相关说明并链接本轮替代合同；历史任务与报告只保留历史，不篡改旧验证。不得修改共享队列。

## 必要验证

使用可控时钟/fake transport 及随机本机 loopback，禁止访问生产 8801、COM、设备或受试者。测试需实际断言命令数量与顺序：

- 启用零 UDP；IDLE 非目标零请求；目标 Start 一次；重复目标 no-op；非目标 Stop 一次；重复非目标 no-op；重新目标 Start。
- 从未发送 Start 的主动关闭、自然 EOF、网络异常、录制故障、窗口退出零 Stop；已取消/发送前校验失败的 Start 零补偿。
- 实际发送 Start 后拒绝/超时/未知，各最多一次补偿；Stop 拒绝/未知保持 FAULT，不循环重试；两个现场错误文本不得当停止成功。
- 取消与 sendto 竞态、重复收尾回调、Stop 在途故障、故障后重新勾选、旧 session/generation 迟到结果和回调。
- 当前年龄过期时必要 Stop 保留；Stop 等待期间过期缓存不会重新 Start；预留 Stop socket 和租约行为不退化。
- Curry TCP→staging→fake Rally→GUI/SessionWriter 集成：有 Start 和无 Start 的 EOF 均覆盖，全部可接纳事件先于 session_finished；回放零网络命令。

完成 `uv sync --locked`、必要编译、`git diff --check`、最终完整回归（现有基线报告 203 passed，按实际新结果报告）；按已有方式离屏启动关闭及 800×600 截图。不要把离屏或合成测试称为 Windows/真机通过。

## 交付

报告写 `reports/ISSUE5_RALLY_ARMING_FIX_REPORT.md`，列实际修改、命令及结果、证据表、未验证项、dirty 范围。只实现本任务；不提交/推送/合并/评论、不改 Git index、不清理既有文件、不另发子任务。回报后由主线程验收。
