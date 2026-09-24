# Issue 8 主线程工程验收

日期：2026-09-24。结论：**Issue 8 GUI 滚轮防误触工程范围验收通过**。远端 Issue #8 仍为 open，本次未提交或推送。

主线程复核了[执行报告](ISSUE8_GUI_WHEEL_GUARD_REPORT.md)、`ui.py` 的 `_WheelScrollGuard`、UI 测试及 README/Windows 操作说明。保护器只注册到 Rally 模式、真实档案、刺激策略、Curry 端口和回放块号五个控件及其子对象；关闭控件的滚轮改为移动设置区滚动条，弹出列表的滚轮只移动列表滚动条。没有全局拦截，也没有改变原有业务信号连接或 Rally/会话逻辑。

测试使用真实 `QWheelEvent` 覆盖五个控件、焦点状态、`angleDelta`/`pixelDelta`、修饰键，以及弹出列表的 viewport/view/window。断言设置区实际滚动、选项和数值不变、相关业务信号不发出；同时验证错误区和只读诊断可滚动，显式点击、键盘、程序化更新及回放上一块/下一块仍有效。执行者报告 `uv sync --locked`、UI **10 passed**、完整回归 **248 passed**、编译和 `git diff --check` 通过。主线程未重复运行全量测试；本轮实际 `git diff --check` 通过。

该结论限于软件工程和离屏合成事件证据。Windows 原生鼠标/触控板，尤其是实际 Qt popup 事件路由，仍待现场复核；真实 Curry、Rally 和设备未连接。此处不将离屏结果写成 Windows 原生验收。
