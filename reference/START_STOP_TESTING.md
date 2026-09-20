# Rally API 与串口启停测试

这三个文件可整体复制到另一台 Windows 电脑。两个 Python 程序只使用标准库，不需要安装或更新任何依赖。

## 安全前提

- 首次测试使用厂商允许的测试负载/仿体和独立输出观测，不连接受试者。
- 确保 Rally/设备的独立停止或急停手段可用；脚本回复成功不等于已证明物理输出开始或归零。
- API 测试和串口测试分开进行，不同时运行。
- 串口测试前关闭友善串口调试助手，一个 COM 口同时只能由一个程序占用。
- `cycle --duration` 是从发送 START 边界计起的 0.1–10 秒软件预算，不是硬件级运行时长或停止时限保证。回复等待会消耗该预算；预算到期、拒绝、未知回复或异常时，程序进入 `finally` 并尝试一次 STOP。设备实际停止时间取决于通信、设备处理与停止确认，仍必须使用独立停止方式并核实物理输出。

## 使用当前工作区环境

在 PowerShell 中设置当前工作区的 Python：

```powershell
$python = "D:\OneDrive\桌面\学习资料\研究生科研的东西\myproject\Sleep_Elecstimulation_2\.venv\Scripts\python.exe"
$tools = "D:\OneDrive\桌面\sticontroller"
```

如果另一台电脑的工作区位置不同，只修改 `$python` 为那台电脑已有的同一项目 `.venv\Scripts\python.exe`；不执行 `pip install`、`uv add` 或 `uv sync`。

## Rally UDP API 测试

`RallyStartStopTest.py` 只允许本机 loopback，默认目标是 `127.0.0.1:8801`。

1. 先启动 Rally，确认设备、原始协议和独立停止手段已就绪。
2. 只打印将要使用的命令，不发送：

```powershell
& $python "$tools\RallyStartStopTest.py" dry-run
```

3. 先单独测试停止命令：

```powershell
& $python "$tools\RallyStartStopTest.py" stop --execute
```

4. 在测试负载上测试2秒启动—自动停止：

```powershell
& $python "$tools\RallyStartStopTest.py" cycle --duration 2 --execute --confirm I_ACCEPT_PHYSICAL_STIMULATION
```

程序只把已知的精确成功回复当作 API 成功：通用回复 `RALLY_ERROR_SUCCESS`，以及实测得到的启动回复 `启动刺激成功` 和停止回复 `停止刺激成功`。Rally 可能从动态 UDP 端口回复；程序会要求回复来自同一个 `127.0.0.1` 主机，允许源端口不是 8801，并把实际端口打印出来。START 的回复等待上限是 `min(--timeout, duration 截止时的剩余时间)`；忽略其他主机的回复不会重置截止时间。若成功回复早于截止，脚本只等待剩余预算；拒绝、未知回复、超时或异常则不再等待，直接尝试一次 STOP。STOP 有独立的有限 `--timeout`，不会因为 START 预算耗尽而跳过。例：`--duration 2 --timeout 5` 时，START 回复在 1.5 秒到达，最多再等约 0.5 秒就发起 STOP；若 START 回复等到 2 秒仍未到，则截止时发起 STOP。该软件截止不保证 Rally 或硬件在 2 秒时已经停止。如果回复超时、来源主机错误、非 UTF-8 或其他错误码，保留完整终端输出，并用 Rally/硬件独立停止。

UDP 精确成功回复仍可分别确认 API 的 START/STOP 命令结果；这不是对物理输出开始或归零的证明。STOP 的 API 请求/等待也有独立的 `--timeout`。干跑正常返回 0；显式 STOP 的已知精确成功回复返回 0，未确认或错误为非零；cycle 的 START 失败即使后续 STOP 得到确认仍以非零报告 START 失败，STOP 未确认返回 3。

## Windows 串口测试

串口脚本固定帧：

- START：`01 E1 01 00 C0`（尾字节十进制 192）
- STOP：`01 E1 01 00 80`（尾字节十进制 128）
- 默认：115200 baud、8N1、无奇偶校验、无流控

1. 列出 Windows 注册表中的 COM 口：

```powershell
& $python "$tools\SerialStartStopTest.py" list
```

2. 以 COM4 为例做干跑，不打开串口：

```powershell
& $python "$tools\SerialStartStopTest.py" dry-run --port COM4
```

3. 关闭其他占用 COM4 的程序后，先单独发送 STOP：

```powershell
& $python "$tools\SerialStartStopTest.py" stop --port COM4 --execute
```

4. 在测试负载上发送 START，2秒后自动发送 STOP：

```powershell
& $python "$tools\SerialStartStopTest.py" cycle --port COM4 --duration 2 --execute --confirm I_ACCEPT_PHYSICAL_STIMULATION
```

脚本会原样打印 TX/RX 十六进制字节，并标注“收到字节；命令/物理状态未确认”。当前未知设备确认帧的正式含义，所以任意 START 字节（包括错误帧和回显）都不会被当作启动确认；收到第一个回复字节后立即进入保护性 STOP。若一直没有字节，则最多等待 `min(--read-seconds, START 预算剩余时间)` 后进入 STOP。`duration` 覆盖 START 写入与回复读取的软件预算；回复可令 cycle 提前停止，它不承诺一定运行满该时长。

串口 STOP 的写入使用独立 1 秒有限写超时，之后的读取最多等待 `--read-seconds`；任何字节、错误帧、回显或空回复都不能确认停止。stop/cycle 命令执行后都会返回非零码 3 并提示“停止未确认，请使用独立停止方式并核实物理输出”；I/O 异常保留错误退出码，但同样明确 STOP 是否尝试及其状态。`list` 和 `dry-run` 正常返回 0。软件截止只能约束程序等待与写入超时设置，不能保证 Windows 调度、驱动、Rally 或设备在该时刻完成物理停止。

## 需要带回的测试证据

每项测试保留：

- 完整命令和终端输出；
- Rally/设备界面的启动前、启动后、停止后状态；
- 测试负载上的实际输出观察；
- UDP 回复字符串或串口 RX 十六进制字节；
- 命令到设备状态变化的大致延迟。

如果启动或停止没有确认，不继续进行 ONNX 联动。
