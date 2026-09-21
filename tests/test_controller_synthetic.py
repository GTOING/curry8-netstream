from __future__ import annotations

import socket
import struct
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from curry_netstream.models import DataBlock
from curry_netstream.protocol import (
    BASIC_INFO_FORMAT,
    CHANNEL_INFO_FORMAT,
    DATA_EEG,
    DATA_INFO,
    DATA_TYPE_FLOAT32,
    FrameHeader,
    ID_DATA,
    INFO_BASIC_INFO,
    INFO_CHANNEL_INFO,
    REQUEST_BASIC_INFO,
    REQUEST_CHANNEL_INFO,
    REQUEST_STREAMING_START,
    REQUEST_STREAMING_STOP,
)
from sleep_stim_controller.controller import ConnectionState, CurrySessionController
from sleep_stim_controller.rally import (
    RALLY_START_COMMAND,
    RALLY_START_SUCCESS,
    RALLY_STOP_COMMAND,
    RALLY_STOP_SUCCESS,
    RallyControlEndpoint,
)


def channel_record(channel_id: int, label: str) -> bytes:
    raw_label = (label.encode("utf-16-le") + b"\x00\x00").ljust(80, b"\x00")
    return struct.pack(
        CHANNEL_INFO_FORMAT,
        channel_id,
        raw_label,
        0,
        0,
        0,
        0.0,
        0.0,
        0.0,
        0,
        -1,
        1.0,
        0,
        0,
        0,
    )


def data_frame(code: int, request: int, payload: bytes, sample: int = 0) -> bytes:
    return (
        FrameHeader(
            chid=ID_DATA,
            code=code,
            request=request,
            sample=sample,
            size1=len(payload),
            size2=0,
        ).pack()
        + payload
    )


def recv_exact(sock: socket.socket, size: int) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < size:
        try:
            chunk = sock.recv(size - len(chunks))
        except socket.timeout:
            continue
        if not chunk:
            return None
        chunks.extend(chunk)
    return bytes(chunks)


class SyntheticCurryServer:
    """One reusable loopback Curry handshake fixture with scenario modes."""

    def __init__(self, mode: str = "two_blocks", sample_origin: int = 100) -> None:
        self.mode = mode
        self.sample_origin = sample_origin
        self.requests: list[int] = []
        self.ready = threading.Event()
        self.accepted = threading.Event()
        self.partial_sent = threading.Event()
        self.release_stream = threading.Event()
        self.close_stream = threading.Event()
        self.done = threading.Event()
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, name="synthetic-curry")

    def __enter__(self) -> "SyntheticCurryServer":
        self.thread.start()
        assert self.ready.wait(2.0)
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self.release_stream.set()
        self.close_stream.set()
        try:
            self._listener.close()
        finally:
            self.thread.join(2.0)

    def _serve(self) -> None:
        connection: socket.socket | None = None
        try:
            self.ready.set()
            while not self._stop.is_set():
                try:
                    connection, _ = self._listener.accept()
                    break
                except socket.timeout:
                    continue
                except OSError:
                    return
            if connection is None:
                return
            with connection:
                connection.settimeout(0.2)
                self.accepted.set()
                while not self._stop.is_set():
                    raw = recv_exact(connection, 20)
                    if raw is None:
                        return
                    header = FrameHeader.unpack(raw)
                    self.requests.append(header.request)
                    if header.request == REQUEST_BASIC_INFO:
                        if self.mode == "wait_basic":
                            continue
                        if self.mode in {
                            "onnx_staging",
                            "onnx_staging_hold",
                            "onnx_staging_eof",
                            "onnx_staging_eof_hold",
                        }:
                            payload = struct.pack(
                                BASIC_INFO_FORMAT, 24, 1, 100, 4, 1, 1
                            )
                        else:
                            payload = struct.pack(
                                BASIC_INFO_FORMAT, 24, 2, 10, 4, 1, 1
                            )
                        connection.sendall(
                            data_frame(DATA_INFO, INFO_BASIC_INFO, payload)
                        )
                    elif header.request == REQUEST_CHANNEL_INFO:
                        if self.mode == "wait_channel":
                            continue
                        if self.mode in {
                            "onnx_staging",
                            "onnx_staging_hold",
                            "onnx_staging_eof",
                            "onnx_staging_eof_hold",
                        }:
                            payload = channel_record(1, "Fpz-Cz")
                        else:
                            payload = (
                                channel_record(1, "C3")
                                + channel_record(2, "C4")
                            )
                        connection.sendall(
                            data_frame(DATA_INFO, INFO_CHANNEL_INFO, payload)
                        )
                    elif header.request == REQUEST_STREAMING_START:
                        self._send_stream(connection)
                        if self.mode == "onnx_staging_eof_hold":
                            self.close_stream.wait(5.0)
                        if self.mode in {
                            "eof",
                            "short_packets_eof",
                            "onnx_staging_eof",
                            "onnx_staging_eof_hold",
                        }:
                            return
                    elif header.request == REQUEST_STREAMING_STOP:
                        return
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # A controller may close the synthetic peer on backlog/validation failure.
            return
        finally:
            self.done.set()

    def _block_frame(self, start_sample: int, n_samples: int = 300) -> bytes:
        if self.mode in {
            "onnx_staging",
            "onnx_staging_hold",
            "onnx_staging_eof",
            "onnx_staging_eof_hold",
        }:
            n_samples = 3000
            timeline = np.arange(n_samples, dtype=np.float32) / 100.0
            values = (
                20.0 * np.sin(2.0 * np.pi * 10.0 * timeline)
            ).astype("<f4")[:, None]
        else:
            values = (
                np.arange(n_samples * 2, dtype="<f4").reshape(n_samples, 2)
                + start_sample * 2
            )
        return data_frame(
            DATA_EEG,
            DATA_TYPE_FLOAT32,
            values.tobytes(),
            sample=start_sample,
        )

    def _nonfinite_block_frame(self) -> bytes:
        values = np.arange(300 * 2, dtype="<f4").reshape(300, 2)
        values[0, 0] = np.nan
        return data_frame(
            DATA_EEG,
            DATA_TYPE_FLOAT32,
            values.tobytes(),
            sample=self.sample_origin,
        )

    def _send_stream(self, connection: socket.socket) -> None:
        if self.mode in {"idle", "wait_basic", "wait_channel"}:
            return
        if self.mode == "header_partial":
            connection.sendall(self._block_frame(self.sample_origin)[:7])
            self.partial_sent.set()
            return
        if self.mode == "payload_partial":
            frame = self._block_frame(self.sample_origin)
            connection.sendall(frame[:20] + frame[20:70])
            self.partial_sent.set()
            return
        if self.mode == "invalid":
            connection.sendall(self._block_frame(self.sample_origin, n_samples=0))
            return
        if self.mode == "nonfinite":
            connection.sendall(self._nonfinite_block_frame())
            return
        first = self._block_frame(self.sample_origin)
        step = (
            3000
            if self.mode
            in {
                "onnx_staging",
                "onnx_staging_hold",
                "onnx_staging_eof",
                "onnx_staging_eof_hold",
            }
            else 300
        )
        second = self._block_frame(self.sample_origin + step)
        if self.mode == "fragmented":
            for part in (first[:7], first[7:20], first[20:53], first[53:]):
                connection.sendall(part)
                time.sleep(0.30)
            connection.sendall(second)
            return
        if self.mode == "discontinuous":
            connection.sendall(first)
            connection.sendall(self._block_frame(self.sample_origin + 301))
            return
        if self.mode == "hold_blocks":
            self.release_stream.wait(5.0)
        if self.mode in {"onnx_staging_hold", "onnx_staging_eof_hold"}:
            self.release_stream.wait(5.0)
        if self.mode == "many_blocks":
            for index in range(8):
                connection.sendall(
                    self._block_frame(self.sample_origin + 300 * index)
                )
            return
        if self.mode in {"short_packets", "short_packets_eof", "short_packets_hold"}:
            packet_sizes = [73, 227, 111, 189, 200]
            frames = []
            start = self.sample_origin
            for packet_size in packet_sizes:
                frames.append(self._block_frame(start, n_samples=packet_size))
                start += packet_size
            joined = b"".join(frames)
            # Exercise the Curry client's frame reassembly while each decoded
            # DataBlock remains a deliberately short transport packet.
            for offset in range(0, len(joined), 97):
                connection.sendall(joined[offset : offset + 97])
            return
        connection.sendall(first)
        connection.sendall(second)


