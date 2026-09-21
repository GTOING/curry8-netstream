"""Capture the P4-B 800x600 synthetic Rally evidence image.

This script uses a random loopback UDP fixture and the real runtime/UI wiring.
It never binds or sends to production port 8801.
"""
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import numpy as np
from PySide6.QtWidgets import QApplication

from curry_netstream.models import DataBlock, SessionInfo

from sleep_stim_controller.rally import (
    RALLY_START_COMMAND,
    RALLY_START_SUCCESS,
    RALLY_STOP_COMMAND,
    RALLY_STOP_SUCCESS,
    RallyControlEndpoint,
)
from sleep_stim_controller.staging import (
    BlockContext,
    ModelDescriptor,
    ProcessingResult,
    ProcessingStatus,
)
from sleep_stim_controller.stimulation import StimulationConfig, TriggerMode
from sleep_stim_controller.stimulation_runtime import StimulationRuntime
from sleep_stim_controller.ui import MainWindow


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "reports" / "P4B_RALLY_START_STOP_800X600.png"


class FakeRally:
    def __init__(self) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.settimeout(0.05)
        self.endpoint = RallyControlEndpoint("127.0.0.1", self.socket.getsockname()[1])
        self.stop_event = threading.Event()
        self.request_event = threading.Event()
        self.requests: list[bytes] = []
        self.thread = threading.Thread(target=self._run, name="p4b-screenshot-rally", daemon=False)
        self.thread.start()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                data, source = self.socket.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            self.requests.append(bytes(data))
            self.request_event.set()
            response = (
                RALLY_START_SUCCESS.encode("utf-8")
                if data == RALLY_START_COMMAND.encode("utf-8")
                else RALLY_STOP_SUCCESS.encode("utf-8")
            )
            # Reply from a fresh dynamic loopback source port, as Rally does.
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as responder:
                responder.bind(("127.0.0.1", 0))
                responder.sendto(response, source)

    def wait_for(self, count: int, app: QApplication, timeout: float = 1.5) -> None:
        deadline = time.monotonic() + timeout
        while len(self.requests) < count:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"synthetic Rally request count did not reach {count}")
            app.processEvents()
            self.request_event.wait(0.02)
            self.request_event.clear()
        app.processEvents()

    def close(self) -> None:
        self.stop_event.set()
        self.socket.close()
        self.thread.join(1.0)


class PipelineFixture:
    session_id = "p4b-synthetic-session"
    recording_enabled = False

    def acquire_external_work(self) -> bool:
        return True

    def release_external_work(self) -> None:
        return None

    def enqueue_session_event(self, _event) -> bool:
        return True


def make_result() -> tuple[BlockContext, ProcessingResult]:
    now = time.monotonic_ns()
    block = DataBlock(
        data=np.zeros((1, 300), dtype=np.float32),
        start_sample=0,
        sample_rate_hz=10.0,
        labels=["C3"],
    )
    context = BlockContext(
        session_id="p4b-synthetic-session",
        block_id=1,
        block=block,
        received_utc="2026-09-21T00:00:00Z",
        received_monotonic_ns=now,
    )
    result = ProcessingResult(
        session_id=context.session_id,
        block_id=1,
        start_sample=0,
        end_sample_exclusive=300,
        model=ModelDescriptor(
            "onnx-sleep-staging:fixture",
            "sha256:fixture",
            is_test_double=False,
            confidence_meaning="synthetic probability",
        ),
        status=ProcessingStatus.SUCCESS,
        stage="N2",
        confidence=0.91,
        started_monotonic_ns=now,
        finished_monotonic_ns=now + 1,
        elapsed_ms=1.0,
    )
    return context, result


def main() -> None:
    app = QApplication.instance() or QApplication([])
    fake = FakeRally()
    window = MainWindow()
    runtime = StimulationRuntime(
        window,
        request_timeout_seconds=0.5,
        rally_control_endpoint=fake.endpoint,
    )
    pipeline = PipelineFixture()
    try:
        runtime.automatic_status_changed.connect(window.set_stimulation_auto_status)
        runtime.rally_status_changed.connect(window.set_rally_status)
        runtime.decision_changed.connect(window.set_stimulation_decision)
        runtime.request_changed.connect(window.set_stimulation_request)

        window.set_live_stimulation_session(pipeline.session_id)
        window.begin_session()
        window.set_session(SessionInfo(1, 10.0, ["C3"]))
        window.set_state("streaming", "合成 Curry 会话 · BasicInfo / ChannelInfo 已完成")
        window.set_rally_mode("real")
        window.real_control_confirm_checkbox.setChecked(True)
        window.stage_checkboxes["N2"].setChecked(True)
        window.stimulation_strategy_combo.setCurrentIndex(1)
        window.min_interval_edit.setText("0")
        window.max_result_age_edit.setText("5")
        window.set_error("")

        config = StimulationConfig(
            target_stages=frozenset({"N2"}),
            trigger_mode=TriggerMode.EACH_MATCHING_BLOCK,
            min_request_interval_seconds=0.0,
            max_result_age_seconds=5.0,
            protocol=None,
            config_version=2,
        )
        runtime.configure(config)
        runtime.begin_session(
            pipeline.session_id,
            pipeline,
            False,
            generation=1,
            model_configured=True,
        )
        runtime.set_session_ready(True)
        if not runtime.set_control_mode("real"):
            raise RuntimeError("could not select real mode")
        if not runtime.set_automatic_enabled(True):
            raise RuntimeError("could not enable synthetic real mode")
        fake.wait_for(1, app)
        deadline = time.monotonic() + 1.5
        while not runtime.real_status["baseline_ready"]:
            if time.monotonic() >= deadline:
                raise RuntimeError("synthetic baseline did not complete")
            app.processEvents()
            fake.request_event.wait(0.02)
            fake.request_event.clear()

        context, result = make_result()
        window.show_block(context.block, 1, 0)
        window.set_processing_result(result)
        runtime.process_block(context, result, generation=1)
        fake.wait_for(2, app)
        deadline = time.monotonic() + 1.5
        while runtime.real_status["confirmed_state"] != "RUNNING":
            if time.monotonic() >= deadline:
                raise RuntimeError("synthetic Start Stim did not complete")
            app.processEvents()
            fake.request_event.wait(0.02)
            fake.request_event.clear()

        app.processEvents()
        window.set_rally_status(runtime.real_status)
        window.rally_mode_hint_label.setText(
            "合成证据：随机 loopback 假 Rally · 不是生产 127.0.0.1:8801 · "
            "API 确认不等于物理输出确认"
        )
        window.set_diagnostics(
            "P4-B 合成接线已完成：Stop Stim 基线 → 合格 ONNX N2 → Start Stim。\n"
            f"假端点：{fake.endpoint.host}:{fake.endpoint.port}（随机测试端口）\n"
            "未连接真实 Rally、Curry 硬件或刺激设备。"
        )
        window.hint_label.setText(
            "合成 P4-B 证据：本次使用显式 ONNX fixture；真实模式默认关闭，"
            "EEG 波形仍在 Curry 8 查看。"
        )
        window.workflow_section.set_expanded(False)
        window.resize(800, 600)
        window.show()
        window.settings_scroll.ensureWidgetVisible(window.rally_control_status_label)
        app.processEvents()
        if not window.grab().save(str(OUTPUT)):
            raise RuntimeError(f"failed to save {OUTPUT}")
        print(f"saved {OUTPUT}")
    finally:
        runtime.shutdown()
        runtime.join(2.0)
        fake.close()
        window.close()


if __name__ == "__main__":
    main()
