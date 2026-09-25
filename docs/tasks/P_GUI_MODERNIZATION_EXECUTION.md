你是现代化控制台 UI 执行线程。

## 目标与工作分支

按[设计调研与基线](../P_GUI_MODERNIZATION_DESIGN.md)、[实施计划](../../decision_records/plans/P_GUI_MODERNIZATION_PLAN.md)在正式仓库 `/Users/xuqinghe/sleep/main-development` 完成一轮 PySide6 QWidget 界面重组与视觉优化。发布时 `main@c636c244e89e785af75134dc711d48b5d051976b`；开始先核对实际 `main`、远端、dirty 和 Git index。**从当前最新 main 创建 `feature/modern-control-ui` 分支并仅在该分支开发**；若同名分支已有内容，不覆盖、重置或清理，先报告现场并采取不破坏现有工作的方式继续。保留既有未提交内容和暂存区。不在本任务合并 main、创建 PR 或推送远端；完成后交主线程验收，再决定发布和合并。

## 实施范围

先读 `src/sleep_stim_controller/ui.py`、`app.py`／controller 接线、`tests/test_ui.py`，以及 [P-GUI 合同](../P_GUI_CONTRACT.md)、[Issue 5/6 控制指南](../ISSUE6_PARADIGM_GUI_GUIDE.md)与[Issue 8 防误触任务](ISSUE8_GUI_WHEEL_GUARD_EXECUTION.md)。先做现有控件／状态／信号映射和 800×600、1000×720 关键页面线框，再重组。设计方向和页面职责以设计基线第 3–4 节为准；允许在不改业务语义的前提下调整局部排版和组件拆分。

使用现有 PySide6 Widgets 与作用域样式，优先复用控件、objectName、signal／slot 和数据更新入口；不要为了外观重写数据管线或引入完整主题／Fluent 框架。固定运行栏显示实时／回放、连接、30 秒进度、模型结果、自动控制／Rally API 确认、记录状态；错误与独立停止提示不随页面消失。四个浅层页面分别用于运行总览、刺激控制、记录回放、连接／模型／诊断。真实自动控制的启用必须保持显式确认和现有 arming 条件；启用后的关闭入口始终可达并只调用现有停用路径，不设计软件“急停”。所有期别、控制与记录显示来自真实现有状态；测试替身必须标识。

保留默认 NoModel、真实控制关闭、目标期默认空、会话记录／CSV 默认关闭、Curry 短包累积 30 秒、Rally 发送责任和未知状态、范式 Apply、旧 generation 隔离、session 单写者、只读回放、EEG 波形仅在 Curry 8 查看。保持 Issue 8 滚轮防误触与键盘／点击行为。不得更改刺激协议参数、模型预处理、数据 schema、网络端点或物理输出结论；不得访问生产 `127.0.0.1:8801`、真实设备、COM 或受试者。

## 验证与交付

更新有语义的 UI 测试与必要的合成端到端测试，检查导航／样式本身零 Rally 请求，显式模拟和受控假端流程可用，回放不发包；覆盖默认、握手／进度、分期成功／失败、`ARMED/IDLE`、`RUNNING`、`FAULT/UNKNOWN`、记录／CSV 故障和完整／不完整回放。真正的 Qt 事件覆盖焦点、键盘、滚轮、控件锁定和错误可读；测试 `800×600`、默认尺寸和长中文／路径／错误。按需核对色彩对比与可访问名称。运行 `uv sync --locked`、相关测试、最终完整回归、编译与 `git diff --check`；Windows 桌面可用时另做原生鼠标、125%／200% 缩放、高对比度与截图核验，否则说明未验证。

报告写 `reports/P_GUI_MODERNIZATION_REPORT.md`：列出最终页面、组件选择及许可／依赖、关键状态样图、行为与回归证据、截图路径、Windows 未验证项。至少交付默认、800×600、真实模式故障、回放与长文本的离屏截图，标明合成场景；不把离屏界面或假 Rally 回复称为真机验收。不要修改 `decision_records/ACTIVE_QUEUE.md`、主线程设计／验收结论或历史报告；不要提交、推送、合并或另行分发任务。完成后回报 feature 分支名、HEAD、dirty 状态和事实证据供主线程验收。
