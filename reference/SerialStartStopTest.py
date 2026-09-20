"""Windows serial test for five-byte stimulation start/stop frames.

Uses only Python's standard library and the Win32 API.  It sends nothing
unless ``--execute`` is present.  The ``cycle`` action always attempts the
stop frame in a ``finally`` block and intentionally has no persistent start.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import re
import sys
import time
from datetime import datetime


START_FRAME = bytes.fromhex("01 E1 01 00 C0")
STOP_FRAME = bytes.fromhex("01 E1 01 00 80")
CONFIRMATION = "I_ACCEPT_PHYSICAL_STIMULATION"
PORT_PATTERN = re.compile(r"COM([1-9][0-9]*)", re.IGNORECASE)


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
    kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    kernel32.FlushFileBuffers.restype = wintypes.BOOL
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
            timeouts = COMMTIMEOUTS(
                0xFFFFFFFF,
                0,
                self.read_timeout_ms,
                0,
                1000,
            )
            _checked(
                kernel32.SetCommTimeouts(handle, ctypes.byref(timeouts)),
                "SetCommTimeouts",
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

    def purge_input(self) -> None:
        assert kernel32 is not None
        _checked(kernel32.PurgeComm(self._open_handle(), PURGE_RXCLEAR), "PurgeComm")

    def write(self, payload: bytes) -> None:
        assert kernel32 is not None
        handle = self._open_handle()
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
        _checked(kernel32.FlushFileBuffers(handle), "FlushFileBuffers")

    def read_for(self, seconds: float) -> bytes:
        assert kernel32 is not None
        if seconds < 0:
            raise ValueError("read duration must be non-negative")
        handle = self._open_handle()
        deadline = time.monotonic() + seconds
        chunks: list[bytes] = []
        while time.monotonic() < deadline:
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
            else:
                time.sleep(0.01)
        return b"".join(chunks)


def send_frame(
    serial_port: WindowsSerialPort,
    frame: bytes,
    *,
    label: str,
    read_seconds: float,
) -> bytes:
    serial_port.purge_input()
    serial_port.write(frame)
    print(f"[{_timestamp()}] TX {serial_port.port} {label}: {_hex(frame)}")
    response = serial_port.read_for(read_seconds)
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

    if args.read_seconds < 0 or args.read_seconds > 10:
        raise ValueError("--read-seconds must be between 0 and 10")
    _require_execution(args, starts_stimulation=args.action == "cycle")

    with WindowsSerialPort(args.port, args.baud) as serial_port:
        if args.action == "stop":
            response = send_frame(
                serial_port,
                STOP_FRAME,
                label="STOP",
                read_seconds=args.read_seconds,
            )
            return 0 if response else 2

        if not 0.1 <= args.duration <= 10.0:
            raise ValueError("--duration must be between 0.1 and 10 seconds")
        start_sent = False
        result_code = 2
        stop_response = b""
        try:
            start_sent = True
            start_response = send_frame(
                serial_port,
                START_FRAME,
                label="START",
                read_seconds=args.read_seconds,
            )
            if not start_response:
                print("Start was not acknowledged; sending STOP immediately.")
            else:
                print(
                    f"A response was received. Observe the approved test load for "
                    f"{args.duration:g}s; STOP will then be sent automatically."
                )
                deadline = time.monotonic() + args.duration
                while time.monotonic() < deadline:
                    time.sleep(min(0.05, deadline - time.monotonic()))
                result_code = 0
        finally:
            if start_sent:
                try:
                    stop_response = send_frame(
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
                if not stop_response:
                    print(
                        "STOP WAS NOT CONFIRMED. Use the independent hardware/Rally "
                        "stop procedure and verify output physically.",
                        file=sys.stderr,
                    )
        return result_code if stop_response else 3


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
