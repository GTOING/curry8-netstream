# PR3 Rally 参考工具修复事实报告

日期：2026-09-21

## 检出与基线

- 仓库：`https://github.com/GTOING/curry8-netstream`
- 独立工作区：`/Users/xuqinghe/sleep/pr3-rally-reference-repair-20260921`
- 分支：`codex/rally-reference-code`
- 实际 HEAD：`341b1d67bda56f16de893c70a7095df7ce921c7b`。开工时远端分支与 PR #3 head 均为该提交；修复保留此基线，没有回退或更新提交。
- 原有 `/Users/xuqinghe/sleep/curry8-netstream` 是带未提交 README/源码修改的 `main` 检出；旧临时目录不是有效 Git 仓库。均未复用或修改。

## 修改文件

- `reference/RallyStartStopTest.py`：UDP cycle 在 START 的发送边界固定 `monotonic` 截止；回复等待使用 `min(--timeout, 剩余 cycle 预算)`，忽略其他来源不重置截止；成功回复只等待剩余预算，拒绝/未知/超时/异常不再额外等待，均进入一次 STOP。START/STOP 超时参数和 cycle duration 在 socket 创建前校验有限性及范围。
- `reference/SerialStartStopTest.py`：START 写入前设定 cycle 截止，并将剩余时限传给同步写入；`COMMTIMEOUTS` 写超时随预算设置，移除无写超时保障的 `FlushFileBuffers`。读取循环每轮按剩余时间收紧 `ReadFile` 超时。任意 START 字节都不代表启动确认，收到字节即进入保护性 STOP；没有字节时最多等待读取预算与 START 剩余预算中的较短者。STOP 使用独立 1 秒写超时及独立 `--read-seconds` 等待，始终报告停止未确认。
- `reference/START_STOP_TESTING.md`：记录软件预算起点、发送/回复时间线、停止确认语义、退出码和硬件/驱动限制。
- `tests/test_rally_reference_tools.py`：新增 53 项假 socket、假串口/Win32 与可控 monotonic 时钟测试，不打开真实 COM 口或发送 UDP。
- `reports/PR3_RALLY_REFERENCE_REPAIR_REPORT.md`：本事实报告。

## 行为核对

- UDP 示例：`duration=2` 秒、START 成功回复在 1.5 秒到达，只等待约 0.5 秒后发起 STOP；其他来源回复不会重置这 2 秒截止。若 START 拒绝、未知、异常或在截止前未收到回复，立即进入 `finally` 尝试一次 STOP。STOP 继续使用独立 `--timeout`。
- 串口示例：`duration=2`、`read-seconds=10` 时，START 写入和读取都按同一个 2 秒截止计算剩余超时，不会再人为串联成“10 秒读取 + 2 秒运行”；任意字节（包括错误帧或回显）到达就立即发起 STOP。STOP 写入/读取另有独立有限预算，但收到任何字节仍不能宣称命令或物理停止已确认；实际 Win32 调用和驱动调度不构成硬件时限保证。
- 串口 `stop`/`cycle` 正常执行后统一返回 3，因为确认帧语义未知；`list`/`dry-run` 正常返回 0。调用异常保留非零错误码并提示状态未确认；cycle 的 START 写入/读取发生异常时仍在 `finally` 尝试 STOP，STOP 异常不会遮蔽原始 START 错误。
- Win32 写入使用现有 `SetCommTimeouts`/`WriteFile`，未引入新串口 API或大型依赖。Microsoft 文档说明 communications handle 上的 `FlushFileBuffers` 不受 write timeout 约束，且会等待待发送写操作完成；`COMMTIMEOUTS` 用 multiplier/constant 限制同步读写。参考：[Read and Write Operations](https://learn.microsoft.com/en-us/windows/win32/devio/read-and-write-operations)、[COMMTIMEOUTS](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-commtimeouts)。

## 命令与实际结果

- `/Users/xuqinghe/sleep/.venv/bin/pytest -q tests/test_rally_reference_tools.py`：`53 passed`。
- `/Users/xuqinghe/sleep/.venv/bin/python reference/RallyStartStopTest.py dry-run`：退出码 0，输出明确为 no UDP datagram sent。
- `/Users/xuqinghe/sleep/.venv/bin/python reference/SerialStartStopTest.py dry-run --port COM4`：退出码 0，输出明确为 COM port 未打开且未发送帧。
- `git diff --check`：通过。
- 将新增测试与既有 `tests/test_rally.py` 一起运行：新增 53 项通过；既有 9 项因沙箱拒绝绑定随机本机 UDP 端口（`PermissionError: Operation not permitted`）失败。这些既有应用层模拟器测试不在参考脚本改动路径内；未尝试绕过该限制。系统 `python3` 未安装 pytest，测试改用已存在的项目 `.venv`，未安装依赖。

## 未执行与平台限制

当前执行环境为 macOS。Win32 行为由假接口覆盖，未在 Windows 驱动/真实 COM 口上验证；未连接 Curry、Rally 或设备，未发真实 START/STOP，也未验证物理输出。毫秒级 `COMMTIMEOUTS`、操作系统调度、驱动、Rally 与设备处理均意味着这些只是软件等待/写入预算，不是“到时硬件必已停止”的保证。未修改 `RealtimeControlAPIDemo`/`Manager`、PR #2 或共享任务队列；未提交、推送、合并或评论 PR。
