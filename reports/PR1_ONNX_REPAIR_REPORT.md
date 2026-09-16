# PR #1 ONNX 修复执行报告

日期：2026-09-16。目标分支：`codex/onnx-sleep-staging`。PR 基准为 `09f62c4f74fbdf5064682c9ebd7c0772d81c4340`，父提交/base 为 `6289e1e81b695695e2a381cbf36cadc7a0d7971b`；只读 `git ls-remote` 已核对远端 PR head、目标分支和 main 均与任务指定 SHA 一致。本地修复在该 PR 提交上实施；未创建修复提交，未合并、推送或发送 GitHub 评论。

## 修复内容

- 在现有 `ModelDescriptor` / P2 单写者记录中增加向后兼容的可选 `configuration`。ONNX 描述记录完整模型 SHA-256、所选标签与匹配规则、从实际输入解析的标签/索引、输出标签映射、预处理版本和参数，以及实际 source rate/notch 状态。输入点数和上下文 epoch 只在 ONNX `prepare` 成功后写入；准备状态与失败原因保留在 manifest 和结果事件。运行中首次解析到实际通道后，pipeline 会在第一条 `processing_result` 前更新同一模型描述。
- 预处理器对样本断点、采样率变化发出明确的 `PreprocessingDiscontinuity`。发生断点的块仍失败，同时清空滤波器与 ONNX 历史；后续块按新连续段填充历史。有效 EEG epoch 在普通 ONNX 推理失败后仍可作为后续上下文，不把推理错误误判成采样中断。
- 默认应用走 `NoModel`。模型选择框默认关闭，路径初始为空；关闭时不校验或加载模型，缺失模型不会阻塞连接、采集、记录或回放。仅在用户连接前显式启用并选择 ONNX 后校验/创建适配器；配置错误明确显示，不静默回退。连接或回放中模型选项不可切换；传入的测试/依赖注入工厂仍优先保留。刺激自动决策仍默认关闭。
- 更新 ONNX 合同与 README 的默认行为、记录快照和连续段说明；未改 P1/P2/P3 历史合同、报告、模型权重、研究参数或共享队列。

记录示例（100 Hz 合成输入、6000 点模型；通道值来自实际 block 标签解析）：

```json
{
  "model_content_identity": {
    "algorithm": "sha256",
    "digest": "E256DEF7BA8454314D8D53B5FF2DB5680CFE5237088A19A2FE59052C20ACF943"
  },
  "channel_selection": {
    "requested_label": "Fpz-Cz",
    "match_rule": "trim_whitespace_and_casefold_then_exact_unique_match",
    "resolved_label": "Fpz-Cz",
    "resolved_index": 0,
    "resolved_from_block_id": 1
  },
  "input_points": 6000,
  "context_epochs": 2,
  "label_mapping": [
    {"index": 0, "label": "W"},
    {"index": 1, "label": "N1"},
    {"index": 2, "label": "N2"},
    {"index": 3, "label": "N3"},
    {"index": 4, "label": "REM"}
  ],
  "preprocessing": {
    "implementation": "StreamingEegPreprocessor",
    "version": "onnx-eeg-preprocess-v1",
    "input_units": "µV",
    "notch": {"frequency_hz": 50.0, "q": 30.0, "applied_to_observed_input": false},
    "bandpass": {"family": "Butterworth", "order": 4, "low_hz": 0.3, "high_hz": 35.0},
    "target_sample_rate_hz": 100.0,
    "normalization": "none",
    "first_epoch_padding": "repeat_earliest_available_epoch_in_segment",
    "observed_source_sample_rate_hz": 100.0
  },
  "preparation": {"status": "prepared", "error": null}
}
```

准备失败记录保留 `input_points: null` / `context_epochs: null` 及具体错误，不宣称模型已准备成功。v1 reader 对不含可选配置字段的旧 NoModel/替身描述保持兼容。

## 验证

环境：macOS、Python 3.11.15、uv 0.12.1、离屏 Qt。任务要求的 Windows 原生命令不能在此环境代跑。

- `UV_CACHE_DIR=/private/tmp/sleep-uv-cache uv sync --locked`：通过。沙箱内首次执行因网络/缓存权限失败；经授权后在 PR 临时克隆内同步锁定依赖成功，没有改动主目录虚拟环境或锁文件。
- `QT_QPA_PLATFORM=offscreen uv run --locked pytest tests/test_onnx_staging.py tests/test_p2_recording.py tests/test_ui.py tests/test_controller_synthetic.py`：`58 passed`。
- `QT_QPA_PLATFORM=offscreen uv run --locked pytest`：最终回归 `107 passed in 11.14s`，覆盖 P1/P2/P3、Curry 与 ONNX。
- ONNX 适配器测试捕获 6000/9000 点实际模型输入：样本断点、采样率变化后不含旧段 epoch，恢复窗口仅含新段数据；独立测试确认普通推理失败不清空有效 EEG 上下文。配置测试验证 C3/C4 的可读快照差异、6000 点/2 epoch 持久化、准备失败信息及旧 v1 记录读取。
- 合成 UI/Curry 测试验证默认缺失模型路径仍完成两块记录、结果均 `unavailable`、模拟器零请求；显式启用随包 ONNX 的 GUI 合成回环产生合法推理结果。启动/关闭与模型控件连接期间锁定测试通过。
- 已生成离屏截图：[默认 NoModel](PR1_ONNX_NO_MODEL.png)、[显式 ONNX 配置](PR1_ONNX_EXPLICIT_MODEL.png)。原生窗口未验证：当前环境沿用已记录的零屏幕限制，本轮未重复尝试原生 GUI 启动。

本轮仅证明合成数据上的工程路径与配置持久化；没有执行 Windows 验证、真实 Curry/Rally、受试者实验或科研有效性判断。
