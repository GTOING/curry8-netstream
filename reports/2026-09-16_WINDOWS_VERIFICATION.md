# Windows 安装、原生 GUI、路径权限与回环验证

日期：2026-09-16。平台：Windows。范围仅包括本机软件安装、原生窗口、当前工作区路径和本应用回环；未连接 Curry 或 Rally 真机，未启用真实刺激，模型保持 `NoModel`。

## 结论

本项目在当前 Windows 主机完成锁定安装、导入、原生 Qt 窗口启动/关闭、中文与空格路径会话写入/读回，以及 Curry TCP 与 Rally UDP 合成回环验证。软件工程验证通过；这不构成真实设备、真实刺激、同步精度或模型效果证据。

## 安装与运行时

- `uv 0.11.15`。
- `uv sync --locked` 成功，创建 `.venv` 并安装 13 个锁定包。
- Python：CPython 3.11.15，64 位 Windows。
- 关键导入通过：`sleep_stim_controller`、`curry_netstream`、PySide6 6.11.2、NumPy 2.4.6。
- uv 因用户缓存与 OneDrive 工作区之间不能硬链接而自动退回复制；这是性能提示，不是安装失败。

## 原生 GUI

标准命令 `uv run --locked sleep-stim-controller` 实际创建唯一窗口“睡眠分期与刺激控制台”。Windows 窗口捕获工具能枚举到窗口，但截图连续两次因系统接口 `SetIsBorderRequired failed: 不支持此接口 (0x80004002)` 失败，因此本轮没有可用的原生截图；未把该工具错误计为应用错误。

随后使用不设置 `QT_QPA_PLATFORM` 的原生 Qt 探针启动同一 `build_application()`，由窗口自身触发正常关闭，结果：

- `qt_platform=windows`，`QT_QPA_PLATFORM` 未设置；
- 两个可用屏幕，主屏为 `S2416Q`；
- 窗口可见，默认 1000×720，最小 800×600；
- Qt 事件循环退出码 0；
- `controller.resources_released()` 为真；
- Rally 后台运行时在 3 秒内完成 join；
- 结束后未发现标题为“睡眠分期与刺激控制台”的残留窗口进程。

首次交互式启动在截图工具失败后用 Ctrl+C 清理，退出码 1；它不作为正常关闭证据，正常关闭结论来自随后退出码 0 的原生探针。

## 路径权限

在 Git 已忽略的 `outputs/windows-verification/中文 路径权限/` 下，通过正式 `SessionWriter` 创建会话，写入 `manifest.json`、`events.jsonl` 和一个 `(2, 30)` float32 数据块，再由 `SessionReader` 完整读回。中文标签“通道 A / 通道 B”、形状、样本区间和 `unavailable` 处理结果均通过断言，会话状态为 `closed` 且未标记不完整。

第一次探索性会话只写入块、没有写入 `processing_result`，读取器按合同将其标记为不完整；这验证了不完整性识别，不是路径权限失败。第二个完整会话通过全部断言。

本结论只覆盖当前用户、当前 OneDrive 工作区及其 `outputs/` 子目录，不覆盖只读目录、UAC 保护目录、网络共享或其他账户权限。

## 回归与回环

完整回归：

```text
89 passed in 11.53s
```

随后逐项复跑三个关键回环用例：

- Curry 合成 TCP 握手、两个连续块和有界界面交付；
- P3 从实时合成数据、测试分期、UDP 模拟 Rally、会话记录到只读回放及关闭；
- 默认 `NoModel` 即使配置完整也绝不发送刺激请求。

结果：`3 passed in 1.38s`。

## 剩余边界

- 未验证真实 Curry 8 的通道、采样率、单位、连续数据和设备端状态。
- 未验证真实 Rally 版本、地址、报文、开始/停止语义、独立输出观察或紧急停止路径。
- 未验证跨设备时钟映射与同步误差。
- 模型仍为 `NoModel`，未验证模型加载、预处理、推理速度或准确性。
- 原生 GUI 已取得程序化可见性、尺寸与正常关闭证据，但受 Windows 捕获接口错误影响，本轮没有人工可审阅截图。
