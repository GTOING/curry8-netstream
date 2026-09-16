# UI、Curry 与电刺激参考材料分析

日期：2026-09-15。执行身份：决策主线程。范围：静态阅读、来源归档、开发边界整理。没有运行电刺激脚本或发送设备命令。

## 1. 材料与读取覆盖

- [电刺激原件](../reference/electrostimulation/original/)：全部 7 个 Python 文件；逐项比较 Demo3/4/5 差异。
- [设备说明 PPTX](../reference/electrostimulation/original/便携式经颅电刺激仪-定制开发睡眠.pptx)：读取 31 页文本，并查看第 18、23、24、28、30 页相关嵌入图像，确认参数单位、接线及兼容版本。未在 PowerPoint 中逐页渲染验收。
- [UI 模板](../reference/desktop-ui-template/README.md)：已读 app.py、requirements.txt、smoke_check.py 和预览图；原 ZIP 与展开文件均保留。
- [Curry](../curry8-netstream/README.md)：此前已读全部源码和测试；本次确认归档入口。来源 main，提交 `0d1067c5ea557dac5f15cdd92268e21b6936f83b`。

## 2. Rally 实际接入方式

Demo 脚本仅使用 Python 标准库 json/socket/time，没有 `import rally`。Rally 是外部软件服务，本机缺少的是该软件及设备运行环境，不能据此安装同名 Python 包替代。

脚本发送 UTF-8 UDP 数据到 `127.0.0.1:8801`，报文为 `RealTimeControl`、一个空格、JSON。随后 `recvfrom(1024)` 等待错误码字符串，通过 `errorCode_Message` 映射消息。脚本地址只适用于 Rally 与脚本同机；是否支持远程地址和监听配置仍需目标环境验证。

PPT 第 26–30 页描述定制接口：

- 面向 NSS18 便携刺激器和 Rally；支持 tDCS、tACS 正弦调参，不支持 tRNS/tPCS 调参。
- Rally 接到请求后构造新协议，停止当前刺激，下发新协议，再开始刺激。这会改变正在执行的刺激过程，不能当作无中断参数更新。
- 修改通道须属于当前协议刺激通道子集，返回通道不可修改；文档描述最大 8 刺激通道 + 2 返回通道。
- API 不支持改变当前协议的双盲、伪刺激属性。
- 错误码 `RALLY_ERROR_INVALID_STATUS` 表示设备不处于刺激中，说明调参有前置设备状态要求。
- 第 30 页说明 API 调参时立即停止、手动停止原行为为渐停。截图列出硬件 42、固件 21、Rally `1.01.00.315`，仅是材料中的兼容示例，目标设备版本尚未核实。

### 字段证据

| 字段 | 资料含义 | 单位/边界 |
| --- | --- | --- |
| SD | 刺激总时长 | 第 28 页明确为秒；第 9 页称时长包含渐升渐降 |
| FR / FD | 渐升 / 渐降时长 | 秒，见第 18 页 UI 标识和第 28 页示例 |
| CHS | 待修改通道列表 | 当前协议刺激通道子集 |
| N | 通道名称 | 实际名称匹配规则待核对 |
| T | 类型 | tD / tA |
| A | 电流幅值 | μA，第 28 页；第 16 页说明 tACS 电流指峰值 |
| F | 频率 | Hz |
| P | 相位 | 度 |
| D | 直流偏置 | μA |

tDCS 极性在第 26 页列为支持项，但现有 Demo 未给出对应 JSON 字段或枚举，不能猜测。错误码中相关拼写也应按供应商原值保留。

第 9 页提到总电流 10 mA，第 18 页新增限制标题为 4 mA，显示材料有版本/设备差异。第 20 页称超过 40 分钟提示但可保存。以上均不等于本实验参数或当前设备上限；应待版本和实验要求明确后定合同。

## 3. 示例脚本差异

以下是原代码值，仅用于识别差异，不是本项目推荐配置。各 Demo 均 FR=FD=30，循环发送 50 次，每次收到响应后 sleep(600)，注释写的“5 秒”与代码不符。

