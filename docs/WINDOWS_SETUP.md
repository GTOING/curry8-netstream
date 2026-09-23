# Windows 环境配置

在完整项目副本中，双击根目录 **setup_windows.cmd**。需要联网下载 uv、Python 和锁定依赖；支持 64 位 Windows、Windows PowerShell 5.1 或 Windows 上的 PowerShell 7。通常无需管理员权限。

也可在项目根目录打开 PowerShell：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1
```

脚本自动定位项目，支持目录包含空格或中文；检查 Curry 本地依赖完整性，复用已有 uv。缺少 uv 时从官方安装地址安装 0.12.1 到当前用户 `%LOCALAPPDATA%\sleep-stim-controller\uv-bin`，不修改永久 PATH。安装方式依据 [uv 官方文档](https://docs.astral.sh/uv/getting-started/installation/)。ExecutionPolicy Bypass 只作用于本次启动的进程，不修改系统策略；组织策略限制仍需遵守。

脚本读取 `.python-version`，安装 Python 3.11，用 `uv sync --locked` 创建/同步项目 `.venv`（包含测试依赖），再检查主程序、Curry、Qt、NumPy 及当前版本存在的 ONNX/SciPy 模块导入。锁文件过期会明确失败，不自动重写。重复运行复用安装和缓存；已有跨平台 `.venv` 会提示人工移开，不自行删除。不要把 macOS 的 `.venv` 拷贝到 Windows。

配置成功后，在项目根目录运行，无需激活虚拟环境或设置 uv PATH：

```powershell
.\.venv\Scripts\sleep-stim-controller.exe
```

可选：配置环境并运行离屏回归测试（测试会使用本机合成 TCP/UDP，不连接真实设备）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1 -RunTests
```

测试结束会恢复原 `QT_QPA_PLATFORM`；配置过程也恢复临时修改的进程环境变量。默认只安装和检查导入，不启动 GUI、不加载模型、不连接 Curry/Rally。安装运行时依赖不等于启用模型，正式默认仍为 NoModel。

## Curry 发送配置

控制器接收端不要求 Curry 的每个 TCP 网络包包含 30 秒数据。Curry 的 `Blocks Per Second`/`Auto` 只决定发送频率；控制器按网络包中的绝对 `start_sample` 和握手采样率累积不重叠的 30 秒分析窗口。保持非压缩 float32 流，首次现场排查建议选择 Raw（Processed Data 可能改变包长或增加延迟），并记录 Curry 版本、通道顺序、采样率、包样本数和发包设置。发包频率与 30 秒分析窗口相互独立，不要为凑窗修改采样率；缺样、重复、乱序、NaN/Inf 或元信息变化会停止本次会话。

配置完成后可先运行本机合成回环（不会连接真实 Curry，也不会发送刺激）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1 -RunTests
```

回环测试验证 TCP 分段/粘包、短包跨窗、记录/回放和取消收尾；它不能替代 Windows 上的真实 Curry 验收。

## P4-B Rally 启停模式

GUI 默认选择“本机模拟（P3）”，真实 Rally 模式保持关闭。真实模式只允许固定本机 UDP 端点 `127.0.0.1:8801`，不提供任意地址输入，也不读取或修改 Rally 当前已加载的刺激协议；操作员须先在 Rally 中加载并检查协议，再勾选界面的明确确认框。只有当前实时 Curry 会话完成握手、连接前显式选择 ONNX、目标期/策略/结果年龄等配置完整时，真实自动控制才可启用。

显式启用只进入 `ARMED/IDLE` 并记录操作者确认，零 UDP；启用前旧结果资格会清掉。之后目标期发送 `Start Stim`，非目标期在 IDLE/STOPPED 时 no-op、在 RUNNING 时发送 `Stop Stim`。GUI 周期会用已填写的最大结果年龄检查静默流，真正发送 `Start Stim` 前还会复核 session、armed 状态和缓存年龄，过期缓存不能在等待 Stop 后再次启动。真实命令没有 vendor request id，程序使用单 worker、每请求独立 UDP socket 和迟到回复隔离；回复主机必须为 `127.0.0.1`，源端口可以是 Rally 的动态端口。普通 Start 受隔离容量上限约束，并保留一次 Stop/启动不确定补偿容量，不释放旧 socket 复用端口。只有实际发送 Start 的收尾责任或已确认 RUNNING 才触发必要 Stop；未发送/取消的 Start 不补 Stop。界面展示操作者确认、发送中、API 最近确认和收尾责任，API 确认不等于物理输出确认。

启动拒绝、未知或超时不自动重试启动，只进行一次有界补偿停止；停止失败显示 `FAULT/UNKNOWN`，此时必须使用 Rally/硬件侧独立停止。未发送/取消的 Start、已确认 STOPPED 或纯 IDLE 收尾不产生 Stop。断流、模型失败、关闭窗口和退出只在存在上述责任或已确认 RUNNING 时发送一次必要 Stop；自然 TCP EOF/网络异常在网络线程放行 pipeline 关闭前先登记 runtime 收尾，普通 pipeline 收尾/记录故障不会撤销必要 Stop 的专用收尾租约。writer 在同一 condition 锁内越过最终事件排空点后关闭新租约准入；记录不可用时会报告控制事件缺失而不虚报保存成功。可写记录会在最终控制事件后写入 `session_finished`。操作系统异常或进程被杀不能保证停止。当前实现的自动化验证仅使用随机 loopback 假端点；本项目没有在 Windows 上连接真实 Rally、Curry 或电刺激硬件，macOS 离屏结果不代表 Windows 原生窗口或硬件验收。

失败时保留项目、缓存与失败环境，退出码为 1；依提示检查网络、写入权限或项目完整性后重新运行。该脚本不安装 Curry/Rally 软件、设备驱动或修改防火墙。

验证限制：脚本在 macOS 工作区生成并进行静态检查，尚未实际执行 Windows 安装流程；此前应用测试结果不代表此脚本已在 Windows 实测。

## Issue 6 版本化范式模式

当前 GUI 可在“真实协议配置”中显式选择“版本化范式（Start + Apply）”，再选一个包含 `paradigm.json` 及其全部协议文件的目录。加载器会在控制启用前检查 schema/version、引用路径、阶段映射、刺激/返回通道、数值、SD 单位声明和基础协议名；运行会话使用冻结快照而不是重新读取磁盘文件。选择包不等于启用控制。启用仍要求实时 Curry 会话、显式 ONNX、操作者确认基础协议与当前未刺激状态、合格配置以及包可用于真实模式；启用本身进入 `ARMED/IDLE`，不会发 UDP。

当前仓库只带 `tests/fixtures/issue6_paradigm_test_only/` 合成包，带测试单位与非生产分类，不能真实 arming；没有生产包，也没有批准 A/B/C 刺激参数、SD→时间的生产换算或设备上限。W→A、N1→B、N2→C、N3/REM→Stop 是工程映射，不是临床/设备安全建议。Issue 中 FR=30 和 Start 后基础输出时窗只是待验证假设，不能用作 Windows 配置或硬件时限。详见 [Issue 6 GUI 与范式包指南](ISSUE6_PARADIGM_GUI_GUIDE.md)。本任务仅运行离屏 GUI 和随机 loopback 合成测试；Windows 原生窗口、Rally、端口 8801、COM、电刺激硬件及物理输出均未验证。
