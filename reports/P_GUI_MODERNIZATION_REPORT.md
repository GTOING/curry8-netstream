# 现代化控制台 UI 执行事实报告

日期：2026-09-25。执行分支：`feature/modern-control-ui`，基于 `main@c636c244e89e785af75134dc711d48b5d051976b`（开始时本地 `origin/main` 同提交；未执行远端 fetch）。本轮未提交、推送、合并或创建 PR；主线程原有 dirty 文档与空 Git index 保留。所有截图均为 macOS Qt offscreen 合成场景，不是 Windows、真机或物理输出验收。

## 页面、映射与线框

原 `MainWindow` 的控件对象、objectName、公共 setter 和业务信号沿用，`app.py` 接线与控制器未变。单长页拆为固定运行栏＋四个原生 `QTabWidget` 页面，页面独立滚动：

| 页面 | 原控件／显示入口 | 操作边界 |
| --- | --- | --- |
| 运行总览 | `summary_section`、`set_assembly_progress`、`set_processing_result`、目标期勾选和 `set_rally_status`、近期刺激事件 | 显示真实会话／块／最新期别、期望与 API 确认；无结果不生成期别 |
| 刺激控制 | 原 Rally 模式／档案、范式包、五期选择、策略、模拟协议／本机模拟端、操作者确认、自动控制勾选 | 真实模式仍需既有 arming；默认模拟／关闭，启用后固定栏的“关闭自动控制”仅经原 `stimulation_auto_requested(False)` 停用路径，不是硬件急停 |
| 记录与回放 | 原记录／CSV 独立选项、目录、回放打开／退出／块导航、CSV 重建与状态 | 默认均关闭；回放明确只读且不发送命令 |
| 连接／模型／诊断 | 原 Curry 地址端口、ONNX 启用／文件／通道、握手标签、会话详情、诊断 | 连接后沿用既有锁定与配置读取 |

线框核对：1000×720 为“标题／模式／连接 → 3 列×2 行运行摘要（进度、模型、记录；自动控制、API、固定关闭）→ 独立停止／错误 → 四页签 → 页内滚动”。800×600 保留相同关键操作与提示在首屏，摘要文字按列换行，页内滚动容纳策略和路径。没有自制标题栏或波形。默认画面与 800×600、真实模式故障画面已逐张目视检查；长错误/路径完整内容仍可滚动与选择复制。

仅使用项目已有 PySide6 Widgets 与作用域 QSS；未引入第三方控件、图标、字体或新依赖，沿用现有 PySide6/Qt 许可与部署义务，不复制 GPL Fluent 模板。现有控件被重新分配至四页，各页滚轮守卫仍将设置控件滚轮导向所在页滚动；弹出列表、键盘、点击与回放导航保留。视觉检查促成了明确的空状态、故障优先级、可见焦点和禁用文字对比调整。实测配色对比：正文／背景 12.55:1、主色／白 6.59:1、故障／浅红 6.74:1、提醒／浅橙 5.93:1、禁用文字／浅底 5.05:1。

## 合成状态样图

- [默认 NoModel／模拟关闭](P_GUI_MODERNIZATION_DEFAULT.png)：1000×720。
- [800×600 刺激页](P_GUI_MODERNIZATION_800X600.png)：目标期、策略和固定栏可见；下方设置页内滚动。
- [真实模式 FAULT/UNKNOWN](P_GUI_MODERNIZATION_FAULT_SYNTHETIC.png)：独立停止提示与完整错误在页签外，API 确认与物理输出分离；此画面由 UI 合成状态构造，没有发送 Stop。
- [完整只读回放](P_GUI_MODERNIZATION_REPLAY_SYNTHETIC.png)：合成一块会话，记录状态完整，无实时累积与 Rally 发送。
- [长文本／记录与 CSV 故障](P_GUI_MODERNIZATION_LONG_TEXT_SYNTHETIC.png)：800×600，长错误、长目录和双故障状态。

可复现截图入口：`QT_QPA_PLATFORM=offscreen uv run --locked python scripts/capture_modern_ui.py`（本机沙箱需设置工作区内 `UV_CACHE_DIR`）。脚本只调用 UI setter 构造明确标注的合成场景，不建立 Curry/Rally 连接。

## 行为与验证

