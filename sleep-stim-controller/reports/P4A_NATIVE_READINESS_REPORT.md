# P4-A 原生桌面排障与接入准备报告

日期：2026-09-16。任务：P4-A。结论：本次执行 shell 中原生窗口不可用；获得了能定位当前退出路径的 macOS 崩溃栈，但未证明普通登录桌面启动正常，也未修复或宣称修复退出 139。已完成接入 runbook 与运行限制说明。

## 实际修改

- 新增 docs/P4_INTEGRATION_RUNBOOK.md：当前接口映射、原生桌面最短复核、未来 Curry 核验、可选模型、Rally/刺激资料缺口、同步证据要求和不可推导结论。
- 更新 README.md：记录当前执行 shell 的原生桌面限制并链接 runbook/报告。
- 新增本报告。
- 未修改 src、tests、pyproject.toml、uv.lock、P1/P2/P3 控制性合同、历史报告、ACTIVE_QUEUE 或 reference。未初始化 Git、提交、推送、建分支/工作树；没有连接 Curry、Rally、设备或受试者。

## 环境与历史

- macOS 27.0（Build 26A428），arm64；uv 0.12.1；Python 3.11.15（项目 .venv）；PySide6 6.11.2、shiboken6 6.11.2、Qt 6.11.2。
- 检查确认 QT_QPA_PLATFORM 未设置；Qt 报告 platform=cocoa，独立 QApplication 诊断得到 screens=0、primaryScreen=None。PasteBoard、Input Source 与 hiservices XPC 同时有服务不可用错误。
- 旧 P2 报告曾记录正式入口退出 139，伴随同类系统 GUI 服务错误；P3 保留 139 未定事实。本轮没有因重复结果无变化而反复重试；新增一次最小 Qt 与一次正式入口对照，并取得新崩溃栈。
- Computer Use 的 app 列表可见 Terminal，但 get_app_state 对 com.apple.Terminal 返回安全拒绝；没有读取或操作用户 Terminal，也没有关闭用户进程。shell 的 pgrep 因 sysmon/sysmond 不可用无法列举进程；本轮启动的 Qt 进程均已以信号退出，独立 screen 探针正常返回。

## 原生诊断尝试

### 最小 Qt 窗口

运行未指定 offscreen platform 的临时 800×600 QMainWindow 基线程序，计时器设为 20 秒自动关闭：

    UV_CACHE_DIR=/private/tmp/sleep-uv-cache uv run --locked python -c 'from PySide6.QtCore import QTimer, qVersion; from PySide6.QtWidgets import QApplication, QLabel, QMainWindow; app=QApplication([]); window=QMainWindow(); window.setWindowTitle("P4-A native Qt baseline"); window.resize(800, 600); window.setCentralWidget(QLabel("Native Qt baseline — no EEG or device connection")); window.show(); print("native_qt=show; qt="+qVersion()+"; size=800x600", flush=True); app.aboutToQuit.connect(lambda: print("native_qt=aboutToQuit", flush=True)); QTimer.singleShot(20000, window.close); raise SystemExit(app.exec())'

实际退出码 134（SIGABRT / Abort trap: 6），未进入事件循环、自动关闭计时器没有执行；Qt 明确输出 Cannot create window: no screens available。崩溃线程在 Qt QWindowPrivate::init → QWidget::create/setVisible → QMainWindow.show 路径。系统生成的本机记录：[最小 Qt 崩溃栈](</Users/xuqinghe/Library/Logs/DiagnosticReports/python3.11-2026-09-16-184810.ips>)。

### Qt 平台探针

    UV_CACHE_DIR=/private/tmp/sleep-uv-cache uv run --locked python -c 'from PySide6.QtGui import QGuiApplication; from PySide6.QtWidgets import QApplication; app=QApplication([]); print("qt_platform="+app.platformName()); screens=QGuiApplication.screens(); print("screens="+str(len(screens))); print("primary_screen="+str(QGuiApplication.primaryScreen()))'

退出码 0；输出为 qt_platform=cocoa、screens=0、primary_screen=None。该探针仅建立 QApplication，没有创建窗口或连接业务设备。

### 正式入口

    UV_CACHE_DIR=/private/tmp/sleep-uv-cache uv run --locked sleep-stim-controller

实际退出码 **139**（SIGSEGV）。未出现可观察窗口；进程在进入 QApplication 事件循环前的 MainWindow 构建阶段崩溃，没有正常关闭阶段可检查。macOS .ips 报告为 EXC_BAD_ACCESS / KERN_INVALID_ADDRESS at 0x8。faultingThread 栈顶：

    QScreen::virtualSiblings()
    QScreen::virtualGeometry()
    QScreen::virtualSize()
    QGraphicsView::sizeHint()
    QWidgetItemV2::maximumSize()
    QBoxLayout::sizeHint()
    QLayout::totalSizeHint()
    QScrollArea::setWidget()
    PySide6 wrapper setWidget()

对应系统记录：[正式入口崩溃栈](</Users/xuqinghe/Library/Logs/DiagnosticReports/python3.11-2026-09-16-184932.ips>)。这与独立探针的零屏幕状态吻合：当前即时失败路径是在空 QScreen 状态下，UI 的 QGraphicsView sizeHint 查询虚拟屏幕并发生空地址访问。它强烈支持“当前 shell 没有可用 Qt screen”是本次退出路径的必要环境条件；不能单凭该栈证明普通 macOS 桌面也会崩溃，也不能确定此 shell 为何没有 screen。故未对业务 UI、生命周期、PySide6 版本或依赖作无证据修改。

## 原生验收状态

正式入口未产生可供观察的窗口，不能核验 disconnected/NoModel/自动决策关闭/模拟端关闭状态、800×600 控件与错误区可访问性、窗口关闭或线程资源释放。没有生成 P4A 原生截图。历史 P3 800×600 UI 自动化和默认状态截图、[P3 执行报告中的 88 passed](P3_STIMULATION_SIMULATION_REPORT.md) 仅是既有离屏/工程证据，不是本轮原生验证；本轮也没有执行 offscreen 以冒充原生通过。

在有正常登录桌面的 macOS 会话中最短复核方式见 [runbook](../docs/P4_INTEGRATION_RUNBOOK.md)。执行标准命令时保持不连接 Curry/Rally；观察默认 UI，将窗口调整至 800×600，关闭窗口并记录退出码。若再次异常，按实际启动时间查看最近的 python3*.ips。当前唯一真实阻塞是本任务 shell 没有可用 Qt screen 且 Computer Use 不允许访问 Terminal；需由桌面会话提供原生窗口观察条件，不能从本轮结果宣称已解决。

## 验证与未执行

- 诊断命令：最小 Qt 窗口退出 134；cocoa screen 探针退出 0 且 screen count 为 0；标准正式入口退出 139。错误和崩溃栈摘要如上，原系统 .ips 文件保留在 DiagnosticReports，未复制、改写或清理。
- 本次仅新增/修改文档，没有修改工程代码或依赖；按项目规则未机械重跑 uv sync --locked、pytest、合成 TCP/UDP 或 Curry 完整回归。P3 执行报告中的 88 passed 是先前结果，本报告不声称为本轮测试。
- 未能操作原生 GUI；仅尝试读取 Terminal 状态即被 Computer Use 安全策略拒绝。未连接 Curry/Rally/真实设备，未执行实际刺激，未接入模型本体，未开展受试者/正式实验，也未实现时钟同步。
- 缺失资料及后续判定条件列于 docs/P4_INTEGRATION_RUNBOOK.md。P4-A 不自动进入真机/同步后续批次，也不修改队列。
