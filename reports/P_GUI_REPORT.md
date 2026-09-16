# P-GUI 界面整理与优化报告

日期：2026-09-16。本轮移除实时及回放 EEG 波形，改为分期与刺激控制台。EEG 波形由 Curry 8 查看，完整数据仍通过既有接收、校验、模型接口、记录和回放 reader。工程完整回归通过；原生窗口可用性尚未验证通过。

## 实际改动

- ui.py：删除 PlotPanel、PyQtGraph/QGraphicsView、右侧分隔器/画布、绘图降采样和数组生成。默认 1000×720、最小 800×600；固定模式、连接按钮及错误区，主体依次为数据/分期摘要、刺激配置、会话记录/回放。刺激参数使用两列表单，连接参数/会话详情/诊断可折叠；长错误有独立滚动区，协议和目录使用可换行、滚动、复制的只读文本框。动态文本按纯文本显示。
- app.py：复用 pump_latest 更新摘要；回放逐块读取前和加载失败时清除旧块、结果与刺激事件。保留请求 token/generation 检查；回放 reader 继续读取和验证 NPY，不因去图跳过验证。
- controller.py：仅更新界面摘要交接/替换计数文案，注明替换不表示原始 EEG 丢失；后台处理、保存和策略逻辑未改。
- tests/test_ui.py、tests/test_controller_synthetic.py：替换曲线断言并增加长文本及损坏回放块验证，详见下节。
- pyproject.toml/uv.lock：移除根 PyQtGraph 依赖。其传递依赖 colorama 已从当前环境卸载；锁文件仍含 pytest 的 Windows 条件依赖 colorama。NumPy 保留。项目生产源码不再引用 PyQtGraph、PlotPanel 或 QGraphicsView。
- README.md 更新现行界面与运行限制；本报告、四张截图及 P_GUI_capture.py 提供可复核证据。

## 数据与兼容性证据

未修改 Curry 独立仓库、staging.py、recording.py、replay.py 或刺激策略/通信实现。保留 NoModel、默认不记录、空目标期/策略/实验参数、关闭自动决策及本应用拥有的随机回环模拟端限制。schema v1 未变，原始数组未经过 UI 变换。

保留全部既有测试，首轮 88 项通过；最终新增一项长文本 UI 测试，合计 89 项。原“动态曲线/X/Y 长度”断言替换为无 QGraphicsView/QSplitter、块编号、标签、样本区间及 30 秒摘要断言；回放隔离中的曲线前后数组一致性替换为同一块元信息与分期结果前后一致性。既有合成 TCP 全块处理/保存、EOF/断开历史标记、pending 隔离、空/坏会话、模型替身、NoModel 零发送、回放不发送、未知请求收尾与资源释放覆盖保留。

窗口接线集成测试在实际临时会话中读取第二块后，故意损坏第一块 NPY，再导航回第一块：reader 报错、旧第二块摘要/分期/事件清除，再导航第二块可恢复显示；过程中没有重连 TCP。该损坏仅作用于测试临时目录。UI 长文本测试检查 800×600 不膨胀、正文无水平溢出、自动决策可滚动到达、长错误和无空格长路径有可用滚动范围，连接与错误区不属于主体滚动内容。测试替身分期结果在界面明确标注。

## 实际验证

依赖更新先尝试离线解析，临时缓存缺少索引而失败；默认沙箱网络解析也失败。经正常权限流程访问包索引后，uv lock 成功，仅移除 pyqtgraph 0.14.0；没有升级 Qt/NumPy。

最终命令与结果：

    UV_CACHE_DIR=/private/tmp/sleep-uv-cache uv sync --locked
    Resolved 13 packages in 26ms
    Checked 12 packages in 4ms

    QT_QPA_PLATFORM=offscreen UV_CACHE_DIR=/private/tmp/sleep-uv-cache uv run --locked pytest
    89 passed in 10.25s

完整回归覆盖根级及 Curry 测试。网络权限仅用于合成本机 TCP 与本应用随机 UDP 模拟测试。首轮完整回归为 88 passed、1 warning：客户端按积压保护关闭时合成服务 sendall 发生 BrokenPipeError；fixture 已处理 BrokenPipeError/ConnectionResetError 作为对端关闭，最终回归无该警告。

最终 UI 定向检查为 2 passed。另实际构建正式 app wiring、显示窗口并通过 QTimer 调用 window.close，结果 offscreen_exit=0、controller_released=True、transport_joined=True。离屏截图运行有 Qt 字体别名提示，不影响图像输出。

## 截图

均为 QT_QPA_PLATFORM=offscreen 的 Qt widget 抓图，并非原生桌面截图。脚本复用现有合成记录 fixture、临时目录及 SessionReader；未连接设备。长文本和回放画面标注测试替身。

- [默认状态，1000×720](P_GUI_DEFAULT.png)
- [最小窗口，800×600](P_GUI_800X600.png)
- [长错误与长路径，800×600](P_GUI_LONG_TEXT.png)
- [合成记录第二块回放与导航，1000×720](P_GUI_REPLAY.png)

复现：

    QT_QPA_PLATFORM=offscreen UV_CACHE_DIR=/private/tmp/sleep-uv-cache uv run --locked python reports/P_GUI_capture.py

## 原生尝试与限制

去图后实际运行一次标准入口（未设置 offscreen）：

    UV_CACHE_DIR=/private/tmp/sleep-uv-cache uv run --locked sleep-stim-controller

本轮进程 35568 于 19:17 启动，退出 **134**，输出 Cannot create window: no screens available，仍伴有 PasteBoard/Input Source/XPC 服务错误。没有可观察原生窗口或正常关闭证据。去图移除了先前 QGraphicsView 路径，但零屏幕问题仍在；P4-A 的历史 139 和其报告保持原样，不将退出码变化表述为原生修复。800×600 与资源释放的本轮证据限离屏环境。

运行入口仍为 uv run --locked sleep-stim-controller。待正常 macOS 桌面会话核验真实窗口、布局和关闭。未连接真实 Curry/Rally/8801、真实模型、刺激设备或受试者，未实现同步；没有修改合同、队列、历史报告、reference 或独立 Curry 仓库，没有 Git 初始化/提交/推送或启动后续阶段。
