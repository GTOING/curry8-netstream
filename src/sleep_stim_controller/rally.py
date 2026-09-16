"""Owned loopback Rally simulator and single-owner UDP request worker.

The desktop application can transmit only through a simulator instance it
created and still owns. Each request uses a distinct source port retained until
that simulator instance ends, so the vendor response (which has no request ID)
cannot be mistaken for the reply to a later command.
"""
from __future__ import annotations

import heapq
import math
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .stimulation import (
    REALTIME_CONTROL_TOKEN,
    RequestStatus,
    StimulusRequest,
    ParsedRallyResponse,
    parse_rally_response,
)


@dataclass(frozen=True, slots=True)
class SimulatedRallyEndpoint:
    host: str
    port: int
    owner_id: str


@dataclass(frozen=True, slots=True)
class SimulatedReply:
    data: bytes | str = "RALLY_ERROR_SUCCESS"
    delay_seconds: float = 0.0
    wrong_source: bool = False


ReplyFactory = Callable[[int, bytes], Sequence[SimulatedReply]]


class LoopbackRallySimulator:
    """A testable Rally-shaped UDP responder, bound only to random loopback."""

    def __init__(
        self,
        reply_factory: ReplyFactory | None = None,
        *,
        poll_interval_seconds: float = 0.02,
    ) -> None:
        self._reply_factory = reply_factory or (
            lambda _index, _request: (SimulatedReply(),)
        )
        self._poll_interval = poll_interval_seconds
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._endpoint: SimulatedRallyEndpoint | None = None
        self._requests: list[tuple[bytes, tuple[str, int]]] = []
        self._received = threading.Condition(self._lock)

    @property
    def endpoint(self) -> SimulatedRallyEndpoint | None:
        with self._lock:
            return self._endpoint

    @property
    def active(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def requests(self) -> tuple[tuple[bytes, tuple[str, int]], ...]:
        with self._lock:
            return tuple(self._requests)

    def owns(self, endpoint: SimulatedRallyEndpoint) -> bool:
        with self._lock:
            return self._endpoint is endpoint

    def start(self) -> SimulatedRallyEndpoint:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                assert self._endpoint is not None
                return self._endpoint
            server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            server.bind(("127.0.0.1", 0))
            server.settimeout(self._poll_interval)
            host, port = server.getsockname()[:2]
            self._socket = server
            self._endpoint = SimulatedRallyEndpoint(
                str(host), int(port), uuid.uuid4().hex
            )
            self._requests.clear()
            self._stop.clear()
            thread = threading.Thread(
                target=self._run,
                name="owned-loopback-rally-simulator",
                daemon=False,
            )
            self._thread = thread
            thread.start()
            return self._endpoint

    def wait_for_requests(self, count: int, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._received:
            while len(self._requests) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._received.wait(remaining)
            return True

    def stop(self, timeout: float = 1.0) -> None:
        with self._lock:
            thread = self._thread
            server = self._socket
            self._stop.set()
            if server is not None:
                try:
                    server.close()
                except OSError:
                    pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                raise RuntimeError("本机模拟端线程未能在限定时间内退出")
        with self._lock:
            self._thread = None
            self._socket = None
            self._endpoint = None
            self._received.notify_all()

    def _run(self) -> None:
        with self._lock:
            server = self._socket
        if server is None:
            return
        scheduled: list[tuple[float, int, tuple[str, int], bytes, bool]] = []
        serial = 0
        request_number = 0
        while not self._stop.is_set():
            now = time.monotonic()
            while scheduled and scheduled[0][0] <= now:
                _, _, destination, data, wrong_source = heapq.heappop(scheduled)
                try:
                    if wrong_source:
                        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as other:
                            other.sendto(data, destination)
                    else:
                        server.sendto(data, destination)
                except OSError:
                    if self._stop.is_set():
                        return
            try:
                request, source = server.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            if source[0] != "127.0.0.1":
                continue
            if not request.startswith((REALTIME_CONTROL_TOKEN + " ").encode("utf-8")):
                continue
            with self._received:
                self._requests.append((bytes(request), (str(source[0]), int(source[1]))))
                request_number = len(self._requests)
                self._received.notify_all()
            try:
                replies = self._reply_factory(request_number, bytes(request))
            except Exception:
                replies = ()
            now = time.monotonic()
            for reply in replies:
                if not isinstance(reply, SimulatedReply):
                    continue
                delay = max(0.0, float(reply.delay_seconds))
                data = (
                    reply.data.encode("utf-8")
                    if isinstance(reply.data, str)
                    else bytes(reply.data)
                )
                serial += 1
                heapq.heappush(
                    scheduled,
                    (now + delay, serial, (str(source[0]), int(source[1])), data, reply.wrong_source),
                )


@dataclass(frozen=True, slots=True)
class RallyRequestOutcome:
    status: RequestStatus
    message: str
    response_code: str | None = None
    raw_text: str | None = None
    received_monotonic_ns: int | None = None


@dataclass(slots=True)
class _Command:
    request: StimulusRequest
    endpoint: SimulatedRallyEndpoint
    cancelled: threading.Event = field(default_factory=threading.Event)
    sent: bool = False
    reservation: object | None = None


BeforeSend = Callable[[StimulusRequest, int], tuple[bool, str, object | None]]
OnSent = Callable[[StimulusRequest, int, object | None], None]
OnNotSent = Callable[
    [StimulusRequest, str, object | None, RequestStatus], None
]
OnOutcome = Callable[[StimulusRequest, RallyRequestOutcome], None]


class RallyTransportWorker:
    """Serial, bounded UDP sender; all sockets are owned by its one worker."""

    def __init__(
        self,
        *,
        owns_endpoint: Callable[[SimulatedRallyEndpoint], bool],
        before_send: BeforeSend,
        on_sent: OnSent,
        on_not_sent: OnNotSent,
        on_outcome: OnOutcome,
        on_diagnostic: Callable[[str], None] | None = None,
        on_stopped: Callable[[], None] | None = None,
        timeout_seconds: float = 1.0,
        max_quarantined_sockets: int = 128,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds 必须大于 0")
        if (
            isinstance(max_quarantined_sockets, bool)
            or not isinstance(max_quarantined_sockets, int)
            or max_quarantined_sockets < 1
        ):
            raise ValueError("max_quarantined_sockets 必须大于 0")
        self._owns_endpoint = owns_endpoint
        self._before_send = before_send
        self._on_sent = on_sent
        self._on_not_sent = on_not_sent
        self._on_outcome = on_outcome
        self._on_diagnostic = on_diagnostic or (lambda _message: None)
        self._on_stopped = on_stopped or (lambda: None)
        self._timeout_seconds = float(timeout_seconds)
        self.max_quarantined_sockets = max_quarantined_sockets
        self._condition = threading.Condition(threading.RLock())
        self._pending: _Command | None = None
        self._current: _Command | None = None
        self._shutdown = False
        self._quarantined: list[tuple[str, socket.socket]] = []
        self._release_owners: set[str] = set()
        self._thread = threading.Thread(
            target=self._run,
            name="rally-udp-transport",
            daemon=False,
        )
        self._thread.start()

    @property
    def busy(self) -> bool:
        with self._condition:
            return self._pending is not None or self._current is not None

    @property
    def active(self) -> bool:
        return self._thread.is_alive()

    @property
    def quarantined_socket_count(self) -> int:
        with self._condition:
            return len(self._quarantined)

    @property
    def timeout_seconds(self) -> float:
        with self._condition:
            return self._timeout_seconds

    def set_timeout_seconds(self, seconds: float) -> None:
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(float(seconds))
            or seconds <= 0
        ):
            raise ValueError("timeout_seconds 必须为有限正数")
        with self._condition:
            if self._pending is not None or self._current is not None:
                raise RuntimeError("请求等待响应期间不能更改通信超时")
            self._timeout_seconds = float(seconds)

    def submit(
        self,
        request: StimulusRequest,
        endpoint: SimulatedRallyEndpoint,
    ) -> bool:
        if not self._owns_endpoint(endpoint):
            return False
        with self._condition:
            if self._shutdown or self._pending is not None or self._current is not None:
                return False
            self._pending = _Command(request, endpoint)
            self._condition.notify_all()
            return True

    def cancel_unsent(self) -> StimulusRequest | None:
        """Cancel only work not yet sent; a sent request remains unresolved."""
        with self._condition:
            if self._pending is not None:
                self._pending.cancelled.set()
            if self._current is not None and not self._current.sent:
                self._current.cancelled.set()
            self._condition.notify_all()
            return self._pending.request if self._pending is not None else None

    def release_endpoint(self, owner_id: str) -> None:
        """Ask the socket-owning worker to close this endpoint's old ports."""
        with self._condition:
            self._release_owners.add(owner_id)
            self._condition.notify_all()

    def shutdown(self) -> None:
        with self._condition:
            self._shutdown = True
            if self._pending is not None:
                self._pending.cancelled.set()
            if self._current is not None and not self._current.sent:
                self._current.cancelled.set()
            self._condition.notify_all()

    def join(self, timeout: float | None = None) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            to_close: list[socket.socket] = []
            command: _Command | None = None
            should_stop = False
            with self._condition:
                while (
                    self._pending is None
                    and not self._shutdown
                    and not self._release_owners
                ):
                    self._condition.wait()
                if self._release_owners:
                    owners = self._release_owners
                    self._release_owners = set()
                    retained = []
                    for owner_id, request_socket in self._quarantined:
                        if owner_id in owners:
                            to_close.append(request_socket)
                        else:
                            retained.append((owner_id, request_socket))
                    self._quarantined = retained
                elif self._pending is None and self._shutdown:
                    should_stop = True
                else:
                    command = self._pending
                    self._pending = None
                    self._current = command
            for request_socket in to_close:
                try:
                    request_socket.close()
                except OSError:
                    pass
            if should_stop:
                break
            if command is None:
                continue
            try:
                self._run_command(command)
            except Exception as exc:
                try:
                    if command.sent:
                        self._on_outcome(
                            command.request,
                            RallyRequestOutcome(
                                RequestStatus.UNKNOWN,
                                f"UDP 执行异常，结果未知：{exc}",
                            ),
                        )
                    else:
                        self._on_not_sent(
                            command.request,
                            f"UDP 请求未发送：{exc}",
                            command.reservation,
                            RequestStatus.NOT_SENT,
                        )
                except Exception:
                    pass
            finally:
                with self._condition:
                    self._current = None
                    self._condition.notify_all()
        with self._condition:
            sockets = [request_socket for _, request_socket in self._quarantined]
            self._quarantined.clear()
        for request_socket in sockets:
            try:
                request_socket.close()
            except OSError:
                pass
        try:
            self._on_stopped()
        except Exception:
            # Thread shutdown must complete even if a UI observer has gone away.
            pass

    def _run_command(self, command: _Command) -> None:
        request = command.request
        if command.cancelled.is_set():
            self._on_not_sent(
                request, "自动决策已关闭，请求尚未发送", None,
                RequestStatus.NOT_SENT,
            )
            return
        if not self._owns_endpoint(command.endpoint):
            self._on_not_sent(
                request, "模拟端已关闭，请求尚未发送", None,
                RequestStatus.NOT_SENT,
            )
            return
        with self._condition:
            quarantine_full = len(self._quarantined) >= self.max_quarantined_sockets
        if quarantine_full:
            self._on_not_sent(
                request,
                "迟到响应隔离端口已达会话上限",
                None,
                RequestStatus.NOT_SENT,
            )
            return
        payload = self._build_payload(request)
        request_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            request_socket.bind(("127.0.0.1", 0))
            request_socket.settimeout(0.05)
        except Exception:
            request_socket.close()
            raise
        # Bind the one-off source port first, then recheck age/config/interval
        # as close as practical to sendto; queued candidates are never reused.
        allowed, reason, reservation = self._before_send(
            request, time.monotonic_ns()
        )
        command.reservation = reservation
        if not allowed:
            request_socket.close()
            self._on_not_sent(
                request, reason, reservation, RequestStatus.NOT_SENT
            )
            return
        if command.cancelled.is_set() or self._shutdown:
            request_socket.close()
            self._on_not_sent(
                request, "自动决策已关闭，请求尚未发送", reservation,
                RequestStatus.NOT_SENT,
            )
            return

        cancelled_before_send = False
        send_error: OSError | None = None
        with self._condition:
            if command.cancelled.is_set() or self._shutdown:
                cancelled_before_send = True
            else:
                try:
                    request_socket.sendto(
                        payload,
                        (command.endpoint.host, command.endpoint.port),
                    )
                except OSError as exc:
                    send_error = exc
                else:
                    command.sent = True
                    self._quarantined.append((command.endpoint.owner_id, request_socket))
        if cancelled_before_send:
            request_socket.close()
            self._on_not_sent(
                request, "自动决策已关闭，请求尚未发送", reservation,
                RequestStatus.NOT_SENT,
            )
            return
        if send_error is not None:
            request_socket.close()
            self._on_not_sent(
                request,
                f"UDP sendto 失败；为避免误报，结果未知：{send_error}",
                reservation,
                RequestStatus.UNKNOWN,
            )
            return
        sent_ns = time.monotonic_ns()
        self._on_sent(request, sent_ns, reservation)
        deadline = time.monotonic() + self.timeout_seconds
        expected = (command.endpoint.host, command.endpoint.port)
        while not self._shutdown:
            if not self._owns_endpoint(command.endpoint):
                self._on_outcome(
                    request,
                    RallyRequestOutcome(
                        RequestStatus.UNKNOWN,
                        "本应用模拟端已关闭；已发送请求未获确认，结果未知",
                    ),
                )
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._on_outcome(
                    request,
                    RallyRequestOutcome(
                        RequestStatus.UNKNOWN,
                        "模拟端响应超时；不自动重发，结果未知",
                    ),
                )
                return
            request_socket.settimeout(min(0.05, remaining))
            try:
                data, source = request_socket.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                self._on_outcome(
                    request,
                    RallyRequestOutcome(
                        RequestStatus.UNKNOWN,
                        f"等待响应时 socket 关闭；结果未知：{exc}",
                    ),
                )
                return
            if (str(source[0]), int(source[1])) != expected:
                self._on_diagnostic(
                    f"忽略非本应用模拟端响应来源 {source[0]}:{source[1]}"
                )
                continue
            parsed = parse_rally_response(data)
            self._on_outcome(
                request,
                RallyRequestOutcome(
                    parsed.status,
                    parsed.message,
                    parsed.code,
                    parsed.raw_text,
                    time.monotonic_ns(),
                ),
            )
            return
        self._on_outcome(
            request,
            RallyRequestOutcome(
                RequestStatus.UNKNOWN,
                "应用关闭时请求已发送但未获确认；结果未知",
            ),
        )

    @staticmethod
    def _build_payload(request: StimulusRequest) -> bytes:
        from .stimulation import build_rally_message

        return build_rally_message(request.protocol)
