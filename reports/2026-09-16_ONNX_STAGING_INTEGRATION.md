# ONNX 睡眠分期接入报告

日期：2026-09-16。目标平台：Windows。`LiteSleepNet` 仓库在本任务中只读，未修改。

## 结果

控制器已接入单通道 ONNX 睡眠分期。用户可在连接前选择 ONNX 文件和 Curry 通道标签；默认发布模型为 LiteSleepNet EDF20 FP32 6000 点模型。运行时读取实际 ONNX 输入时间维，因此也接受符合合同的 3000、9000 或其他 3000 整数倍模型，不把 Curry TCP 帧数误当作模型输入长度。

预处理合同已实现为：输入按 µV 解释、50 Hz 工频处理、0.3–35 Hz 四阶 Butterworth 带通、100 Hz 输出、不做逐样本 z-score。滤波器跨 30 秒块保留状态；`start_sample` 不连续或采样率变化会显式失败并清空历史。单通道标签要求完整唯一匹配。

默认 6000 点模型使用 `[前一 30 秒, 当前 30 秒]`，首个 epoch 复制自身；输出映射为 `W/N1/N2/N3/REM`，置信度为 logits 的 softmax 最大概率。

## 模型身份与发布

- 控制器模型：`src/sleep_stim_controller/models/litesleepnet_edf20_fp32_6000.onnx`。
- 来源：相邻只读仓库 `LiteSleepNet/artifacts/onnx/benchmarks/edf20/model_fp32_6000.onnx`。
- SHA-256：`E256DEF7BA8454314D8D53B5FF2DB5680CFE5237088A19A2FE59052C20ACF943`；源与副本一致。
- 输入：`input float32 [batch_size, 1, 6000]`。
- 输出：`output float32 [batch_size, 5]`，实测为 logits。
- 本地 wheel 构建成功，并核对 wheel 同时包含 ONNX 和 JSON 身份清单。

## 验证

- ONNX 单元与 UI 配置定向测试：`10 passed`。
- 真实 ONNX GUI 合成回环：Curry TCP 握手为 100 Hz、单通道 `Fpz-Cz`，连续发送两个 30 秒块；两个块均进入真实 ONNX，第二块在 GUI 显示合法期别、置信度与模型 ID。
- 最终完整回归：`97 passed in 12.55s`，无 warning。
- Windows 原生 GUI 探针：Qt 平台 `windows`，窗口可见，1000×720，最小 800×600，事件循环退出 0，控制器与 Rally 后台资源均正常释放。
- `uv sync --locked` 与 wheel 构建通过；新增锁定依赖为 ONNX Runtime、SciPy 及其传递依赖。

## 未完成边界

本轮没有真实 Curry 8 信号，因此尚未核实真机标签是否恰为 `Fpz-Cz`、设备输出是否确实为 µV、真机 `start_sample` 连续性和真实推理准确性。ONNX 工程执行成功不等于模型在该受试者或设备上的分期效果已经成立。真实刺激仍未启用。
