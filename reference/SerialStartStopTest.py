"""Windows serial test for five-byte stimulation start/stop frames.

Uses only Python's standard library and the Win32 API.  It sends nothing
unless ``--execute`` is present.  The ``cycle`` action always attempts the
stop frame in a ``finally`` block and intentionally has no persistent start.
"""

from __future__ import annotations

import argparse
import ctypes
import math
from ctypes import wintypes
import re
import sys
import time
from datetime import datetime


START_FRAME = bytes.fromhex("01 E1 01 00 C0")
STOP_FRAME = bytes.fromhex("01 E1 01 00 80")
CONFIRMATION = "I_ACCEPT_PHYSICAL_STIMULATION"
PORT_PATTERN = re.compile(r"COM([1-9][0-9]*)", re.IGNORECASE)
STOP_WRITE_TIMEOUT_SECONDS = 1.0
_monotonic = time.monotonic


def _validate_duration(seconds: float) -> None:
    if not math.isfinite(seconds) or not 0.1 <= seconds <= 10.0:
        raise ValueError("--duration must be between 0.1 and 10 finite seconds")


def _validate_read_seconds(seconds: float) -> None:
    if not math.isfinite(seconds) or not 0 <= seconds <= 10.0:
        raise ValueError("--read-seconds must be between 0 and 10 finite seconds")


def _timeout_milliseconds(seconds: float) -> int:
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("serial I/O timeout must be a finite number greater than zero")
    return max(1, min(0xFFFFFFFE, math.ceil(seconds * 1000)))


if sys.platform == "win32":
    import winreg

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:
    kernel32 = None


class DCB(ctypes.Structure):
    _fields_ = [
        ("DCBlength", wintypes.DWORD),
        ("BaudRate", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("wReserved", wintypes.WORD),
        ("XonLim", wintypes.WORD),
        ("XoffLim", wintypes.WORD),
        ("ByteSize", wintypes.BYTE),
        ("Parity", wintypes.BYTE),
        ("StopBits", wintypes.BYTE),
        ("XonChar", ctypes.c_char),
        ("XoffChar", ctypes.c_char),
        ("ErrorChar", ctypes.c_char),
        ("EofChar", ctypes.c_char),
        ("EvtChar", ctypes.c_char),
        ("wReserved1", wintypes.WORD),
    ]


class COMMTIMEOUTS(ctypes.Structure):
    _fields_ = [
        ("ReadIntervalTimeout", wintypes.DWORD),
        ("ReadTotalTimeoutMultiplier", wintypes.DWORD),
        ("ReadTotalTimeoutConstant", wintypes.DWORD),
        ("WriteTotalTimeoutMultiplier", wintypes.DWORD),
        ("WriteTotalTimeoutConstant", wintypes.DWORD),
    ]


GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
PURGE_TXABORT = 0x0001
PURGE_RXABORT = 0x0002
PURGE_TXCLEAR = 0x0004
PURGE_RXCLEAR = 0x0008
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def _configure_win32() -> None:
    assert kernel32 is not None
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetCommState.argtypes = [wintypes.HANDLE, ctypes.POINTER(DCB)]
    kernel32.GetCommState.restype = wintypes.BOOL
    kernel32.SetCommState.argtypes = [wintypes.HANDLE, ctypes.POINTER(DCB)]
    kernel32.SetCommState.restype = wintypes.BOOL
    kernel32.SetCommTimeouts.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(COMMTIMEOUTS),
    ]
    kernel32.SetCommTimeouts.restype = wintypes.BOOL
    kernel32.SetupComm.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    kernel32.SetupComm.restype = wintypes.BOOL
    kernel32.PurgeComm.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.PurgeComm.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL


def _win_error(operation: str) -> OSError:
    code = ctypes.get_last_error()
    return OSError(code, f"{operation} failed: {ctypes.FormatError(code).strip()}")


def _checked(result: int, operation: str) -> None:
    if not result:
        raise _win_error(operation)


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _hex(data: bytes) -> str:
    return data.hex(" ").upper()


def list_serial_ports() -> list[tuple[str, str]]:
    if sys.platform != "win32":
        raise RuntimeError("SerialStartStopTest.py only supports Windows")
    ports: list[tuple[str, str]] = []
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DEVICEMAP\SERIALCOMM",
        )
    except FileNotFoundError:
        return ports
    with key:
        index = 0
        while True:
            try:
                device, port, _ = winreg.EnumValue(key, index)
            except OSError:
                break
            ports.append((str(port), str(device)))
            index += 1
    return sorted(ports, key=lambda item: item[0])


