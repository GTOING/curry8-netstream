# Curry 8 NetStreaming EEG 客户端

通过 Curry 8 的 NetStreaming TCP/IP 接口完成自动握手，并把每个未压缩
EEG 数据包解码为 NumPy 数组：

```text
shape = [EEG 通道数, 每通道采样点数]
```

如果 Curry 每 30 秒发送一个数据包，采样率为 `fs`，则正常输出应满足：

```text
每通道采样点数 = fs × 30
```

## 协议依据与当前边界

协议实现已根据本地 `reference/` 中的 Curry Python 参考客户端完成。该目录
只用于本机分析，已由 `.gitignore` 排除，不会提交。

当前已经明确并实现：

- 20 字节大端消息头：`>4sHHIII`；
- `CTRL` 与 `DATA` 消息；
- 基础信息、通道信息、开始/停止推流的请求码；
- 24 字节小端 `BasicInfoAcq`；
- 144 字节小端 `NetStreamingChannelInfo`；
- 未压缩、小端 `float32` EEG；
- 线上数据为采样点优先交织，输出转为 `[channel, sample]`；
- TCP 半包跨超时保留，以及最大 payload 长度检查。

仓库尚缺一份真实 Curry EEG 网络抓包，因此解码已经通过合成端到端测试，
但仍需连接目标 Curry 8 做最终真机验证。压缩的 float32 ZIP 数据暂不支持；
请将服务器配置为未压缩格式。

## 自动握手流程

正常 `stream()` 会依次执行：

```text
连接 Curry
  → 请求 BasicInfo（request=6）
  → 解析 EEG 通道数、采样率和数据大小
  → 请求 ChannelInfo（request=3）
  → 解析 UTF-16LE 通道名称
  → 请求开始推流（request=8）
  → 接收 DATA_Eeg / Float32（code=2, request=1）
  → 输出 DataBlock.data[channel, sample]
  → 结束时请求停止推流（request=9）
```

## 环境

依赖 Python 3.11+ 和 NumPy。可以用 Conda 创建环境：

```powershell
conda env create -f environment.yml
conda activate curry8
```

## 测试

测试只依赖标准库 `unittest` 和 NumPy，不需要 Curry 硬件：

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

测试覆盖真实 CTRL 抓包回归、协议结构尺寸、基础/通道信息、交织 EEG 解码、
自动握手，以及 TCP 帧头半包后发生超时的恢复。

## 连接 Curry

在 Curry 中启用 NetStreaming Server，使用未压缩 float32 数据格式，然后运行：

```powershell
python scripts/run_client.py `
  --host <Curry-IP> `
  --port 4455 `
  --expected-seconds 30 `
  --dump capture.bin `
  --log-level DEBUG
```

程序会为每个 EEG 包打印：

```text
[block 1] start_sample=... shape=(通道数, 采样点数) sr=...Hz duration=30.000s
```

`--dump` 会保留原始入站字节，便于真机验证失败时复现。用 `--max-blocks 2`
可以在收到两个 EEG 包后自动请求停止推流并退出。

纯诊断模式不会解析数据：

```powershell
python scripts/run_client.py --host <Curry-IP> --port 4455 `
  --peek 70 --dump capture_70s.bin --log-level DEBUG
```

## 代码结构

```text
src/curry_netstream/protocol.py  协议常量、结构体和 EEG 解码
src/curry_netstream/client.py    TCP 连接、自动握手、分帧和分发
src/curry_netstream/models.py    SessionInfo / DataBlock
scripts/run_client.py            命令行真机入口
tests/                            协议及合成端到端测试
```
