# Rally API 与串口启停测试

这三个文件可整体复制到另一台 Windows 电脑。两个 Python 程序只使用标准库，不需要安装或更新任何依赖。

## 安全前提

- 首次测试使用厂商允许的测试负载/仿体和独立输出观测，不连接受试者。
- 确保 Rally/设备的独立停止或急停手段可用；脚本回复成功不等于已证明物理输出开始或归零。
- API 测试和串口测试分开进行，不同时运行。
- 串口测试前关闭友善串口调试助手，一个 COM 口同时只能由一个程序占用。
- `cycle` 最长只允许 10 秒，并在 `finally` 中尝试停止；仍必须观察设备端的实际输出。

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

程序只把已知的精确成功回复当作 API 成功：通用回复 `RALLY_ERROR_SUCCESS`，以及实测得到的启动回复 `启动刺激成功` 和停止回复 `停止刺激成功`。Rally 可能从动态 UDP 端口回复；程序会要求回复来自同一个 `127.0.0.1` 主机，允许源端口不是 8801，并把实际端口打印出来。如果超时、来源主机错误、非 UTF-8 或其他错误码，保留完整终端输出，并用 Rally/硬件独立停止。

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

脚本会原样打印 TX/RX 十六进制字节。当前未知设备确认帧的正式含义，所以“收到字节”只证明有串口回复，不自动宣称刺激已启动或停止。

## 需要带回的测试证据

每项测试保留：

- 完整命令和终端输出；
- Rally/设备界面的启动前、启动后、停止后状态；
- 测试负载上的实际输出观察；
- UDP 回复字符串或串口 RX 十六进制字节；
- 命令到设备状态变化的大致延迟。

如果启动或停止没有确认，不继续进行 ONNX 联动。
