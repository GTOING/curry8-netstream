# 当前任务队列

更新：2026-09-16。当前角色：决策主线程。

## 当前目标与状态

**最新目标修正：正式部署平台为 Windows；单通道 ONNX 睡眠分期已接入。** 预处理冻结为输入 µV、50 Hz 工频处理、0.3–35 Hz 带通、100 Hz、不做逐样本 z-score；模型时间长度从 ONNX 自动读取并要求为 3000 的整数倍，通道名由用户选择且完整唯一匹配。默认 LiteSleepNet EDF20 FP32 6000 点模型已复制进控制器发布包，LiteSleepNet 原仓库保持只读。Windows 锁定安装、原生 GUI、当前工作区中文/空格路径、TCP/UDP 合成回环和真实 ONNX GUI 回环均已于 2026-09-16 验证通过；尚未取得真实 Curry/Rally 设备、真实模型效果或同步证据。下一工程优先项仍是真实设备接入及其安全/启停合同。

开发睡眠分期实验电刺激控制器：Curry 8 每包 30 秒 EEG，预留模型接口进行 W/N1/N2/N3/REM 分期，用户多选目标睡眠期控制刺激。当前资料阅读、归档、需求整理和开发目录规范化已完成，开发流程规划已形成；P1 收口回报已复核，工程范围通过主线程验收。执行者最终回归 35 passed；主线程核对关键实现、测试与截图，未重复跑全套测试。P2 收口修复已复核，工程范围通过主线程验收（执行者最终回归 52 passed）。实时/回放隔离问题已收口；原生 macOS 启动 139 的当前 shell 失败路径已在 P4-A 定位至零屏幕环境下的 Qt 屏幕查询；零屏幕成因及普通桌面可用性仍待验证。P3 实现回报已复核，模拟工程范围通过主线程验收；执行者完整回归 88 passed，主线程核对关键代码、测试与默认界面截图，未重跑测试。P4-A 诊断与接入文档已通过主线程验收；系统崩溃栈已核对，原生桌面启动/布局/关闭仍待验证，139 未判为已修复。P-GUI 已通过工程验收，执行者最终回归 89 passed；原生本轮退出 134、零屏幕问题仍未解决。真机与同步后续批次未发布。

## 已完成与入口

- [ONNX 睡眠分期接入](../reports/2026-09-16_ONNX_STAGING_INTEGRATION.md)：默认 6000 点模型、可选单通道、状态化滤波、自动时间维合同、W/N1/N2/N3/REM 输出和 GUI 回环已完成；最终完整回归 97 passed，wheel 包含模型与身份清单。详细语义见 [ONNX 合同](../docs/P_MODEL_ONNX_CONTRACT.md)。

- [Windows 安装、原生 GUI、路径权限与回环验证](../reports/2026-09-16_WINDOWS_VERIFICATION.md)：CPython 3.11.15 / PySide6 6.11.2 锁定安装成功，原生 Qt `windows` 平台窗口可见并正常关闭，中文路径完整会话读回通过，完整回归 89 passed，三个关键 TCP/UDP 回环用例复跑通过；未连接真实设备。Windows 截图工具受 `0x80004002` 限制，本轮无原生截图。

- [用户需求与范围](../docs/SLEEP_STIM_REQUIREMENTS.md)：模型本体不在开发范围，UI 模板只作基础；用户最新说明优先于根规则中的旧项目设备背景。
- [参考资料索引](../reference/README.md)：电刺激原件、UI 原包及展开文件、Curry 克隆。
- [静态分析记录](../reports/2026-09-15_reference_review.md)：Rally 接口语义、版本差异、旧脚本问题与同步证据边界。
- Curry 仓库已克隆至 `curry8-netstream/`，main @ `0d1067c`；现有 uv `.venv` 已配齐 Curry/UI 开发依赖，导入和离屏 UI 验证已通过。

- [开发目录整理记录](../reports/2026-09-15_development_layout.md)：根级 src 包、pyproject.toml、uv.lock 与统一测试入口已建立；uv sync --locked 成功，11 项 Curry 既有测试本轮实际通过。根目录仍未初始化 Git。

## 当前开发计划

- [开发流程](plans/SLEEP_STIM_DEVELOPMENT_PLAN.md)：P0 最小合同 → P1 桌面与 EEG → P2 分期接口/记录 → P3 刺激策略与模拟联调 → P4 真机与同步 → P5 收口。计划已整理，详细接口和实验参数尚未冻结。

## 最新完成任务：P-GUI

用户要求取消控制器中的 EEG 波形显示，由 Curry 8 查看；本轮重新组织控制界面。

- [P-GUI 合同](../docs/P_GUI_CONTRACT.md)：替代 P1/P2 波形展示要求，保留 EEG 接收、分期、记录与回放数据链路。
- [P-GUI 执行 prompt](../docs/tasks/P_GUI_EXECUTION.md)：已实施并通过主线程工程验收。
- [P-GUI 执行报告](../reports/P_GUI_REPORT.md)：去除波形/PyQtGraph，执行者最终回归 89 passed，离屏启动关闭通过。
- [P-GUI 主线程验收](../reports/P_GUI_ACCEPTANCE.md)：代码、测试与四张截图已复核，主线程未重跑测试。原生本轮退出 134，零屏幕限制仍在。