- `UV_CACHE_DIR=... uv sync --locked`：通过。首次默认用户级 uv 缓存写入被沙箱拒绝，改用工作区缓存后成功；依赖锁未变。
- `pytest tests/test_ui.py -q`：13 passed。覆盖四页鼠标／键盘导航与样式刷新零业务信号；默认、握手、30 秒进度、分期成功／失败与测试替身、`ARMED/IDLE`／`RUNNING`／`FAULT/UNKNOWN`、固定关闭按钮原路径、记录／CSV 故障、完整／不完整回放、旧实时 setter 不覆盖回放。真正 `QWheelEvent` 覆盖设置／回放数值、焦点、修饰键、像素滚轮与下拉列表；长错误和目录在 800×600 可滚动。
- `pytest tests/test_ui.py tests/test_controller_synthetic.py tests/test_issue7_session_csv.py -q`：58 passed。随机本机假 Curry/Rally 端集成用例证明页签／样式在显式启用前零 Rally 请求，之后原 Start/Stop、记录归档和回放链路保持。沙箱默认禁止绑定随机本机 UDP，获测试权限后通过；未访问生产 `127.0.0.1:8801`。
- 最终 `pytest -q`：251 passed in 25.01s。`python -m compileall -q src/sleep_stim_controller scripts/capture_modern_ui.py` 与 `git diff --check` 通过。

未在 Windows 原生桌面执行鼠标、125%／200% DPI、高对比度、系统字体和截图核验；macOS offscreen 结果不能替代这些检查。未连接真实 Curry、Rally/8801、COM、刺激硬件或受试者，合成 API 回复不构成物理输出或科研效果证据。

## 收口补充（2026-09-25）

依据主线程首次复核，只在现有 `feature/modern-control-ui` 未提交工作区修正三项局部问题，前述首次 **251 passed** 记录保留为历史证据。本段是新一轮结果，不替换首次执行事实。

1. 固定栏记录／CSV 简报现在依勾选、本次会话忙碌／结束状态和控制器原始状态词判断：未启用、已选择待连接、初始化中、正在写入、会话已关闭、CSV 已写入完成、失败及不完整。`已写入 N 行` 只有在会话关闭或写者已退出时才显示完成；若会话结束但没有 CSV 完成证据，则显示“已关闭（写入未确认）”，不推断文件已生成。详细控制器文本仍在记录页及固定栏 tooltip。新会话 `begin_session` 清除上一会话的完成／故障简报，零块／零行也不会回到“待连接”。
2. 连接前勾选 ONNX 只显示“已选择，待连接校验／加载”；文件尚未选时明确提示。取消勾选恢复 NoModel。模型选择不会触发文件校验／加载，也不会冒充本次真实分期；实时成功、失败、NoModel 与回放结果仍由既有结果 setter 驱动，连接前配置变化不覆盖已结束或回放中的结果。
3. 运行总览默认改为简洁的“尚无实时会话”与“检查连接与模型设置”入口，按钮只切页；实时／故障／回放显示突出 30 秒数值与最新真实期别或无结果原因的主信息，原始块、采样、期望／API 状态仍在下方详情。固定栏 API 最近确认分行显示并与“物理输出未验证”分开；运行栏的关闭自动控制按钮、独立停止及错误区始终在页签之外。收紧了初始重复文案，沿用 Widgets、现有焦点样式和 Issue 8 滚轮守卫，无新增依赖。

收口后重新生成并目视核对[默认](P_GUI_MODERNIZATION_DEFAULT.png)、[800×600](P_GUI_MODERNIZATION_800X600.png)、[真实故障](P_GUI_MODERNIZATION_FAULT_SYNTHETIC.png)、[完整回放](P_GUI_MODERNIZATION_REPLAY_SYNTHETIC.png)、[长文本](P_GUI_MODERNIZATION_LONG_TEXT_SYNTHETIC.png)五张原有合成截图；另增[连接前 ONNX 已选](P_GUI_MODERNIZATION_MODEL_SELECTED_SYNTHETIC.png)和[零块／零行会话已关闭](P_GUI_MODERNIZATION_RECORDING_CLOSED_SYNTHETIC.png)。所有状态由截图脚本显式构造，未发起设备连接或真实刺激。

验证：工作区缓存下 `uv sync --locked` 通过；`pytest tests/test_ui.py -q` 为 **16 passed**，新增覆盖状态复现、零行／失败／重连、配置与结果隔离、空状态导航和 800×600 故障可见性；UI＋合成控制器＋CSV 定向集为 **61 passed**（14.93s），随机本机假 Rally 在空状态按钮／切页／样式刷新后仍为零请求，显式启停走原路径；最终 `pytest -q` 为 **254 passed in 25.28s**。`compileall` 与 `git diff --check` 通过。分支 HEAD 仍为 `c636c244e89e785af75134dc711d48b5d051976b`，Git index 为空；未提交、推送或合并。Windows 原生鼠标、DPI 125%／200%、高对比度和真机／物理输出仍未验证。