| 文件 | SD（秒） | 刺激内容 |
| --- | --- | --- |
| RealtimeControlAPIDemo.py | 12000 | F5/AF3/F1/FC3，tD，各 A=400 |
| RealtimeControlAPIDemo2.py | 14000 | 同四通道，tD，各 A=1 |
| RealtimeControlAPIDemo3.py | 14003 | 前述四通道 tA、A=400、F=10；CZ/P3/P4/POZ tA、A=1、F=0.01 |
| RealtimeControlAPIDemo4.py | 14004 | 前四通道 A=1、F=0.01；后四通道 A=400、F=11，均 tA |
| RealtimeControlAPIDemo5.py | 14005 | 八通道均 tA、A=1、F=0.01 |

Demo3/4/5 的 P、D 都为 0。文件头关于 Fpz/Fp2 等八通道的注释与实际构造的通道不同。

### 关键控制问题

1. Demo5 在管理器中名为“无电流协议”，实际 A=1 μA，不能映射为零电流或停止。
2. 管理器 `_kill()` 只结束 Python 子进程，未向 Rally 发送停止设备命令。进程退出不证明设备停止。
3. API 脚本无接收超时、响应来源校验、请求关联标识；未知错误码直接索引可能抛 KeyError。响应丢失时既无法完成等待，也无法判定命令是否已执行，后续不能直接设计无条件重试。
4. 所有调参请求都可能启动新的刺激过程。50 次循环不是实验授权，也不是睡眠分期控制策略。
5. 未提供独立开始/停止/状态查询接口和刺激发生时间戳。现有成功码不证明实际起止时间已测量。

## 4. 旧管理器与同步

`RealtimeControlManager` 注册 nosleep/sleep/nodian 到 Demo3/4/5。活动时间线为 0、60、80、200、240 秒，其分钟注释与代码不一致；它按固定时间调度，没有读取脑电或睡眠分期。

实现为 threading.Timer + 子进程启动/杀死 + 输出线程，路径依赖 os.getcwd()。切换时 sleep(1)。多个回调和监控线程共享 current_process/is_running，存在状态竞争；stdout/stderr 均 PIPE，但仅消费 stdout，且 readline 阻塞。所谓“UDP正常”只是本地 bind 成功，没有验证 Rally 可达。

后续优化方向（尚未实施）：将报文构造和响应解析抽为适配器，使用明确的设备状态和请求结果，串行处理控制动作，让后台通信与 Qt 主线程分离；停止必须依据实际接口确认。具体策略须由后续合同确定。

PPT 第 23/24 页是 W3/W4 联用 UDP/外触发接线方案，不是 Curry 8 时钟同步协议。管理器 datetime.now() 只用于时间显示，Timer 也没有跨设备时钟校准。当前材料不能证明 Curry 与 Rally 的时间对齐能力；同步继续留待后续指令。

`curry8networktest.py` 是直接连接本机 4455 并打印 recv 字节的旧探测脚本，不包含握手或解码，且顶层导入会触发连接。本次仅按文本读取，后续优先复用正式 Curry 客户端。

## 5. UI 与 Curry 复用边界

UI 提供 CollapsibleSection、PlotPanel、MainWindow；通过 action_requested/configuration_changed 输出请求，set_status/set_hint/set_error/set_details 回填状态。五图是默认展示数量。set_series 在 GUI 线程调用，缓冲、降采样、线程结束和网络由业务层负责。顶部 status_summary 与底部状态栏目前没有跟随 set_status 自动更新，是后续接入细节。

Curry 客户端负责 TCP 自动握手、20 字节大端帧头、未压缩小端 float32 EEG 解码为 [channel, sample]。尚无真实 EEG 包随仓库提供。30 秒检查只在 CLI 警告，通道映射、单位、额外缩放及目标配置仍需证据。连续性异常会报错；持续超时会继续等待，接收断连可能仍以成功退出码结束。

## 6. 环境与完成状态

现有 uv 环境：`/Users/xuqinghe/sleep/.venv`，Python 3.11.15、NumPy 2.4.6、pytest 9.1.1、PySide6 6.11.2、PyQtGraph 0.14.0，Curry 可编辑安装。前一轮已验证导入和 UI 离屏窗口/曲线渲染。本轮不重复测试，不安装 Rally，不运行刺激代码。

归档原件采用内容相同的复制，保留所有版本；UI ZIP 保留并展开；原目录不变。文档链接及复制一致性在收尾检查。根工作目录目前无 Git 元数据，归档是本地文件可用，不代表已 Git 提交或远端保存。

后续唯一当前动作：等待用户开发说明。需求见 [用户需求整理](../docs/SLEEP_STIM_REQUIREMENTS.md)，动态状态见 [任务队列](../decision_records/ACTIVE_QUEUE.md)。
