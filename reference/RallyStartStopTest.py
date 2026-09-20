"""Safely test Rally's UDP Start Stim / Stop Stim commands.

This utility sends nothing unless ``--execute`` is present.  The ``cycle``
action always attempts ``Stop Stim`` in a ``finally`` block and intentionally
does not provide a persistent start-only mode.
"""

from __future__ import annotations

import argparse
import ipaddress
import math
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8801
START_COMMAND = "Start Stim"
STOP_COMMAND = "Stop Stim"
SUCCESS_RESPONSE = "RALLY_ERROR_SUCCESS"
CONFIRMATION = "I_ACCEPT_PHYSICAL_STIMULATION"
SUCCESS_RESPONSES_BY_COMMAND = {
    START_COMMAND: frozenset({SUCCESS_RESPONSE, "启动刺激成功"}),
    STOP_COMMAND: frozenset({SUCCESS_RESPONSE, "停止刺激成功"}),
}
_monotonic = time.monotonic
_sleep = time.sleep


@dataclass(frozen=True, slots=True)
class UdpReply:
    command: str
    text: str
    sender_host: str
    sender_port: int
    deadline: float | None = None

    @property
    def succeeded(self) -> bool:
        accepted = SUCCESS_RESPONSES_BY_COMMAND.get(
            self.command,
            frozenset({SUCCESS_RESPONSE}),
        )
        return self.text.strip() in accepted


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _loopback_endpoint(host: str, port: int) -> tuple[str, int]:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("--host must be a numeric loopback address") from exc
    if not address.is_loopback:
        raise ValueError("This test utility only permits a loopback Rally endpoint")
    if address.version != 4:
        raise ValueError("This test utility currently requires an IPv4 loopback address")
    if not 1 <= port <= 65535:
        raise ValueError("--port must be between 1 and 65535")
    return str(address), port


def _validate_timeout(timeout_seconds: float) -> None:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("--timeout must be a finite number greater than zero")


def _validate_duration(duration_seconds: float) -> None:
    if not math.isfinite(duration_seconds) or not 0.1 <= duration_seconds <= 10.0:
        raise ValueError("--duration must be between 0.1 and 10 finite seconds")


def send_rally_command(
    command: str,
    *,
    host: str,
    port: int,
    timeout_seconds: float,
    budget_seconds: float | None = None,
) -> UdpReply:
    _validate_timeout(timeout_seconds)
    if budget_seconds is not None:
        _validate_duration(budget_seconds)
    endpoint = _loopback_endpoint(host, port)
    payload = command.encode("utf-8")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.bind(("127.0.0.1", 0))
        # Start the cycle budget at the send boundary, not after its reply.
        sent_at = _monotonic()
        command_deadline = sent_at + timeout_seconds
        cycle_deadline = (
            sent_at + budget_seconds if budget_seconds is not None else None
        )
        wait_limit = (
            min(timeout_seconds, budget_seconds)
            if budget_seconds is not None
            else timeout_seconds
        )
        client.sendto(payload, endpoint)
        print(f"[{_timestamp()}] TX {endpoint[0]}:{endpoint[1]} {command!r}")
        while True:
            deadline = (
                min(command_deadline, cycle_deadline)
                if cycle_deadline is not None
                else command_deadline
            )
            remaining = deadline - _monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Rally did not reply to {command!r} within its "
                    f"{wait_limit:g}s send-time wait limit"
                )
            client.settimeout(remaining)
            data, sender = client.recvfrom(4096)
            sender_host, sender_port = sender[0], int(sender[1])
            # Rally listens on 8801 but may send its reply from a separate,
            # dynamically allocated UDP source port.  Keep the host check to
            # reject replies from another machine; do not require the source
            # port to equal the destination port.
            if sender_host != endpoint[0]:
                print(
                    f"[{_timestamp()}] ignored UDP reply from "
                    f"{sender_host}:{sender_port}"
                )
                continue
            if sender_port != endpoint[1]:
                print(
                    f"[{_timestamp()}] Rally replied from dynamic UDP port "
                    f"{sender_port} (request destination port was {endpoint[1]})"
                )
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.hex(" ").upper()
                raise RuntimeError(f"Rally returned non-UTF-8 bytes: {text}")
            reply = UdpReply(command, text, sender_host, sender_port, cycle_deadline)
            print(
                f"[{_timestamp()}] RX {sender_host}:{sender_port} "
                f"{text!r} success={reply.succeeded}"
            )
            return reply


def _require_execution(args: argparse.Namespace, *, starts_stimulation: bool) -> None:
    if not args.execute:
        raise ValueError("No command sent: add --execute after completing dry-run checks")
    if starts_stimulation and args.confirm != CONFIRMATION:
        raise ValueError(
            "cycle requires --confirm I_ACCEPT_PHYSICAL_STIMULATION"
        )


def _stop(args: argparse.Namespace) -> bool:
    reply = send_rally_command(
        STOP_COMMAND,
        host=args.host,
        port=args.port,
        timeout_seconds=args.timeout,
    )
    return reply.succeeded


def run(args: argparse.Namespace) -> int:
    _validate_timeout(args.timeout)
    endpoint = _loopback_endpoint(args.host, args.port)
    print(f"Rally endpoint: {endpoint[0]}:{endpoint[1]}")
    print(f"Start command: {START_COMMAND!r}")
    print(f"Stop command:  {STOP_COMMAND!r}")

    if args.action == "dry-run":
        print("DRY RUN ONLY: no UDP datagram was sent.")
        return 0

    if args.action == "stop":
        _require_execution(args, starts_stimulation=False)
        return 0 if _stop(args) else 2

    _require_execution(args, starts_stimulation=True)
    _validate_duration(args.duration)

    start_sent = False
    result_code = 2
    stop_ok = False
    try:
        start_sent = True
        reply = send_rally_command(
            START_COMMAND,
            host=args.host,
            port=args.port,
            timeout_seconds=args.timeout,
            budget_seconds=args.duration,
        )
        if not reply.succeeded:
            print("Start was not confirmed; issuing Stop Stim immediately.")
        else:
            print(
                f"Start confirmed. Observe the approved test load for "
                f"{args.duration:g}s; Stop Stim will then be sent automatically."
            )
            deadline = reply.deadline
            assert deadline is not None
            while True:
                remaining = deadline - _monotonic()
                if remaining <= 0:
                    break
                _sleep(min(0.05, remaining))
            result_code = 0
    finally:
        if start_sent:
            try:
                stop_ok = _stop(args)
            except Exception as exc:  # still surface an explicit emergency warning
                print(f"STOP ATTEMPT FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            if not stop_ok:
                print(
                    "STOP WAS NOT CONFIRMED. Use the independent hardware/Rally "
                    "stop procedure and verify output physically.",
                    file=sys.stderr,
                )
    return result_code if stop_ok else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Test Rally UDP Start Stim / Stop Stim with safe defaults."
    )
    parser.add_argument("action", choices=("dry-run", "stop", "cycle"))
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--duration", type=float, default=2.0)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    return parser


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except KeyboardInterrupt:
        print("Interrupted; if cycle had started, its finally block attempted Stop Stim.")
        return 130
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
