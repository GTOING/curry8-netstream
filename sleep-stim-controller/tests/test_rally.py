from __future__ import annotations

import json
import threading
import time
import uuid

import pytest

from sleep_stim_controller.rally import (
    LoopbackRallySimulator,
    RallyTransportWorker,
    SimulatedReply,
    SimulatedRallyEndpoint,
)
from sleep_stim_controller.stimulation import (
    RequestStatus,
    StimulationDecision,
    StimulusRequest,
    build_rally_message,
    parse_protocol_scheme,
)


def scheme():
    # Fixture-only protocol values; not an application default or experiment setting.
    return parse_protocol_scheme(
        {
            "schema_version": 1,
            "name": "UDP fixture",
            "protocol_version": "fixture-1",
            "initial_stimulus_channels": ["C3"],
            "return_channels": ["Ref"],
            "payload": {
                "SD": 1.0,
                "FR": 0.0,
                "FD": 0.0,
                "CHS": [{"N": "C3", "T": "tD", "A": 0.5}],
            },
        }
    )


def request(index: int) -> StimulusRequest:
    request_id = f"request-{index}"
    decision = StimulationDecision(
        decision_id=f"decision-{index}",
        session_id="session-fixture",
        block_id=index,
        config_version=2,
        stage="N2",
        model={"model_id": "fixture", "version": "1", "is_test_double": True},
        start_sample=(index - 1) * 300,
        end_sample_exclusive=index * 300,
        received_monotonic_ns=time.monotonic_ns(),
        decided_monotonic_ns=time.monotonic_ns(),
        allowed=True,
        reason="fixture-only candidate",
        request_id=request_id,
        test_double=True,
    )
    return StimulusRequest(
        request_id=request_id,
        decision=decision,
        protocol=scheme(),
        received_monotonic_ns=decision.received_monotonic_ns or 0,
        max_result_age_seconds=30.0,
    )


class TransportHarness:
    def __init__(
        self,
        simulator: LoopbackRallySimulator,
        *,
        timeout: float = 0.5,
        before_send=None,
    ) -> None:
        self.outcomes = []
        self.sent = []
        self.not_sent = []
        self.diagnostics = []
        self.stopped = threading.Event()
        self._outcome_events: list[threading.Event] = []
        self.worker = RallyTransportWorker(
            owns_endpoint=simulator.owns,
            before_send=before_send or (lambda _request, _now: (True, "", None)),
            on_sent=lambda *args: self.sent.append(args),
            on_not_sent=lambda *args: self.not_sent.append(args),
            on_outcome=self._on_outcome,
            on_diagnostic=self.diagnostics.append,
            on_stopped=self.stopped.set,
            timeout_seconds=timeout,
        )

    def _on_outcome(self, req, outcome) -> None:
        self.outcomes.append((req, outcome))
        if len(self._outcome_events) >= len(self.outcomes):
            self._outcome_events[len(self.outcomes) - 1].set()

    def submit_and_wait(self, req, endpoint, timeout: float = 2.0):
        event = threading.Event()
        self._outcome_events.append(event)
        assert self.worker.submit(req, endpoint)
        assert event.wait(timeout)
        return self.outcomes[-1][1]

    def close(self, simulator: LoopbackRallySimulator) -> None:
        endpoint = simulator.endpoint
        simulator.stop()
        if endpoint is not None:
            self.worker.release_endpoint(endpoint.owner_id)
        self.worker.shutdown()
        assert self.worker.join(2.0)
        assert self.stopped.is_set()


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("RALLY_ERROR_SUCCESS", RequestStatus.API_SUCCESS),
        ("RALLY_ERROR_START_STIM", RequestStatus.API_REJECTED),
        ("RALLY_ERROR_NOT_A_CODE", RequestStatus.UNKNOWN),
    ],
)
def test_transport_classifies_simulated_response_and_uses_vendor_wire_shape(
    reply, expected
) -> None:
    simulator = LoopbackRallySimulator(
        lambda _index, _data: (SimulatedReply(reply),)
    )
    endpoint = simulator.start()
    harness = TransportHarness(simulator)
    try:
        outcome = harness.submit_and_wait(request(1), endpoint)
        assert outcome.status is expected
        assert harness.worker.quarantined_socket_count == 1
        assert simulator.wait_for_requests(1)
        packet, (host, source_port) = simulator.requests[0]
        assert host == "127.0.0.1"
        assert source_port > 0
        assert packet == build_rally_message(request(1).protocol)
        assert json.loads(packet.decode().split(" ", 1)[1])["CHS"][0]["N"] == "C3"
        assert b"request_id" not in packet
    finally:
        harness.close(simulator)
    assert harness.worker.quarantined_socket_count == 0
    assert not simulator.active


