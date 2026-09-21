from __future__ import annotations

import queue
import socket
import threading
import time
from dataclasses import replace

import numpy as np

from curry_netstream.models import DataBlock

from sleep_stim_controller.rally import (
    RALLY_START_COMMAND,
    RALLY_START_SUCCESS,
    RALLY_STOP_COMMAND,
    RALLY_STOP_SUCCESS,
    RallyControlEndpoint,
    RallyControlRequest,
    RallyControlTransportWorker,
    parse_rally_control_response,
)
from sleep_stim_controller.recording import SessionReader, SessionWriter
from sleep_stim_controller.staging import (
    BlockContext,
    ModelDescriptor,
    NoModelAdapter,
    ProcessingResult,
    ProcessingPipeline,
    ProcessingStatus,
)
from sleep_stim_controller.stimulation import (
    RequestStatus,
    StimulationConfig,
    TriggerMode,
)
from sleep_stim_controller.stimulation_runtime import StimulationRuntime


class BinaryRallyFixture:
    def __init__(self, replies: list[bytes | None]) -> None:
        self.replies = queue.Queue()
        for reply in replies:
            self.replies.put(reply)
        self.requests: list[tuple[bytes, tuple[str, int]]] = []
        self.received = threading.Event()
        self.request_changed = threading.Event()
        self._stop = threading.Event()
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.settimeout(0.05)
        self.endpoint = RallyControlEndpoint("127.0.0.1", self._socket.getsockname()[1])
        self._thread = threading.Thread(target=self._run, daemon=False)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data, source = self._socket.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            self.requests.append((bytes(data), (str(source[0]), int(source[1]))))
            self.received.set()
            self.request_changed.set()
            try:
                reply = self.replies.get_nowait()
            except queue.Empty:
                reply = None
            if reply is not None:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as responder:
                    responder.bind(("127.0.0.1", 0))
                    responder.sendto(reply, source)

    def close(self) -> None:
        self._stop.set()
        self._socket.close()
        self._thread.join(1.0)

    def reply_to(self, index: int, reply: bytes) -> None:
        """Inject a reply to a retained request source port."""
        source = self.requests[index][1]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as responder:
            responder.bind(("127.0.0.1", 0))
            responder.sendto(reply, source)

    def wait_for_count(self, count: int, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        while len(self.requests) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.request_changed.wait(remaining)
            self.request_changed.clear()
        return True


def _request(command: str = RALLY_START_COMMAND) -> RallyControlRequest:
    return RallyControlRequest(
        request_id="fixture-request",
        session_id="fixture-session",
        generation=1,
        command=command,
        reason="fixture",
        block_id=None,
        stage=None,
        model=None,
        target_stages=("N2",),
        desired_state="RUNNING" if command == RALLY_START_COMMAND else "STOPPED",
        confirmed_state="STOPPED",
        runtime_state="STARTING" if command == RALLY_START_COMMAND else "STOPPING",
        created_utc="2026-09-21T00:00:00Z",
        created_monotonic_ns=1,
    )


def test_real_reply_is_exact_and_command_specific() -> None:
    assert parse_rally_control_response(
        RALLY_START_COMMAND, RALLY_START_SUCCESS.encode("utf-8")
    ).status.value == "api_success"
    assert parse_rally_control_response(
        RALLY_STOP_COMMAND, RALLY_STOP_SUCCESS.encode("utf-8")
    ).status.value == "api_success"
    assert parse_rally_control_response(
        RALLY_START_COMMAND, RALLY_STOP_SUCCESS.encode("utf-8")
    ).status.value != "api_success"
    assert parse_rally_control_response(
        RALLY_START_COMMAND, b"RALLY_ERROR_SUCCESS"
    ).status.value == "api_success"
    assert parse_rally_control_response(RALLY_START_COMMAND, b" ").status.value != "api_success"
    assert parse_rally_control_response(RALLY_START_COMMAND, b"\xff").status.value != "api_success"


def test_real_worker_uses_binary_commands_dynamic_reply_port_and_socket_quarantine() -> None:
    fixture = BinaryRallyFixture([RALLY_START_SUCCESS.encode("utf-8")])
    outcomes = []
    done = threading.Event()
    worker = RallyControlTransportWorker(
        on_sent=lambda *_args: None,
        on_not_sent=lambda *_args: None,
        on_outcome=lambda _request, outcome: (outcomes.append(outcome), done.set()),
        endpoint=fixture.endpoint,
        timeout_seconds=0.2,
    )
    try:
        assert worker.submit(_request())
        assert done.wait(1.0)
        assert outcomes[0].status.value == "api_success"
        assert outcomes[0].response_source_host == "127.0.0.1"
        assert outcomes[0].response_source_port != fixture.endpoint.port
        assert fixture.requests[0][0] == RALLY_START_COMMAND.encode("utf-8")
        assert worker.quarantined_socket_count == 1
    finally:
        worker.shutdown()
        assert worker.join(1.0)
        fixture.close()


def test_real_worker_reserves_one_extra_stop_slot_without_reusing_old_socket() -> None:
    fixture = BinaryRallyFixture(
        [RALLY_STOP_SUCCESS.encode("utf-8"), RALLY_STOP_SUCCESS.encode("utf-8")]
    )
    outcomes = []
    done = threading.Event()
    worker = RallyControlTransportWorker(
        on_sent=lambda *_args: None,
        on_not_sent=lambda *_args: None,
        on_outcome=lambda _request, outcome: (outcomes.append(outcome), done.set()),
        endpoint=fixture.endpoint,
        timeout_seconds=0.2,
        max_quarantined_sockets=1,
    )
    try:
        assert worker.submit(_request(RALLY_STOP_COMMAND))
        assert done.wait(1.0)
        assert worker.quarantined_socket_count == 1
        # The normal capacity is full, so a new Start is refused before it can
        # consume the reserved stop slot.
        assert not worker.submit(_request(RALLY_START_COMMAND))
        done.clear()
        assert worker.submit(_request(RALLY_STOP_COMMAND))
        assert done.wait(1.0)
        assert [item[0] for item in fixture.requests] == [
            RALLY_STOP_COMMAND.encode("utf-8"),
            RALLY_STOP_COMMAND.encode("utf-8"),
        ]
        assert worker.quarantined_socket_count == 2
        assert all(item.status is RequestStatus.API_SUCCESS for item in outcomes)
    finally:
        worker.shutdown()
        assert worker.join(1.0)
        fixture.close()


def test_late_start_success_and_wrong_command_success_cannot_confirm_stop() -> None:
    fixture = BinaryRallyFixture([None, RALLY_START_SUCCESS.encode("utf-8")])
    outcomes = []
    done = threading.Event()
    worker = RallyControlTransportWorker(
        on_sent=lambda *_args: None,
        on_not_sent=lambda *_args: None,
        on_outcome=lambda _request, outcome: (outcomes.append(outcome), done.set()),
        endpoint=fixture.endpoint,
        timeout_seconds=0.05,
        max_quarantined_sockets=2,
    )
    try:
        assert worker.submit(_request(RALLY_START_COMMAND))
        assert done.wait(1.0)
        assert outcomes[0].status is RequestStatus.UNKNOWN
        fixture.reply_to(0, RALLY_START_SUCCESS.encode("utf-8"))
        done.clear()
        assert worker.submit(_request(RALLY_STOP_COMMAND))
        assert done.wait(1.0)
        # The Stop socket receives a Start success text, which is a
        # command-mismatched response and therefore cannot confirm Stop.
        assert outcomes[1].status is RequestStatus.UNKNOWN
        assert len(fixture.requests) == 2
    finally:
        worker.shutdown()
        assert worker.join(1.0)
        fixture.close()


def test_start_timeout_can_use_reserved_stop_capacity_for_compensation() -> None:
    fixture = BinaryRallyFixture(
        [RALLY_STOP_SUCCESS.encode("utf-8"), None, RALLY_STOP_SUCCESS.encode("utf-8")]
    )
    runtime = StimulationRuntime(
        request_timeout_seconds=0.05,
        rally_control_endpoint=fixture.endpoint,
        real_transport_factory=lambda **kwargs: RallyControlTransportWorker(
            max_quarantined_sockets=2, **kwargs
        ),
    )
    pipeline = _PipelineFixture()
    status_changed = threading.Event()
    runtime.rally_status_changed.connect(lambda _status: status_changed.set())
    try:
        runtime.configure(_real_config())
        runtime.begin_session(
            "session-real", pipeline, False, generation=1, model_configured=True
        )
        runtime.set_session_ready(True)
        assert runtime.set_control_mode("real")
        assert runtime.set_automatic_enabled(True)
        assert fixture.wait_for_count(1)
        deadline = time.monotonic() + 1.0
        while not runtime.real_status["baseline_ready"]:
            assert time.monotonic() < deadline
            status_changed.wait(0.02)
            status_changed.clear()
        runtime.process_block(*_result(1, "N2"), generation=1)
        assert fixture.wait_for_count(3, timeout=1.0)
        assert [item[0] for item in fixture.requests] == [
            RALLY_STOP_COMMAND.encode("utf-8"),
            RALLY_START_COMMAND.encode("utf-8"),
            RALLY_STOP_COMMAND.encode("utf-8"),
        ]
        assert runtime.real_status["enabled"] is False
        deadline = time.monotonic() + 1.0
        while pipeline.leases != 0:
            assert time.monotonic() < deadline
            status_changed.wait(0.02)
            status_changed.clear()
        assert pipeline.leases == 0
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


class _PipelineFixture:
    session_id = "session-real"

    def __init__(self, *, recording_enabled: bool = False) -> None:
        self.recording_enabled = recording_enabled
        self.leases = 0
        self.events = []
        self.closing = False
        self.fail_events = False
        self.failed_events = 0

    def acquire_external_work(self) -> bool:
        if self.closing:
            return False
        self.leases += 1
        return True

    def acquire_control_work(self) -> bool:
        self.leases += 1
        return True

    def release_external_work(self) -> None:
        self.leases -= 1

    def enqueue_session_event(self, event) -> bool:
        self.events.append(event)
        if self.fail_events:
            self.failed_events += 1
            return False
        return True


def _real_config() -> StimulationConfig:
    return StimulationConfig(
        target_stages=frozenset({"N2"}),
        trigger_mode=TriggerMode.EACH_MATCHING_BLOCK,
        min_request_interval_seconds=0.0,
        max_result_age_seconds=5.0,
        protocol=None,
        config_version=2,
    )


class _FakeClock:
    def __init__(self, value: int = 10_000_000_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value

    def advance_seconds(self, seconds: float) -> None:
        self.value += int(seconds * 1_000_000_000)


def _result(
    block_id: int, stage: str, *, now_ns: int | None = None
) -> tuple[BlockContext, ProcessingResult]:
    now = time.monotonic_ns() if now_ns is None else now_ns
    block = DataBlock(
        data=np.zeros((1, 300), dtype=np.float32),
        start_sample=(block_id - 1) * 300,
        sample_rate_hz=10.0,
        labels=["C3"],
    )
    context = BlockContext(
        session_id="session-real",
        block_id=block_id,
        block=block,
        received_utc="2026-09-21T00:00:00Z",
        received_monotonic_ns=now,
    )
    result = ProcessingResult(
        session_id="session-real",
        block_id=block_id,
        start_sample=block.start_sample,
        end_sample_exclusive=block.start_sample + block.n_samples,
        model=ModelDescriptor(
            "onnx-sleep-staging:fixture",
            "sha256:fixture",
            is_test_double=False,
            confidence_meaning="fixture probability",
        ),
        status=ProcessingStatus.SUCCESS,
        stage=stage,
        confidence=0.5,
        started_monotonic_ns=now,
        finished_monotonic_ns=now + 1,
        elapsed_ms=1.0,
    )
    return context, result


def test_runtime_baseline_then_binary_target_and_non_target_are_idempotent() -> None:
    fixture = BinaryRallyFixture(
        [
            RALLY_STOP_SUCCESS.encode("utf-8"),
            RALLY_START_SUCCESS.encode("utf-8"),
            RALLY_STOP_SUCCESS.encode("utf-8"),
        ]
    )
    pipeline = _PipelineFixture()
    runtime = StimulationRuntime(
        request_timeout_seconds=0.2,
        rally_control_endpoint=fixture.endpoint,
    )
    status_changed = threading.Event()
    runtime.rally_status_changed.connect(lambda _status: status_changed.set())

    def wait_for_status(predicate, timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            remaining = deadline - time.monotonic()
            assert remaining > 0
            status_changed.wait(remaining)
            status_changed.clear()

    try:
        runtime.configure(_real_config())
        runtime.begin_session(
            "session-real",
            pipeline,
            False,
            generation=1,
            model_configured=True,
        )
        runtime.set_session_ready(True)
        assert runtime.set_control_mode("real")
        assert runtime.set_automatic_enabled(True)
        assert fixture.received.wait(1.0)
        assert fixture.requests[0][0] == RALLY_STOP_COMMAND.encode("utf-8")
        assert runtime.real_status["baseline_ready"] is False
        wait_for_status(lambda: runtime.real_status["baseline_ready"])
        context, result = _result(1, "N2")
        events, _evaluation = runtime.process_block(context, result, generation=1)
        assert events and events[0]["payload"]["phase"] == "decision"
        assert fixture.wait_for_count(2)
        assert fixture.requests[1][0] == RALLY_START_COMMAND.encode("utf-8")
        wait_for_status(lambda: runtime.real_status["confirmed_state"] == "RUNNING")
        duplicate, _ = runtime.process_block(context, result, generation=1)
        assert duplicate == ()
        context2, result2 = _result(2, "W")
        runtime.process_block(context2, result2, generation=1)
        assert fixture.wait_for_count(3)
        assert fixture.requests[2][0] == RALLY_STOP_COMMAND.encode("utf-8")
        wait_for_status(lambda: runtime.real_status["confirmed_state"] == "STOPPED")
        runtime.set_automatic_enabled(False)
        assert len(fixture.requests) == 3
        assert pipeline.leases == 0
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_real_runtime_tick_stops_a_silent_stream_after_configured_age() -> None:
    clock = _FakeClock()
    fixture = BinaryRallyFixture(
        [
            RALLY_STOP_SUCCESS.encode("utf-8"),
            RALLY_START_SUCCESS.encode("utf-8"),
            RALLY_STOP_SUCCESS.encode("utf-8"),
        ]
    )
    runtime = StimulationRuntime(
        request_timeout_seconds=0.2,
        monotonic_ns=clock,
        rally_control_endpoint=fixture.endpoint,
    )
    pipeline = _PipelineFixture()
    status_changed = threading.Event()
    runtime.rally_status_changed.connect(lambda _status: status_changed.set())
    try:
        runtime.configure(_real_config())
        runtime.begin_session(
            "session-real", pipeline, False, generation=1, model_configured=True
        )
        runtime.set_session_ready(True)
        assert runtime.set_control_mode("real")
        assert runtime.set_automatic_enabled(True)
        assert fixture.wait_for_count(1)
        deadline = time.monotonic() + 1.0
        while not runtime.real_status["baseline_ready"]:
            assert time.monotonic() < deadline
            status_changed.wait(0.02)
            status_changed.clear()
        context, result = _result(1, "N2", now_ns=clock.value)
        runtime.process_block(context, result, generation=1)
        assert fixture.wait_for_count(2)
        deadline = time.monotonic() + 1.0
        while runtime.real_status["confirmed_state"] != "RUNNING":
            assert time.monotonic() < deadline
            status_changed.wait(0.02)
            status_changed.clear()
        clock.advance_seconds(6.0)
        assert runtime.tick() is True
        assert fixture.wait_for_count(3)
        deadline = time.monotonic() + 1.0
        while runtime.real_status["confirmed_state"] != "STOPPED":
            assert time.monotonic() < deadline
            status_changed.wait(0.02)
            status_changed.clear()
        assert runtime.real_status["enabled"] is False
        assert runtime.real_status["runtime_state"] == "STOPPED"
        assert [item[0] for item in fixture.requests] == [
            RALLY_STOP_COMMAND.encode("utf-8"),
            RALLY_START_COMMAND.encode("utf-8"),
            RALLY_STOP_COMMAND.encode("utf-8"),
        ]
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_real_runtime_rechecks_cached_result_before_followup_start_after_stop() -> None:
    clock = _FakeClock()
    fixture = BinaryRallyFixture(
        [RALLY_STOP_SUCCESS.encode("utf-8"), RALLY_START_SUCCESS.encode("utf-8"), None]
    )
    runtime = StimulationRuntime(
        request_timeout_seconds=0.5,
        monotonic_ns=clock,
        rally_control_endpoint=fixture.endpoint,
    )
    pipeline = _PipelineFixture()
    status_changed = threading.Event()
    runtime.rally_status_changed.connect(lambda _status: status_changed.set())
    try:
        runtime.configure(_real_config())
        runtime.begin_session(
            "session-real", pipeline, False, generation=1, model_configured=True
        )
        runtime.set_session_ready(True)
        assert runtime.set_control_mode("real")
        assert runtime.set_automatic_enabled(True)
        assert fixture.wait_for_count(1)
        deadline = time.monotonic() + 1.0
        while not runtime.real_status["baseline_ready"]:
            assert time.monotonic() < deadline
            status_changed.wait(0.02)
            status_changed.clear()
        runtime.process_block(*_result(1, "N2", now_ns=clock.value), generation=1)
        assert fixture.wait_for_count(2)
        deadline = time.monotonic() + 1.0
        while runtime.real_status["confirmed_state"] != "RUNNING":
            assert time.monotonic() < deadline
            status_changed.wait(0.02)
            status_changed.clear()
        runtime.process_block(*_result(2, "W", now_ns=clock.value), generation=1)
        assert fixture.wait_for_count(3)
        # A newer target result arrives while Stop is in flight, then becomes
        # stale before Stop's reply. It must not be reused for a new Start.
        runtime.process_block(*_result(3, "N2", now_ns=clock.value), generation=1)
        clock.advance_seconds(6.0)
        fixture.reply_to(2, RALLY_STOP_SUCCESS.encode("utf-8"))
        deadline = time.monotonic() + 1.0
        while runtime.real_status["runtime_state"] != "STOPPED":
            assert time.monotonic() < deadline
            status_changed.wait(0.02)
            status_changed.clear()
        assert runtime.real_status["enabled"] is False
        assert len(fixture.requests) == 3
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_inflight_start_receives_stop_priority_on_user_disable() -> None:
    fixture = BinaryRallyFixture(
        [RALLY_STOP_SUCCESS.encode("utf-8"), None, RALLY_STOP_SUCCESS.encode("utf-8")]
    )
    runtime = StimulationRuntime(
        request_timeout_seconds=0.05,
        rally_control_endpoint=fixture.endpoint,
    )
    pipeline = _PipelineFixture()
    status_changed = threading.Event()
    runtime.rally_status_changed.connect(lambda _status: status_changed.set())
    try:
        runtime.configure(_real_config())
        runtime.begin_session(
            "session-real", pipeline, False, generation=1, model_configured=True
        )
        runtime.set_session_ready(True)
        assert runtime.set_control_mode("real")
        assert runtime.set_automatic_enabled(True)
        assert fixture.wait_for_count(1)
        deadline = time.monotonic() + 1.0
        while not runtime.real_status["baseline_ready"]:
            assert time.monotonic() < deadline
            status_changed.wait(0.02)
            status_changed.clear()
        runtime.process_block(*_result(1, "N2"), generation=1)
        assert fixture.wait_for_count(2)
        runtime.set_automatic_enabled(False, reason="fixture user exit")
        assert fixture.wait_for_count(3, timeout=1.0)
        deadline = time.monotonic() + 1.0
        while runtime.real_status["confirmed_state"] != "STOPPED":
            assert time.monotonic() < deadline
            status_changed.wait(0.02)
            status_changed.clear()
        assert [item[0] for item in fixture.requests] == [
            RALLY_STOP_COMMAND.encode("utf-8"),
            RALLY_START_COMMAND.encode("utf-8"),
            RALLY_STOP_COMMAND.encode("utf-8"),
        ]
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_start_failure_has_one_compensation_stop_and_never_retries_start() -> None:
    fixture = BinaryRallyFixture(
        [
            RALLY_STOP_SUCCESS.encode("utf-8"),
            b"RALLY_ERROR_START_STIM",
            RALLY_STOP_SUCCESS.encode("utf-8"),
        ]
    )
    runtime = StimulationRuntime(
        request_timeout_seconds=0.2,
        rally_control_endpoint=fixture.endpoint,
    )
    pipeline = _PipelineFixture()
    status_changed = threading.Event()
    runtime.rally_status_changed.connect(lambda _status: status_changed.set())

    def wait_for_count(count: int) -> None:
        assert fixture.wait_for_count(count, timeout=1.0)
        status_changed.set()

    try:
        runtime.configure(_real_config())
        runtime.begin_session(
            "session-real", pipeline, False, generation=1, model_configured=True
        )
        runtime.set_session_ready(True)
        assert runtime.set_control_mode("real")
        assert runtime.set_automatic_enabled(True)
        wait_for_count(1)
        deadline = time.monotonic() + 1.0
        while not runtime.real_status["baseline_ready"]:
            assert time.monotonic() < deadline
            status_changed.wait(0.05)
            status_changed.clear()
        context, result = _result(1, "N2")
        runtime.process_block(context, result, generation=1)
        wait_for_count(3)
        deadline = time.monotonic() + 1.0
        while runtime.real_status["runtime_state"] != "STOPPED":
            assert time.monotonic() < deadline
            status_changed.wait(0.05)
            status_changed.clear()
        assert [item[0] for item in fixture.requests] == [
            RALLY_STOP_COMMAND.encode("utf-8"),
            RALLY_START_COMMAND.encode("utf-8"),
            RALLY_STOP_COMMAND.encode("utf-8"),
        ]
        assert runtime.real_status["enabled"] is False
        assert runtime.real_status["runtime_state"] == "STOPPED"
        assert pipeline.leases == 0
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_stop_failure_enters_fault_and_requires_independent_stop() -> None:
    fixture = BinaryRallyFixture(
        [
            RALLY_STOP_SUCCESS.encode("utf-8"),
            RALLY_START_SUCCESS.encode("utf-8"),
            b"RALLY_ERROR_STOP_STIM",
        ]
    )
    runtime = StimulationRuntime(
        request_timeout_seconds=0.2,
        rally_control_endpoint=fixture.endpoint,
    )
    pipeline = _PipelineFixture()
    status_changed = threading.Event()
    runtime.rally_status_changed.connect(lambda _status: status_changed.set())

    def wait_until(predicate) -> None:
        deadline = time.monotonic() + 1.0
        while not predicate():
            assert time.monotonic() < deadline
            status_changed.wait(0.05)
            status_changed.clear()

    try:
        runtime.configure(_real_config())
        runtime.begin_session(
            "session-real", pipeline, False, generation=1, model_configured=True
        )
        runtime.set_session_ready(True)
        assert runtime.set_control_mode("real")
        assert runtime.set_automatic_enabled(True)
        wait_until(lambda: runtime.real_status["baseline_ready"])
        context, result = _result(1, "N2")
        runtime.process_block(context, result, generation=1)
        wait_until(lambda: runtime.real_status["confirmed_state"] == "RUNNING")
        context2, result2 = _result(2, "W")
        runtime.process_block(context2, result2, generation=1)
        wait_until(lambda: runtime.real_status["runtime_state"] == "FAULT/UNKNOWN")
        assert len(fixture.requests) == 3
        assert runtime.real_status["enabled"] is False
        assert runtime.real_status["independent_stop_required"] is True
        assert pipeline.leases == 0
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_old_generation_and_non_onnx_result_do_not_request_real_start() -> None:
    fixture = BinaryRallyFixture([RALLY_STOP_SUCCESS.encode("utf-8")])
    runtime = StimulationRuntime(
        request_timeout_seconds=0.2,
        rally_control_endpoint=fixture.endpoint,
    )
    pipeline = _PipelineFixture()
    status_changed = threading.Event()
    runtime.rally_status_changed.connect(lambda _status: status_changed.set())
    try:
        runtime.configure(_real_config())
        runtime.begin_session(
            "session-real", pipeline, False, generation=7, model_configured=True
        )
        runtime.set_session_ready(True)
        assert runtime.set_control_mode("real")
        assert runtime.set_automatic_enabled(True)
        assert fixture.wait_for_count(1)
        deadline = time.monotonic() + 1.0
        while not runtime.real_status["baseline_ready"]:
            assert time.monotonic() < deadline
            status_changed.wait(0.05)
            status_changed.clear()
        context, result = _result(1, "N2")
        runtime.process_block(context, result, generation=6)
        runtime.process_block(
            context,
            replace(
                result,
                model=ModelDescriptor(
                    "none", "not-connected", is_test_double=False
                ),
            ),
            generation=7,
        )
        assert len(fixture.requests) == 1
        assert runtime.real_status["confirmed_state"] == "STOPPED"
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_recording_enabled_routes_real_control_events_through_pipeline_writer(tmp_path) -> None:
    fixture = BinaryRallyFixture([RALLY_STOP_SUCCESS.encode("utf-8")])
    ready = threading.Event()
    finished = threading.Event()
    status_changed = threading.Event()
    outcomes = []

    def on_ready(error) -> None:
        assert error is None
        ready.set()

    pipeline = ProcessingPipeline(
        session_id="recorded-real-session",
        host="127.0.0.1",
        port=4455,
        recording_enabled=True,
        recording_root=tmp_path,
        model_factory=NoModelAdapter,
        on_ready=on_ready,
        on_finished=lambda outcome: (outcomes.append(outcome), finished.set()),
    )
    runtime = StimulationRuntime(
        request_timeout_seconds=0.2,
        rally_control_endpoint=fixture.endpoint,
    )
    runtime.rally_status_changed.connect(lambda _status: status_changed.set())
    try:
        pipeline.start()
        assert ready.wait(1.0)
        runtime.configure(_real_config())
        runtime.begin_session(
            "recorded-real-session", pipeline, True, generation=1, model_configured=True
        )
        runtime.set_session_ready(True)
        assert runtime.set_control_mode("real")
        assert runtime.set_automatic_enabled(True)
        assert fixture.wait_for_count(1)
        deadline = time.monotonic() + 1.0
        while not runtime.real_status["baseline_ready"]:
            assert time.monotonic() < deadline
            status_changed.wait(0.05)
            status_changed.clear()
        runtime.set_automatic_enabled(False)
        pipeline.finish(cancelled=False, error=None)
        assert finished.wait(1.0)
        assert outcomes and outcomes[0].session_path
        reader = SessionReader(outcomes[0].session_path)
        assert [event["payload"]["phase"] for event in reader.control_events] == [
            "config",
            "sent",
            "outcome",
        ]
        assert reader.control_events[-1]["payload"]["outcome"] == "api_success"
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        if pipeline.is_alive():
            pipeline.finish(cancelled=True, error=None)
            finished.wait(1.0)
        fixture.close()


def test_recording_decision_is_queued_before_transport_events() -> None:
    fixture = BinaryRallyFixture(
        [
            RALLY_STOP_SUCCESS.encode("utf-8"),
            RALLY_START_SUCCESS.encode("utf-8"),
            RALLY_STOP_SUCCESS.encode("utf-8"),
        ]
    )
    pipeline = _PipelineFixture(recording_enabled=True)
    runtime = StimulationRuntime(
        request_timeout_seconds=0.2,
        rally_control_endpoint=fixture.endpoint,
    )
    status_changed = threading.Event()
    runtime.rally_status_changed.connect(lambda _status: status_changed.set())
    try:
        runtime.configure(_real_config())
        runtime.begin_session(
            "session-real", pipeline, True, generation=1, model_configured=True
        )
        runtime.set_session_ready(True)
        assert runtime.set_control_mode("real")
        assert runtime.set_automatic_enabled(True)
        assert fixture.wait_for_count(1)
        deadline = time.monotonic() + 1.0
        while not runtime.real_status["baseline_ready"]:
            assert time.monotonic() < deadline
            status_changed.wait(0.05)
            status_changed.clear()
        context, result = _result(1, "N2")
        events, _evaluation = runtime.process_block(context, result, generation=1)
        assert events == ()
        assert fixture.wait_for_count(2)
        deadline = time.monotonic() + 1.0
        while runtime.real_status["confirmed_state"] != "RUNNING":
            assert time.monotonic() < deadline
            status_changed.wait(0.05)
            status_changed.clear()
        phases = [event["payload"]["phase"] for event in pipeline.events]
        decision_index = phases.index("decision")
        assert phases[decision_index : decision_index + 3] == [
            "decision",
            "sent",
            "outcome",
        ]
        runtime.set_automatic_enabled(False)
        assert fixture.wait_for_count(3)
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_eof_close_and_record_failure_keep_required_stop_alive_until_outcome() -> None:
    fixture = BinaryRallyFixture(
        [
            RALLY_STOP_SUCCESS.encode("utf-8"),
            RALLY_START_SUCCESS.encode("utf-8"),
            RALLY_STOP_SUCCESS.encode("utf-8"),
        ]
    )
    pipeline = _PipelineFixture(recording_enabled=True)
    runtime = StimulationRuntime(
        request_timeout_seconds=0.2,
        rally_control_endpoint=fixture.endpoint,
    )
    status_changed = threading.Event()
    diagnostics: list[str] = []
    runtime.rally_status_changed.connect(lambda _status: status_changed.set())
    runtime.diagnostic.connect(diagnostics.append)
    try:
        runtime.configure(_real_config())
        runtime.begin_session(
            "session-real", pipeline, True, generation=1, model_configured=True
        )
        runtime.set_session_ready(True)
        assert runtime.set_control_mode("real")
        assert runtime.set_automatic_enabled(True)
        assert fixture.wait_for_count(1)
        deadline = time.monotonic() + 1.0
        while not runtime.real_status["baseline_ready"]:
            assert time.monotonic() < deadline
            status_changed.wait(0.02)
            status_changed.clear()
        runtime.process_block(*_result(1, "N2"), generation=1)
        assert fixture.wait_for_count(2)
        deadline = time.monotonic() + 1.0
        while runtime.real_status["confirmed_state"] != "RUNNING":
            assert time.monotonic() < deadline
            status_changed.wait(0.02)
            status_changed.clear()

        # Simulate the TCP EOF/close handoff and a writer that can no longer
        # accept ordinary work. The dedicated control lease still admits Stop.
        pipeline.closing = True
        pipeline.fail_events = True
        runtime.on_session_stopping("Curry EOF；会话正在关闭")
        assert fixture.wait_for_count(3)
        deadline = time.monotonic() + 1.0
        while runtime.real_status["confirmed_state"] != "STOPPED":
            assert time.monotonic() < deadline
            status_changed.wait(0.02)
            status_changed.clear()
        assert pipeline.leases == 0
        phases = [event["payload"]["phase"] for event in pipeline.events]
        assert phases[-2:] == ["sent", "outcome"]
        assert pipeline.failed_events >= 1
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_rally_control_events_round_trip_without_eeg_blocks(tmp_path) -> None:
    writer = SessionWriter.create(
        tmp_path,
        session_id="empty-real-session",
        host="127.0.0.1",
        port=4455,
        descriptor=ModelDescriptor("none", "not-connected"),
    )
    base = {
        "schema_version": 1,
        "phase": "config",
        "mode": "real",
        "command": None,
        "reason": "fixture config",
        "stage": None,
        "model": None,
        "target_stages": [],
        "desired_state": "STOPPED",
        "confirmed_state": None,
        "runtime_state": "STOPPING",
        "created_utc": "2026-09-21T00:00:00Z",
        "created_monotonic_ns": 1,
        "outcome": "configured",
    }
    writer.append_extension_event(
        {
            "event_type": "rally_control",
            "session_id": "empty-real-session",
            "block_id": None,
            "payload": base,
        }
    )
    for phase, command, outcome, request_id in (
        ("sent", RALLY_STOP_COMMAND, "sent", "r1"),
        ("outcome", RALLY_STOP_COMMAND, "api_success", "r1"),
    ):
        payload = dict(base)
        payload.update(
            phase=phase,
            command=command,
            reason="fixture transport",
            outcome=outcome,
            sent_monotonic_ns=2,
            received_monotonic_ns=3 if phase == "outcome" else None,
            response=(
                {
                    "text": RALLY_STOP_SUCCESS,
                    "bytes_b64": "5p2O5Yqg5Yqo5oiQ5Yqf",
                    "source": {"host": "127.0.0.1", "port": 54321},
                }
                if phase == "outcome"
                else None
            ),
        )
        writer.append_extension_event(
            {
                "event_type": "rally_control",
                "session_id": "empty-real-session",
                "block_id": None,
                "request_id": request_id,
                "payload": payload,
            }
        )
    writer.finish(
        status="closed",
        reason="fixture",
        accepted_blocks=0,
        rejected_blocks=0,
        completed_results=0,
        unprocessed_blocks=0,
    )
    reader = SessionReader(writer.path)
    assert reader.entries == ()
    assert len(reader.control_events) == 3
    assert reader.overview.control_events == reader.control_events


def test_pipeline_closes_lease_admission_before_writer_finish_and_drains_events(
    tmp_path, monkeypatch
) -> None:
    """The close barrier rejects late leases without dropping accepted events."""
    import json
    from pathlib import Path

    finish_entered = threading.Event()
    release_finish = threading.Event()
    ready = threading.Event()
    finished = threading.Event()
    outcomes = []
    original_finish = SessionWriter.finish

    def finish_with_barrier(self, **kwargs):
        finish_entered.set()
        if not release_finish.wait(2.0):
            raise AssertionError("writer finish barrier timed out")
        return original_finish(self, **kwargs)

    monkeypatch.setattr(SessionWriter, "finish", finish_with_barrier)
    pipeline = ProcessingPipeline(
        session_id="close-barrier-session",
        host="127.0.0.1",
        port=4455,
        recording_enabled=True,
        recording_root=tmp_path,
        model_factory=NoModelAdapter,
        on_ready=lambda error: ready.set(),
        on_finished=lambda outcome: (outcomes.append(outcome), finished.set()),
    )
    config_event = {
        "event_type": "rally_control",
        "session_id": "close-barrier-session",
        "block_id": None,
        "payload": {
            "schema_version": 1,
            "phase": "config",
            "mode": "real",
            "command": None,
            "reason": "close barrier fixture",
            "stage": None,
            "model": None,
            "target_stages": [],
            "desired_state": "STOPPED",
            "confirmed_state": None,
            "runtime_state": "STOPPING",
            "created_utc": "2026-09-21T00:00:00Z",
            "created_monotonic_ns": 1,
            "outcome": "configured",
        },
    }
    pipeline.start()
    assert ready.wait(1.0)
    assert pipeline.acquire_control_work()
    assert pipeline.enqueue_session_event(config_event)
    pipeline.finish(cancelled=False, error=None)
    pipeline.release_external_work()
    assert finish_entered.wait(1.0)
    assert pipeline.acquire_control_work() is False
    assert pipeline.reserve_session_event() is None
    assert pipeline.enqueue_session_event(config_event) is False
    release_finish.set()
    assert finished.wait(2.0)
    assert outcomes and outcomes[0].error is None

    session_path = outcomes[0].session_path
    assert session_path is not None
    session_path = Path(session_path)
    reader = SessionReader(session_path)
    assert [event["payload"]["phase"] for event in reader.control_events] == ["config"]
    journal = [
        json.loads(line)
        for line in (session_path / "events.jsonl").read_text("utf-8").splitlines()
    ]
    assert [event["event_type"] for event in journal] == [
        "rally_control",
        "session_finished",
    ]