class SyntheticBinaryRally:
    """Random-port binary Rally fixture for the P4-B end-to-end test."""

    def __init__(self, replies: list[bytes | None]) -> None:
        self._replies = list(replies)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.requests: list[tuple[bytes, tuple[str, int]]] = []
        self.request_changed = threading.Event()
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.settimeout(0.05)
        self.endpoint = RallyControlEndpoint("127.0.0.1", self._socket.getsockname()[1])
        self._thread = threading.Thread(target=self._run, name="synthetic-rally")
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data, source = self._socket.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                self.requests.append((bytes(data), (str(source[0]), int(source[1]))))
                reply = self._replies.pop(0) if self._replies else None
            self.request_changed.set()
            if reply is not None:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as responder:
                    responder.bind(("127.0.0.1", 0))
                    responder.sendto(reply, source)

    def wait_for_count(self, count: int, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                if len(self.requests) >= count:
                    return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.request_changed.wait(remaining)
            self.request_changed.clear()

    def close(self) -> None:
        self._stop.set()
        self._socket.close()
        self._thread.join(1.0)


def wait_until(qapp, predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    qapp.processEvents()
    return predicate()


def pump_until(qapp, predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Drive queued Qt callbacks while a worker-side Event controls progress."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
    qapp.processEvents()
    return predicate()


def test_real_loopback_handshake_two_blocks_and_bounded_handoff(qapp) -> None:
    with SyntheticCurryServer("two_blocks") as server:
        controller = CurrySessionController()
        assert controller.connect("127.0.0.1", server.port)
        assert wait_until(qapp, lambda: controller.buffer_stats().replaced_count >= 1)

        result = controller.take_latest_block()
        assert result is not None
        queued, stats = result
        assert queued.number == 2
        assert queued.block.n_samples == 300
        assert queued.block.labels == ["C3", "C4"]
        assert stats.replaced_count >= 1
        assert controller.state is ConnectionState.STREAMING

        assert controller.disconnect()
        assert wait_until(qapp, lambda: not controller.is_busy())
        assert controller.state is ConnectionState.DISCONNECTED
        assert REQUEST_STREAMING_STOP in server.requests


def test_fragmented_header_and_payload_survive_receive_timeouts() -> None:
    from curry_netstream.client import CurryClient

    with SyntheticCurryServer("fragmented") as server:
        client = CurryClient(
            "127.0.0.1", server.port, timeout=3.0, receive_timeout=0.1
        )
        blocks: list[DataBlock] = []
        try:
            client.connect()
            result = client.stream(blocks.append, max_blocks=2)
            assert result.completed
            assert result.blocks_received == 2
            assert [block.start_sample for block in blocks] == [100, 400]
        finally:
            client.close()


def test_short_curry_packets_are_assembled_into_windows_and_recorded(
    qapp, tmp_path, monkeypatch
) -> None:
    """Exercise real TCP framing plus sample-point window assembly."""
    import json

    from sleep_stim_controller.recording import SessionReader

    utc_values = iter(f"packet-{index}" for index in range(1, 10))
    mono_values = iter(range(1001, 1010))
    from types import SimpleNamespace

    monkeypatch.setattr(
        "sleep_stim_controller.controller.utc_now_iso",
        lambda: next(utc_values),
    )
    monkeypatch.setattr(
        "sleep_stim_controller.controller.time",
        SimpleNamespace(monotonic_ns=lambda: next(mono_values)),
    )
    results = []
    with SyntheticCurryServer("short_packets", sample_origin=100) as server:
        controller = CurrySessionController()
        controller.processing_changed.connect(
            lambda result: results.append(result) if result is not None else None
        )
        assert controller.connect(
            "127.0.0.1",
            server.port,
            recording_enabled=True,
            recording_root=tmp_path,
        )
        assert wait_until(
            qapp,
            lambda: controller.assembly_snapshot is not None
            and controller.assembly_snapshot.completed_windows == 2
            and controller.assembly_snapshot.partial_samples == 200,
        )
        assert wait_until(qapp, lambda: len(results) == 2)
        snapshot = controller.assembly_snapshot
        assert snapshot is not None
        assert snapshot.received_packets == 5
        assert snapshot.received_samples == 800
        assert snapshot.completed_windows == 2
        assert snapshot.accepted_windows == 2
        assert snapshot.partial_start_sample == 700
        assert [result.start_sample for result in results] == [100, 400]
        assert controller.state is ConnectionState.STREAMING

        assert controller.disconnect()
        assert wait_until(qapp, lambda: controller.resources_released())

    session_path = Path(controller.session_path)
    reader = SessionReader(session_path)
    assert [entry.start_sample for entry in reader.entries] == [100, 400]
    expected = np.arange(2 * 800, dtype=np.float32).reshape(800, 2) + 200
    first, _ = reader.read_block(0)
    second, _ = reader.read_block(1)
    np.testing.assert_array_equal(first.data, expected[:300].T)
    np.testing.assert_array_equal(second.data, expected[300:600].T)
    assert reader.session_finished_payload is not None
    assert reader.session_finished_payload["stream_assembly"] == {
        "window_seconds": 30.0,
        "window_samples": 300,
        "received_packets": 5,
        "received_samples": 800,
        "completed_windows": 2,
        "accepted_windows": 2,
        "partial_samples": 200,
        "partial_start_sample": 700,
    }
    events = [
        json.loads(line)
        for line in (session_path / "events.jsonl").read_text("utf-8").splitlines()
    ]
    block_events = [event for event in events if event["event_type"] == "block_saved"]
    assert [event["payload"]["received_utc"] for event in block_events] == [
        "packet-2",
        "packet-4",
    ]
    assert [event["payload"]["received_monotonic_ns"] for event in block_events] == [
        1002,
        1004,
    ]


def test_short_packet_progress_is_not_emitted_once_per_packet(qapp) -> None:
    progress = []
    with SyntheticCurryServer("short_packets") as server:
        controller = CurrySessionController()
        controller.assembly_progress_changed.connect(progress.append)
        assert controller.connect("127.0.0.1", server.port)
        assert wait_until(
            qapp,
            lambda: controller.assembly_snapshot is not None
            and controller.assembly_snapshot.received_packets == 5,
        )
        assert controller.disconnect()
        assert wait_until(qapp, lambda: controller.resources_released())
    # One handshake snapshot, a bounded timer flush and the final snapshot are
    # sufficient; network packet count must not become Qt signal count.
    assert len(progress) <= 4


def test_silent_stream_can_be_cancelled_without_error(qapp) -> None:
    with SyntheticCurryServer("idle") as server:
        controller = CurrySessionController()
        assert controller.connect("127.0.0.1", server.port)
        assert wait_until(qapp, lambda: controller.state is ConnectionState.STREAMING)

        started = time.monotonic()
        assert controller.disconnect()
        assert wait_until(qapp, lambda: not controller.is_busy())
        assert time.monotonic() - started < 5.0
        assert controller.state is ConnectionState.DISCONNECTED


def test_remote_eof_and_invalid_block_are_visible_errors(qapp) -> None:
    errors: list[str] = []
    with SyntheticCurryServer("eof") as server:
        controller = CurrySessionController()
        controller.error_changed.connect(errors.append)
        assert controller.connect("127.0.0.1", server.port)
        assert wait_until(qapp, lambda: controller.state is ConnectionState.ERROR)
        assert any("服务器关闭" in error for error in errors)

    errors.clear()
    with SyntheticCurryServer("invalid") as server:
        controller = CurrySessionController()
        controller.error_changed.connect(errors.append)
        assert controller.connect("127.0.0.1", server.port)
        assert wait_until(qapp, lambda: controller.state is ConnectionState.ERROR)
        assert any("EEG payload 为空" in error for error in errors)


def test_duplicate_connection_is_rejected_and_reconnect_uses_new_session(qapp) -> None:
    with SyntheticCurryServer("idle") as first:
        controller = CurrySessionController()
        assert controller.connect("127.0.0.1", first.port)
        assert not controller.connect("127.0.0.1", first.port)
        assert wait_until(qapp, lambda: controller.state is ConnectionState.STREAMING)
        assert controller.disconnect()
        assert wait_until(qapp, lambda: not controller.is_busy())

    with SyntheticCurryServer("two_blocks") as second:
        assert controller.connect("127.0.0.1", second.port)
        assert wait_until(qapp, lambda: controller.buffer_stats().pending)
        assert controller.disconnect()
        assert wait_until(qapp, lambda: not controller.is_busy())


@pytest.mark.parametrize(
    "mode",
    ["wait_basic", "wait_channel", "header_partial", "payload_partial"],
)
def test_handshake_and_partial_frame_waits_cancel_and_release_resources(
    qapp,
    mode: str,
) -> None:
    with SyntheticCurryServer(mode) as server:
        controller = CurrySessionController()
        assert controller.connect("127.0.0.1", server.port)
        assert wait_until(qapp, lambda: server.accepted.is_set())
        if mode == "wait_basic":
            assert wait_until(qapp, lambda: REQUEST_BASIC_INFO in server.requests)
        elif mode == "wait_channel":
            assert wait_until(qapp, lambda: REQUEST_CHANNEL_INFO in server.requests)
        else:
            assert wait_until(qapp, lambda: server.partial_sent.is_set())

        started = time.monotonic()
        assert controller.disconnect()
        assert wait_until(
            qapp,
            lambda: controller.resources_released() and server.done.is_set(),
        )
        assert time.monotonic() - started < 5.0
        assert not controller.worker_alive()
        assert controller.state is ConnectionState.DISCONNECTED


@pytest.mark.parametrize("mode", ["idle", "payload_partial"])
def test_active_window_close_waits_for_worker_and_socket_cleanup(qapp, mode: str) -> None:
    from sleep_stim_controller.app import build_application

    with SyntheticCurryServer(mode) as server:
        application, window, controller = build_application()
        window.show()
        try:
            assert controller.connect("127.0.0.1", server.port)
            assert wait_until(qapp, lambda: controller.state is ConnectionState.STREAMING)
            if mode == "payload_partial":
                assert wait_until(qapp, lambda: server.partial_sent.is_set())

            window.close()
            assert window.isVisible()
            assert wait_until(
                qapp,
                lambda: (
                    not window.isVisible()
                    and controller.resources_released()
                    and server.done.is_set()
                ),
            )
            assert not controller.worker_alive()
        finally:
            if controller.is_busy():
                controller.disconnect()
                wait_until(qapp, lambda: controller.resources_released())
            window.close()


def test_pending_block_is_historical_after_eof_before_gui_poll(qapp) -> None:
    from sleep_stim_controller.app import build_application, pump_latest

    with SyntheticCurryServer("eof") as server:
        application, window, controller = build_application()
        window._p1_poll_timer.stop()  # deterministic: emulate timer not firing yet
        window.show()
        try:
            assert controller.connect("127.0.0.1", server.port)
            assert wait_until(
                qapp,
                lambda: controller.state is ConnectionState.ERROR
                and controller.buffer_stats().pending,
            )
            assert pump_latest(controller, window)
            assert "历史数据" in window.data_status_label.text()
            assert "会话错误" in window.data_status_label.text()
            assert window.error_label.isVisible()
            assert controller.resources_released()
        finally:
            window.close()


def test_pending_block_is_historical_after_active_disconnect(qapp) -> None:
    from sleep_stim_controller.app import build_application, pump_latest

    with SyntheticCurryServer("two_blocks") as server:
        application, window, controller = build_application()
        window._p1_poll_timer.stop()
        window.show()
        try:
            assert controller.connect("127.0.0.1", server.port)
            assert wait_until(
                qapp,
                lambda: controller.state is ConnectionState.STREAMING
                and controller.buffer_stats().pending,
            )
            assert controller.disconnect()
            assert wait_until(qapp, lambda: controller.resources_released())
            assert pump_latest(controller, window)
            assert "历史数据" in window.data_status_label.text()
            assert "用户主动断开" in window.data_status_label.text()
        finally:
            window.close()


def test_reconnect_discards_old_pending_block_and_new_block_is_current(qapp) -> None:
    from sleep_stim_controller.app import build_application, pump_latest

    application, window, controller = build_application()
    window._p1_poll_timer.stop()
    window.show()
    try:
        with SyntheticCurryServer("two_blocks") as first:
            assert controller.connect("127.0.0.1", first.port)
            assert wait_until(qapp, lambda: controller.buffer_stats().pending)
            assert controller.disconnect()
            assert wait_until(qapp, lambda: controller.resources_released())

        with SyntheticCurryServer("hold_blocks", sample_origin=1000) as second:
            assert controller.connect("127.0.0.1", second.port)
            assert wait_until(qapp, lambda: controller.state is ConnectionState.STREAMING)
            # connect() clears the previous session's pending item before the
            # new stream can deliver anything.
            assert controller.buffer_stats().delivered_count == 0
            assert not controller.buffer_stats().pending
            second.release_stream.set()
            assert wait_until(qapp, lambda: controller.buffer_stats().pending)
            result = controller.take_latest_block()
            assert result is not None
            queued, _ = result
            assert queued.block.start_sample == 1300
            assert not queued.historical
            window.show_block(
                queued.block,
                queued.number,
                0,
                queued.historical,
                queued.history_reason,
            )
            assert "最新有效块 #2" in window.data_status_label.text()
            assert "start_sample=1300" in window.data_status_label.text()
            assert "历史数据" not in window.data_status_label.text()
            assert controller.disconnect()
            assert wait_until(qapp, lambda: controller.resources_released())
    finally:
        window.close()


@pytest.mark.parametrize("first_mode", ["short_packets_eof", "short_packets_hold"])
def test_partial_tail_isolated_across_eof_cancel_and_reconnect(
    qapp, first_mode: str
) -> None:
    controller = CurrySessionController()
    try:
        with SyntheticCurryServer(first_mode) as first:
            assert controller.connect("127.0.0.1", first.port)
            assert pump_until(
                qapp,
                lambda: (
                    controller.assembly_snapshot is not None
                    and controller.assembly_snapshot.completed_windows == 2
                    and controller.assembly_snapshot.partial_samples == 200
                ),
            )
            if first_mode == "short_packets_eof":
                assert pump_until(
                    qapp,
                    lambda: controller.resources_released() and first.done.is_set(),
                )
                assert controller.state is ConnectionState.ERROR
            else:
                assert controller.disconnect()
                assert pump_until(
                    qapp,
                    lambda: controller.resources_released() and first.done.is_set(),
                )
                assert controller.state is ConnectionState.DISCONNECTED
            first_snapshot = controller.assembly_snapshot
            assert first_snapshot is not None
            assert first_snapshot.partial_start_sample == 700

        with SyntheticCurryServer("short_packets_hold", sample_origin=1000) as second:
            assert controller.connect("127.0.0.1", second.port)
            # A new generation starts with no residual assembler state.
            assert controller.assembly_snapshot is None
            assert pump_until(
                qapp,
                lambda: (
                    controller.assembly_snapshot is not None
                    and controller.assembly_snapshot.received_packets == 5
                ),
            )
            snapshot = controller.assembly_snapshot
            assert snapshot is not None
            assert snapshot.completed_windows == 2
            assert snapshot.partial_samples == 200
            assert snapshot.partial_start_sample == 1600
            assert controller.disconnect()
            assert pump_until(qapp, controller.resources_released)
    finally:
        if controller.is_busy():
            controller.disconnect()
            pump_until(qapp, controller.resources_released)


def test_stale_generation_callbacks_cannot_replace_new_assembly_progress(qapp) -> None:
    controller = CurrySessionController()
    try:
        with SyntheticCurryServer("short_packets_hold") as first:
            assert controller.connect("127.0.0.1", first.port)
            assert pump_until(
                qapp,
                lambda: controller.assembly_snapshot is not None
                and controller.assembly_snapshot.received_packets == 5,
            )
            old_generation = controller._generation
            old_cancel_event = controller._cancel_event
            assert controller.disconnect()
            assert pump_until(qapp, controller.resources_released)

        with SyntheticCurryServer("short_packets_hold", sample_origin=2000) as second:
            assert controller.connect("127.0.0.1", second.port)
            assert pump_until(
                qapp,
                lambda: controller.assembly_snapshot is not None
                and controller.assembly_snapshot.received_packets == 5,
            )
            before = controller.assembly_snapshot
            assert before is not None

            # These emulate a late callback from the already-closed first
            # worker. They must not finalize or replace the second assembler.
            controller._accept_block(
                old_generation,
                old_cancel_event,
                DataBlock(
                    data=np.zeros((2, 300), dtype=np.float32),
                    start_sample=700,
                    sample_rate_hz=10.0,
                    labels=["C3", "C4"],
                ),
            )
            controller._on_network_finished(
                old_generation,
                True,
                RuntimeError("late old generation"),
            )
            assert controller._finalize_assembly(old_generation) is None
            qapp.processEvents()
            after = controller.assembly_snapshot
            assert after == before
            assert controller.state is ConnectionState.STREAMING
            assert controller.disconnect()
            assert pump_until(qapp, controller.resources_released)
    finally:
        if controller.is_busy():
            controller.disconnect()
            pump_until(qapp, controller.resources_released)


@pytest.mark.parametrize(
    ("mode", "message"),
    [("discontinuous", "采样不连续"), ("nonfinite", "NaN")],
)
def test_discontinuous_and_nonfinite_tcp_data_reach_visible_error(
    qapp,
    mode: str,
    message: str,
) -> None:
    from sleep_stim_controller.app import build_application

    with SyntheticCurryServer(mode) as server:
        application, window, controller = build_application()
        window._p1_poll_timer.stop()
        window.show()
        try:
            assert controller.connect("127.0.0.1", server.port)
            assert wait_until(
                qapp,
                lambda: controller.state is ConnectionState.ERROR
                and window.error_label.isVisible(),
            )
            assert message in window.error_label.text()
            assert controller.resources_released()
        finally:
            window.close()


def test_loopback_pipeline_processes_every_accepted_block_independent_of_summary_buffer(
    qapp,
) -> None:
    from sleep_stim_controller.staging import ProcessingStatus

    results = []
    with SyntheticCurryServer("two_blocks") as server:
        controller = CurrySessionController()
        controller.processing_changed.connect(
            lambda result: results.append(result) if result is not None else None
        )
        assert controller.connect("127.0.0.1", server.port)
        assert wait_until(
            qapp,
            lambda: len(results) == 2
            and controller.buffer_stats().replaced_count >= 1,
        )
        assert [result.block_id for result in results] == [1, 2]
        assert all(result.status is ProcessingStatus.UNAVAILABLE for result in results)
        assert all(result.stage is None for result in results)
        assert controller.session_path is None
        assert controller.disconnect()
        assert wait_until(qapp, lambda: controller.resources_released())
        assert controller.pipeline_outcome.accepted_blocks == 2
        assert controller.pipeline_outcome.saved_blocks == 0


def test_gui_loopback_runs_packaged_onnx_and_displays_sleep_stage(qapp) -> None:
    from sleep_stim_controller.app import build_application
    from sleep_stim_controller.onnx_staging import STAGE_LABELS, default_model_path
    from sleep_stim_controller.staging import ProcessingStatus

    with SyntheticCurryServer("onnx_staging") as server:
        application, window, controller = build_application()
        window.show()
        try:
            window.host_edit.setText("127.0.0.1")
            window.port_spin.setValue(server.port)
            window.model_path_edit.setText(str(default_model_path()))
            window.model_enabled_checkbox.setChecked(True)
            assert window.model_configuration()["channel_name"] == "Fpz-Cz"
            window.connect_button.click()
            assert wait_until(
                qapp,
                lambda: controller.latest_processing is not None
                and controller.latest_processing.block_id == 2,
                timeout=15.0,
            )
            assert not window.model_enabled_checkbox.isEnabled()
            result = controller.latest_processing
            assert result.status is ProcessingStatus.SUCCESS
            assert result.stage in STAGE_LABELS
            assert result.confidence is not None
            assert result.model.model_id.startswith("onnx-sleep-staging:")
            assert result.stage in window.processing_status_label.text()
            assert "confidence=" in window.processing_status_label.text()
            assert "Fpz-Cz" in window.available_model_channels_label.text()
            window.disconnect_button.click()
            assert wait_until(qapp, lambda: controller.resources_released())
        finally:
            window.close()


def test_loopback_recording_and_window_wired_offline_replay(
    qapp, tmp_path, monkeypatch
) -> None:
    import json

    from sleep_stim_controller.app import build_application
    from sleep_stim_controller.staging import NoModelAdapter

    selected_replay: dict[str, str] = {}

    def choose_directory(parent, title, *args):
        if title == "选择已保存的会话目录":
            return selected_replay.get("path", "")
        return str(tmp_path)

    monkeypatch.setattr(
        "sleep_stim_controller.app.QFileDialog.getExistingDirectory",
        choose_directory,
    )
    application, window, controller = build_application(model_factory=NoModelAdapter)
    window.show()
    try:
        window.choose_recording_dir_button.click()
        window.recording_checkbox.setChecked(True)
        with SyntheticCurryServer("two_blocks") as server:
            window.host_edit.setText("127.0.0.1")
            window.port_spin.setValue(server.port)
            window.connect_button.click()
            assert wait_until(
                qapp,
                lambda: controller.state is ConnectionState.STREAMING
                and controller.latest_processing is not None
                and controller.latest_processing.block_id == 2,
            )
            assert "记录中：" in window.recording_status_label.text()
            assert window.disconnect_button.isEnabled()
            window.disconnect_button.click()
            assert wait_until(qapp, lambda: controller.resources_released())

        session_path = Path(controller.session_path)
        selected_replay["path"] = str(session_path)
        manifest = json.loads((session_path / "manifest.json").read_text("utf-8"))
        assert manifest["status"] == "closed"
        assert manifest["model"]["model_id"] == "none"
        assert manifest["counts"]["saved_blocks"] == 2
        assert manifest["counts"]["processing_results"] == 2

        request_count = server.requests.count(REQUEST_STREAMING_START)
        window.open_replay_button.click()
        assert wait_until(
            qapp,
            lambda: "离线回放" in window.data_status_label.text()
            and "block_id=1" in window.block_metadata_label.text(),
        )
        assert not window.connect_button.isEnabled()
        assert "未接入" in window.processing_status_label.text()
        assert not window.replay_previous_button.isEnabled()
        assert window.replay_next_button.isEnabled()
        window.replay_next_button.click()
        assert wait_until(
            qapp,
            lambda: "block_id=2" in window.block_metadata_label.text(),
        )
        assert window.replay_previous_button.isEnabled()
        assert not window.replay_next_button.isEnabled()
        assert not controller.connect("127.0.0.1", server.port)
        assert server.requests.count(REQUEST_STREAMING_START) == request_count
        # Corrupt only this temporary fixture after a good block was shown.
        (session_path / "blocks/00000001.npy").write_bytes(b"invalid-npy")
        window.replay_previous_button.click()
        assert wait_until(qapp, lambda: "损坏或不可读" in window.data_status_label.text())
        assert not window._has_displayed_block
        assert "block_id=2" not in window.block_metadata_label.text()
        assert "block_id=2" not in window.processing_status_label.text()
        assert window._p3_replay_events == ()
        window.replay_index_spin.setValue(2)
        assert wait_until(qapp, lambda: "block_id=2" in window.block_metadata_label.text())
        assert server.requests.count(REQUEST_STREAMING_START) == request_count
        window.exit_replay_button.click()
        assert wait_until(
            qapp,
            lambda: window.connect_button.isEnabled()
            and not window._p2_replay_worker.active,
        )
        assert not controller.is_busy()
    finally:
        replay_worker = getattr(window, "_p2_replay_worker", None)
        if replay_worker is not None and replay_worker.active:
            replay_worker.close()
            assert wait_until(qapp, lambda: not replay_worker.active)
        if controller.is_busy():
            controller.disconnect()
            assert wait_until(qapp, lambda: controller.resources_released())
        window.close()


@pytest.mark.parametrize("replay_kind", ["recorded", "empty", "failed"])
def test_replay_mode_isolates_disconnected_live_pending_block(
    qapp, tmp_path, monkeypatch, replay_kind: str
) -> None:
    from sleep_stim_controller.app import build_application, pump_latest
    from sleep_stim_controller.recording import SessionReader

    def make_recorded_session(mode: str, sample_origin: int) -> Path:
        recorder = CurrySessionController()
        session_path: str | None = None
        with SyntheticCurryServer(mode, sample_origin=sample_origin) as server:
            assert recorder.connect(
                "127.0.0.1",
                server.port,
                recording_enabled=True,
                recording_root=tmp_path,
            )
            try:
                if mode == "two_blocks":
                    assert wait_until(
                        qapp,
                        lambda: recorder.latest_processing is not None
                        and recorder.latest_processing.block_id == 2,
                    )
                else:
                    assert wait_until(
                        qapp,
                        lambda: recorder.state is ConnectionState.STREAMING,
                    )
                assert recorder.disconnect()
                assert wait_until(qapp, lambda: recorder.resources_released())
                session_path = recorder.session_path
            finally:
                if recorder.is_busy():
                    recorder.disconnect()
                    assert wait_until(qapp, lambda: recorder.resources_released())
        assert session_path is not None
        return Path(session_path)

    if replay_kind == "recorded":
        replay_path = make_recorded_session("two_blocks", sample_origin=900)
        recorded = SessionReader(replay_path)
        assert recorded.overview.block_count == 2
        assert recorded.entries[0].start_sample == 900
    elif replay_kind == "empty":
        replay_path = make_recorded_session("idle", sample_origin=900)
        assert SessionReader(replay_path).overview.block_count == 0
    else:
        replay_path = tmp_path / "missing-replay-session"

    selected_replay = {"path": str(replay_path)}

    def choose_directory(parent, title, *args):
        if title == "选择已保存的会话目录":
            return selected_replay["path"]
        return str(tmp_path)

    monkeypatch.setattr(
        "sleep_stim_controller.app.QFileDialog.getExistingDirectory",
        choose_directory,
    )
    application, window, controller = build_application()
    window._p1_poll_timer.stop()
    window.show()
    try:
        with SyntheticCurryServer("two_blocks", sample_origin=100) as live_server:
            assert controller.connect("127.0.0.1", live_server.port)
            assert wait_until(
                qapp,
                lambda: controller.state is ConnectionState.STREAMING
                and controller.buffer_stats().pending
                and controller.buffer_stats().replaced_count >= 1,
            )
            assert controller.disconnect()
            assert wait_until(qapp, lambda: controller.resources_released())

        # The disconnected P1 history block is deliberately still pending,
        # and is from another session/sample interval than this replay.
        assert controller.buffer_stats().pending
        selected_replay["path"] = str(replay_path)
        window.open_replay_button.click()

        # Exercise the normal summary-pump entry point while loading and after
        # the replay result arrives. It must never consume live-session data.
        assert not controller.buffer_stats().pending
        assert not pump_latest(controller, window)

        if replay_kind == "recorded":
            assert wait_until(
                qapp,
                lambda: "block_id=1" in window.block_metadata_label.text()
                and "block_id=1" in window.processing_status_label.text(),
            )
            assert "[900, 1200)" in window.block_metadata_label.text()
            assert "start_sample=900" in window.data_status_label.text()
            assert "start_sample=400" not in window.data_status_label.text()
            assert "离线回放" in window.processing_status_label.text()
            summary_before = window.block_metadata_label.text()
            result_before = window.processing_status_label.text()

            assert not pump_latest(controller, window)
            assert window.block_metadata_label.text() == summary_before
            assert window.processing_status_label.text() == result_before
            assert "block_id=1" in window.block_metadata_label.text()
            assert "[900, 1200)" in window.block_metadata_label.text()
            assert "block_id=1" in window.processing_status_label.text()

            window.exit_replay_button.click()
            assert wait_until(
                qapp,
                lambda: window.connect_button.isEnabled()
                and not window._p2_replay_worker.active,
            )
        elif replay_kind == "empty":
            assert wait_until(
                qapp,
                lambda: "0 个已记录块" in window.replay_status_label.text()
                and window._p2_replay_worker.active,
            )
            assert not window._has_displayed_block
            assert not pump_latest(controller, window)
            assert "start_sample=400" not in window.data_status_label.text()

            window.exit_replay_button.click()
            assert wait_until(
                qapp,
                lambda: window.connect_button.isEnabled()
                and not window._p2_replay_worker.active,
            )
        else:
            assert wait_until(
                qapp,
                lambda: window.error_label.isVisible()
                and "无法打开离线回放" in window.error_label.text()
                and window.connect_button.isEnabled()
                and not window._p2_replay_worker.active,
            )
            assert not window._has_displayed_block
            assert not pump_latest(controller, window)

        assert not controller.buffer_stats().pending
        assert not pump_latest(controller, window)
        assert "start_sample=400" not in window.data_status_label.text()
        if replay_kind == "recorded":
            assert "start_sample=900" in window._last_block_summary
            assert "[900, 1200)" in window.block_metadata_label.text()
        else:
            assert not window._has_displayed_block
            assert "start_sample=400" not in window.block_metadata_label.text()
    finally:
        replay_worker = getattr(window, "_p2_replay_worker", None)
        if replay_worker is not None and replay_worker.active:
            replay_worker.close()
            assert wait_until(qapp, lambda: not replay_worker.active)
        if controller.is_busy():
            controller.disconnect()
            assert wait_until(qapp, lambda: controller.resources_released())
        window.close()


def test_loopback_slow_processor_overflow_is_visible_and_stops_stream(qapp) -> None:
    from sleep_stim_controller.staging import (
        ModelDescriptor,
        PredictionCancelled,
        StagePrediction,
    )

    class SlowAdapter:
        descriptor = ModelDescriptor("slow-fixture", "test", is_test_double=True)

        def prepare(self, cancel_event):
            return None

        def predict(self, block, data, cancel_event):
            cancel_event.wait(2)
            if cancel_event.is_set():
                raise PredictionCancelled("stopped on overflow")
            return StagePrediction("N2")

        def close(self, cancel_event):
            return None

    with SyntheticCurryServer("many_blocks") as server:
        controller = CurrySessionController(
            model_factory=SlowAdapter,
            pending_block_limit=1,
        )
        errors = []
        controller.error_changed.connect(errors.append)
        assert controller.connect("127.0.0.1", server.port)
        assert wait_until(qapp, lambda: controller.state is ConnectionState.ERROR)
        assert wait_until(qapp, lambda: controller.resources_released())
        assert "队列已满" in errors[-1]
        assert controller.pipeline_outcome.rejected_blocks >= 1
        assert controller.pipeline_outcome.accepted_blocks <= 2
        snapshot = controller.assembly_snapshot
        assert snapshot is not None
        assert snapshot.completed_windows >= snapshot.accepted_windows
        assert snapshot.accepted_windows == controller.pipeline_outcome.accepted_blocks
        assert controller.pipeline_outcome.stream_assembly == snapshot.to_dict()
        assert server.requests.count(REQUEST_STREAMING_START) == 1


def test_recording_initialization_failure_does_not_start_curry(qapp, tmp_path) -> None:
    with SyntheticCurryServer("idle") as server:
        controller = CurrySessionController()
        errors = []
        controller.error_changed.connect(errors.append)
        assert controller.connect(
            "127.0.0.1",
            server.port,
            recording_enabled=True,
            recording_root=tmp_path / "missing-parent",
        )
        assert wait_until(qapp, lambda: controller.resources_released())
        assert controller.state is ConnectionState.ERROR
        assert not server.accepted.is_set()
        assert any("保存父目录" in error for error in errors)


def test_connection_failure_retains_failed_session_and_valid_prefix(qapp, tmp_path) -> None:
    import json

    from sleep_stim_controller.recording import SessionReader

    with SyntheticCurryServer("eof") as server:
        controller = CurrySessionController()
        assert controller.connect(
            "127.0.0.1",
            server.port,
            recording_enabled=True,
            recording_root=tmp_path,
        )
        assert wait_until(qapp, lambda: controller.resources_released())
        assert controller.state is ConnectionState.ERROR
        session_path = Path(controller.session_path)
        manifest = json.loads((session_path / "manifest.json").read_text("utf-8"))
        assert manifest["status"] == "failed"
        assert manifest["counts"]["saved_blocks"] == 2
        reader = SessionReader(session_path)
        assert reader.overview.incomplete
        assert [entry.block_id for entry in reader.entries] == [1, 2]
        assert all(entry.processing_result["status"] == "unavailable" for entry in reader.entries)


def test_processing_failure_waits_for_network_handoff_and_finishes_failed_archive(
    qapp, tmp_path, monkeypatch
) -> None:
    import json

    from sleep_stim_controller.recording import SessionReader, SessionWriter

    save_entered = threading.Event()
    release_failure = threading.Event()
    original_save = SessionWriter.save_processing_result
    failed_once = False

    def fail_processing_result(self, result):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            save_entered.set()
            if not release_failure.wait(2.0):
                raise OSError("test barrier timeout")
            raise OSError("injected processing-result failure")
        return original_save(self, result)

    monkeypatch.setattr(SessionWriter, "save_processing_result", fail_processing_result)
    controller = CurrySessionController()
    with SyntheticCurryServer("short_packets_hold") as server:
        assert controller.connect(
            "127.0.0.1",
            server.port,
            recording_enabled=True,
            recording_root=tmp_path,
        )
        assert pump_until(qapp, save_entered.is_set, timeout=2.0)
        # The processing worker is held inside save_processing_result while
        # the TCP peer remains open; the summary cannot have come from the
        # later Qt lifecycle callback yet.
        assert not server.done.is_set()
        assert controller.pipeline_outcome is None
        release_failure.set()
        assert pump_until(
            qapp,
            lambda: controller.resources_released() and server.done.is_set(),
        )

    outcome = controller.pipeline_outcome
    assert outcome is not None
    assert outcome.error is not None
    assert "injected processing-result failure" in outcome.error
    assert outcome.saved_blocks == 1
    assert outcome.completed_results == 0
    snapshot = controller.assembly_snapshot
    assert snapshot is not None
    assert snapshot.completed_windows == 2
    assert snapshot.partial_samples == 200
    assert outcome.stream_assembly == snapshot.to_dict()

    session_path = Path(controller.session_path)
    reader = SessionReader(session_path)
    assert reader.session_finished_payload is not None
    assert reader.session_finished_payload["status"] == "failed"
    assert reader.session_finished_payload["stream_assembly"] == snapshot.to_dict()
    journal = [
        json.loads(line)
        for line in (session_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert journal[-1]["event_type"] == "session_finished"
    manifest = json.loads((session_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"


def configure_p3_window(window, monkeypatch, tmp_path: Path) -> Path:
    import json

    protocol_path = tmp_path / "fixture-protocol.json"
    protocol_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "integration fixture",
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
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sleep_stim_controller.ui.QFileDialog.getOpenFileName",
        lambda *_args, **_kwargs: (str(protocol_path), "JSON 文件 (*.json)"),
    )
    window.stage_checkboxes["N2"].setChecked(True)
    window.stimulation_strategy_combo.setCurrentIndex(1)
    window.min_interval_edit.setText("0")
    window.max_result_age_edit.setText("60")
    window.choose_protocol_button.click()
    return protocol_path


def test_p3_full_live_udp_recording_replay_and_shutdown(qapp, tmp_path, monkeypatch) -> None:
    from sleep_stim_controller.app import build_application
    from sleep_stim_controller.rally import LoopbackRallySimulator
    from sleep_stim_controller.recording import SessionReader
    from sleep_stim_controller.staging import ModelDescriptor, StagePrediction

    class FixtureAdapter:
        descriptor = ModelDescriptor(
            "p3-integration-fixture",
            "fixture-1",
            is_test_double=True,
            confidence_meaning="fixture-only score",
        )

        def prepare(self, cancel_event):
            return None

        def predict(self, context, data, cancel_event):
            return StagePrediction("N2", 0.75)

        def close(self, cancel_event):
            return None

    simulator_holder = {}

    def simulator_factory():
        simulator = LoopbackRallySimulator()
        simulator_holder["simulator"] = simulator
        return simulator

    replay_selection = {"path": ""}

    def choose_directory(_parent, title, *_args):
        if title == "選擇已保存的会话目录" or "已保存的会话目录" in title:
            return replay_selection["path"]
        return str(tmp_path)

    monkeypatch.setattr(
        "sleep_stim_controller.ui.QFileDialog.getExistingDirectory",
        choose_directory,
    )
    monkeypatch.setattr(
        "sleep_stim_controller.app.QFileDialog.getExistingDirectory",
        choose_directory,
    )
    application, window, controller = build_application(
        model_factory=FixtureAdapter,
        simulator_factory=simulator_factory,
    )
    runtime = window._p3_runtime
    window.show()
    try:
        configure_p3_window(window, monkeypatch, tmp_path)
        window.choose_recording_dir_button.click()
        window.recording_checkbox.setChecked(True)
        assert not runtime.config.issues()

        window.simulator_start_button.click()
        assert wait_until(qapp, lambda: runtime.simulator_active)
        endpoint_text = window.simulator_status_label.text()
        assert "127.0.0.1:" in endpoint_text
        assert "随机端口" in endpoint_text
        window.stimulation_auto_checkbox.setChecked(True)
        assert runtime.automatic_enabled
        assert not window.stage_checkboxes["N2"].isEnabled()

        with SyntheticCurryServer("two_blocks") as server:
            window.host_edit.setText("127.0.0.1")
            window.port_spin.setValue(server.port)
            window.connect_button.click()
            assert wait_until(qapp, lambda: controller.state is ConnectionState.STREAMING)
            reached_second_decision = wait_until(
                qapp,
                lambda: (
                    controller.latest_processing is not None
                    and controller.latest_processing.block_id == 2
                    and "实时 block=2" in window.stimulation_recent_label.text()
                    and len(simulator_holder["simulator"].requests) >= 1
                ),
            )
            assert reached_second_decision, (
                f"state={controller.state!r}; error={window.error_label.text()!r}; "
                f"processing={controller.latest_processing!r}; "
                f"p3={window.stimulation_recent_label.text()!r}; "
                f"automatic={runtime.automatic_enabled}; "
                f"config_issues={runtime.config.issues()!r}; "
                f"diagnostics={controller.diagnostics_text()!r}; "
                f"udp={simulator_holder['simulator'].requests!r}"
            )
            assert wait_until(
                qapp,
                lambda: not runtime.request_busy
                and "api_success" in window.stimulation_recent_label.text(),
            )
            assert controller.disconnect()
            assert wait_until(qapp, lambda: controller.resources_released())

        assert not runtime.automatic_enabled
        session_path = Path(controller.session_path)
        reader = SessionReader(session_path)
        assert not reader.overview.incomplete, reader.overview.issues
        assert reader.overview.block_count == 2
        all_events = [
            event
            for entry in reader.entries
            for event in entry.stimulation_events
        ]
        decisions = [event for event in all_events if event["event_type"] == "decision"]
        sent = [event for event in all_events if event["event_type"] == "request_sent"]
        outcomes = [
            event for event in all_events if event["event_type"] == "request_outcome"
        ]
        assert len(decisions) == 2
        assert all(event["payload"]["test_double"] for event in decisions)
        assert sent and outcomes
        assert outcomes[0]["payload"]["status"] == "api_success"
        assert all(b"request_id" not in packet for packet, _ in simulator_holder["simulator"].requests)
        assert all(address[0] == "127.0.0.1" for _, address in simulator_holder["simulator"].requests)
        before_replay = len(simulator_holder["simulator"].requests)

        replay_selection["path"] = str(session_path)
        window.open_replay_button.click()
        assert wait_until(
            qapp,
            lambda: "离线回放" in window.data_status_label.text()
            and "block=1" in window.stimulation_recent_label.text()
            and "测试替身" in window.stimulation_recent_label.text(),
        )
        assert len(simulator_holder["simulator"].requests) == before_replay
        assert not runtime.automatic_enabled
        window.exit_replay_button.click()
        assert wait_until(
            qapp,
            lambda: not window._p2_replay_worker.active and not window._replay_mode,
        )
        assert len(simulator_holder["simulator"].requests) == before_replay
    finally:
        replay_worker = getattr(window, "_p2_replay_worker", None)
        if replay_worker is not None and replay_worker.active:
            replay_worker.close()
            assert wait_until(qapp, lambda: not replay_worker.active)
        if controller.is_busy():
            controller.disconnect()
            assert wait_until(qapp, lambda: controller.resources_released())
        window.close()
        runtime.shutdown()
        assert wait_until(qapp, lambda: runtime.join(0.01))
        assert not runtime.simulator_active
        assert runtime.quarantined_socket_count == 0


def test_p3_default_no_model_never_sends_even_with_complete_simulation_config(
    qapp, tmp_path, monkeypatch
) -> None:
    import json

    from sleep_stim_controller.app import build_application
    from sleep_stim_controller.recording import SessionReader
    from sleep_stim_controller.rally import LoopbackRallySimulator
    from sleep_stim_controller.staging import ProcessingStatus

    simulator_holder = {}

    def simulator_factory():
        simulator = LoopbackRallySimulator()
        simulator_holder["simulator"] = simulator
        return simulator

    application, window, controller = build_application(
        simulator_factory=simulator_factory
    )
    runtime = window._p3_runtime
    try:
        window.model_path_edit.setText(str(tmp_path / "missing-model.onnx"))
        assert not window.model_enabled_checkbox.isChecked()
        window.set_recording_root(str(tmp_path))
        window.recording_checkbox.setChecked(True)
        configure_p3_window(window, monkeypatch, tmp_path)
        assert not runtime.config.issues()
        window.simulator_start_button.click()
        assert wait_until(qapp, lambda: runtime.simulator_active)
        window.stimulation_auto_checkbox.setChecked(True)
        assert runtime.automatic_enabled
        with SyntheticCurryServer("two_blocks") as server:
            window.host_edit.setText("127.0.0.1")
            window.port_spin.setValue(server.port)
            window.connect_button.click()
            assert wait_until(
                qapp,
                lambda: controller.latest_processing is not None
                and controller.latest_processing.block_id == 2
                and "模型未接入" in window.stimulation_recent_label.text(),
            )
            assert controller.latest_processing.status is ProcessingStatus.UNAVAILABLE
            assert simulator_holder["simulator"].requests == ()
            assert controller.disconnect()
            assert wait_until(qapp, lambda: controller.resources_released())
        assert simulator_holder["simulator"].requests == ()
        session_path = Path(controller.session_path)
        manifest = json.loads((session_path / "manifest.json").read_text("utf-8"))
        assert manifest["status"] == "closed"
        assert manifest["counts"]["saved_blocks"] == 2
        reader = SessionReader(session_path)
        assert not reader.overview.incomplete, reader.overview.issues
        assert len(reader.entries) == 2
        assert all(
            entry.processing_result["status"] == "unavailable"
            for entry in reader.entries
        )
    finally:
        if controller.is_busy():
            controller.disconnect()
            assert wait_until(qapp, lambda: controller.resources_released())
        window.close()
        runtime.shutdown()
        assert wait_until(qapp, lambda: runtime.join(0.01))
        assert not runtime.simulator_active


def test_p3_api_rejection_disables_automatic_without_retry(qapp, tmp_path, monkeypatch) -> None:
    from sleep_stim_controller.app import build_application
    from sleep_stim_controller.rally import LoopbackRallySimulator, SimulatedReply
    from sleep_stim_controller.staging import ModelDescriptor, StagePrediction

    class FixtureAdapter:
        descriptor = ModelDescriptor(
            "p3-reject-fixture", "fixture-1", is_test_double=True
        )

        def prepare(self, cancel_event):
            return None

        def predict(self, context, data, cancel_event):
            return StagePrediction("N2")

        def close(self, cancel_event):
            return None

    simulator_holder = {}

    def simulator_factory():
        simulator = LoopbackRallySimulator(
            lambda _index, _request: (SimulatedReply("RALLY_ERROR_START_STIM"),)
        )
        simulator_holder["simulator"] = simulator
        return simulator

    application, window, controller = build_application(
        model_factory=FixtureAdapter,
        simulator_factory=simulator_factory,
    )
    runtime = window._p3_runtime
    try:
        configure_p3_window(window, monkeypatch, tmp_path)
        window.simulator_start_button.click()
        assert wait_until(qapp, lambda: runtime.simulator_active)
        window.stimulation_auto_checkbox.setChecked(True)
        with SyntheticCurryServer("two_blocks") as server:
            window.host_edit.setText("127.0.0.1")
            window.port_spin.setValue(server.port)
            window.connect_button.click()
            assert wait_until(
                qapp,
                lambda: len(simulator_holder["simulator"].requests) == 1
                and not runtime.request_busy
                and not runtime.automatic_enabled,
            )
            assert "拒绝" in window.stimulation_auto_status_label.text()
            assert len(simulator_holder["simulator"].requests) == 1
            assert controller.disconnect()
            assert wait_until(qapp, lambda: controller.resources_released())
        assert len(simulator_holder["simulator"].requests) == 1
    finally:
        if controller.is_busy():
            controller.disconnect()
            assert wait_until(qapp, lambda: controller.resources_released())
        window.close()
        runtime.shutdown()
        assert wait_until(qapp, lambda: runtime.join(0.01))
        assert not runtime.simulator_active


def test_p3_window_exit_records_inflight_request_as_unknown_before_session_finish(
    qapp, tmp_path, monkeypatch
) -> None:
    import json

    from sleep_stim_controller.app import build_application
    from sleep_stim_controller.rally import LoopbackRallySimulator
    from sleep_stim_controller.recording import SessionReader
    from sleep_stim_controller.staging import ModelDescriptor, StagePrediction

    class FixtureAdapter:
        descriptor = ModelDescriptor(
            "p3-exit-fixture", "fixture-1", is_test_double=True
        )

        def prepare(self, cancel_event):
            return None

        def predict(self, context, data, cancel_event):
            return StagePrediction("N2")

        def close(self, cancel_event):
            return None

    simulator_holder = {}

    def simulator_factory():
        simulator = LoopbackRallySimulator(lambda _index, _request: ())
        simulator_holder["simulator"] = simulator
        return simulator

    monkeypatch.setattr(
        "sleep_stim_controller.ui.QFileDialog.getExistingDirectory",
        lambda *_args, **_kwargs: str(tmp_path),
    )
    application, window, controller = build_application(
        model_factory=FixtureAdapter,
        simulator_factory=simulator_factory,
        request_timeout_seconds=5.0,
    )
    runtime = window._p3_runtime
    window.show()
    try:
        configure_p3_window(window, monkeypatch, tmp_path)
        window.request_timeout_edit.setText("5.0")
        window.choose_recording_dir_button.click()
        window.recording_checkbox.setChecked(True)
        window.simulator_start_button.click()
        assert wait_until(qapp, lambda: runtime.simulator_active)
        window.stimulation_auto_checkbox.setChecked(True)
        with SyntheticCurryServer("two_blocks") as server:
            window.host_edit.setText("127.0.0.1")
            window.port_spin.setValue(server.port)
            window.connect_button.click()
            assert wait_until(
                qapp,
                lambda: runtime.request_busy
                and len(simulator_holder["simulator"].requests) == 1,
            )
            session_path = Path(controller.session_path)
            window.close()
            assert wait_until(
                qapp,
                lambda: (
                    not window.isVisible()
                    and controller.resources_released()
                    and runtime.join(0.01)
                ),
            )
        reader = SessionReader(session_path)
        assert not reader.overview.incomplete, reader.overview.issues
        all_events = [
            event
            for entry in reader.entries
            for event in entry.stimulation_events
        ]
        sent = [event for event in all_events if event["event_type"] == "request_sent"]
        outcomes = [
            event for event in all_events if event["event_type"] == "request_outcome"
        ]
        assert len(sent) == 1
        assert len(outcomes) == 1
        assert outcomes[0]["payload"]["status"] == "unknown"
        assert sent[0]["request_id"] == outcomes[0]["request_id"]
        assert sent[0]["request_id"]
        journal = [
            json.loads(line)
            for line in (session_path / "events.jsonl").read_text("utf-8").splitlines()
        ]
        outcome_sequence = next(
            event["sequence"]
            for event in journal
            if event["event_type"] == "request_outcome"
        )
        finish_sequence = next(
            event["sequence"]
            for event in journal
            if event["event_type"] == "session_finished"
        )
        assert outcome_sequence < finish_sequence
        assert not runtime.simulator_active
        assert runtime.quarantined_socket_count == 0
    finally:
        if controller.is_busy():
            controller.disconnect()
            assert wait_until(qapp, lambda: controller.resources_released())
        window.close()
        runtime.shutdown()
        assert wait_until(qapp, lambda: runtime.join(0.01))


def test_p4b_curry_tcp_staging_fake_rally_gui_and_recording_closeout(
    qapp, tmp_path
) -> None:
    import json

    from sleep_stim_controller.app import build_application
    from sleep_stim_controller.recording import SessionReader
    from sleep_stim_controller.staging import ModelDescriptor, StagePrediction

    class FixtureOnnxAdapter:
        descriptor = ModelDescriptor(
            "onnx-sleep-staging:synthetic",
            "sha256:synthetic",
            is_test_double=False,
            confidence_meaning="synthetic probability for contract wiring",
        )

        def prepare(self, cancel_event):
            return None

        def predict(self, context, data, cancel_event):
            return StagePrediction("N2", 0.8)

        def close(self, cancel_event):
            return None

    rally = SyntheticBinaryRally(
        [
            RALLY_STOP_SUCCESS.encode("utf-8"),
            RALLY_START_SUCCESS.encode("utf-8"),
            RALLY_STOP_SUCCESS.encode("utf-8"),
        ]
    )
    application, window, controller = build_application(
        model_factory=FixtureOnnxAdapter,
        request_timeout_seconds=0.2,
        rally_control_endpoint=rally.endpoint,
    )
    runtime = window._p3_runtime
    window.show()
    server = None
    try:
        window.set_recording_root(str(tmp_path))
        window.recording_checkbox.setChecked(True)
        window.rally_mode_combo.setCurrentIndex(1)
        window.stage_checkboxes["N2"].setChecked(True)
        window.stimulation_strategy_combo.setCurrentIndex(1)
        window.min_interval_edit.setText("0")
        window.max_result_age_edit.setText("5")
        window._request_stimulation_configuration()

        server = SyntheticCurryServer("onnx_staging_hold")
        server.__enter__()
        window.host_edit.setText("127.0.0.1")
        window.port_spin.setValue(server.port)
        window.connect_button.click()
        assert wait_until(qapp, lambda: controller.state is ConnectionState.STREAMING)
        window.real_control_confirm_checkbox.setChecked(True)
        window.stimulation_auto_checkbox.setChecked(True)
        assert wait_until(qapp, lambda: runtime.real_status["baseline_ready"]), (
            runtime.real_status,
            runtime.config,
            window.stimulation_auto_status_label.text(),
            window.error_label.text(),
        )
        server.release_stream.set()
        assert wait_until(
            qapp,
            lambda: (
                len(rally.requests) >= 2
                and runtime.real_status["confirmed_state"] == "RUNNING"
            ),
        ), (
            runtime.real_status,
            controller.state,
            controller.latest_processing,
            server.requests if server is not None else None,
            server.done.is_set() if server is not None else None,
            controller.assembly_snapshot,
            controller.diagnostics_text(),
            window.stimulation_auto_status_label.text(),
            window.error_label.text(),
            [item[0] for item in rally.requests],
        )
        assert [item[0] for item in rally.requests[:2]] == [
            RALLY_STOP_COMMAND.encode("utf-8"),
            RALLY_START_COMMAND.encode("utf-8"),
        ]
        assert wait_until(
            qapp, lambda: "RUNNING" in window.rally_control_status_label.text()
        )
        assert "RUNNING" in window.rally_control_status_label.text()
        assert "自动控制=开" in window.rally_control_status_label.text()

        window.disconnect_button.click()
        assert wait_until(
            qapp,
            lambda: controller.resources_released() and len(rally.requests) >= 3,
        )
        assert [item[0] for item in rally.requests[:3]] == [
            RALLY_STOP_COMMAND.encode("utf-8"),
            RALLY_START_COMMAND.encode("utf-8"),
            RALLY_STOP_COMMAND.encode("utf-8"),
        ]
        session_path = Path(controller.session_path)
        reader = SessionReader(session_path)
        phases = [event["payload"]["phase"] for event in reader.control_events]
        assert phases[0] == "config"
        assert phases[-2:] == ["sent", "outcome"]
        assert [
            event["payload"]["command"]
            for event in reader.control_events
            if event["payload"]["phase"] == "sent"
        ] == [RALLY_STOP_COMMAND, RALLY_START_COMMAND, RALLY_STOP_COMMAND]
        journal = [
            json.loads(line)
            for line in (session_path / "events.jsonl").read_text("utf-8").splitlines()
        ]
        last_control_sequence = max(
            event["sequence"]
            for event in journal
            if event["event_type"] == "rally_control"
        )
        finish_sequence = next(
            event["sequence"]
            for event in journal
            if event["event_type"] == "session_finished"
        )
        assert last_control_sequence < finish_sequence
        assert json.loads((session_path / "manifest.json").read_text("utf-8"))["status"] == "closed"
    finally:
        if controller.is_busy():
            controller.disconnect()
            assert wait_until(qapp, lambda: controller.resources_released())
        window.close()
        runtime.shutdown()
        assert wait_until(qapp, lambda: runtime.join(0.01))
        if server is not None:
            server.__exit__(None, None, None)
        rally.close()


def test_p4b_natural_tcp_eof_registers_stop_before_writer_close(qapp, tmp_path) -> None:
    """A peer EOF registers real Stop before ProcessingPipeline closes its writer."""
    import json

    from sleep_stim_controller.app import build_application
    from sleep_stim_controller.recording import SessionReader
    from sleep_stim_controller.staging import ModelDescriptor, StagePrediction

    class FixtureOnnxAdapter:
        descriptor = ModelDescriptor(
            "onnx-sleep-staging:synthetic-eof",
            "sha256:synthetic-eof",
            is_test_double=False,
            confidence_meaning="synthetic probability for contract wiring",
        )

        def prepare(self, cancel_event):
            return None

        def predict(self, context, data, cancel_event):
            return StagePrediction("N2", 0.8)

        def close(self, cancel_event):
            return None

    rally = SyntheticBinaryRally(
        [
            RALLY_STOP_SUCCESS.encode("utf-8"),
            RALLY_START_SUCCESS.encode("utf-8"),
            RALLY_STOP_SUCCESS.encode("utf-8"),
        ]
    )
    application, window, controller = build_application(
        model_factory=FixtureOnnxAdapter,
        request_timeout_seconds=0.2,
        rally_control_endpoint=rally.endpoint,
    )
    runtime = window._p3_runtime
    window.show()
    server = None
    try:
        window.set_recording_root(str(tmp_path))
        window.recording_checkbox.setChecked(True)
        window.rally_mode_combo.setCurrentIndex(1)
        window.stage_checkboxes["N2"].setChecked(True)
        window.stimulation_strategy_combo.setCurrentIndex(1)
        window.min_interval_edit.setText("0")
        window.max_result_age_edit.setText("5")
        window._request_stimulation_configuration()

        server = SyntheticCurryServer("onnx_staging_eof_hold")
        server.__enter__()
        window.host_edit.setText("127.0.0.1")
        window.port_spin.setValue(server.port)
        window.connect_button.click()
        assert wait_until(qapp, lambda: controller.state is ConnectionState.STREAMING)
        window.real_control_confirm_checkbox.setChecked(True)
        window.stimulation_auto_checkbox.setChecked(True)
        assert wait_until(qapp, lambda: runtime.real_status["baseline_ready"])

        server.release_stream.set()
        assert wait_until(
            qapp,
            lambda: (
                len(rally.requests) >= 2
                and runtime.real_status["confirmed_state"] == "RUNNING"
            ),
        )

        # Let the synthetic Curry peer close its TCP socket. This path does
        # not click the GUI disconnect button; the network worker owns EOF.
        server.close_stream.set()
        assert wait_until(
            qapp,
            lambda: (
                server.done.is_set()
                and controller.resources_released()
                and len(rally.requests) >= 3
            ),
        )
        assert [item[0] for item in rally.requests[:3]] == [
            RALLY_STOP_COMMAND.encode("utf-8"),
            RALLY_START_COMMAND.encode("utf-8"),
            RALLY_STOP_COMMAND.encode("utf-8"),
        ]

        session_path = Path(controller.session_path)
        reader = SessionReader(session_path)
        assert [
            event["payload"]["phase"] for event in reader.control_events
        ] == [
            "config",
            "sent",
            "outcome",
            "decision",
            "sent",
            "outcome",
            "decision",
            "sent",
            "outcome",
        ]
        journal = [
            json.loads(line)
            for line in (session_path / "events.jsonl")
            .read_text("utf-8")
            .splitlines()
        ]
        control_sequences = [
            event["sequence"]
            for event in journal
            if event["event_type"] == "rally_control"
        ]
        finish_sequence = next(
            event["sequence"]
            for event in journal
            if event["event_type"] == "session_finished"
        )
        assert control_sequences and max(control_sequences) < finish_sequence
        assert reader.session_finished_payload is not None
        # Curry reports a peer EOF as a failed/incomplete network session;
        # the relevant closeout guarantee is that all accepted control events
        # still precede this truthful final status.
        assert reader.session_finished_payload["status"] == "failed"
        assert not controller.is_busy()
    finally:
        if controller.is_busy():
            controller.disconnect()
            assert wait_until(qapp, lambda: controller.resources_released())
        window.close()
        runtime.shutdown()
        assert wait_until(qapp, lambda: runtime.join(0.01))
        if server is not None:
            server.__exit__(None, None, None)
        rally.close()
