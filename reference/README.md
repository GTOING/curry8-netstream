# 开发参考材料归档

归档日期：2026-09-15。当前工作目录为 `/Users/xuqinghe/sleep`；它尚无根级 `.git`，唯一现有 Git 仓库为其下的 `curry8-netstream/`。本次归档落在工作目录根级，未初始化 Git、提交或推送。

## 来源与目录

| 材料 | 本地归档 | 原始来源 | 用途 |
| --- | --- | --- | --- |
| 电刺激 Python 与设备说明 | [electrostimulation/original](electrostimulation/original/) | `/Users/xuqinghe/exp/ele/无线电刺激定制-华师` | Rally 调参协议、旧调度器、设备版本与接线参考 |
| UI 模板原包及展开文件 | [desktop-ui-template](desktop-ui-template/) | `/Users/xuqinghe/exp/desktop-ui-template-20260915/desktop-ui-template-20260915.zip` | 通用桌面布局、信号与绘图接口 |
| Curry 接入代码 | [现有克隆](../curry8-netstream/README.md) | https://github.com/GTOING/curry8-netstream.git | EEG TCP 收包解码；不再复制一套代码 |

电刺激原件共 8 个：5 个 `RealtimeControlAPIDemo*.py`、`RealtimeControlManager.py`、`curry8networktest.py`、1 个 PPTX。UI 原包和包内 5 个文件均已保存。原件内容不改写，后续实现使用独立业务代码。原件中的示例指令、参数、实验时间线不产生本项目执行授权。

原 PPTX 包含设备网络配置资料，整理文档不抄录连接口令。原件仅做本地保留，本次无外发。

## 阅读入口

- [用户需求整理](../docs/SLEEP_STIM_REQUIREMENTS.md)
- [代码与文档分析](../reports/2026-09-15_reference_review.md)
- [当前任务队列](../decision_records/ACTIVE_QUEUE.md)
