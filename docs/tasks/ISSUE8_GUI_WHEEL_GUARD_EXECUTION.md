你是 Issue 8 GUI 滚轮防误触执行线程。

## 目标、依据与边界

在正式工作区 `/Users/xuqinghe/sleep/main-development` 实现[远端 Issue #8：GUI 滚轮防误触](https://github.com/GTOING/curry8-netstream/issues/8)：鼠标滚轮或触控板滚动不得改变界面的选项与数值，但设置页、错误区和只读内容仍可滚动。发布时基线为 `main@9e99a97f31f4103de7efbff768362680580a1485`。开始先核对实际 HEAD、工作区及 Git index；若基线已前进，以当前代码和 Issue #8 的有效要求为准，保留所有既有改动。

重点阅读 `src/sleep_stim_controller/ui.py`、`tests/test_ui.py`、[GUI 合同](../P_GUI_CONTRACT.md)及[范式 GUI 指南](../ISSUE6_PARADIGM_GUI_GUIDE.md)。当前直接受影响的是 Rally 模式 `rally_mode_combo`、真实协议档案 `rally_profile_combo`、刺激策略 `stimulation_strategy_combo` 三个 `QComboBox`，Curry 端口 `port_spin` 与回放块号 `replay_index_spin` 两个 `QSpinBox`。设置容器为 `settings_scroll`；错误区为 `error_scroll`。检查是否还有实际受滚轮改值的控件，并将其纳入同一规则。本任务只改 GUI 交互及相应测试/使用说明，不更改 Rally、Curry、模型、范式、会话记录或回放的数据与控制语义。

## 实施方法

1. 在 UI 层做局部、可复用的滚轮保护，可使用控件子类或定向事件过滤器。**无论控件是否获得焦点、是否悬停，以及普通滚轮、高精度触控板 `pixelDelta`、Ctrl/Shift 修饰键，滚轮都不能改变上述控件的选择、数值或触发由此带来的业务信号。** 仅调整焦点策略不足以满足要求。不要在应用级吞掉全部 `QEvent.Wheel`。
2. 对设置区中关闭的下拉框和数值框，处理滚轮后应使外层 `settings_scroll` 正常滚动；只把控件数值保持不变、却使页面无法滚动，不算完成。注意 Qt 控件、其子对象与滚动区 viewport 的事件路由，避免重复滚动或事件循环。回放块号若无可滚动父容器，只需抑制滚轮改值；不要让它误导航。
3. 打开 `QComboBox` 弹出列表时也不能因滚轮提交新选项；允许弹出列表自身滚动以浏览条目。覆盖 popup/view/viewport 实际接收事件的路径，并保持原选项直至用户显式点击或键盘确认。不能以禁用整个下拉框替代此行为。
4. 保留用户明确的鼠标点击、键盘操作、回放上一块/下一块按钮及程序化设置；这些操作原有的配置校验、启用/禁用状态、实时与回放互斥、Rally arming 和业务信号行为继续成立。不要改变现有信号连接来掩盖滚轮引起的状态改变。
5. 更新适用的 GUI 使用说明，说明滚轮只负责滚动、如何用点击/键盘调节选项和数值。无需改动没有可见布局变化的样式或额外引入全局输入框架。

## 验证与交付

用真正的 Qt `QWheelEvent` 离屏测试，而不只调用自定义处理函数或比较过滤器返回值。覆盖上述三个下拉框和两个数值框、获得/未获得焦点、普通 `angleDelta` 与环境可行时的 `pixelDelta`、至少一种 Ctrl/Shift 修饰情形、打开的下拉列表；逐一核对值不变且滚轮没有发出 `rally_mode_requested`、`rally_profile_requested`、`stimulation_configuration_changed`、`replay_index_requested` 等业务信号，Curry 端口与目标睡眠期保持原值。用足够高的设置内容和非边界初始位置，证明滚轮经过设置控件时外层滚动条实际移动；核对错误区及其他只读可滚动内容仍能滚动。再验证点击、键盘选择、程序化更新及上一块/下一块按钮仍按原语义工作，默认与 `800×600` 布局均可用。尽量覆盖 popup 的真实事件接收位置；若特定 Qt 平台无法自动化模拟，报告实际覆盖和限制，不把未执行的交互判为通过。

执行 `uv sync --locked`、相关测试和最终完整回归、Python 编译及 `git diff --check`。如可用 Windows 原生桌面，人工复核鼠标/触控板在设置区和弹出列表中的行为；否则明确记为未验证，macOS 离屏不代替 Windows 原生证据。无需连接 Curry、Rally、生产 `127.0.0.1:8801`、COM、设备或受试者，不发送真实刺激命令。

将事实和命令结果写入 `reports/ISSUE8_GUI_WHEEL_GUARD_REPORT.md`，注明修改位置、滚轮转发方式、测试覆盖、剩余限制及 Windows 验证情况。若布局没有可见变化，无需为本任务生成静态截图；如实际调整布局，应附 `800×600` 截图。不要修改 `decision_records/ACTIVE_QUEUE.md`、历史报告或主线程验收结论；不要提交、推送、合并、关闭/评论远端 issue，也不要再分发执行任务。完成后回报主线程验收。
