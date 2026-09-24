from __future__ import annotations

import json
import queue
import socket
import threading
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

from curry_netstream.models import DataBlock

from sleep_stim_controller.rally import (
    RALLY_START_COMMAND,
    RALLY_START_SUCCESS,
    RALLY_STOP_COMMAND,
    RALLY_STOP_SUCCESS,
    RALLY_APPLY_COMMAND,
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
from sleep_stim_controller.paradigm import load_paradigm_package


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
    assert parse_rally_control_response(RALLY_APPLY_COMMAND, b"RALLY_ERROR_SUCCESS").status.value == "api_success"
    assert parse_rally_control_response(RALLY_APPLY_COMMAND, b"RALLY_ERROR_INVALID_CHANNEL").status.value == "api_rejected"
    assert parse_rally_control_response(RALLY_APPLY_COMMAND, b"RALLY_ERROR_NOT_IN_REFERENCE").status.value == "unknown"
    assert parse_rally_control_response(RALLY_APPLY_COMMAND, b"RALLY_ERROR_SUCCESS ").status.value == "unknown"


def test_apply_worker_sends_exact_single_space_and_compact_canonical_json() -> None:
    payload = '{"CHS":[{"A":0.125,"N":"TEST_STIM_A","T":"tD"}],"FD":5,"FR":3,"SD":101}'
    request = replace(
        _request(RALLY_START_COMMAND),
        command=RALLY_APPLY_COMMAND,
        desired_state="RUNNING",
        runtime_state="APPLYING(A)",
        protocol_name="A",
        protocol_payload_json=payload,
        protocol_sha256="a" * 64,
        protocol_payload_sha256="b" * 64,
    )
    fixture = BinaryRallyFixture([b"RALLY_ERROR_SUCCESS"])
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
        assert worker.submit(request)
        assert done.wait(1.0)
        assert outcomes[0].status is RequestStatus.API_SUCCESS
        assert fixture.requests[0][0] == b"RealTimeControl " + payload.encode("utf-8")
        assert fixture.requests[0][1][1] != fixture.endpoint.port
    finally:
        worker.shutdown()
        assert worker.join(1.0)
        fixture.close()


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
        [None, RALLY_STOP_SUCCESS.encode("utf-8")]
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
        runtime.process_block(*_result(1, "N2"), generation=1)
        assert fixture.wait_for_count(2, timeout=1.0)
        assert [item[0] for item in fixture.requests] == [
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


def test_unsent_start_rejection_never_creates_compensating_stop() -> None:
    fixture = BinaryRallyFixture([])

    def transport_factory(**kwargs):
        original_before_send = kwargs["before_send"]

        def reject_start(request, now_ns):
            if request.command == RALLY_START_COMMAND:
                return False, "fixture send-time rejection"
            return original_before_send(request, now_ns)

        kwargs["before_send"] = reject_start
        return RallyControlTransportWorker(**kwargs)

    runtime = StimulationRuntime(
        request_timeout_seconds=0.2,
        rally_control_endpoint=fixture.endpoint,
        real_transport_factory=transport_factory,
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
        runtime.process_block(*_result(1, "N2"), generation=1)
        deadline = time.monotonic() + 1.0
        while runtime.real_status["last_outcome"] is None:
            assert time.monotonic() < deadline
            status_changed.wait(0.02)
            status_changed.clear()
        assert fixture.requests == []
        assert runtime.real_status["last_outcome"]["status"] == "not_sent"
        assert runtime.real_status["start_sent_responsibility"] is False
        assert runtime.real_status["independent_stop_required"] is False
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_close_cancels_before_send_without_compensating_stop() -> None:
    fixture = BinaryRallyFixture([])
    entered_before_send = threading.Event()
    release_before_send = threading.Event()

    def transport_factory(**kwargs):
        original_before_send = kwargs["before_send"]

        def gated_before_send(request, now_ns):
            entered_before_send.set()
            release_before_send.wait(1.0)
            return original_before_send(request, now_ns)

        kwargs["before_send"] = gated_before_send
        return RallyControlTransportWorker(**kwargs)

    runtime = StimulationRuntime(
        request_timeout_seconds=0.2,
        rally_control_endpoint=fixture.endpoint,
        real_transport_factory=transport_factory,
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
        runtime.process_block(*_result(1, "N2"), generation=1)
        assert entered_before_send.wait(1.0)
        runtime.on_session_stopping("窗口关闭；Start 尚未发送")
        release_before_send.set()
        deadline = time.monotonic() + 1.0
        while runtime.real_status["last_outcome"] is None:
            assert time.monotonic() < deadline
            status_changed.wait(0.02)
            status_changed.clear()
        assert fixture.requests == []
        assert runtime.real_status["start_sent_responsibility"] is False
        assert runtime.real_status["independent_stop_required"] is False
    finally:
        release_before_send.set()
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_cancelled_start_close_fault_and_shutdown_do_not_deadlock_or_send() -> None:
    fixture = BinaryRallyFixture([])
    entered_before_send = threading.Event()
    release_before_send = threading.Event()
    not_sent_callback_entered = threading.Event()

    def transport_factory(**kwargs):
        def gated_before_send(_request, _now_ns):
            entered_before_send.set()
            assert release_before_send.wait(2.0)
            return True, ""

        original_not_sent = kwargs["on_not_sent"]

        def observed_not_sent(*args, **callback_kwargs):
            not_sent_callback_entered.set()
            original_not_sent(*args, **callback_kwargs)

        kwargs["before_send"] = gated_before_send
        kwargs["on_not_sent"] = observed_not_sent
        return RallyControlTransportWorker(**kwargs)

    runtime = StimulationRuntime(
        request_timeout_seconds=0.2,
        rally_control_endpoint=fixture.endpoint,
        real_transport_factory=transport_factory,
    )
    pipeline = _PipelineFixture()
    shutdown_started = threading.Event()
    shutdown_done = threading.Event()
    shutdown_progressed_while_runtime_locked = False
    race_barrier = threading.Barrier(3)
    race_threads: list[threading.Thread] = []
    race_errors: list[BaseException] = []
    shutdown_thread: threading.Thread | None = None

    try:
        runtime.configure(_real_config())
        runtime.begin_session(
            "session-real", pipeline, False, generation=1, model_configured=True
        )
        runtime.set_session_ready(True)
        assert runtime.set_control_mode("real")
        assert runtime.set_automatic_enabled(True)
        runtime.process_block(*_result(1, "N2"), generation=1)
        assert entered_before_send.wait(1.0)

        def shutdown_worker() -> None:
            shutdown_started.set()
            runtime._real_transport.shutdown()
            shutdown_done.set()

        def close_and_fault() -> None:
            try:
                race_barrier.wait(timeout=1.0)
                runtime.on_session_stopping("并发收尾测试")
                runtime._handle_real_fault("并发故障测试")
            except BaseException as exc:  # preserve worker failures for the assertion
                race_errors.append(exc)

        with runtime._lock:
            # Match the production runtime→worker lock order and mark cancel
            # before allowing the command to reach its final send check.
            runtime._real_transport.cancel_unsent()
            release_before_send.set()
            assert not_sent_callback_entered.wait(1.0)

            shutdown_thread = threading.Thread(target=shutdown_worker, daemon=True)
            shutdown_thread.start()
            assert shutdown_started.wait(1.0)
            race_threads = [
                threading.Thread(target=close_and_fault, daemon=True)
                for _ in range(2)
            ]
            for thread in race_threads:
                thread.start()
            race_barrier.wait(timeout=1.0)
            shutdown_progressed_while_runtime_locked = shutdown_done.wait(1.0)

        assert shutdown_progressed_while_runtime_locked
        shutdown_thread.join(1.0)
        assert not shutdown_thread.is_alive()
        for thread in race_threads:
            thread.join(1.0)
            assert not thread.is_alive()
        assert race_errors == []

        runtime.shutdown()
        assert runtime.join(1.0)
        assert fixture.requests == []
        assert pipeline.leases == 0
    finally:
        release_before_send.set()
        runtime.shutdown()
        assert runtime.join(1.0)
        if shutdown_thread is not None:
            shutdown_thread.join(1.0)
        for thread in race_threads:
            thread.join(1.0)
        fixture.close()


def test_cancel_after_send_boundary_cannot_reclassify_start_as_unsent() -> None:
    fixture = BinaryRallyFixture([RALLY_START_SUCCESS.encode("utf-8")])
    boundary = threading.Event()
    release = threading.Event()
    outcome_done = threading.Event()
    outcomes = []

    def on_send_started(_request) -> None:
        boundary.set()
        release.wait(1.0)

    worker = RallyControlTransportWorker(
        on_send_started=on_send_started,
        on_sent=lambda *_args: None,
        on_not_sent=lambda *_args: None,
        on_outcome=lambda _request, outcome: (outcomes.append(outcome), outcome_done.set()),
        endpoint=fixture.endpoint,
        timeout_seconds=0.2,
    )
    try:
        assert worker.submit(_request())
        assert boundary.wait(1.0)
        worker.cancel_unsent()
        release.set()
        assert outcome_done.wait(1.0)
        assert outcomes[0].status is RequestStatus.API_SUCCESS
        assert fixture.wait_for_count(1)
        assert fixture.requests[0][0] == RALLY_START_COMMAND.encode("utf-8")
    finally:
        release.set()
        worker.shutdown()
        assert worker.join(1.0)
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
    block_id: int,
    stage: str,
    *,
    now_ns: int | None = None,
    session_id: str = "session-real",
) -> tuple[BlockContext, ProcessingResult]:
    now = time.monotonic_ns() if now_ns is None else now_ns
    block = DataBlock(
        data=np.zeros((1, 300), dtype=np.float32),
        start_sample=(block_id - 1) * 300,
        sample_rate_hz=10.0,
        labels=["C3"],
    )
    context = BlockContext(
        session_id=session_id,
        block_id=block_id,
        block=block,
        received_utc="2026-09-21T00:00:00Z",
        received_monotonic_ns=now,
    )
    result = ProcessingResult(
        session_id=session_id,
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


def _paradigm_result(
    block_id: int,
    stage: str,
    *,
    now_ns: int | None = None,
) -> tuple[BlockContext, ProcessingResult]:
    context, result = _result(block_id, stage, now_ns=now_ns)
    context.block.labels[:] = ["TEST_EEG_A"]
    model = replace(
        result.model,
        configuration={
            "channel_selection": {
                "requested_label": "TEST_EEG_A",
                "resolved_label": "TEST_EEG_A",
                "resolved_index": 0,
            }
        },
    )
    return context, replace(result, model=model)


def _armed_paradigm_runtime(
    fixture: BinaryRallyFixture,
    *,
    pipeline: _PipelineFixture | None = None,
    recording_enabled: bool = False,
    monotonic_ns=None,
    real_transport_factory=None,
    max_result_age_seconds: float = 5.0,
) -> tuple[StimulationRuntime, _PipelineFixture]:
    pipeline = pipeline or _PipelineFixture(recording_enabled=recording_enabled)
    runtime = StimulationRuntime(
        request_timeout_seconds=0.2,
        monotonic_ns=monotonic_ns or time.monotonic_ns,
        rally_control_endpoint=fixture.endpoint,
        real_transport_factory=real_transport_factory,
        allow_synthetic_paradigm_for_tests=True,
    )
    runtime.configure(
        replace(_real_config(), max_result_age_seconds=max_result_age_seconds)
    )
    runtime.select_paradigm(
        load_paradigm_package(
            Path(__file__).parent / "fixtures" / "issue6_paradigm_test_only"
        )
    )
    assert runtime.set_real_profile("paradigm")
    runtime.begin_session(
        "session-real",
        pipeline,
        recording_enabled,
        generation=1,
        model_configured=True,
    )
    runtime.set_session_ready(True)
    assert runtime.set_control_mode("real")
    assert runtime.set_automatic_enabled(True)
    return runtime, pipeline


class _FailingSendtoSocket:
    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def bind(self, address) -> None:
        self._socket.bind(address)

    def settimeout(self, timeout: float) -> None:
        self._socket.settimeout(timeout)

    def sendto(self, _data: bytes, _address) -> int:
        raise OSError("controlled sendto failure")

    def close(self) -> None:
        self._socket.close()


def test_paradigm_capacity_rejection_stops_once_and_releases_writer_lease() -> None:
    fixture = BinaryRallyFixture(
        [RALLY_START_SUCCESS.encode(), RALLY_STOP_SUCCESS.encode()]
    )

    def capacity_one_factory(**kwargs):
        return RallyControlTransportWorker(max_quarantined_sockets=1, **kwargs)

    runtime, pipeline = _armed_paradigm_runtime(
        fixture,
        recording_enabled=True,
        real_transport_factory=capacity_one_factory,
    )
    try:
        runtime.process_block(*_paradigm_result(1, "W"), generation=1)
        assert fixture.wait_for_count(2, timeout=1.0)
        _wait_until(
            lambda: runtime.real_status["confirmed_state"] == "STOPPED"
            and pipeline.leases == 0
        )
        time.sleep(0.03)
        assert [item[0] for item in fixture.requests] == [
            RALLY_START_COMMAND.encode(),
            RALLY_STOP_COMMAND.encode(),
        ]
        assert runtime.real_status["api_confirmed_protocol"] is None
        assert pipeline.leases == 0
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        assert not runtime._real_transport.active
        fixture.close()


def test_initial_start_submit_rejection_has_no_retry_or_stop() -> None:
    fixture = BinaryRallyFixture([])

    class RejectInitialStartWorker(RallyControlTransportWorker):
        def __init__(self, **kwargs):
            self.start_rejected = False
            super().__init__(**kwargs)

        def submit(self, request):
            if request.command == RALLY_START_COMMAND and not self.start_rejected:
                self.start_rejected = True
                return False
            return super().submit(request)

    runtime, pipeline = _armed_paradigm_runtime(
        fixture,
        recording_enabled=True,
        real_transport_factory=RejectInitialStartWorker,
    )
    try:
        runtime.process_block(*_paradigm_result(1, "W"), generation=1)
        assert fixture.requests == []
        assert runtime.real_status["start_sent_responsibility"] is False
        assert runtime.real_status["enabled"] is False
        assert runtime.real_status["last_outcome"]["status"] == "not_sent"
        assert pipeline.leases == 0
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_apply_sendto_unknown_does_not_retry_and_stops_once() -> None:
    fixture = BinaryRallyFixture(
        [RALLY_START_SUCCESS.encode(), RALLY_STOP_SUCCESS.encode()]
    )

    def failing_apply_factory(**kwargs):
        def socket_factory(request):
            if request.command == RALLY_APPLY_COMMAND:
                return _FailingSendtoSocket()
            return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        kwargs["socket_factory"] = socket_factory
        return RallyControlTransportWorker(**kwargs)

    runtime, pipeline = _armed_paradigm_runtime(
        fixture,
        recording_enabled=True,
        real_transport_factory=failing_apply_factory,
    )
    try:
        runtime.process_block(*_paradigm_result(1, "W"), generation=1)
        assert fixture.wait_for_count(2, timeout=1.0)
        _wait_until(
            lambda: runtime.real_status["confirmed_state"] == "STOPPED"
            and pipeline.leases == 0
        )
        apply_outcomes = [
            event
            for event in runtime.real_control_history
            if event.get("event_type") == "paradigm_control"
            and event["payload"].get("operation") == "apply"
            and event["payload"].get("phase") == "outcome"
        ]
        assert len(apply_outcomes) == 1
        assert apply_outcomes[0]["payload"]["outcome"] == "unknown"
        assert runtime.real_status["api_confirmed_protocol"] is None
        assert [item[0] for item in fixture.requests] == [
            RALLY_START_COMMAND.encode(),
            RALLY_STOP_COMMAND.encode(),
        ]
        assert pipeline.leases == 0
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_start_sendto_unknown_has_no_retry_or_stop_without_send_evidence() -> None:
    fixture = BinaryRallyFixture([])

    def failing_start_factory(**kwargs):
        def socket_factory(request):
            if request.command == RALLY_START_COMMAND:
                return _FailingSendtoSocket()
            return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        kwargs["socket_factory"] = socket_factory
        return RallyControlTransportWorker(**kwargs)

    runtime, pipeline = _armed_paradigm_runtime(
        fixture,
        recording_enabled=True,
        real_transport_factory=failing_start_factory,
    )
    try:
        runtime.process_block(*_paradigm_result(1, "W"), generation=1)
        _wait_until(lambda: runtime.real_status["last_outcome"] is not None)
        assert runtime.real_status["last_outcome"]["status"] == "unknown"
        assert runtime.real_status["start_sent_responsibility"] is False
        assert runtime.real_status["api_confirmed_protocol"] is None
        assert runtime.real_status["enabled"] is False
        assert fixture.requests == []
        assert pipeline.leases == 0
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_apply_rejected_at_expired_sd_deadline_does_not_retry() -> None:
    clock = _FakeClock(time.monotonic_ns())
    fixture = BinaryRallyFixture(
        [
            RALLY_START_SUCCESS.encode(),
            b"RALLY_ERROR_SUCCESS",
            RALLY_STOP_SUCCESS.encode(),
        ]
    )
    entered_apply_guard = threading.Event()
    release_apply_guard = threading.Event()

    def gated_factory(**kwargs):
        original_before_send = kwargs["before_send"]

        def gated_before_send(request, now_ns):
            if request.command == RALLY_APPLY_COMMAND and request.protocol_name == "B":
                entered_apply_guard.set()
                assert release_apply_guard.wait(1.0)
            return original_before_send(request, now_ns)

        kwargs["before_send"] = gated_before_send
        return RallyControlTransportWorker(**kwargs)

    runtime, pipeline = _armed_paradigm_runtime(
        fixture,
        recording_enabled=True,
        monotonic_ns=clock,
        real_transport_factory=gated_factory,
        max_result_age_seconds=100.0,
    )
    try:
        runtime.process_block(
            *_paradigm_result(1, "W", now_ns=clock.value), generation=1
        )
        assert fixture.wait_for_count(2, timeout=1.0)
        _wait_until(lambda: runtime.real_status["api_confirmed_protocol"] == "A")
        expiry = runtime.real_status["estimated_expiry_monotonic_ns"]
        assert isinstance(expiry, int)

        runtime.process_block(
            *_paradigm_result(2, "N1", now_ns=clock.value), generation=1
        )
        assert entered_apply_guard.wait(1.0)
        clock.value = expiry
        release_apply_guard.set()

        assert fixture.wait_for_count(3, timeout=1.0)
        _wait_until(
            lambda: runtime.real_status["runtime_state"] == "EXPIRED/UNKNOWN"
            and pipeline.leases == 0
        )
        assert [item[0] for item in fixture.requests] == [
            RALLY_START_COMMAND.encode(),
            fixture.requests[1][0],
            RALLY_STOP_COMMAND.encode(),
        ]
        assert fixture.requests[1][0].startswith(b"RealTimeControl ")
        assert runtime.real_status["enabled"] is False
        assert runtime.real_status["api_confirmed_protocol"] is None
        assert sum(
            item[0].startswith(b"RealTimeControl ") for item in fixture.requests
        ) == 1
        assert any(
            event.get("event_type") == "paradigm_control"
            and event["payload"].get("action") == "expired"
            for event in runtime.real_control_history
        )
        assert pipeline.leases == 0
    finally:
        release_apply_guard.set()
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_superseded_apply_replans_only_latest_eligible_protocol_once() -> None:
    fixture = BinaryRallyFixture(
        [
            RALLY_START_SUCCESS.encode(),
            b"RALLY_ERROR_SUCCESS",
            b"RALLY_ERROR_SUCCESS",
            RALLY_STOP_SUCCESS.encode(),
        ]
    )
    entered_apply_guard = threading.Event()
    release_apply_guard = threading.Event()

    def gated_factory(**kwargs):
        original_before_send = kwargs["before_send"]

        def gated_before_send(request, now_ns):
            if request.command == RALLY_APPLY_COMMAND and request.protocol_name == "B":
                entered_apply_guard.set()
                assert release_apply_guard.wait(1.0)
            return original_before_send(request, now_ns)

        kwargs["before_send"] = gated_before_send
        return RallyControlTransportWorker(**kwargs)

    runtime, _pipeline = _armed_paradigm_runtime(
        fixture, real_transport_factory=gated_factory
    )
    try:
        runtime.process_block(*_paradigm_result(1, "W"), generation=1)
        assert fixture.wait_for_count(2, timeout=1.0)
        _wait_until(lambda: runtime.real_status["api_confirmed_protocol"] == "A")

        runtime.process_block(*_paradigm_result(2, "N1"), generation=1)
        assert entered_apply_guard.wait(1.0)
        runtime.process_block(*_paradigm_result(3, "N2"), generation=1)
        release_apply_guard.set()

        assert fixture.wait_for_count(3, timeout=1.0)
        _wait_until(lambda: runtime.real_status["api_confirmed_protocol"] == "C")
        package = load_paradigm_package(
            Path(__file__).parent / "fixtures" / "issue6_paradigm_test_only"
        )
        assert json.loads(fixture.requests[2][0].partition(b" ")[2]) == (
            package.protocol_map["C"].payload
        )
        assert all(
            b'"N":"TEST_STIM_B"' not in item[0]
            for item in fixture.requests
        )

        runtime.set_automatic_enabled(False)
        assert fixture.wait_for_count(4, timeout=1.0)
        _wait_until(lambda: runtime.real_status["confirmed_state"] == "STOPPED")
        assert sum(item[0].startswith(b"RealTimeControl ") for item in fixture.requests) == 2
    finally:
        release_apply_guard.set()
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def _wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


def test_synthetic_paradigm_is_blocked_from_real_arming_without_test_override() -> None:
    fixture = BinaryRallyFixture([])
    runtime = StimulationRuntime(
        request_timeout_seconds=0.05,
        rally_control_endpoint=fixture.endpoint,
    )
    try:
        runtime.configure(_real_config())
        runtime.select_paradigm(load_paradigm_package(
            Path(__file__).parent / "fixtures" / "issue6_paradigm_test_only"
        ))
        assert runtime.set_real_profile("paradigm")
        runtime.begin_session(
            "session-real", _PipelineFixture(), False, generation=1, model_configured=True
        )
        runtime.set_session_ready(True)
        assert runtime.set_control_mode("real")
        assert runtime.set_automatic_enabled(True) is False
        assert fixture.requests == []
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_paradigm_stage_mapping_uses_start_apply_switch_stop_and_noop() -> None:
    fixture = BinaryRallyFixture([b"RALLY_ERROR_SUCCESS"] * 8)
    runtime = StimulationRuntime(
        request_timeout_seconds=0.2,
        rally_control_endpoint=fixture.endpoint,
        allow_synthetic_paradigm_for_tests=True,
    )
    try:
        runtime.configure(_real_config())
        runtime.select_paradigm(load_paradigm_package(
            Path(__file__).parent / "fixtures" / "issue6_paradigm_test_only"
        ))
        assert runtime.set_real_profile("paradigm")
        runtime.begin_session(
            "session-real", _PipelineFixture(), False, generation=1, model_configured=True
        )
        runtime.set_session_ready(True)
        assert runtime.set_control_mode("real")
        assert runtime.set_automatic_enabled(True)

        runtime.process_block(*_paradigm_result(1, "W"), generation=1)
        assert fixture.wait_for_count(2)
        _wait_until(lambda: runtime.real_status["api_confirmed_protocol"] == "A")
        assert fixture.requests[0][0] == b"Start Stim"
        assert fixture.requests[1][0].startswith(b"RealTimeControl {")

        runtime.process_block(*_paradigm_result(2, "W"), generation=1)
        time.sleep(0.03)
        assert len(fixture.requests) == 2

        runtime.process_block(*_paradigm_result(3, "N1"), generation=1)
        assert fixture.wait_for_count(3)
        _wait_until(lambda: runtime.real_status["api_confirmed_protocol"] == "B")
        assert fixture.requests[2][0].startswith(b"RealTimeControl {")

        runtime.process_block(*_paradigm_result(4, "N2"), generation=1)
        assert fixture.wait_for_count(4)
        _wait_until(lambda: runtime.real_status["api_confirmed_protocol"] == "C")
        assert fixture.requests[3][0].startswith(b"RealTimeControl {")

        runtime.process_block(*_paradigm_result(5, "N3"), generation=1)
        assert fixture.wait_for_count(5)
        _wait_until(lambda: runtime.real_status["confirmed_state"] == "STOPPED")
        assert fixture.requests[4][0] == b"Stop Stim"

        runtime.process_block(*_paradigm_result(6, "REM"), generation=1)
        time.sleep(0.03)
        assert len(fixture.requests) == 5

        runtime.process_block(*_paradigm_result(7, "W"), generation=1)
        assert fixture.wait_for_count(7)
        _wait_until(lambda: runtime.real_status["api_confirmed_protocol"] == "A")
        assert fixture.requests[5][0] == b"Start Stim"

        runtime.set_automatic_enabled(False)
        assert fixture.wait_for_count(8)
        _wait_until(lambda: runtime.real_status["confirmed_state"] == "STOPPED")
        assert fixture.requests[7][0] == b"Stop Stim"
        assert runtime.real_status["physical_output_confirmed"] is False
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_start_and_apply_inflight_keep_only_latest_paradigm_protocol() -> None:
    fixture = BinaryRallyFixture(
        [None, b"RALLY_ERROR_SUCCESS", None, b"RALLY_ERROR_SUCCESS", RALLY_STOP_SUCCESS.encode()]
    )
    runtime = StimulationRuntime(
        request_timeout_seconds=0.8,
        rally_control_endpoint=fixture.endpoint,
        allow_synthetic_paradigm_for_tests=True,
    )
    try:
        runtime.configure(_real_config())
        runtime.select_paradigm(load_paradigm_package(
            Path(__file__).parent / "fixtures" / "issue6_paradigm_test_only"
        ))
        assert runtime.set_real_profile("paradigm")
        runtime.begin_session("session-real", _PipelineFixture(), False, generation=1, model_configured=True)
        runtime.set_session_ready(True)
        assert runtime.set_control_mode("real")
        assert runtime.set_automatic_enabled(True)

        runtime.process_block(*_paradigm_result(1, "W"), generation=1)
        assert fixture.wait_for_count(1)
        runtime.process_block(*_paradigm_result(2, "N1"), generation=1)
        runtime.process_block(*_paradigm_result(3, "N2"), generation=1)
        time.sleep(0.03)
        assert len(fixture.requests) == 1
        fixture.reply_to(0, b"RALLY_ERROR_SUCCESS")
        assert fixture.wait_for_count(2)
        _wait_until(lambda: runtime.real_status["api_confirmed_protocol"] == "C")
        assert b'"A":0.25' in fixture.requests[1][0]
        assert b'"N":"TEST_STIM_B"' not in fixture.requests[1][0]

        runtime.process_block(*_paradigm_result(4, "W"), generation=1)
        assert fixture.wait_for_count(3)
        assert b'"A":0.125' in fixture.requests[2][0]
        runtime.process_block(*_paradigm_result(5, "N1"), generation=1)
        runtime.process_block(*_paradigm_result(6, "N2"), generation=1)
        time.sleep(0.03)
        assert len(fixture.requests) == 3
        fixture.reply_to(2, b"RALLY_ERROR_SUCCESS")
        count_reached = fixture.wait_for_count(4)
        assert count_reached, (
            f"runtime={runtime.real_status!r}; "
            f"requests={[item[0] for item in fixture.requests]!r}; "
            "history=["
            + ", ".join(
                f"{event['payload'].get('phase')}:{event['payload'].get('operation')}:{event['payload'].get('outcome')}:{event['payload'].get('reason')}"
                for event in runtime.real_control_history[-6:]
            )
            + "]"
        )
        _wait_until(lambda: runtime.real_status["api_confirmed_protocol"] == "C")
        assert b'"A":0.25' in fixture.requests[3][0]
        assert len(fixture.requests) == 4

        runtime.set_automatic_enabled(False)
        assert fixture.wait_for_count(5)
        _wait_until(lambda: runtime.real_status["confirmed_state"] == "STOPPED")
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_n3_during_sent_start_has_stop_priority_and_skips_apply() -> None:
    fixture = BinaryRallyFixture([None, RALLY_STOP_SUCCESS.encode("utf-8")])
    runtime = StimulationRuntime(
        request_timeout_seconds=0.8,
        rally_control_endpoint=fixture.endpoint,
        allow_synthetic_paradigm_for_tests=True,
    )
    try:
        runtime.configure(_real_config())
        runtime.select_paradigm(load_paradigm_package(
            Path(__file__).parent / "fixtures" / "issue6_paradigm_test_only"
        ))
        assert runtime.set_real_profile("paradigm")
        runtime.begin_session("session-real", _PipelineFixture(), False, generation=1, model_configured=True)
        runtime.set_session_ready(True)
        assert runtime.set_control_mode("real")
        assert runtime.set_automatic_enabled(True)
        runtime.process_block(*_paradigm_result(1, "W"), generation=1)
        assert fixture.wait_for_count(1)
        runtime.process_block(*_paradigm_result(2, "N3"), generation=1)
        fixture.reply_to(0, b"RALLY_ERROR_SUCCESS")
        assert fixture.wait_for_count(2)
        _wait_until(lambda: runtime.real_status["confirmed_state"] == "STOPPED")
        assert [request[0] for request in fixture.requests] == [b"Start Stim", b"Stop Stim"]
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_paradigm_apply_rejection_stops_once_and_never_confirms_target() -> None:
    fixture = BinaryRallyFixture(
        [
            b"RALLY_ERROR_SUCCESS",
            b"RALLY_ERROR_INVALID_CHANNEL",
            RALLY_STOP_SUCCESS.encode("utf-8"),
        ]
    )
    runtime = StimulationRuntime(
        request_timeout_seconds=0.2,
        rally_control_endpoint=fixture.endpoint,
        allow_synthetic_paradigm_for_tests=True,
    )
    try:
        runtime.configure(_real_config())
        runtime.select_paradigm(load_paradigm_package(
            Path(__file__).parent / "fixtures" / "issue6_paradigm_test_only"
        ))
        assert runtime.set_real_profile("paradigm")
        runtime.begin_session("session-real", _PipelineFixture(), False, generation=1, model_configured=True)
        runtime.set_session_ready(True)
        assert runtime.set_control_mode("real")
        assert runtime.set_automatic_enabled(True)
        runtime.process_block(*_paradigm_result(1, "W"), generation=1)
        assert fixture.wait_for_count(3)
        _wait_until(lambda: runtime.real_status["confirmed_state"] == "STOPPED")
        assert runtime.real_status["enabled"] is False
        assert runtime.real_status["api_confirmed_protocol"] is None
        assert runtime.real_status["runtime_state"] == "STOPPED"
        assert [request[0] for request in fixture.requests] == [
            b"Start Stim",
            fixture.requests[1][0],
            b"Stop Stim",
        ]
        assert fixture.requests[1][0].startswith(b"RealTimeControl {")
        time.sleep(0.03)
        assert len(fixture.requests) == 3
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_paradigm_expiry_revokes_run_stops_once_and_requires_rearm() -> None:
    clock = _FakeClock(time.monotonic_ns())
    fixture = BinaryRallyFixture([b"RALLY_ERROR_SUCCESS"] * 3)
    runtime = StimulationRuntime(
        request_timeout_seconds=0.2,
        monotonic_ns=clock,
        rally_control_endpoint=fixture.endpoint,
        allow_synthetic_paradigm_for_tests=True,
    )
    try:
        runtime.configure(replace(_real_config(), max_result_age_seconds=100.0))
        runtime.select_paradigm(load_paradigm_package(
            Path(__file__).parent / "fixtures" / "issue6_paradigm_test_only"
        ))
        assert runtime.set_real_profile("paradigm")
        runtime.begin_session("session-real", _PipelineFixture(), False, generation=1, model_configured=True)
        runtime.set_session_ready(True)
        assert runtime.set_control_mode("real")
        assert runtime.set_automatic_enabled(True)
        runtime.process_block(*_paradigm_result(1, "W", now_ns=clock.value), generation=1)
        assert fixture.wait_for_count(2)
        _wait_until(lambda: runtime.real_status["api_confirmed_protocol"] == "A")
        expiry = runtime.real_status["estimated_expiry_monotonic_ns"]
        assert isinstance(expiry, int)
        assert runtime.tick(expiry - 1) is False
        assert runtime.tick(expiry) is True
        assert fixture.wait_for_count(3)
        _wait_until(lambda: runtime.real_status["confirmed_state"] == "STOPPED")
        assert runtime.real_status["runtime_state"] == "EXPIRED/UNKNOWN"
        assert runtime.real_status["enabled"] is False
        assert fixture.requests[2][0] == b"Stop Stim"

        runtime.process_block(*_paradigm_result(2, "W", now_ns=expiry), generation=1)
        time.sleep(0.03)
        assert len(fixture.requests) == 3
        assert runtime.set_automatic_enabled(True) is True
        assert runtime.real_status["enabled"] is True
        assert len(fixture.requests) == 3
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_runtime_armed_idle_and_binary_target_non_target_are_idempotent() -> None:
    fixture = BinaryRallyFixture(
        [
            RALLY_START_SUCCESS.encode("utf-8"),
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
        assert runtime.real_status["runtime_state"] == "ARMED/IDLE"
        assert runtime.real_status["operator_confirmed"] is True
        assert fixture.requests == []

        # A non-target result while armed is a no-op and must not send a
        # Stop merely because the session exists.
        runtime.process_block(*_result(1, "W"), generation=1)
        time.sleep(0.05)
        assert fixture.requests == []

        context, result = _result(2, "N2")
        events, _evaluation = runtime.process_block(context, result, generation=1)
        assert events and events[0]["payload"]["phase"] == "decision"
        assert fixture.wait_for_count(1)
        assert fixture.requests[0][0] == RALLY_START_COMMAND.encode("utf-8")
        wait_for_status(lambda: runtime.real_status["confirmed_state"] == "RUNNING")
        duplicate, _ = runtime.process_block(context, result, generation=1)
        assert duplicate == ()
        runtime.process_block(*_result(3, "N2"), generation=1)
        time.sleep(0.05)
        assert len(fixture.requests) == 1

        runtime.process_block(*_result(4, "W"), generation=1)
        assert fixture.wait_for_count(2)
        assert fixture.requests[1][0] == RALLY_STOP_COMMAND.encode("utf-8")
        wait_for_status(lambda: runtime.real_status["confirmed_state"] == "STOPPED")
        assert runtime.real_status["runtime_state"] == "ARMED/IDLE"
        runtime.process_block(*_result(5, "W"), generation=1)
        time.sleep(0.05)
        assert len(fixture.requests) == 2

        runtime.process_block(*_result(6, "N2"), generation=1)
        assert fixture.wait_for_count(3)
        assert fixture.requests[2][0] == RALLY_START_COMMAND.encode("utf-8")
        wait_for_status(lambda: runtime.real_status["confirmed_state"] == "RUNNING")
        runtime.set_automatic_enabled(False)
        assert fixture.wait_for_count(4)
        assert fixture.requests[3][0] == RALLY_STOP_COMMAND.encode("utf-8")
        wait_for_status(lambda: runtime.real_status["confirmed_state"] == "STOPPED")
        assert len(fixture.requests) == 4
        assert pipeline.leases == 0
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_rearming_ignores_old_and_prearm_results_until_a_new_result_arrives() -> None:
    clock = _FakeClock()
    fixture = BinaryRallyFixture(
        [
            RALLY_START_SUCCESS.encode("utf-8"),
            RALLY_STOP_SUCCESS.encode("utf-8"),
            RALLY_START_SUCCESS.encode("utf-8"),
            RALLY_STOP_SUCCESS.encode("utf-8"),
        ]
    )
    pipeline = _PipelineFixture()
    runtime = StimulationRuntime(
        request_timeout_seconds=0.2,
        monotonic_ns=clock,
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
            "session-real", pipeline, False, generation=1, model_configured=True
        )
        runtime.set_session_ready(True)
        assert runtime.set_control_mode("real")
        assert runtime.set_automatic_enabled(True)

        first_context, first_result = _result(1, "N2", now_ns=clock.value)
        runtime.process_block(first_context, first_result, generation=1)
        assert fixture.wait_for_count(1)
        wait_for_status(lambda: runtime.real_status["confirmed_state"] == "RUNNING")

        runtime.set_automatic_enabled(False, reason="rearm boundary fixture")
        assert fixture.wait_for_count(2)
        wait_for_status(lambda: runtime.real_status["confirmed_state"] == "STOPPED")

        # This result is completed before the second enable but delivered only
        # after it. Its received age remains valid, so completion time is the
        # only boundary that can reject it without manufacturing a fault.
        old_context, old_result = _result(2, "N2", now_ns=clock.value)
        overlap_context, overlap_result = _result(3, "N2", now_ns=clock.value)
        clock.advance_seconds(2.0)
        overlap_result = replace(
            overlap_result, finished_monotonic_ns=clock.value + 1
        )
        armed_at_ns = clock.value
        assert runtime.set_automatic_enabled(True)
        assert overlap_result.started_monotonic_ns < armed_at_ns
        assert overlap_result.finished_monotonic_ns > armed_at_ns
        duplicate_old, _ = runtime.process_block(
            first_context, first_result, generation=1
        )
        delayed_old, _ = runtime.process_block(
            old_context, old_result, generation=1
        )
        assert duplicate_old == ()
        assert delayed_old == ()
        assert len(fixture.requests) == 2
        assert runtime.real_status["enabled"] is True
        assert runtime.real_status["runtime_state"] == "ARMED/IDLE"

        new_events, _ = runtime.process_block(
            overlap_context, overlap_result, generation=1
        )
        assert new_events and new_events[0]["payload"]["command"] == RALLY_START_COMMAND
        assert fixture.wait_for_count(3)
        wait_for_status(lambda: runtime.real_status["confirmed_state"] == "RUNNING")
        duplicate_new, _ = runtime.process_block(
            overlap_context, overlap_result, generation=1
        )
        assert duplicate_new == ()
        assert len(fixture.requests) == 3
        assert [request[0] for request in fixture.requests] == [
            RALLY_START_COMMAND.encode("utf-8"),
            RALLY_STOP_COMMAND.encode("utf-8"),
            RALLY_START_COMMAND.encode("utf-8"),
        ]
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        assert pipeline.leases == 0
        fixture.close()


def test_disarmed_blocks_advance_session_high_water_without_false_gap() -> None:
    clock = _FakeClock()
    fixture = BinaryRallyFixture(
        [
            RALLY_START_SUCCESS.encode("utf-8"),
            RALLY_STOP_SUCCESS.encode("utf-8"),
        ]
    )
    pipeline = _PipelineFixture()
    runtime = StimulationRuntime(
        request_timeout_seconds=0.2,
        monotonic_ns=clock,
        rally_control_endpoint=fixture.endpoint,
    )
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
        runtime.set_automatic_enabled(False, reason="disabled high-water fixture")

        for block_id in (1, 2, 3):
            runtime.process_block(
                *_result(block_id, "N2", now_ns=clock.value), generation=1
            )
        clock.advance_seconds(2.0)
        assert runtime.set_automatic_enabled(True)

        duplicate, _ = runtime.process_block(
            *_result(3, "N2", now_ns=clock.value - 2_000_000_000), generation=1
        )
        assert duplicate == ()
        runtime.process_block(*_result(4, "W", now_ns=clock.value), generation=1)
        assert runtime.real_status["enabled"] is True
        assert runtime.real_status["runtime_state"] == "ARMED/IDLE"
        assert fixture.requests == []

        runtime.process_block(*_result(5, "N2", now_ns=clock.value), generation=1)
        assert fixture.wait_for_count(1)
        deadline = time.monotonic() + 1.0
        while runtime.real_status["confirmed_state"] != "RUNNING":
            assert time.monotonic() < deadline
            status_changed.wait(0.02)
            status_changed.clear()
        assert [request[0] for request in fixture.requests] == [
            RALLY_START_COMMAND.encode("utf-8")
        ]
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        assert pipeline.leases == 0
        fixture.close()


def test_new_session_resets_block_high_water_for_restarted_block_ids() -> None:
    clock = _FakeClock()
    fixture = BinaryRallyFixture(
        [
            RALLY_START_SUCCESS.encode("utf-8"),
            RALLY_STOP_SUCCESS.encode("utf-8"),
            RALLY_START_SUCCESS.encode("utf-8"),
            RALLY_STOP_SUCCESS.encode("utf-8"),
        ]
    )
    first_pipeline = _PipelineFixture()
    second_pipeline = None
    runtime = StimulationRuntime(
        request_timeout_seconds=0.2,
        monotonic_ns=clock,
        rally_control_endpoint=fixture.endpoint,
    )
    status_changed = threading.Event()
    runtime.rally_status_changed.connect(lambda _status: status_changed.set())

    def wait_for_state(state: str) -> None:
        deadline = time.monotonic() + 1.0
        while runtime.real_status["confirmed_state"] != state:
            assert time.monotonic() < deadline
            status_changed.wait(0.02)
            status_changed.clear()

    try:
        runtime.configure(_real_config())
        runtime.begin_session(
            "session-real", first_pipeline, False, generation=1, model_configured=True
        )
        runtime.set_session_ready(True)
        assert runtime.set_control_mode("real")
        assert runtime.set_automatic_enabled(True)
        runtime.process_block(*_result(8, "N2", now_ns=clock.value), generation=1)
        assert fixture.wait_for_count(1)
        wait_for_state("RUNNING")
        runtime.set_automatic_enabled(False, reason="finish first session")
        assert fixture.wait_for_count(2)
        wait_for_state("STOPPED")
        runtime.finish_session("session-real")

        second_pipeline = _PipelineFixture()
        second_pipeline.session_id = "session-next"
        runtime.begin_session(
            "session-next", second_pipeline, False, generation=2, model_configured=True
        )
        runtime.set_session_ready(True)
        assert runtime.set_automatic_enabled(True)
        runtime.process_block(
            *_result(1, "N2", now_ns=clock.value, session_id="session-next"),
            generation=2,
        )
        assert fixture.wait_for_count(3)
        wait_for_state("RUNNING")
        assert [request[0] for request in fixture.requests[:3]] == [
            RALLY_START_COMMAND.encode("utf-8"),
            RALLY_STOP_COMMAND.encode("utf-8"),
            RALLY_START_COMMAND.encode("utf-8"),
        ]
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        assert first_pipeline.leases == 0
        if second_pipeline is not None:
            assert second_pipeline.leases == 0
        fixture.close()


def test_real_runtime_tick_stops_a_silent_stream_after_configured_age() -> None:
    clock = _FakeClock()
    fixture = BinaryRallyFixture(
        [
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
        context, result = _result(1, "N2", now_ns=clock.value)
        runtime.process_block(context, result, generation=1)
        assert fixture.wait_for_count(1)
        deadline = time.monotonic() + 1.0
        while runtime.real_status["confirmed_state"] != "RUNNING":
            assert time.monotonic() < deadline
            status_changed.wait(0.02)
            status_changed.clear()
        clock.advance_seconds(6.0)
        assert runtime.tick() is True
        assert fixture.wait_for_count(2)
        deadline = time.monotonic() + 1.0
        while runtime.real_status["confirmed_state"] != "STOPPED":
            assert time.monotonic() < deadline
            status_changed.wait(0.02)
            status_changed.clear()
        assert runtime.real_status["enabled"] is False
        assert runtime.real_status["runtime_state"] == "STOPPED"
        assert [item[0] for item in fixture.requests] == [
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
        [RALLY_START_SUCCESS.encode("utf-8"), None]
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
        runtime.process_block(*_result(1, "N2", now_ns=clock.value), generation=1)
        assert fixture.wait_for_count(1)
        deadline = time.monotonic() + 1.0
        while runtime.real_status["confirmed_state"] != "RUNNING":
            assert time.monotonic() < deadline
            status_changed.wait(0.02)
            status_changed.clear()
        runtime.process_block(*_result(2, "W", now_ns=clock.value), generation=1)
        assert fixture.wait_for_count(2)
        # A newer target result arrives while Stop is in flight, then becomes
        # stale before Stop's reply. It must not be reused for a new Start.
        runtime.process_block(*_result(3, "N2", now_ns=clock.value), generation=1)
        clock.advance_seconds(6.0)
        fixture.reply_to(1, RALLY_STOP_SUCCESS.encode("utf-8"))
        deadline = time.monotonic() + 1.0
        while runtime.real_status["runtime_state"] != "STOPPED":
            assert time.monotonic() < deadline
            status_changed.wait(0.02)
            status_changed.clear()
        assert runtime.real_status["enabled"] is False
        assert len(fixture.requests) == 2
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_inflight_start_receives_stop_priority_on_user_disable() -> None:
    fixture = BinaryRallyFixture(
        [None, RALLY_STOP_SUCCESS.encode("utf-8")]
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
        runtime.process_block(*_result(1, "N2"), generation=1)
        assert fixture.wait_for_count(1)
        runtime.set_automatic_enabled(False, reason="fixture user exit")
        assert fixture.wait_for_count(2, timeout=1.0)
        deadline = time.monotonic() + 1.0
        while runtime.real_status["confirmed_state"] != "STOPPED":
            assert time.monotonic() < deadline
            status_changed.wait(0.02)
            status_changed.clear()
        assert [item[0] for item in fixture.requests] == [
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
        context, result = _result(1, "N2")
        runtime.process_block(context, result, generation=1)
        wait_for_count(2)
        deadline = time.monotonic() + 1.0
        while runtime.real_status["runtime_state"] != "STOPPED":
            assert time.monotonic() < deadline
            status_changed.wait(0.05)
            status_changed.clear()
        assert [item[0] for item in fixture.requests] == [
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
        context, result = _result(1, "N2")
        runtime.process_block(context, result, generation=1)
        wait_until(lambda: runtime.real_status["confirmed_state"] == "RUNNING")
        context2, result2 = _result(2, "W")
        runtime.process_block(context2, result2, generation=1)
        wait_until(lambda: runtime.real_status["runtime_state"] == "FAULT/UNKNOWN")
        assert len(fixture.requests) == 2
        assert runtime.real_status["enabled"] is False
        assert runtime.real_status["independent_stop_required"] is True
        assert pipeline.leases == 0
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_old_generation_and_non_onnx_result_do_not_request_real_start() -> None:
    fixture = BinaryRallyFixture([])
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
        time.sleep(0.05)
        assert len(fixture.requests) == 0
        assert runtime.real_status["confirmed_state"] is None
        assert runtime.real_status["runtime_state"] == "FAULT/UNKNOWN"
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_recording_enabled_routes_real_control_events_through_pipeline_writer(tmp_path) -> None:
    fixture = BinaryRallyFixture(
        [RALLY_START_SUCCESS.encode("utf-8"), RALLY_STOP_SUCCESS.encode("utf-8")]
    )
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
        context, result = _result(1, "N2")
        runtime.process_block(
            replace(context, session_id="recorded-real-session"),
            replace(result, session_id="recorded-real-session"),
            generation=1,
        )
        assert fixture.wait_for_count(1)
        deadline = time.monotonic() + 1.0
        while runtime.real_status["confirmed_state"] != "RUNNING":
            assert time.monotonic() < deadline
            status_changed.wait(0.05)
            status_changed.clear()
        runtime.set_automatic_enabled(False)
        assert fixture.wait_for_count(2)
        pipeline.finish(cancelled=False, error=None)
        assert finished.wait(1.0)
        assert outcomes and outcomes[0].session_path
        reader = SessionReader(outcomes[0].session_path)
        assert [event["payload"]["phase"] for event in reader.control_events] == [
            "config",
            "decision",
            "sent",
            "outcome",
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
        context, result = _result(1, "N2")
        events, _evaluation = runtime.process_block(context, result, generation=1)
        assert events == ()
        assert fixture.wait_for_count(1)
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
        assert fixture.wait_for_count(2)
    finally:
        runtime.shutdown()
        assert runtime.join(1.0)
        fixture.close()


def test_eof_close_and_record_failure_keep_required_stop_alive_until_outcome() -> None:
    fixture = BinaryRallyFixture(
        [
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
        runtime.process_block(*_result(1, "N2"), generation=1)
        assert fixture.wait_for_count(1)
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
        assert fixture.wait_for_count(2)
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


def test_stage_csv_failure_keeps_required_rally_stop_and_jsonl_result(
    monkeypatch, tmp_path
) -> None:
    from sleep_stim_controller.staging import StagePrediction

    original_append = SessionWriter._append_stage_csv_result
    append_attempts: list[int] = []

    def fail_first_csv_append(writer, result) -> None:
        append_attempts.append(result.block_id)
        if len(append_attempts) == 1:
            raise OSError("injected derived CSV failure")
        original_append(writer, result)

    original_save_block = SessionWriter.save_block
    context_sizes_after_save: list[tuple[int, int]] = []
    writers: list[SessionWriter] = []

    def track_saved_contexts(writer, context) -> None:
        original_save_block(writer, context)
        writers.append(writer)
        context_sizes_after_save.append(
            (context.block_id, len(writer._stage_csv_contexts))
        )

    monkeypatch.setattr(SessionWriter, "_append_stage_csv_result", fail_first_csv_append)
    monkeypatch.setattr(SessionWriter, "save_block", track_saved_contexts)
    fixture = BinaryRallyFixture(
        [RALLY_START_SUCCESS.encode("utf-8"), RALLY_STOP_SUCCESS.encode("utf-8")]
    )
    statuses: list[str] = []
    ready = threading.Event()
    csv_failed = threading.Event()
    finished = threading.Event()
    outcomes = []

    def on_ready(error) -> None:
        assert error is None
        ready.set()

    def on_csv_status(status: str) -> None:
        statuses.append(status)
        if "injected derived CSV failure" in status:
            csv_failed.set()

    class Adapter:
        descriptor = ModelDescriptor(
            "onnx-sleep-staging:fixture",
            "sha256:fixture",
            confidence_meaning="fixture probability",
        )

        def prepare(self, _cancel_event) -> None:
            return None

        def predict(self, _context, _block, _cancel_event):
            return StagePrediction("N2", 0.5)

        def close(self, _cancel_event) -> None:
            return None

    runtime = StimulationRuntime(
        request_timeout_seconds=0.2,
        rally_control_endpoint=fixture.endpoint,
    )
    pipeline = ProcessingPipeline(
        session_id="csv-rally-stop",
        host="127.0.0.1",
        port=4455,
        recording_enabled=True,
        recording_root=tmp_path,
        stage_csv_enabled=True,
        pending_limit=16,
        model_factory=Adapter,
        on_ready=on_ready,
        on_stimulation_result=lambda context, result: runtime.process_block(
            context, result, generation=1
        ),
        on_stage_csv_status=on_csv_status,
        on_finished=lambda outcome: (outcomes.append(outcome), finished.set()),
    )
    try:
        runtime.configure(_real_config())
        runtime.begin_session(
            "csv-rally-stop", pipeline, True, generation=1, model_configured=True
        )
        runtime.set_session_ready(True)
        assert runtime.set_control_mode("real")
        assert runtime.set_automatic_enabled(True)
        pipeline.start()
        assert ready.wait(2.0)
        context, _ = _result(1, "N2", session_id="csv-rally-stop")
        assert pipeline.enqueue(context)
        assert csv_failed.wait(2.0)
        for block_id in range(2, 9):
            context, _ = _result(block_id, "N2", session_id="csv-rally-stop")
            assert pipeline.enqueue(context)
        assert fixture.wait_for_count(1)
        deadline = time.monotonic() + 1.0
        while runtime.real_status["confirmed_state"] != "RUNNING":
            assert time.monotonic() < deadline
            time.sleep(0.01)

        runtime.on_session_stopping("fixture session ended")
        assert fixture.wait_for_count(2)
        deadline = time.monotonic() + 1.0
        while runtime.real_status["confirmed_state"] != "STOPPED":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        pipeline.handoff_network_end(None, cancelled=False, error=None)
        pipeline.finish(cancelled=False, error=None)
        assert finished.wait(3.0)

        assert [payload for payload, _source in fixture.requests] == [
            RALLY_START_COMMAND.encode("utf-8"),
            RALLY_STOP_COMMAND.encode("utf-8"),
        ]
        assert outcomes[0].error is None
        assert any("injected derived CSV failure" in status for status in statuses)
        session_path = Path(outcomes[0].session_path)
        reader = SessionReader(session_path)
        assert len(reader.entries) == 8
        assert [entry.processing_result["status"] for entry in reader.entries] == [
            "success"
        ] * 8
        assert append_attempts == [1]
        assert context_sizes_after_save == [(1, 1)] + [
            (block_id, 0) for block_id in range(2, 9)
        ]
        assert writers and all(writer is writers[0] for writer in writers)
        assert writers[0]._stage_csv_contexts == {}
        phases = [event["payload"]["phase"] for event in reader.control_events]
        assert "sent" in phases and "outcome" in phases
        commands = [event["payload"]["command"] for event in reader.control_events]
        assert RALLY_START_COMMAND in commands
        assert RALLY_STOP_COMMAND in commands
        manifest = json.loads((session_path / "manifest.json").read_text("utf-8"))
        assert manifest["stage_csv"]["status"] == "failed"
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
