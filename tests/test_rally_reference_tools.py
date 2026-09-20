from __future__ import annotations

import ctypes
import importlib.util
import math
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest
from ctypes import wintypes


ROOT = Path(__file__).resolve().parents[1]


def load_reference_tool(name: str, relative_path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


udp = load_reference_tool("rally_reference_udp", "reference/RallyStartStopTest.py")
serial_tool = load_reference_tool(
    "rally_reference_serial", "reference/SerialStartStopTest.py"
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


@dataclass(frozen=True)
class Datagram:
    delay: float
    payload: bytes
    sender: tuple[str, int] = ("127.0.0.1", 8801)
    error: Exception | None = None


class FakeUdpNetwork:
    def __init__(self, clock: FakeClock, replies: dict[str, list[Datagram]]) -> None:
        self.clock = clock
        self.replies = {key: list(value) for key, value in replies.items()}
        self.sent: list[tuple[str, float]] = []
        self.sockets: list[FakeUdpSocket] = []

    def socket(self, *_args) -> "FakeUdpSocket":
        result = FakeUdpSocket(self)
        self.sockets.append(result)
        return result


class FakeUdpSocket:
    def __init__(self, network: FakeUdpNetwork) -> None:
        self.network = network
        self.command = ""
        self.timeout = 0.0
        self.timeouts: list[float] = []

    def __enter__(self) -> "FakeUdpSocket":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def bind(self, _address) -> None:
        return None

    def sendto(self, payload: bytes, _endpoint) -> None:
        self.command = payload.decode("utf-8")
        self.network.sent.append((self.command, self.network.clock.now))

    def settimeout(self, seconds: float) -> None:
        self.timeout = seconds
        self.timeouts.append(seconds)

    def recvfrom(self, _size: int):
        pending = self.network.replies.get(self.command, [])
        if not pending:
            self.network.clock.advance(self.timeout)
            raise socket.timeout("fixture timeout")
        item = pending.pop(0)
        if item.delay > self.timeout:
            self.network.clock.advance(self.timeout)
            raise socket.timeout("fixture deadline")
        self.network.clock.advance(item.delay)
        if item.error is not None:
            raise item.error
        return item.payload, item.sender


def install_udp_fakes(monkeypatch, replies: dict[str, list[Datagram]]):
    clock = FakeClock()
    network = FakeUdpNetwork(clock, replies)
    monkeypatch.setattr(udp.socket, "socket", network.socket)
    monkeypatch.setattr(udp, "_monotonic", clock.monotonic)
    monkeypatch.setattr(udp, "_sleep", clock.sleep)
    return clock, network


def udp_args(*arguments: str):
    return udp.build_parser().parse_args(arguments)


def accepted_udp_reply(command: str) -> Datagram:
    return Datagram(0.0, udp.SUCCESS_RESPONSE.encode())


def test_udp_start_reply_wait_consumes_duration_and_waits_only_remainder(
    monkeypatch, capsys
) -> None:
    clock, network = install_udp_fakes(
        monkeypatch,
        {
            udp.START_COMMAND: [Datagram(1.5, b"RALLY_ERROR_SUCCESS")],
            udp.STOP_COMMAND: [accepted_udp_reply(udp.STOP_COMMAND)],
        },
    )
    args = udp_args(
        "cycle", "--duration", "2", "--timeout", "5", "--execute",
        "--confirm", udp.CONFIRMATION,
    )

    assert udp.run(args) == 0
    assert [command for command, _ in network.sent] == [
        udp.START_COMMAND,
        udp.STOP_COMMAND,
    ]
    assert network.sent[1][1] == pytest.approx(2.0)
    assert clock.now == pytest.approx(2.0)
    assert "STOP WAS NOT CONFIRMED" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("start_reply", "expected_time"),
    [
        (Datagram(0.9, b"RALLY_ERROR_SUCCESS"), 1.0),
        (Datagram(0.1, b"RALLY_ERROR_INVALID_STATUS"), 0.1),
    ],
)
def test_udp_near_deadline_or_rejection_proceeds_to_one_stop_immediately(
    monkeypatch, start_reply, expected_time
) -> None:
    _clock, network = install_udp_fakes(
        monkeypatch,
        {
            udp.START_COMMAND: [start_reply],
            udp.STOP_COMMAND: [accepted_udp_reply(udp.STOP_COMMAND)],
        },
    )
    args = udp_args(
        "cycle", "--duration", "1", "--timeout", "5", "--execute",
        "--confirm", udp.CONFIRMATION,
    )

    result = udp.run(args)

    assert result == (0 if start_reply.payload == b"RALLY_ERROR_SUCCESS" else 2)
    assert len(network.sent) == 2
    assert network.sent[1][1] == pytest.approx(expected_time)


def test_udp_timeout_and_receive_exception_still_attempt_one_stop(monkeypatch) -> None:
    _clock, network = install_udp_fakes(
        monkeypatch,
        {
            udp.START_COMMAND: [],
            udp.STOP_COMMAND: [accepted_udp_reply(udp.STOP_COMMAND)],
        },
    )
    args = udp_args(
        "cycle", "--duration", "1", "--timeout", "5", "--execute",
        "--confirm", udp.CONFIRMATION,
    )
    with pytest.raises(socket.timeout):
        udp.run(args)
    assert [item[0] for item in network.sent] == [
        udp.START_COMMAND,
        udp.STOP_COMMAND,
    ]
    assert network.sent[1][1] == pytest.approx(1.0)

    _clock, network = install_udp_fakes(
        monkeypatch,
        {
            udp.START_COMMAND: [Datagram(0.1, b"", error=OSError("receive failed"))],
            udp.STOP_COMMAND: [accepted_udp_reply(udp.STOP_COMMAND)],
        },
    )
    with pytest.raises(OSError, match="receive failed"):
        udp.run(args)
    assert [item[0] for item in network.sent] == [
        udp.START_COMMAND,
        udp.STOP_COMMAND,
    ]


def test_udp_unrelated_source_does_not_reset_start_deadline(monkeypatch) -> None:
    _clock, network = install_udp_fakes(
        monkeypatch,
        {
            udp.START_COMMAND: [
                Datagram(0.75, b"noise", sender=("127.0.0.2", 9000)),
                Datagram(0.3, b"RALLY_ERROR_SUCCESS"),
            ],
            udp.STOP_COMMAND: [accepted_udp_reply(udp.STOP_COMMAND)],
        },
    )
    args = udp_args(
        "cycle", "--duration", "1", "--timeout", "5", "--execute",
        "--confirm", udp.CONFIRMATION,
    )

    with pytest.raises(socket.timeout):
        udp.run(args)

    assert network.sockets[0].timeouts[0] == pytest.approx(1.0)
    assert network.sockets[0].timeouts[1] == pytest.approx(0.25)
    assert network.sent[1][1] == pytest.approx(1.0)


def test_udp_stop_gets_its_own_timeout_after_start_budget_expires(monkeypatch) -> None:
    _clock, network = install_udp_fakes(
        monkeypatch,
        {
            udp.START_COMMAND: [],
            udp.STOP_COMMAND: [Datagram(0.2, b"RALLY_ERROR_SUCCESS")],
        },
    )
    args = udp_args(
        "cycle", "--duration", "1", "--timeout", "2", "--execute",
        "--confirm", udp.CONFIRMATION,
    )

    with pytest.raises(socket.timeout):
        udp.run(args)

    assert len(network.sockets) == 2
    assert network.sockets[0].timeouts == [pytest.approx(1.0)]
    assert network.sockets[1].timeouts == [pytest.approx(2.0)]


@pytest.mark.parametrize(
    ("command", "response", "succeeded"),
    [
        (udp.START_COMMAND, "RALLY_ERROR_SUCCESS", True),
        (udp.START_COMMAND, "启动刺激成功", True),
        (udp.STOP_COMMAND, "RALLY_ERROR_SUCCESS", True),
        (udp.STOP_COMMAND, "停止刺激成功", True),
        (udp.START_COMMAND, "停止刺激成功", False),
        (udp.STOP_COMMAND, "启动刺激成功", False),
    ],
)
def test_udp_exact_success_replies_remain_command_specific(
    command: str, response: str, succeeded: bool
) -> None:
    reply = udp.UdpReply(command, response, "127.0.0.1", 9999)
    assert reply.succeeded is succeeded


@pytest.mark.parametrize(
    ("action", "options"),
    [
        ("cycle", ["--timeout", "nan", "--duration", "1", "--execute", "--confirm", udp.CONFIRMATION]),
        ("cycle", ["--timeout", "inf", "--duration", "1", "--execute", "--confirm", udp.CONFIRMATION]),
        ("cycle", ["--timeout", "1", "--duration", "nan", "--execute", "--confirm", udp.CONFIRMATION]),
        ("cycle", ["--timeout", "1", "--duration", "inf", "--execute", "--confirm", udp.CONFIRMATION]),
        ("stop", ["--timeout", "inf", "--execute"]),
    ],
)
def test_udp_nonfinite_timing_is_rejected_before_socket_creation(
    monkeypatch, action: str, options: list[str]
) -> None:
    _clock, network = install_udp_fakes(monkeypatch, {})
    with pytest.raises(ValueError):
        udp.run(udp_args(action, *options))
    assert network.sockets == []


def test_udp_dry_run_and_missing_execution_confirmation_send_nothing(
    monkeypatch, capsys
) -> None:
    _clock, network = install_udp_fakes(monkeypatch, {})
    assert udp.run(udp_args("dry-run")) == 0
    with pytest.raises(ValueError, match="--execute"):
        udp.run(udp_args("stop"))
    with pytest.raises(ValueError, match="--confirm"):
        udp.run(udp_args("cycle", "--execute"))
    assert network.sockets == []
    assert "DRY RUN ONLY" in capsys.readouterr().out


class FakeSerialPort:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.port = "COM4"
        self.writes: list[tuple[bytes, float, float | None]] = []
        self.reads: list[tuple[float, float | None, bool]] = []
        self.responses: dict[bytes, tuple[float, bytes, Exception | None]] = {}
        self.start_write_error: Exception | None = None
        self.stop_write_error: Exception | None = None

    def __enter__(self) -> "FakeSerialPort":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def purge_input(self) -> None:
        return None

    def write(self, payload: bytes, *, deadline: float | None = None) -> None:
        self.writes.append((payload, self.clock.now, deadline))
        error = (
            self.start_write_error
            if payload == serial_tool.START_FRAME
            else self.stop_write_error
        )
        if error is not None:
            raise error

    def read_for(
        self,
        seconds: float,
        *,
        deadline: float | None = None,
        stop_on_data: bool = False,
    ) -> bytes:
        effective_deadline = self.clock.now + seconds
        if deadline is not None:
            effective_deadline = min(effective_deadline, deadline)
        self.reads.append((seconds, deadline, stop_on_data))
        payload = self.writes[-1][0]
        delay, response, error = self.responses.get(payload, (seconds, b"", None))
        if error is not None:
            self.clock.advance(min(delay, max(0.0, effective_deadline - self.clock.now)))
            raise error
        available = max(0.0, effective_deadline - self.clock.now)
        if response and delay <= available:
            self.clock.advance(delay)
            return response
        self.clock.advance(available)
        return b""


def install_serial_fake(monkeypatch, fake: FakeSerialPort) -> list[FakeSerialPort]:
    opened: list[FakeSerialPort] = []

    def factory(*_args, **_kwargs):
        opened.append(fake)
        return fake

    monkeypatch.setattr(serial_tool, "WindowsSerialPort", factory)
    monkeypatch.setattr(serial_tool, "_monotonic", fake.clock.monotonic)
    return opened


def serial_args(*arguments: str):
    return serial_tool.build_parser().parse_args(arguments)


def run_serial_cycle(monkeypatch, fake: FakeSerialPort, *options: str):
    install_serial_fake(monkeypatch, fake)
    return serial_tool.run(
        serial_args(
            "cycle", "--port", "COM4", "--execute", "--confirm",
            serial_tool.CONFIRMATION, *options,
        )
    )


def test_serial_no_response_wait_is_clamped_to_start_duration(monkeypatch, capsys) -> None:
    clock = FakeClock()
    fake = FakeSerialPort(clock)
    install_serial_fake(monkeypatch, fake)

    result = serial_tool.run(
        serial_args(
            "cycle", "--port", "COM4", "--duration", "2", "--read-seconds", "10",
            "--execute", "--confirm", serial_tool.CONFIRMATION,
        )
    )

    assert result == 3
    assert [item[0] for item in fake.writes] == [
        serial_tool.START_FRAME,
        serial_tool.STOP_FRAME,
    ]
    assert fake.writes[0][2] == pytest.approx(2.0)
    assert fake.writes[1][1] == pytest.approx(2.0)
    assert fake.reads[0][0] == pytest.approx(10.0)
    assert fake.reads[0][1] == pytest.approx(2.0)
    assert "停止未确认，请使用独立停止方式并核实物理输出" in capsys.readouterr().err


def test_serial_any_start_bytes_trigger_immediate_stop_not_observation_wait(
    monkeypatch, capsys
) -> None:
    clock = FakeClock()
    fake = FakeSerialPort(clock)
    fake.responses[serial_tool.START_FRAME] = (0.9, b"ERR\x00", None)
    fake.responses[serial_tool.STOP_FRAME] = (0.0, b"echo", None)
    install_serial_fake(monkeypatch, fake)

    result = serial_tool.run(
        serial_args(
            "cycle", "--port", "COM4", "--duration", "1", "--read-seconds", "10",
            "--execute", "--confirm", serial_tool.CONFIRMATION,
        )
    )

    output = capsys.readouterr()
    assert result == 3
    assert fake.writes[1][1] == pytest.approx(0.9)
    assert "收到字节；命令/物理状态未确认" in output.out
    assert "停止未确认，请使用独立停止方式并核实物理输出" in output.err
    assert "已确认启动" not in output.out


@pytest.mark.parametrize("start_response", [b"", b"\x00", b"ERR", b"\x01\xe1\x01\x00\xc0"])
@pytest.mark.parametrize("stop_response", [b"", b"\x00", b"ERR", b"\x01\xe1\x01\x00\x80"])
def test_serial_neither_start_nor_stop_rx_is_confirmation(
    monkeypatch, capsys, start_response: bytes, stop_response: bytes
) -> None:
    clock = FakeClock()
    fake = FakeSerialPort(clock)
    fake.responses[serial_tool.START_FRAME] = (0.1, start_response, None)
    fake.responses[serial_tool.STOP_FRAME] = (0.0, stop_response, None)

    result = run_serial_cycle(monkeypatch, fake, "--duration", "1", "--read-seconds", "0.5")

    output = capsys.readouterr()
    assert result == 3
    assert "RX COM4:" in output.out
    assert "停止未确认，请使用独立停止方式并核实物理输出" in output.err
    assert "START confirmed" not in output.out


@pytest.mark.parametrize("response", [b"\x00", b"ERR", b"echo", b""])
def test_serial_standalone_stop_bytes_always_return_unconfirmed_nonzero(
    monkeypatch, capsys, response: bytes
) -> None:
    clock = FakeClock()
    fake = FakeSerialPort(clock)
    fake.responses[serial_tool.STOP_FRAME] = (0.0, response, None)
    install_serial_fake(monkeypatch, fake)

    result = serial_tool.run(
        serial_args("stop", "--port", "COM4", "--execute", "--read-seconds", "0.1")
    )

    output = capsys.readouterr()
    assert result == 3
    assert fake.writes == [(serial_tool.STOP_FRAME, 0.0, None)]
    assert "停止未确认，请使用独立停止方式并核实物理输出" in output.err


def test_serial_standalone_stop_io_exception_reports_unknown_and_nonzero(
    monkeypatch, capsys
) -> None:
    clock = FakeClock()
    fake = FakeSerialPort(clock)
    fake.stop_write_error = OSError("STOP write failed")
    install_serial_fake(monkeypatch, fake)

    result = serial_tool.run(
        serial_args("stop", "--port", "COM4", "--execute", "--read-seconds", "0.1")
    )

    output = capsys.readouterr()
    assert result == 2
    assert fake.writes[0][0] == serial_tool.STOP_FRAME
    assert "STOP ATTEMPT FAILED" in output.err
    assert "停止未确认，请使用独立停止方式并核实物理输出" in output.err


@pytest.mark.parametrize(
    ("action", "options"),
    [
        ("cycle", ["--duration", "nan"]),
        ("cycle", ["--duration", "inf"]),
        ("cycle", ["--read-seconds", "nan"]),
        ("cycle", ["--read-seconds", "inf"]),
        ("stop", ["--read-seconds", "nan"]),
        ("stop", ["--read-seconds", "inf"]),
    ],
)
def test_serial_nonfinite_time_values_are_rejected_before_port_open(
    monkeypatch, action: str, options: list[str]
) -> None:
    clock = FakeClock()
    fake = FakeSerialPort(clock)
    opened = install_serial_fake(monkeypatch, fake)
    arguments = [action, "--port", "COM4", "--execute"]
    if action == "cycle":
        arguments += ["--confirm", serial_tool.CONFIRMATION]
    arguments += options

    with pytest.raises(ValueError):
        serial_tool.run(serial_args(*arguments))
    assert opened == []


def test_serial_dry_run_and_missing_execute_or_confirm_do_not_open_port(
    monkeypatch, capsys
) -> None:
    clock = FakeClock()
    fake = FakeSerialPort(clock)
    opened = install_serial_fake(monkeypatch, fake)

    assert serial_tool.run(serial_args("dry-run", "--port", "COM4")) == 0
    with pytest.raises(ValueError, match="--execute"):
        serial_tool.run(serial_args("stop", "--port", "COM4"))
    with pytest.raises(ValueError, match="--confirm"):
        serial_tool.run(serial_args("cycle", "--port", "COM4", "--execute"))
    assert opened == []
    assert "DRY RUN ONLY" in capsys.readouterr().out


@pytest.mark.parametrize("failure_point", ["write", "read"])
def test_serial_possible_start_failure_still_attempts_stop_and_preserves_warning(
    monkeypatch, capsys, failure_point: str
) -> None:
    clock = FakeClock()
    fake = FakeSerialPort(clock)
    if failure_point == "write":
        fake.start_write_error = OSError("START write failed")
    else:
        fake.responses[serial_tool.START_FRAME] = (
            0.1,
            b"",
            OSError("START read failed"),
        )
    fake.stop_write_error = OSError("STOP write failed")
    install_serial_fake(monkeypatch, fake)

    with pytest.raises(OSError, match=f"START {failure_point} failed"):
        serial_tool.run(
            serial_args(
                "cycle", "--port", "COM4", "--execute", "--confirm",
                serial_tool.CONFIRMATION,
            )
        )

    output = capsys.readouterr()
    assert [item[0] for item in fake.writes] == [
        serial_tool.START_FRAME,
        serial_tool.STOP_FRAME,
    ]
    assert "STOP ATTEMPT FAILED" in output.err
    assert "停止未确认，请使用独立停止方式并核实物理输出" in output.err


class FakeWin32Function:
    def __init__(self, function) -> None:
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


class FakeWin32:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.timeouts: list[tuple[int, int]] = []
        self.write_calls = 0
        self.flush_calls = 0
        self.SetCommTimeouts = FakeWin32Function(self.set_comm_timeouts)
        self.WriteFile = FakeWin32Function(self.write_file)
        self.ReadFile = FakeWin32Function(self.read_file)

    def set_comm_timeouts(self, _handle, pointer) -> int:
        value = ctypes.cast(pointer, ctypes.POINTER(serial_tool.COMMTIMEOUTS)).contents
        self.timeouts.append(
            (int(value.ReadTotalTimeoutConstant), int(value.WriteTotalTimeoutConstant))
        )
        return 1

    def write_file(self, _handle, _buffer, size, written_pointer, _overlapped) -> int:
        self.write_calls += 1
        ctypes.cast(written_pointer, ctypes.POINTER(wintypes.DWORD)).contents.value = size
        return 1

    def read_file(self, _handle, _buffer, _size, received_pointer, _overlapped) -> int:
        timeout_ms = self.timeouts[-1][0]
        ctypes.cast(received_pointer, ctypes.POINTER(wintypes.DWORD)).contents.value = 0
        self.clock.advance(timeout_ms / 1000)
        return 1


def make_unopened_win32_port() -> serial_tool.WindowsSerialPort:
    port = object.__new__(serial_tool.WindowsSerialPort)
    port.port = "COM4"
    port.baud = 115200
    port.read_timeout_ms = 100
    port.handle = 7
    return port


def test_win32_start_and_stop_writes_have_finite_independent_timeouts_without_flush(
    monkeypatch,
) -> None:
    clock = FakeClock()
    api = FakeWin32(clock)
    port = make_unopened_win32_port()
    monkeypatch.setattr(serial_tool, "kernel32", api)
    monkeypatch.setattr(serial_tool, "_monotonic", clock.monotonic)

    port.write(b"START", deadline=clock.now + 0.375)
    port.write(b"STOP")

    assert api.timeouts == [(100, 375), (100, 1000)]
    assert api.write_calls == 2
    assert api.flush_calls == 0


def test_win32_readfile_timeout_shrinks_to_total_remaining_time(monkeypatch) -> None:
    clock = FakeClock()
    api = FakeWin32(clock)
    port = make_unopened_win32_port()
    monkeypatch.setattr(serial_tool, "kernel32", api)
    monkeypatch.setattr(serial_tool, "_monotonic", clock.monotonic)

    assert port.read_for(0.25) == b""

    read_timeouts = [read for read, _write in api.timeouts]
    assert read_timeouts[:2] == [100, 100]
    assert read_timeouts[-1] <= 51
    assert clock.now <= 0.251


def test_serial_invalid_read_budget_rejected_before_win32_calls(monkeypatch) -> None:
    clock = FakeClock()
    api = FakeWin32(clock)
    port = make_unopened_win32_port()
    monkeypatch.setattr(serial_tool, "kernel32", api)
    monkeypatch.setattr(serial_tool, "_monotonic", clock.monotonic)

    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            port.read_for(value)
    assert api.timeouts == []
