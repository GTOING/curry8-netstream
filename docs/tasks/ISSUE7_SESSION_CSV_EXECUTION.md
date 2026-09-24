你是 Issue 7 会话分期 CSV 与本机时间执行线程。

## 目标、依据与工作区

在正式工作区 `/Users/xuqinghe/sleep/main-development` 实现 [远端 Issue #7](https://github.com/GTOING/curry8-netstream/issues/7)：GUI 可选自动生成 `stage_labels.csv`，同一实时 session 按处理结果逐行续写；保存窗口接收时的本机时间；已结束会话可从权威记录重新导出。开始先核对 HEAD、dirty 和 Git index：发布基线为 `main@cf88643aad64c32da698d726b7888cc3d91c72e1`，主线程在 `decision_records/ACTIVE_QUEUE.md` 已有未提交改动，必须保留。若 HEAD 已更新，以实际代码和 Issue #7 核对差异，不覆盖现有工作。

必读：[Issue #7](https://github.com/GTOING/curry8-netstream/issues/7)、[P2 会话合同](../P2_STAGING_RECORDING_CONTRACT.md)、[README 记录说明](../../README.md)。重点复用 `controller.py::_accept_block`、`staging.py::ProcessingPipeline`、`recording.py::SessionWriter/SessionReader`、`ui.py` 的会话记录与回放区及现有测试。Issue #7 规定本轮新增能力；P2 的原始数据、事件顺序、会话唯一性和只读回放边界继续有效。只实施 #7，不顺带实施 #8 滚轮改动或改变 Rally/范式语义。

## 实施范围与关键方法

1. **GUI 与会话配置**：在“会话记录与离线回放”增加“同时自动生成分期 CSV”选项，默认关闭，只有启用会话记录时可选；连接后本 session 的选择锁定。路径显示为本次 `session_<id>/stage_labels.csv`，显示导出进度/失败。记录未启用时不得创建 CSV。新连接始终是新 session、新文件，不向旧会话追加。已结束会话在回放入口提供“导出/重新生成 CSV”；实时会话或写者尚未退出时禁止重建。
2. **本机时间**：在 `_accept_block` 接收最后贡献网络包的边界，只读取一次带本机时区的墙上时间，由同一瞬间派生 `received_utc` 和含 UTC 偏移的 `window_received_local_iso`；现有 monotonic 时间照常独立采集。一个包完成多个窗口时可共享该接收时刻。以向后兼容的可选字段传至 `BlockContext`、`block_saved` 等权威记录，不改动旧 v1 文件。此时间不是 EEG 精确采集时间，也不是推理完成、CSV 写入或物理刺激时间。旧会话缺原始本机时间时，重建 CSV 的本机时间留空并标明来源不可用；保留已有 UTC，不用重建时的电脑时区冒充当时本机时区。
3. **单写者增量 CSV**：复用 `SessionWriter` 所在线程。每个已接纳且写入 `processing_result` 的 30 秒窗口按 `block_id` 写一行；先持久化原有 JSONL 结果，再追加/flush CSV，不另建竞争写者。只在会话开始时写一次稳定表头；每块不得重建或覆盖整个文件。同一块至多一行。文件采用标准 CSV 转义、UTF-8 BOM，便于 Windows Excel 打开。列至少包含 Issue #7 指定的 session/block、原始采样范围/采样率、相对首个完整窗口起点的秒数、带偏移本机接收时间与 UTC、处理状态、stage/confidence、原因、模型标识；加一列区分本机时间为采集时保存还是旧会话缺失。`end_sample_exclusive` 为排他边界；失败、取消、NoModel 均保留真实状态行，但 `stage`/无真实输出的 `confidence` 为空，不生成伪标签。
4. **失败与重建**：CSV 是 JSONL/NPY 的派生表。自动 CSV 在建会话时无法创建，应在开始网络采集前明确失败；采集中 CSV 追加失败应显示具体导出失败/不完整状态，保留仍可写入的权威记录，不谎称 CSV 已保存，也不能阻止必要 Rally Stop 或控制事件收尾。关闭时释放 CSV 句柄。手动导出以已验证的 `block_saved`/`processing_result` 有效前缀为源，在同目录写临时 CSV 后原子替换，不修改原始 JSONL/NPY；覆盖半行、缺行、重复行、旧会话和不完整会话，重建结果行数与可验证前缀一致。不要自动重新打开旧 session 继续实时采集。
5. **文档**：更新 README、Windows 操作说明与相关会话/GUI 使用文档，解释开关、输出路径、各时间列、旧会话重建和错误状态；明确本机接收时间不能作为 Curry/Rally 同步或硬件输出时间。历史报告只读；不要修改共享队列或主线程验收记录。

## 验证与交付

测试应证明实际可观察行为，而非只比较格式化函数：合成短包 TCP 跨窗与一包多窗、NoModel 和受控成功/失败模型，首个处理结果尚在实时采集时 CSV 已有首行，后续在**同一文件**续写；CSV 行与 JSONL、NPY 的块号、样本区间、状态一致。用可控时钟与非零起始采样点核对本机 ISO 时间和 UTC 指向同一瞬间、偏移及相对秒数。覆盖用户断开、自然 EOF、取消、模型/记录/CSV 写入失败、重连新 session、半行重建及旧 v1 会话；证明 CSV 故障不会掩盖必要 Stop 收尾。离屏检查 GUI 开关、路径、失败提示和 800×600 可用性；如有 Windows 原生与 Excel 可用环境，核对实际打开、中文和时间列，否则明确列为未验证，不以 macOS 离屏代替。

执行 `uv sync --locked`、针对性与最终完整回归、Python 编译及 `git diff --check`；验证只用合成数据和随机本机假端，不访问生产 `127.0.0.1:8801`、真实 Curry/Rally、COM、设备或受试者。报告写 `reports/ISSUE7_SESSION_CSV_REPORT.md`，列出实际设计选择、文件/字段、增量与重建证据、故障行为、命令结果和未验证项；如改动 GUI，附 `reports/ISSUE7_SESSION_CSV_800X600.png`。保留所有既有 dirty/untracked 与 Git index；本轮不提交、推送、合并、评论远端 issue 或另发执行任务。完成后回报供主线程验收。