class WindowsSerialPort:
    def __init__(self, port: str, baud: int, read_timeout_ms: int = 100) -> None:
        if sys.platform != "win32":
            raise RuntimeError("SerialStartStopTest.py only supports Windows")
        match = PORT_PATTERN.fullmatch(port.strip())
        if match is None:
            raise ValueError("--port must look like COM4")
        if baud <= 0:
            raise ValueError("--baud must be positive")
        self.port = f"COM{int(match.group(1))}"
        self.baud = baud
        self.read_timeout_ms = read_timeout_ms
        self.handle: int | None = None

    def __enter__(self) -> "WindowsSerialPort":
        assert kernel32 is not None
        _configure_win32()
        path = rf"\\.\{self.port}"
        handle = kernel32.CreateFileW(
            path,
            GENERIC_READ | GENERIC_WRITE,
            0,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            raise _win_error(f"open {self.port}")
        self.handle = handle
        try:
            _checked(kernel32.SetupComm(handle, 4096, 4096), "SetupComm")
            dcb = DCB()
            dcb.DCBlength = ctypes.sizeof(DCB)
            _checked(kernel32.GetCommState(handle, ctypes.byref(dcb)), "GetCommState")
            dcb.BaudRate = self.baud
            # fBinary=1 and fTXContinueOnXoff=1; hardware/software flow control,
            # DTR, RTS, parity checking and abort-on-error are disabled.
            dcb.flags = (1 << 0) | (1 << 7)
            dcb.ByteSize = 8
            dcb.Parity = 0  # NOPARITY
            dcb.StopBits = 0  # ONESTOPBIT
            _checked(kernel32.SetCommState(handle, ctypes.byref(dcb)), "SetCommState")
            self._set_timeouts(
                read_timeout_ms=self.read_timeout_ms,
                write_timeout_ms=_timeout_milliseconds(STOP_WRITE_TIMEOUT_SECONDS),
            )
            _checked(
                kernel32.PurgeComm(
                    handle,
                    PURGE_TXABORT | PURGE_RXABORT | PURGE_TXCLEAR | PURGE_RXCLEAR,
                ),
                "PurgeComm",
            )
            return self
        except Exception:
            kernel32.CloseHandle(handle)
            self.handle = None
            raise

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.handle is not None:
            assert kernel32 is not None
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def _open_handle(self) -> int:
        if self.handle is None:
            raise RuntimeError("serial port is not open")
        return self.handle

    def _set_timeouts(self, *, read_timeout_ms: int, write_timeout_ms: int) -> None:
        assert kernel32 is not None
        timeouts = COMMTIMEOUTS(
            0xFFFFFFFF,
            0,
            read_timeout_ms,
            0,
            write_timeout_ms,
        )
        _checked(
            kernel32.SetCommTimeouts(
                self._open_handle(),
                ctypes.byref(timeouts),
            ),
            "SetCommTimeouts",
        )

    def purge_input(self) -> None:
        assert kernel32 is not None
        _checked(kernel32.PurgeComm(self._open_handle(), PURGE_RXCLEAR), "PurgeComm")

    def write(self, payload: bytes, *, deadline: float | None = None) -> None:
        assert kernel32 is not None
        handle = self._open_handle()
        if deadline is None:
            timeout_seconds = STOP_WRITE_TIMEOUT_SECONDS
        else:
            if not math.isfinite(deadline):
                raise ValueError("serial write deadline must be finite")
            timeout_seconds = deadline - _monotonic()
            if timeout_seconds <= 0:
                raise TimeoutError("START software budget expired before WriteFile")
        timeout_ms = _timeout_milliseconds(timeout_seconds)
        self._set_timeouts(
            read_timeout_ms=self.read_timeout_ms,
            write_timeout_ms=timeout_ms,
        )
        if deadline is not None and deadline <= _monotonic():
            raise TimeoutError("START software budget expired before WriteFile")
        buffer = ctypes.create_string_buffer(payload)
        written = wintypes.DWORD()
        _checked(
            kernel32.WriteFile(
                handle,
                buffer,
                len(payload),
                ctypes.byref(written),
                None,
            ),
            "WriteFile",
        )
        if written.value != len(payload):
            raise OSError(f"short serial write: {written.value}/{len(payload)} bytes")

    def read_for(
        self,
        seconds: float,
        *,
        deadline: float | None = None,
        stop_on_data: bool = False,
    ) -> bytes:
        assert kernel32 is not None
        _validate_read_seconds(seconds)
        if deadline is not None and not math.isfinite(deadline):
            raise ValueError("read deadline must be finite")
        handle = self._open_handle()
        read_deadline = _monotonic() + seconds
        if deadline is not None:
            read_deadline = min(read_deadline, deadline)
        chunks: list[bytes] = []
        while True:
            remaining = read_deadline - _monotonic()
            if remaining <= 0:
                break
            read_timeout_ms = min(
                self.read_timeout_ms,
                _timeout_milliseconds(remaining),
            )
            # Each synchronous ReadFile gets no more than the current remainder.
            self._set_timeouts(
                read_timeout_ms=read_timeout_ms,
                write_timeout_ms=_timeout_milliseconds(STOP_WRITE_TIMEOUT_SECONDS),
            )
            buffer = ctypes.create_string_buffer(256)
            received = wintypes.DWORD()
            _checked(
                kernel32.ReadFile(
                    handle,
                    buffer,
                    len(buffer),
                    ctypes.byref(received),
                    None,
                ),
                "ReadFile",
            )
            if received.value:
                chunks.append(buffer.raw[: received.value])
                if stop_on_data:
                    break
        return b"".join(chunks)


def send_frame(
    serial_port: WindowsSerialPort,
    frame: bytes,
    *,
    label: str,
    read_seconds: float,
    budget_seconds: float | None = None,
    stop_on_data: bool = False,
) -> bytes:
    _validate_read_seconds(read_seconds)
    if budget_seconds is not None:
        _validate_duration(budget_seconds)
    serial_port.purge_input()
    print(f"[{_timestamp()}] TX {serial_port.port} {label}: {_hex(frame)}")
    deadline = None
    if budget_seconds is None:
        # STOP has its own finite write timeout, independent of the START budget.
        serial_port.write(frame)
    else:
        # Begin the START budget immediately before the synchronous write call.
        deadline = _monotonic() + budget_seconds
        serial_port.write(frame, deadline=deadline)
    response = serial_port.read_for(
        read_seconds,
        deadline=deadline,
        stop_on_data=stop_on_data,
    )
    if response:
        print(f"[{_timestamp()}] RX {serial_port.port}: {_hex(response)}")
    else:
        print(f"[{_timestamp()}] RX {serial_port.port}: <no bytes>")
    return response


def _require_execution(args: argparse.Namespace, *, starts_stimulation: bool) -> None:
    if not args.execute:
        raise ValueError("No frame sent: add --execute after completing dry-run checks")
    if starts_stimulation and args.confirm != CONFIRMATION:
        raise ValueError(
            "cycle requires --confirm I_ACCEPT_PHYSICAL_STIMULATION"
        )


def run(args: argparse.Namespace) -> int:
    print(f"Start frame: {_hex(START_FRAME)} (decimal marker 192)")
    print(f"Stop frame:  {_hex(STOP_FRAME)} (decimal marker 128)")
    print(f"Serial settings: {args.baud} baud, 8 data bits, no parity, 1 stop bit")

    if args.action == "list":
        ports = list_serial_ports()
        if not ports:
            print("No COM ports found in the Windows registry.")
        for port, device in ports:
            print(f"{port}: {device}")
        return 0

    if not args.port:
        raise ValueError("--port is required for dry-run, stop and cycle")

    if args.action == "dry-run":
        print(f"Selected port: {args.port}")
        print("DRY RUN ONLY: the COM port was not opened and no frame was sent.")
        return 0

    _validate_read_seconds(args.read_seconds)
    if args.action == "cycle":
        _validate_duration(args.duration)
    _require_execution(args, starts_stimulation=args.action == "cycle")

    with WindowsSerialPort(args.port, args.baud) as serial_port:
        if args.action == "stop":
            try:
                send_frame(
                    serial_port,
                    STOP_FRAME,
                    label="STOP",
                    read_seconds=args.read_seconds,
                )
            except Exception as exc:
                print(
                    f"STOP ATTEMPT FAILED: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                print(
                    "停止未确认，请使用独立停止方式并核实物理输出。",
                    file=sys.stderr,
                )
                return 2
            print(
                "停止未确认，请使用独立停止方式并核实物理输出。",
                file=sys.stderr,
            )
            return 3

        try:
            start_response = send_frame(
                serial_port,
                START_FRAME,
                label="START",
                read_seconds=args.read_seconds,
                budget_seconds=args.duration,
                stop_on_data=True,
            )
            if start_response:
                print(
                    "收到字节；命令/物理状态未确认，立即尝试保护性 STOP。"
                )
            else:
                print(
                    "START 读取预算结束但未收到字节；命令/物理状态未确认，"
                    "立即尝试保护性 STOP。"
                )
        finally:
            try:
                send_frame(
                    serial_port,
                    STOP_FRAME,
                    label="STOP",
                    read_seconds=args.read_seconds,
                )
            except Exception as exc:
                print(
                    f"STOP ATTEMPT FAILED: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
            print(
                "停止未确认，请使用独立停止方式并核实物理输出。",
                file=sys.stderr,
            )
        return 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Test COM start/stop frames using only the Python standard library."
    )
    parser.add_argument("action", choices=("list", "dry-run", "stop", "cycle"))
    parser.add_argument("--port", default="")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--read-seconds", type=float, default=1.0)
    parser.add_argument("--duration", type=float, default=2.0)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    return parser


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except KeyboardInterrupt:
        print("Interrupted; if cycle had started, its finally block attempted STOP.")
        return 130
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
