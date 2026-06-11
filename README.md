# Curry 8 NetStreaming —— Python 调试客户端骨架

用外部 Python 程序，通过 Curry 8 的 **NetStreaming (TCP/IP)** 接口：
1. 获取实时 EEG 数据流；
2. 发送远程控制指令（开始/停止录制、阻抗检测）。

> 本仓库是**可调试的骨架**，重点在清晰的模块边界与可单测的协议层，
> 而非一个开箱即连真机的成品。

---

## ⚠️ 最重要的前提：精确协议待校准

`raw float` 数据包的**精确字节布局，CURRY 8 User Guide 里没有给**。
手册 p.254 / p.256 明确要求向 **curry8help@neuroscan.com** 索取
可运行的 **C++ / MATLAB demo**（内含协议规范）。

因此 [`src/curry_netstream/protocol.py`](src/curry_netstream/protocol.py) 里的
帧头结构、字段顺序、消息码全部是**合理假设 (PLACEHOLDER)**。
拿到官方 demo 后，**通常只需改这一个文件**，其余模块无需改动。

---

## 环境

项目内已创建 conda **prefix 环境**：`./.conda`（Python 3.11 + numpy + pytest）。

```bash
# 重新创建（如需要）
conda create -p ./.conda python=3.11 numpy pytest
# 或用具名环境
conda env create -f environment.yml      # 名为 curry8

# 直接用 prefix 环境里的解释器
./.conda/bin/python --version
```

VS Code：打开本文件夹后，Python 解释器选 `./.conda/bin/python`，
按 F5 即可用 `.vscode/launch.json` 里的两个配置断点调试。

---

## 目录结构

```
src/curry_netstream/
  protocol.py     ★唯一校准点：帧头 / 消息码 / encode·decode
  models.py       DataBlock / Event / SessionInfo（对应 indat/inlabels/...）
  client.py       CurryClient：TCP 连接、收包重组、分发、发控制指令
  logging_util.py 日志 + hexdump
scripts/
  run_client.py   CLI 入口
tests/
  test_protocol.py 用构造字节测协议层（无需硬件即可调试）
```

数据模型与手册变量名的对应（User Guide p.253）：

| 手册变量 | 本项目 |
|---|---|
| `indat` 波形 | `DataBlock.data`（numpy, `[n_ch, n_samp]`）|
| `inlabels` 通道标签 | `SessionInfo.labels` |
| `insampleratehz` 采样率 | `SessionInfo.sample_rate_hz` |
| `instartsample` 起始采样 | `FrameHeader.sample` / `DataBlock.start_sample` |
| `inevents` 事件 | `DataBlock.events` |

---

## 用法

### 1. 跑单元测试（不需要 Curry / 硬件）

```bash
./.conda/bin/python -m pytest
```

这是「只做客户端」时的主要调试路径：在 `tests/test_protocol.py` 打断点，
逐步检查帧头打包/解包、控制指令编码、float32 整形等逻辑。

### 2. 连接真实 Curry Server

在 Curry：`Acquisition → Amplifier Control → NetStreaming` 把本机设为
**Server**（非压缩格式、固定端口），并勾选 *Allow Client to control amplifier*。

```bash
# 接收并打印数据块
./.conda/bin/python scripts/run_client.py --host <CurryIP> --port <端口>

# DEBUG 级 + 原始字节落盘（逆向协议时极有用）
./.conda/bin/python scripts/run_client.py --host <CurryIP> --port <端口> \
    --log-level DEBUG --dump capture.bin --max-blocks 20

# 演示控制流：连上先发阻抗检测，收满 10 块后发停止录制
./.conda/bin/python scripts/run_client.py --host <CurryIP> --port <端口> \
    --send-impedance --stop-after 10
```

---

## 校准协议的步骤（拿到官方 demo 后）

1. 用 `--dump capture.bin` 抓真实字节流；
2. 对照 C++ demo，确认帧头字段顺序/长度、消息码取值、payload 是否含事件/是否压缩；
3. 只改 `protocol.py` 的 `HEADER_FORMAT` / `MessageCode` / `SAMPLE_DTYPE`，
   以及 `client.py` 里两个 `_handle_*` 的 PLACEHOLDER 解析；
4. 跑 `pytest` 确认协议层仍自洽。

---

## 参考

- CURRY 8 User Guide p.252–257《14.2.1.1 Configure as NetStreaming Server or Client》
- 事件码表：User Guide p.553 起
- 触发/TTL（硬件打标记）：User Guide 附录 A，p.984–989
