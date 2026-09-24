# Issue 8 GUI 滚轮防误触执行报告

日期：2026-09-24。已在 `main` 的基线 `9e99a97f31f4103de7efbff768362680580a1485` 完成实现和离屏验证。开始时 Git index 为空；既有 `decision_records/ACTIVE_QUEUE.md` 工作区改动已保留且未修改。远端 Issue 页面在当前浏览环境返回 Cache miss，本轮依据本地限界任务和 GUI 合同执行。

## 实现

- 在 `src/sleep_stim_controller/ui.py` 增加局部 `_WheelScrollGuard`，只注册到 Rally 模式、Rally 档案、刺激策略、Curry 端口和回放块号控件及其子对象；没有应用级拦截。
- 闭合下拉框/数值框收到滚轮时，按 `pixelDelta` 或 `angleDelta` 定向调整外层 `settings_scroll`，事件被消费一次，选项/数值及其业务信号不变。焦点状态与 Ctrl/Shift 修饰键不改变该规则。
- 下拉 popup 的 view、viewport、popup window 和相关子对象均受定向过滤；滚轮只调整 popup 滚动条，不改变选中项。用户仍可点击列表、使用键盘或通过程序化更新改变选项。
- 错误标签收到滚轮时显式转发到所属 `error_scroll`；只读诊断文本使用自身 viewport 滚动。回放块号仍能通过键盘、程序化回放更新及上一块/下一块按钮改变。
- 更新 [README](../README.md) 和 [Windows 配置说明](../docs/WINDOWS_SETUP.md) 的操作指引。未改布局，因此未生成静态截图。

## 验证

- `UV_CACHE_DIR=/private/tmp/issue8-uv-cache uv sync --locked`：通过。
- `QT_QPA_PLATFORM=offscreen UV_CACHE_DIR=/private/tmp/issue8-uv-cache uv run --locked pytest -q tests/test_ui.py -k 'wheel'`：**2 passed**（8 deselected）。测试使用真实 Qt `QWheelEvent`，覆盖三个下拉框、两个 spinbox、焦点/无焦点、`angleDelta`、`pixelDelta`、Ctrl/Shift、popup 的 viewport/view/window 接收路径、值与业务信号、设置滚动条实际移动，以及错误区和只读诊断滚动。
- `QT_QPA_PLATFORM=offscreen UV_CACHE_DIR=/private/tmp/issue8-uv-cache uv run --locked pytest -q tests/test_ui.py`：**10 passed**（1.14 秒）。另外检查点击 popup 项、键盘选择、Rally 程序化更新、端口键盘调整、回放程序化更新及上一块/下一块按钮。
- `QT_QPA_PLATFORM=offscreen UV_CACHE_DIR=/private/tmp/issue8-uv-cache uv run --locked pytest -q`：最终完整回归 **248 passed**（25.79 秒）。
- `UV_CACHE_DIR=/private/tmp/issue8-uv-cache uv run --locked python -m compileall -q src/sleep_stim_controller tests`：通过；`git diff --check`：通过。
- 离屏测试检查默认 1000×720 与 800×600 窗口状态。未调整可见布局。

## 未验证与边界

- 当前环境未进行 Windows 原生桌面鼠标滚轮或触控板人工复核；离屏合成事件不能代替 Windows 原生证据。
- 未连接 Curry、Rally、生产 `127.0.0.1:8801`、COM、刺激设备或受试者，也未发送真实刺激命令。
- 未修改共享队列、GUI 合同或历史报告；未提交、推送或操作远端 issue。
