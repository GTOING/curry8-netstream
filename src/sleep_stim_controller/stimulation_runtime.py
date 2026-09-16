"""Thread-safe live-session orchestration for P3 decisions and simulation."""
from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import Callable

from PySide6.QtCore import QObject, Signal

from .rally import (
    LoopbackRallySimulator,
    RallyRequestOutcome,
    RallyTransportWorker,
    SimulatedRallyEndpoint,
)
from .staging import BlockContext, ProcessingResult
from .stimulation import (
    DecisionEvaluation,
    RequestStatus,
    StimulationConfig,
    StimulationDecisionEngine,
    StimulusRequest,
)


class StimulationRuntime(QObject):
    """Coordinates policy, the app-owned endpoint and the P2 writer handoff."""

    simulator_status_changed = Signal(bool, str)
    automatic_status_changed = Signal(bool, str)
    decision_changed = Signal(object)
    request_changed = Signal(object)
    diagnostic = Signal(str)
    activity_changed = Signal(bool)
    shutdown_completed = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        request_timeout_seconds: float = 1.0,
        simulator_factory: Callable[[], LoopbackRallySimulator] = LoopbackRallySimulator,
    ) -> None:
        super().__init__(parent)
        self._lock = threading.RLock()
        self._engine = StimulationDecisionEngine()
        self._config = StimulationConfig()
        self._automatic_enabled = False
        self._session_id: str | None = None
        self._pipeline = None
        self._recording_enabled = False
        self._logged_config_versions: set[int] = set()
        self._outstanding: dict[str, object] = {}
        self._simulator_factory = simulator_factory
        self._simulator: LoopbackRallySimulator | None = None
        self._endpoint: SimulatedRallyEndpoint | None = None
        self._shutting_down = False
        self._stopped_signal_sent = False
        self._activity = False
        self._transport = RallyTransportWorker(
            owns_endpoint=self._owns_endpoint,
            before_send=self._before_send,
            on_sent=self._on_sent,
            on_not_sent=self._on_not_sent,
            on_outcome=self._on_outcome,
            on_diagnostic=self.diagnostic.emit,
            on_stopped=self._on_transport_stopped,
            timeout_seconds=request_timeout_seconds,
        )

    @property
    def config(self) -> StimulationConfig:
        with self._lock:
            return self._config

    @property
    def automatic_enabled(self) -> bool:
        with self._lock:
            return self._automatic_enabled

    @property
    def simulator_active(self) -> bool:
        with self._lock:
            simulator = self._simulator
            return simulator is not None and simulator.active

    @property
    def simulator_endpoint_text(self) -> str:
        with self._lock:
            endpoint = self._endpoint
            if endpoint is None:
                return "模拟端未运行（不会发送）"
            return (
                f"本应用持有的模拟端：{endpoint.host}:{endpoint.port} · 随机端口"
            )

    @property
    def request_busy(self) -> bool:
        return self._transport.busy

    @property
    def background_busy(self) -> bool:
        with self._lock:
            return self._activity

    @property
    def quarantined_socket_count(self) -> int:
        return self._transport.quarantined_socket_count

    @property
    def request_timeout_seconds(self) -> float:
        return self._transport.timeout_seconds

    def configure(self, config: StimulationConfig) -> StimulationConfig:
        with self._lock:
            if self._automatic_enabled:
                raise ValueError("请先关闭自动决策，再修改配置")
            comparable = replace(config, config_version=self._config.config_version)
            current = self._config
            if comparable == current:
                return current
            updated = replace(config, config_version=current.config_version + 1)
            self._engine.set_config(updated, automatic_enabled=False)
            self._config = updated
            # Future results use the new configuration. Old results are not
            # re-evaluated; the engine keeps last block and send interval.
        issues = updated.issues()
        if issues:
            detail = "配置已更新 · " + "；".join(issues)
        elif self.simulator_active:
            detail = "配置已更新 · 条件齐备；自动决策仍关闭"
        else:
            detail = "配置已更新 · 条件齐备，仍需启动模拟端"
        self.automatic_status_changed.emit(False, detail)
        return updated

    def set_request_timeout(self, seconds: float) -> None:
        self._transport.set_timeout_seconds(seconds)

    def start_simulator(self) -> SimulatedRallyEndpoint:
        with self._lock:
            if self._shutting_down:
                raise RuntimeError("应用正在退出，不能启动模拟端")
            if self._simulator is not None and self._simulator.active:
                assert self._endpoint is not None
                return self._endpoint
            simulator = self._simulator_factory()
            endpoint = simulator.start()
            self._simulator = simulator
            self._endpoint = endpoint
        self.simulator_status_changed.emit(True, self.simulator_endpoint_text)
        self.diagnostic.emit(
            "已启动本应用拥有的 127.0.0.1 随机 UDP 模拟端；默认仅返回模拟成功码。"
        )
        self._refresh_activity()
        return endpoint

    def stop_simulator(self) -> None:
        self.set_automatic_enabled(False, reason="模拟端关闭；自动决策已停止")
        with self._lock:
            simulator = self._simulator
            endpoint = self._endpoint
            self._simulator = None
            self._endpoint = None
        if simulator is not None:
            simulator.stop()
        if endpoint is not None:
            self._transport.release_endpoint(endpoint.owner_id)
        self.simulator_status_changed.emit(False, "模拟端未运行（不会发送）")
        self._refresh_activity()

    def begin_session(self, session_id: str, pipeline, recording_enabled: bool) -> None:
        with self._lock:
            self._session_id = session_id
            self._pipeline = pipeline
            self._recording_enabled = recording_enabled
            self._logged_config_versions.clear()
            self._engine.begin_session(session_id)
        self.diagnostic.emit(f"P3 决策周期已绑定实时会话 {session_id}；回放不会进入策略。")

    def finish_session(self, session_id: str) -> None:
        with self._lock:
            if session_id != self._session_id:
                return
            self._automatic_enabled = False
            self._session_id = None
            self._pipeline = None
            self._recording_enabled = False
        self.automatic_status_changed.emit(False, "实时会话已结束；自动决策已关闭")
        self._refresh_activity()

    def on_session_stopping(self, reason: str = "实时会话正在收尾") -> None:
        self.set_automatic_enabled(False, reason=reason)

    def set_automatic_enabled(self, enabled: bool, *, reason: str = "") -> bool:
        if enabled:
            with self._lock:
                if self._shutting_down:
                    why = "应用正在退出"
                elif self._endpoint is None or not self._owns_endpoint(self._endpoint):
                    why = "请先启动本应用模拟端"
                else:
                    issues = self._config.issues()
                    why = "配置不完整：" + "；".join(issues) if issues else ""
                if why:
                    self._automatic_enabled = False
                else:
                    self._automatic_enabled = True
            if why:
                self.automatic_status_changed.emit(False, why)
                return False
            self.automatic_status_changed.emit(
                True, "自动决策已启用 · 仅向本应用持有的模拟端发送"
            )
            return True

        with self._lock:
            self._automatic_enabled = False
        self._transport.cancel_unsent()
        self.automatic_status_changed.emit(
            False, reason or "自动决策已关闭；已发送请求仍等待响应或记为未知"
        )
        return True

    def process_block(
        self, context: BlockContext, result: ProcessingResult
    ) -> tuple[tuple[dict[str, object], ...], DecisionEvaluation]:
        with self._lock:
            evaluation = self._engine.evaluate(
                result,
                received_monotonic_ns=context.received_monotonic_ns,
                automatic_enabled=self._automatic_enabled,
                simulator_ready=(
                    self._endpoint is not None and self._owns_endpoint(self._endpoint)
                ),
                request_busy=self._transport.busy,
            )
            version = evaluation.decision.config_version
            if version in self._logged_config_versions:
                events = evaluation.events[1:]
            else:
                events = evaluation.events
                self._logged_config_versions.add(version)
        self.decision_changed.emit(evaluation.decision)
        self.diagnostic.emit(
            f"block_id={result.block_id}：{evaluation.decision.reason}"
        )
        return tuple(events), evaluation

    def decision_recorded(self, evaluation: object) -> None:
        if not isinstance(evaluation, DecisionEvaluation):
            return
        request = evaluation.candidate
        if request is None:
            return
        with self._lock:
            pipeline = self._pipeline
            endpoint = self._endpoint
            ready = (
                self._automatic_enabled
                and self._session_id == request.session_id
                and pipeline is not None
                and endpoint is not None
                and self._owns_endpoint(endpoint)
            )
        if not ready:
            self._record_not_sent(
                request,
                "发送前自动决策/模拟端/会话已关闭",
                None,
                pipeline=pipeline,
            )
            return
        if not pipeline.acquire_external_work():
            self._record_not_sent(
                request,
                "会话已开始收尾，请求未发送",
                None,
                pipeline=pipeline,
            )
            return
        with self._lock:
            self._outstanding[request.request_id] = pipeline
        if not self._transport.submit(request, endpoint):
            self._record_not_sent(request, "模拟传输忙碌或端点所有权已失效", None)
            return
        self.request_changed.emit(
            {
                "request_id": request.request_id,
                "status": RequestStatus.WAITING_RESPONSE.value,
                "message": "请求已排队；发送前将再次检查结果年龄和配置",
                "block_id": request.block_id,
            }
        )
        self._refresh_activity()

    def shutdown(self) -> None:
        with self._lock:
            if self._shutting_down:
                return
            self._shutting_down = True
        self.set_automatic_enabled(False, reason="应用退出；自动决策已关闭")
        with self._lock:
            simulator = self._simulator
            endpoint = self._endpoint
            self._simulator = None
            self._endpoint = None
        self._refresh_activity(force=True)
        if simulator is not None:
            simulator.stop()
        if endpoint is not None:
            self._transport.release_endpoint(endpoint.owner_id)
        self._transport.shutdown()
        self.simulator_status_changed.emit(False, "模拟端已关闭")
        self._refresh_activity()

    def join(self, timeout: float | None = None) -> bool:
        return self._transport.join(timeout)

    def _owns_endpoint(self, endpoint: SimulatedRallyEndpoint) -> bool:
        with self._lock:
            simulator = self._simulator
            return simulator is not None and simulator.owns(endpoint)

    def _before_send(
        self, request: StimulusRequest, now_ns: int
    ) -> tuple[bool, str, object | None]:
        with self._lock:
            if not self._automatic_enabled:
                return False, "自动决策已关闭，请求未发送", None
            if self._session_id != request.session_id:
                return False, "请求不属于当前实时会话", None
            if self._config.config_version != request.config_version:
                return False, "配置版本已变化，请求未发送", None
            if self._endpoint is None or not self._owns_endpoint(self._endpoint):
                return False, "本应用模拟端已关闭，请求未发送", None
            age_seconds = (now_ns - request.received_monotonic_ns) / 1_000_000_000
            if age_seconds < 0 or age_seconds > request.max_result_age_seconds:
                return False, "发送前复核：分期结果已过期，请求未发送", None
            sent_ns = self._engine.last_sent_monotonic_ns
            interval = self._config.min_request_interval_seconds
            if (
                sent_ns is not None
                and interval is not None
                and (now_ns - sent_ns) / 1_000_000_000 < interval
            ):
                return False, "发送前复核：未达到最小请求间隔", None
            pipeline = self._pipeline
            recording_enabled = self._recording_enabled
        reservation = (
            pipeline.reserve_session_event()
            if pipeline is not None and recording_enabled
            else None
        )
        if pipeline is not None and recording_enabled and reservation is None:
            self.set_automatic_enabled(False, reason="刺激请求事件无法进入会话记录")
            return False, "请求事件无法记录，已阻止 UDP 发送", None
        return True, "", reservation

    def _on_sent(
        self, request: StimulusRequest, sent_ns: int, reservation: object | None
    ) -> None:
        with self._lock:
            pipeline = self._outstanding.get(request.request_id)
            self._engine.mark_request_sent(request.session_id, sent_ns)
        event_committed = True
        if pipeline is not None and reservation is not None:
            event_committed = pipeline.commit_session_event_reservation(
                str(reservation), request.to_sent_event()
            )
        if not event_committed:
            self.set_automatic_enabled(False, reason="request_sent 事件无法记录")
            self.diagnostic.emit("UDP 已发送，但 request_sent 事件未能写入；会话将标记失败/不完整。")
        self.request_changed.emit(
            {
                "request_id": request.request_id,
                "status": RequestStatus.WAITING_RESPONSE.value,
                "message": "UDP 请求已发至本应用模拟端，等待响应",
                "block_id": request.block_id,
                "sent_monotonic_ns": sent_ns,
            }
        )

    def _on_not_sent(
        self,
        request: StimulusRequest,
        reason: str,
        reservation: object | None,
        status: RequestStatus,
    ) -> None:
        with self._lock:
            pipeline = self._outstanding.get(request.request_id)
        if pipeline is not None and reservation is not None:
            pipeline.cancel_session_event_reservation(str(reservation))
        self._record_outcome(
            request,
            status=status,
            message=reason,
            send_attempted=status is RequestStatus.UNKNOWN,
        )

    def _on_outcome(
        self, request: StimulusRequest, outcome: RallyRequestOutcome
    ) -> None:
        self._record_outcome(
            request,
            status=outcome.status,
            message=outcome.message,
            response_code=outcome.response_code,
            raw_response=outcome.raw_text,
            received_monotonic_ns=outcome.received_monotonic_ns,
            send_attempted=outcome.status is RequestStatus.UNKNOWN,
        )

    def _record_not_sent(
        self,
        request: StimulusRequest,
        reason: str,
        reservation: object | None,
        *,
        pipeline=None,
    ) -> None:
        with self._lock:
            owned_pipeline = self._outstanding.get(request.request_id)
        if owned_pipeline is not None:
            if reservation is not None:
                owned_pipeline.cancel_session_event_reservation(str(reservation))
            pipeline = owned_pipeline
        elif pipeline is not None and reservation is not None:
            pipeline.cancel_session_event_reservation(str(reservation))
        self._record_outcome(
            request,
            status=RequestStatus.NOT_SENT,
            message=reason,
            send_attempted=False,
            pipeline_override=pipeline,
        )

    def _record_outcome(
        self,
        request: StimulusRequest,
        *,
        status: RequestStatus,
        message: str,
        response_code: str | None = None,
        raw_response: str | None = None,
        received_monotonic_ns: int | None = None,
        send_attempted: bool = False,
        pipeline_override=None,
    ) -> None:
        with self._lock:
            pipeline = self._outstanding.pop(request.request_id, None)
            has_external_lease = pipeline is not None
            recording_enabled = self._recording_enabled
        if pipeline is None:
            pipeline = pipeline_override
        event = {
            "event_type": "request_outcome",
            "session_id": request.session_id,
            "block_id": request.block_id,
            "request_id": request.request_id,
            "payload": {
                "config_version": request.config_version,
                "status": status.value,
                "message": message,
                "response_code": response_code,
                "raw_response": raw_response,
                "received_monotonic_ns": received_monotonic_ns,
                "send_attempted": send_attempted,
            },
        }
        if pipeline is not None and getattr(pipeline, "session_id", None) == request.session_id:
            if recording_enabled and getattr(pipeline, "recording_enabled", False):
                pipeline.enqueue_session_event(event)
            if has_external_lease:
                pipeline.release_external_work()
        self.request_changed.emit(
            {
                "request_id": request.request_id,
                "status": status.value,
                "message": message,
                "response_code": response_code,
                "block_id": request.block_id,
            }
        )
        self.diagnostic.emit(
            f"request_id={request.request_id} block_id={request.block_id} "
            f"结果={status.value}：{message}"
        )
        if status in {RequestStatus.UNKNOWN, RequestStatus.API_REJECTED}:
            self.set_automatic_enabled(
                False,
                reason=(
                    "请求结果未知，自动决策已关闭；不自动重发"
                    if status is RequestStatus.UNKNOWN
                    else "模拟 API 报告拒绝，自动决策已关闭；检查后可显式重启"
                ),
            )
        self._refresh_activity()

    def _on_transport_stopped(self) -> None:
        with self._lock:
            self._stopped_signal_sent = True
        self.shutdown_completed.emit()
        self._refresh_activity()

    def _refresh_activity(self, *, force: bool = False) -> None:
        with self._lock:
            active = (
                self._shutting_down and not self._stopped_signal_sent
            ) or (
                self._simulator is not None and self._simulator.active
            ) or self._transport.busy
            changed = force or active != self._activity
            self._activity = active
        if changed:
            self.activity_changed.emit(active)