def test_timeout_wrong_source_and_bad_utf8_are_unknown_without_retry() -> None:
    simulator = LoopbackRallySimulator(
        lambda _index, _data: (SimulatedReply(b"\xff"),)
    )
    endpoint = simulator.start()
    harness = TransportHarness(simulator, timeout=0.05)
    try:
        outcome = harness.submit_and_wait(request(1), endpoint)
        assert outcome.status is RequestStatus.UNKNOWN
        assert "UTF-8" in outcome.message
        assert simulator.wait_for_requests(1)
        assert len(simulator.requests) == 1
    finally:
        harness.close(simulator)

    simulator = LoopbackRallySimulator(
        lambda _index, _data: (
            SimulatedReply("RALLY_ERROR_SUCCESS", wrong_source=True),
        )
    )
    endpoint = simulator.start()
    harness = TransportHarness(simulator, timeout=0.08)
    try:
        outcome = harness.submit_and_wait(request(1), endpoint)
        assert outcome.status is RequestStatus.UNKNOWN
        assert "超时" in outcome.message
        assert any("非本应用模拟端响应来源" in message for message in harness.diagnostics)
        assert len(simulator.requests) == 1
    finally:
        harness.close(simulator)


def test_late_reply_from_timed_out_request_cannot_match_next_request() -> None:
    def replies(index, _data):
        if index == 1:
            return (SimulatedReply("RALLY_ERROR_START_STIM", delay_seconds=0.14),)
        return (SimulatedReply("RALLY_ERROR_SUCCESS", delay_seconds=0.12),)

    simulator = LoopbackRallySimulator(replies)
    endpoint = simulator.start()
    harness = TransportHarness(simulator, timeout=0.04)
    try:
        first = harness.submit_and_wait(request(1), endpoint)
        assert first.status is RequestStatus.UNKNOWN
        deadline = time.monotonic() + 1.0
        while harness.worker.busy and time.monotonic() < deadline:
            time.sleep(0.002)
        assert not harness.worker.busy
        harness.worker.set_timeout_seconds(0.4)
        second = harness.submit_and_wait(request(2), endpoint)
        assert second.status is RequestStatus.API_SUCCESS
        assert len(simulator.requests) == 2
        source_ports = [address[1] for _, address in simulator.requests]
        assert source_ports[0] != source_ports[1]
        assert harness.worker.quarantined_socket_count == 2
    finally:
        harness.close(simulator)


def test_duplicate_late_reply_stays_on_retired_request_socket() -> None:
    def replies(index, _data):
        if index == 1:
            return (
                SimulatedReply("RALLY_ERROR_SUCCESS"),
                SimulatedReply("RALLY_ERROR_START_STIM", delay_seconds=0.13),
            )
        return (SimulatedReply("RALLY_ERROR_SUCCESS", delay_seconds=0.16),)

    simulator = LoopbackRallySimulator(replies)
    endpoint = simulator.start()
    harness = TransportHarness(simulator, timeout=0.04)
    try:
        first = harness.submit_and_wait(request(1), endpoint)
        assert first.status is RequestStatus.API_SUCCESS
        deadline = time.monotonic() + 1.0
        while harness.worker.busy and time.monotonic() < deadline:
            time.sleep(0.002)
        assert not harness.worker.busy
        harness.worker.set_timeout_seconds(0.4)
        second = harness.submit_and_wait(request(2), endpoint)
        assert second.status is RequestStatus.API_SUCCESS
        assert len(simulator.requests) == 2
        assert simulator.requests[0][1][1] != simulator.requests[1][1][1]
    finally:
        harness.close(simulator)


def test_simulator_owns_only_random_loopback_endpoint_and_stops_its_resources() -> None:
    simulator = LoopbackRallySimulator()
    endpoint = simulator.start()
    assert endpoint.host == "127.0.0.1"
    assert endpoint.port not in {0, 8801}
    assert simulator.owns(endpoint)
    simulator.stop()
    assert not simulator.active
    assert not simulator.owns(endpoint)


def test_cancel_before_send_does_not_emit_udp_request() -> None:
    entered = threading.Event()
    continue_before_send = threading.Event()

    def before_send(_request, _now):
        entered.set()
        continue_before_send.wait(1.0)
        return True, "", "reservation"

    simulator = LoopbackRallySimulator()
    endpoint = simulator.start()
    harness = TransportHarness(simulator, before_send=before_send)
    try:
        assert harness.worker.submit(request(1), endpoint)
        assert entered.wait(1.0)
        harness.worker.cancel_unsent()
        continue_before_send.set()
        deadline = time.monotonic() + 1.0
        while not harness.not_sent and time.monotonic() < deadline:
            time.sleep(0.002)
        assert harness.not_sent
        assert harness.not_sent[0][3] is RequestStatus.NOT_SENT
        assert simulator.requests == ()
    finally:
        continue_before_send.set()
        harness.close(simulator)


def test_transport_refuses_endpoint_not_owned_by_this_simulator() -> None:
    simulator = LoopbackRallySimulator()
    endpoint = simulator.start()
    harness = TransportHarness(simulator)
    try:
        counterfeit = SimulatedRallyEndpoint(
            endpoint.host, endpoint.port, owner_id=uuid.uuid4().hex
        )
        assert not harness.worker.submit(request(1), counterfeit)
        assert simulator.requests == ()
        assert not harness.worker.busy
    finally:
        harness.close(simulator)
