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

## 当前进度（2026-09-15）

当前目标只包括“稳定接收并完整解码每个 30 秒 EEG 数据包”，暂不包括
睡眠分期模型、信号预处理和结果回传。

| 环节 | 状态 | 说明 |
|---|---|---|
| 参考协议筛选 | 已完成 | 已从约 2.54 GB 旧工程中保留 3 个 Curry 协议相关文件 |
| 20 字节消息头 | 已完成 | 大端 `>4sHHIII`，并有真实 CTRL 抓包回归测试 |
| 自动握手 | 已完成 | BasicInfo → ChannelInfo → StreamingStart |
| 基础/通道信息 | 已完成 | 解析通道数、采样率、数据大小及 UTF-16LE 标签 |
| EEG payload 解码 | 已完成 | 未压缩小端 float32，输出 `[channel, sample]` |
| TCP 完整收包 | 已完成 | 支持断包、跨超时保留、payload 上限保护 |
| 可取消接收与结果观察 | 已完成 | 接收轮询保留半包，支持取消事件、状态/会话回调及 EOF/协议错误结果 |
| 连续性与 30 秒检查 | 已完成 | 检查 `start_sample` 连续性和每包持续时间 |
| 自动化测试 | 已完成 | 11 项测试全部通过，含完整 30 秒合成数据块 |
| Curry 8 真机验证 | 待完成 | 需要连接目标设备获取至少两个真实 EEG 包 |

代码功能已经实现，当前唯一关键缺口是目标 Curry 8 的真实网络数据。真机测试
通过前，不能把“参考协议和合成测试通过”等同于“目标设备已验证”。

## 协议依据与当前边界

协议实现已根据本地 `reference/curry_netstream_protocol/` 中的 Curry Python
参考客户端完成。当前仅保留：

```text
reference/curry_netstream_protocol/
  currydefs.py       包头、消息码和协议结构体
  currystreaming.py  自动握手、DATA 分发和 EEG 排列
  tcpclient.py       TCP 接收与缓存参考
```

`reference/` 只用于本机分析，已由 `.gitignore` 排除，不会提交。重复副本、
旧 C++ COM 工程、离线 MATLAB、GUI、音频及编译产物已移入
`trash/reference_unrelated_2026-09-15/`，同样不会提交；由使用者手动清理。

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
→ 结束时尽力请求停止推流（request=9）
```

## 环境

依赖 Python 3.10+ 和 NumPy。可以用 Conda 创建环境：

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

当前 11 项测试全部通过，覆盖真实 CTRL 抓包回归、协议结构尺寸、基础/通道
信息、交织 EEG 解码、完整 30 秒合成块、自动握手、连续数据包，以及 TCP
帧头半包后发生超时的恢复。

`CurryClient` 的 `timeout` 默认 3 秒；连接后使用短接收轮询超时，短暂静默
不会被判为失败。`stream()` 仍可按原方式忽略返回值；需要生命周期观察时，
可读取 `StreamResult`，并通过 `cancel_event`、`on_status`、`on_session` 和
`on_error` 取得取消、握手和错误信息。`CurryClient.cancel()` 只请求合作式
停止，最终由拥有客户端的线程执行关闭。

## 连接 Curry

在 Curry 中启用 NetStreaming Server，使用未压缩 float32 数据格式，然后运行：

```powershell
python scripts/run_client.py `
  --host <Curry-IP> `
  --port 4455 `
  --max-blocks 2 `
  --expected-seconds 30 `
  --dump capture_2blocks.bin `
  --log-level DEBUG
```

程序会为每个 EEG 包打印：

```text
[block 1] start_sample=... shape=(通道数, 采样点数) sr=...Hz duration=30.000s
```

`--dump` 会保留原始入站字节，便于真机验证失败时复现。用 `--max-blocks 2`
可以在收到两个 EEG 包后自动请求停止推流并退出。

### 真机验收标准

至少连续收到两个数据包，并同时满足：

1. 每包输出 `duration=30.000s`；
2. 数组形状为 `(EEG通道数, 采样率 × 30)`；
3. 通道标签、数量和 Curry 当前配置一致；
4. 后一包的 `start_sample` 等于前一包起点加前一包采样数；
5. EEG 数值有限且量级合理，不出现整块 `NaN/Inf` 或明显字节序错误；
6. `capture_2blocks.bin` 能用于离线复现同样的解码结果。

若输出提示压缩 EEG，请在 Curry NetStreaming Server 中选择未压缩 float32
格式；当前版本明确拒绝 ZIP payload，避免把压缩字节误解为 EEG。

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
reference/curry_netstream_protocol/ 本地协议参考（已忽略）
trash/                            待手动清理文件（已忽略）
```