## P4-A 文档与诊断已验收，原生验证待完成

- [P4 接入准备合同](../docs/P4_INTEGRATION_READINESS_CONTRACT.md)：先处理原生启动 139 与接入资料，真实采集/刺激/同步另行定界。
- [P4-A 执行 prompt](../docs/tasks/P4A_NATIVE_READINESS_EXECUTION.md)：可独立交付部分已完成；原生窗口验证受执行环境限制。
- [P4-A 执行报告](../reports/P4A_NATIVE_READINESS_REPORT.md)：Cocoa 零屏幕，最小窗口 134、正式入口 139；未改源码/依赖、未重跑测试。
- [P4 接入手册](../docs/P4_INTEGRATION_RUNBOOK.md)：正常桌面核验步骤及后续资料缺项。
- [P4-A 主线程验收](../reports/P4A_NATIVE_READINESS_ACCEPTANCE.md)：诊断/文档通过；待正常桌面提供窗口、800×600 与关闭证据。

## P3 已完成证据

- [P3 合同](../docs/P3_STIMULATION_SIMULATION_CONTRACT.md)：目标期多选、显式策略、Rally 报文与受控本机模拟联调；不启用真实刺激。
- [P3 执行 prompt](../docs/tasks/P3_STIMULATION_SIMULATION_EXECUTION.md)：已实施，模拟工程范围验收通过。
- [P3 执行报告](../reports/P3_STIMULATION_SIMULATION_REPORT.md)：88 passed，默认 NoModel 零请求，仅本应用回环模拟端联调。
- [P3 主线程验收](../reports/P3_STIMULATION_SIMULATION_ACCEPTANCE.md)：策略、端点约束、迟到回复隔离、单写者收尾与回放隔离已复核；原生 GUI 与真机限制保留。

### P2 已完成证据

- [P2 控制性合同](../docs/P2_STAGING_RECORDING_CONTRACT.md)：分期接口、保存默认关闭、v1 会话记录与只读回放；详细实验参数不在本阶段。
- [P2 执行 prompt](../docs/tasks/P2_STAGING_RECORDING_EXECUTION.md)：已实施并完成末尾收口要求，工程验收通过。
- [P2 执行报告](../reports/P2_STAGING_RECORDING_REPORT.md)：最终 52 passed，保留原 49 passed；原生启动退出 139，根因未确定。
- [P2 主线程验收](../reports/P2_STAGING_RECORDING_ACCEPTANCE.md)：末节记录工程验收通过，实时/回放隔离已修复；保留原生可用性待验证项。

### P1 已完成证据

- [P1 桌面与 EEG 接入执行 prompt](../docs/tasks/P1_DESKTOP_EEG_EXECUTION.md)：已实施、补齐并通过主线程验收；文末补充要求保留为历史。
- [P1 控制性合同](../docs/P1_DESKTOP_EEG_CONTRACT.md)：P1 接口、状态、显示和验证已定；不代表整个 P0 的模型/刺激合同已完成。
- [执行报告](../reports/P1_DESKTOP_EEG_REPORT.md)：最终 35 passed，保留首版 24 passed；离屏启动/关闭与最小窗口验证完成，未真机、未原生窗口验收。
- [主线程验收记录](../reports/P1_DESKTOP_EEG_ACCEPTANCE.md)：终态历史标注已修复，半包取消/带连接关闭等证据已补齐；末节记录工程验收通过。主线程未重跑全套测试。

## 授权与后续

用户允许后续整合并优化电刺激代码。P1/P2/P3 与 Windows 原生验证已经完成；本轮单通道 ONNX 睡眠分期也已实现并通过合成回环。真实 Curry/Rally、真实模型效果、电刺激、时钟同步及正式实验仍未实施。主线程默认准备限界执行 prompt 由用户分发，实际调用或创建执行任务需用户明确要求。

Rally 本机缺失；目标 Curry 的实际通道标签/单位证据、目标设备版本与地址、实际刺激配置、触发/停止策略及同步合同尚未明确。现有 API 为刺激中调参，杀掉 Python 进程不等于设备停止；详见分析记录。时钟同步保持后续实施边界，不因参考代码到位自动启动。

## GitHub 发布记录

按用户授权已推送至 `https://github.com/GTOING/curry8-netstream` 的 main，提交 `c68db418dbfada7a489574ca1e88fbaceb25fdd1`，GitHub API 已核对远端 SHA。控制器位于 `sleep-stim-controller/`，包含当前 Curry 依赖源码及本地适配；保留原仓库根内容和历史。未上传虚拟环境、采集数据或 reference 原件。发布工作克隆位于 `/private/tmp/sleep-github-publish-20260916`；当前开发根目录仍未初始化 Git，原嵌套 Curry 工作区改动保留。此发布记录在推送完成后本地追加，尚未包含在上述提交中。

## GitHub 根结构更正

用户要求按当前本地项目直接作为远端根目录发布，替代之前 `sleep-stim-controller/` 包裹结构；不保留旧远端根文件布局，保留提交历史。当前 Curry 适配源码作为项目依赖一并发布，嵌套 Git 元数据、环境、缓存、数据和被忽略参考原件不上传。本次发布执行结果以对话中核实的远端提交为准。
