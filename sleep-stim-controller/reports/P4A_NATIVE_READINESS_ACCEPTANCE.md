# P4-A 主线程验收

日期：2026-09-16。依据：[P4 合同](../docs/P4_INTEGRATION_READINESS_CONTRACT.md)、[执行报告](P4A_NATIVE_READINESS_REPORT.md)、[接入手册](../docs/P4_INTEGRATION_RUNBOOK.md)。

## 结论

P4-A 可独立交付的诊断与接入文档通过验收；原生桌面可用性仍待验证，退出 139 未判定为已修复。P4 真机、模型接入和同步阶段没有完成或获得新增执行授权。

主线程阅读报告、runbook、README，并只读核对报告引用的两份系统 .ips 的异常类型与故障线程栈：最小窗口记录为 EXC_CRASH/SIGABRT，包含 QWindowPrivate::init；正式入口为 EXC_BAD_ACCESS/SIGSEGV、空地址偏移 0x8，栈顶依次为 QScreen::virtualSiblings、virtualGeometry、virtualSize、QGraphicsView::sizeHint。与执行报告一致。

Cocoa 平台 screens=0、primaryScreen=None 来自执行者探针记录，主线程未重新运行。零屏幕与崩溃栈支持当前 shell 下失败路径的判断，但不证明零屏幕的成因、普通登录桌面也会失败，或业务程序在正常桌面已无其他问题。报告中的环境因果推断按此限度理解。

## 交付与验证范围

- 接入手册覆盖现有接口、Curry 数据核验、模型输入输出资料、Rally 初始协议/启停/状态缺口及同步事件与误差要求；后续步骤是准备文档，不是已执行实验。
- README 清楚说明当前 shell 的原生限制及标准启动入口。
- 执行者报告无源码或依赖改动，因此没有为文档交付重跑测试，符合本任务合同。88 passed 仍是 P3 历史结果。
- 本轮主线程未启动 Qt、未执行测试或设备通信，也未重试被拒绝的 Terminal 访问。原始报告和 .ips 保持原样。

## 未完成项与下一步

在可显示窗口的正常 macOS 登录桌面，按 runbook 运行正式入口，观察默认未连接/NoModel/模拟端和自动决策关闭，核验 800×600 控件与错误区可访问性，关闭窗口并记录退出码及进程是否结束。若仍崩溃，保留该次日志和对应栈，再据证据安排修复；无新条件不重复当前 shell 的失败尝试。

原生窗口观察、最小尺寸验证、正常关闭与原生截图尚无证据，不计为通过。真机与同步所需资料继续按 P4 合同收集，未自动发布后续批次。
