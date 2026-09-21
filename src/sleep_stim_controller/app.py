"""Executable P2 desktop application wiring."""
from __future__ import annotations

import sys
from collections.abc import Callable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QFileDialog

from .controller import CurrySessionController
from .onnx_staging import OnnxSleepStagingAdapter
from .replay import SessionReplayWorker
from .rally import LoopbackRallySimulator, RallyControlEndpoint, RallyControlTransportWorker
from .staging import ModelAdapter, NoModelAdapter
from .stimulation_runtime import StimulationRuntime
from .ui import MainWindow


def pump_latest(
    controller: CurrySessionController,
    window: MainWindow,
) -> bool:
    """Deliver one bounded item to the GUI, preserving terminal history."""
    result = controller.take_latest_block()
    if result is None:
        return False
    queued, stats = result
    window.show_block(
        queued.block,
        queued.number,
        stats.replaced_count,
        queued.historical,
        queued.history_reason,
    )
    window.set_diagnostics(controller.diagnostics_text())
    return True


def build_application(
    *,
    model_factory: Callable[[], ModelAdapter] | None = None,
    simulator_factory: Callable[[], LoopbackRallySimulator] = LoopbackRallySimulator,
    request_timeout_seconds: float = 1.0,
    rally_control_endpoint: RallyControlEndpoint | None = None,
    real_transport_factory: Callable[..., RallyControlTransportWorker] | None = None,
) -> tuple[QApplication, MainWindow, CurrySessionController]:
    application = QApplication.instance()
    if application is None:
        application = QApplication(sys.argv)
    window = MainWindow()
    stimulation = StimulationRuntime(
        window,
        request_timeout_seconds=request_timeout_seconds,
        simulator_factory=simulator_factory,
        **(
            {"rally_control_endpoint": rally_control_endpoint}
            if rally_control_endpoint is not None
            else {}
        ),
        **(
            {"real_transport_factory": real_transport_factory}
            if real_transport_factory is not None
            else {}
        ),
    )
    controller = CurrySessionController(
        model_factory=(model_factory if model_factory is not None else NoModelAdapter),
        stimulation_runtime=stimulation,
    )
    replay = SessionReplayWorker(window)
    replay_state: dict[str, object] = {"overview": None}

    def connect_live_session() -> None:
        session_model_factory = model_factory
        if session_model_factory is None:
            if window.model_enabled_checkbox.isChecked():
                try:
                    model_config = window.model_configuration()
                    session_model_factory = lambda config=model_config: (
                        OnnxSleepStagingAdapter(
                            model_path=config["model_path"],
                            channel_name=config["channel_name"],
                        )
                    )
                except (OSError, TypeError, ValueError) as exc:
                    window.set_error(f"模型配置无效：{exc}")
                    return
            else:
                session_model_factory = NoModelAdapter
        controller.connect(
            **window.configuration(),
            model_factory=session_model_factory,
        )

    window.connect_requested.connect(connect_live_session)
    window.disconnect_requested.connect(controller.disconnect)
    window.closing_requested.connect(controller.disconnect)
    window.closing_requested.connect(replay.close)
    window.closing_requested.connect(stimulation.shutdown)
    controller.state_changed.connect(window.set_state)
    controller.busy_changed.connect(window.set_busy)
    controller.error_changed.connect(window.set_error)
    controller.session_changed.connect(window.set_session)
    def begin_live_session() -> None:
        window.set_live_stimulation_session(controller.session_id)
        window.begin_session()

    controller.session_started.connect(begin_live_session)
    controller.session_finished.connect(window.mark_session_finished)
    controller.diagnostics_changed.connect(window.set_diagnostics)
    controller.assembly_progress_changed.connect(window.set_assembly_progress)
    controller.processing_changed.connect(window.set_processing_result)
    controller.recording_changed.connect(window.set_recording_status)
    stimulation.simulator_status_changed.connect(window.set_simulator_status)
    stimulation.automatic_status_changed.connect(window.set_stimulation_auto_status)
    stimulation.rally_status_changed.connect(window.set_rally_status)
    stimulation.decision_changed.connect(window.set_stimulation_decision)
    stimulation.request_changed.connect(window.set_stimulation_request)
    stimulation.activity_changed.connect(window.set_p3_background_busy)

    def apply_stimulation_configuration(config) -> None:
        try:
            stimulation.configure(config)
        except (TypeError, ValueError) as exc:
            window.set_stimulation_auto_status(
                stimulation.automatic_enabled,
                f"配置未应用：{exc}",
            )

    def start_simulator() -> None:
        try:
            stimulation.start_simulator()
            window.set_error("")
        except Exception as exc:
            window.set_error(f"无法启动本机模拟端：{exc}")

    def update_request_timeout(seconds: float) -> None:
        try:
            stimulation.set_request_timeout(seconds)
            window.set_error("")
        except (RuntimeError, TypeError, ValueError) as exc:
            window.request_timeout_edit.setText(
                f"{stimulation.request_timeout_seconds:g}"
            )
            window.set_error(f"未更改模拟通信超时：{exc}")

    window.stimulation_configuration_changed.connect(
        apply_stimulation_configuration
    )
    window.stimulation_auto_requested.connect(
        stimulation.set_automatic_enabled
    )
    window.simulator_start_requested.connect(start_simulator)
    window.simulator_stop_requested.connect(stimulation.stop_simulator)
    window.request_timeout_changed.connect(update_request_timeout)

    def change_rally_mode(mode: str) -> None:
        try:
            if not stimulation.set_control_mode(mode):
                window.set_rally_mode(stimulation.control_mode)
                window.set_error("控制模式切换正在等待当前 Rally/模拟请求收尾")
                return
            window.set_error("")
        except (TypeError, ValueError, RuntimeError) as exc:
            window.set_error(f"控制模式未更改：{exc}")

    window.rally_mode_requested.connect(change_rally_mode)

    def choose_replay_directory() -> None:
        if controller.is_busy():
            window.set_error("实时会话及其清理未结束，暂时不能打开回放。")
            return
        selected = QFileDialog.getExistingDirectory(window, "选择已保存的会话目录")
        if not selected:
            return
        if not controller.set_replay_active(True):
            window.set_error("实时会话及其清理未结束，暂时不能打开回放。")
            return
        window.set_error("")
        window.set_replay_mode(True, f"正在打开离线回放：{selected}")
        window.set_replay_worker_busy(True)
        if not replay.open(selected):
            window.set_replay_worker_busy(False)
            window.set_replay_mode(False)
            controller.set_replay_active(False)
            window.set_error("回放读取器仍在处理上一项请求。")

    def close_replay() -> None:
        if replay.active:
            window.set_replay_mode(True, "正在关闭回放读取器")
            replay.close()

    def request_replay_block(index: int) -> None:
        window.clear_block_summary(f"离线回放 · 正在加载第 {index + 1} 块")
        replay.request_block(index)

    window.replay_open_requested.connect(choose_replay_directory)
    window.replay_exit_requested.connect(close_replay)
    window.replay_index_requested.connect(request_replay_block)

    def replay_opened(generation: int, overview, error) -> None:
        if generation != replay.generation:
            return
        if error is not None:
            window.clear_block_summary("离线回放打开失败；无可显示数据")
            window.set_error(f"无法打开离线回放：{error}")
            window.replay_status_label.setText(f"回放打开失败：{error}")
            return
        replay_state["overview"] = overview
        window.set_replay_ready(overview)
        window.set_replay_control_events(getattr(overview, "control_events", ()))
        if overview.block_count:
            request_replay_block(0)
        elif overview.incomplete:
            window.set_error("该会话没有可回放的数据块；详见不完整性说明。")

    def replay_block_loaded(generation, token, index, block, entry) -> None:
        if generation != replay.generation or token != replay.request_token:
            return
        overview = replay_state.get("overview")
        if overview is None:
            return
        window.show_replay_block(block, entry, index, overview.block_count)
        window.set_recorded_processing_result(entry.processing_result, entry.block_id)
        window.set_replay_stimulation_events(entry.stimulation_events)

    def replay_block_error(generation, token, index, message) -> None:
        if generation != replay.generation or token != replay.request_token:
            return
        window.clear_block_summary(f"离线回放 · 第 {index + 1} 块损坏或不可读")
        window.set_error(f"回放 block #{index + 1} 读取失败：{message}")
        window.replay_status_label.setText(
            f"离线回放 · 第 {index + 1} 块损坏或不可读：{message}"
        )

    def replay_closed(generation: int) -> None:
        if generation != replay.generation:
            return

        def release_after_thread_exit() -> None:
            if replay.active:
                QTimer.singleShot(5, release_after_thread_exit)
                return
            replay_state["overview"] = None
            window.set_replay_worker_busy(False)
            window.set_replay_mode(False)
            controller.set_replay_active(False)

        QTimer.singleShot(0, release_after_thread_exit)

    replay.opened.connect(replay_opened)
    replay.block_loaded.connect(replay_block_loaded)
    replay.block_error.connect(replay_block_error)
    replay.closed.connect(replay_closed)

    poll_timer = QTimer(window)
    poll_timer.setInterval(100)
    poll_timer.timeout.connect(lambda: pump_latest(controller, window))
    poll_timer.start()
    window._p1_poll_timer = poll_timer  # type: ignore[attr-defined]
    window._p2_poll_timer = poll_timer  # type: ignore[attr-defined]
    window._p2_replay_worker = replay  # type: ignore[attr-defined]
    window._p3_runtime = stimulation  # type: ignore[attr-defined]
    return application, window, controller


def main(argv: list[str] | None = None) -> int:
    if argv is not None:
        sys.argv = argv
    application, window, controller = build_application()
    window.show()
    exit_code = application.exec()
    if controller.is_busy():
        controller.disconnect()
    stimulation = getattr(window, "_p3_runtime", None)
    if stimulation is not None:
        stimulation.shutdown()
        if not stimulation.join(3.0):
            print("Rally 模拟传输线程未能在退出期限内结束", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
