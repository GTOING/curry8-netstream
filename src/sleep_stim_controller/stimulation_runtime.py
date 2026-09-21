"""Thread-safe live-session orchestration for P3 decisions and simulation."""
from __future__ import annotations

import math
import threading
import time
import uuid
from dataclasses import replace
from typing import Callable

from PySide6.QtCore import QObject, Signal

from .rally import (
    LoopbackRallySimulator,
    REAL_RALLY_ENDPOINT,
    RALLY_START_COMMAND,
    RALLY_STOP_COMMAND,
    RallyControlEndpoint,
    RallyControlOutcome,
    RallyControlRequest,
    RallyControlTransportWorker,
    RallyRequestOutcome,
    RallyTransportWorker,
    SimulatedRallyEndpoint,
)
from .staging import (
    STAGES,
    BlockContext,
    ProcessingResult,
    ProcessingStatus,
    utc_now_iso,
)
from .stimulation import (
    DecisionEvaluation,
    RequestStatus,
    StimulationConfig,
    StimulationDecisionEngine,
    StimulusRequest,
    TriggerMode,
)


class StimulationRuntime(QObject):
    """Coordinates policy, the app-owned endpoint and the P2 writer handoff."""

    simulator_status_changed = Signal(bool, str)
    automatic_status_changed = Signal(bool, str)
    decision_changed = Signal(object)
    request_changed = Signal(object)
    rally_status_changed = Signal(object)
    diagnostic = Signal(str)
    activity_changed = Signal(bool)
    shutdown_completed = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        request_timeout_seconds: float = 1.0,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        simulator_factory: Callable[[], LoopbackRallySimulator] = LoopbackRallySimulator,
        rally_control_endpoint: RallyControlEndpoint = REAL_RALLY_ENDPOINT,
        real_transport_factory: Callable[..., RallyControlTransportWorker]
        | None = None,
    ) -> None:
        super().__init__(parent)
        if not callable(monotonic_ns):
            raise TypeError("monotonic_ns 必须是可调用时钟")
        self._lock = threading.RLock()
        self._monotonic_ns = monotonic_ns
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
        # P4-B state is deliberately kept beside the existing P3 runtime. It
        # shares the session/pipeline writer hooks, but never shares the P3
        # JSON protocol or its simulator endpoint.
        self._control_mode = "simulation"
        self._real_auto_enabled = False
        self._real_runtime_state = "DISARMED/UNKNOWN"
        self._real_desired_state: str | None = None
        self._real_expected_state: str | None = None
        self._real_confirmed_state: str | None = None
        self._real_confirmed_utc: str | None = None
        self._real_last_request: dict[str, object] | None = None
        self._real_last_outcome: dict[str, object] | None = None
        self._real_independent_stop_required = False
        self._real_session_id: str | None = None
        self._real_generation = 0
        self._real_handshake_ready = False
        self._real_model_configured = False
        self._real_seen_block_id = 0
        self._real_last_valid_block_id = 0
        self._real_latest_valid: tuple[ProcessingResult, BlockContext] | None = None
        self._real_baseline_ready = False
        self._real_stop_after_current = False
        self._real_close_requested = False
        self._real_shutdown_requested = False
        self._real_transport_stopped = False
        self._real_inflight: RallyControlRequest | None = None
        self._real_leases: dict[str, object] = {}
        self._real_control_history: list[dict[str, object]] = []
        self._real_endpoint = rally_control_endpoint
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
        control_factory = real_transport_factory or RallyControlTransportWorker
        self._real_transport = control_factory(
            before_send=self._before_real_send,
            on_sent=self._on_real_sent,
            on_not_sent=self._on_real_not_sent,
            on_outcome=self._on_real_outcome,
            on_diagnostic=self.diagnostic.emit,
            on_stopped=self._on_real_transport_stopped,
            timeout_seconds=request_timeout_seconds,
            endpoint=self._real_endpoint,
        )

    @property
    def config(self) -> StimulationConfig:
        with self._lock:
            return self._config

    @property
    def automatic_enabled(self) -> bool:
        with self._lock:
            return (
                self._real_auto_enabled
                if self._control_mode == "real"
                else self._automatic_enabled
            )

    @property
    def control_mode(self) -> str:
        with self._lock:
            return self._control_mode

    @property
    def real_control_enabled(self) -> bool:
        with self._lock:
            return self._real_auto_enabled

    @property
    def real_status(self) -> dict[str, object]:
        with self._lock:
            return self._real_status_locked()

    @property
    def real_control_history(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            return tuple(dict(event) for event in self._real_control_history)

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

    @property
    def real_request_timeout_seconds(self) -> float:
        return self._real_transport.timeout_seconds

    def configure(self, config: StimulationConfig) -> StimulationConfig:
        with self._lock:
            if self._automatic_enabled or self._real_auto_enabled:
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
        elif self._control_mode == "real":
            detail = "配置已更新 · 真实 Rally 模式仍需当前 ONNX 会话和显式启用"
        elif self.simulator_active:
            detail = "配置已更新 · 条件齐备；自动决策仍关闭"
        else:
            detail = "配置已更新 · 条件齐备，仍需启动模拟端"
        self.automatic_status_changed.emit(False, detail)
        return updated

    def set_request_timeout(self, seconds: float) -> None:
        self._transport.set_timeout_seconds(seconds)
        self._real_transport.set_timeout_seconds(seconds)

    def set_control_mode(self, mode: str) -> bool:
        """Select simulation or real Rally control while disarmed."""
        normalized = str(mode).strip().lower()
        if normalized not in {"simulation", "real"}:
            raise ValueError("控制模式必须是 simulation 或 real")
        with self._lock:
            current = self._control_mode
            enabled = self._real_auto_enabled if current == "real" else self._automatic_enabled
        if normalized == current:
            return True
        if enabled:
            # The worker keeps the real stop lifecycle alive; the new mode is
            # selected only after the current control cycle has been disarmed.
            if current == "real":
                self._disable_real_control(
                    "模式切换；真实自动控制已关闭",
                    close_after=False,
                )
            else:
                self.set_automatic_enabled(False, reason="模式切换；模拟自动决策已关闭")
            with self._lock:
                if current == "real" and self._real_inflight is not None:
                    return False
                if current == "simulation" and self._transport.busy:
                    return False
        elif current == "simulation" and self.simulator_active:
            self.stop_simulator()
            if self._transport.busy:
                return False
        elif current == "real":
            with self._lock:
                needs_stop = self._real_confirmed_state == "RUNNING" or (
                    self._real_runtime_state in {"STARTING", "RUNNING", "STOPPING"}
                )
            if needs_stop:
                self._disable_real_control(
                    "模式切换；先完成真实 Rally Stop Stim 收尾",
                    close_after=False,
                )
            with self._lock:
                if self._real_inflight is not None or self._real_transport.busy:
                    return False
        with self._lock:
            self._control_mode = normalized
            if normalized == "real":
                self._real_runtime_state = "DISARMED/UNKNOWN"
                self._real_expected_state = None
                self._real_desired_state = None
        self.automatic_status_changed.emit(
            False,
            "真实 Rally 模式已选择 · 默认关闭；先连接当前实时 ONNX 会话",
        ) if normalized == "real" else self.automatic_status_changed.emit(
            False,
            "本机模拟模式已选择 · 自动决策默认关闭",
        )
        self._emit_real_status()
        return True

    def start_simulator(self) -> SimulatedRallyEndpoint:
        with self._lock:
            if self._shutting_down:
                raise RuntimeError("应用正在退出，不能启动模拟端")
            if self._control_mode != "simulation":
                raise RuntimeError("真实 Rally 模式已选择，不能启动本机模拟端")
            if self._real_auto_enabled or self._real_transport.busy:
                raise RuntimeError("真实 Rally 停止收尾尚未完成，不能启动本机模拟端")
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

    # ------------------------------------------------------------------
    # P4-B real Rally start/stop control

    def _real_config_issues_locked(self) -> tuple[str, ...]:
        config = self._config
        problems: list[str] = []
        targets = config.target_stages
        if not isinstance(targets, (set, frozenset)) or not targets:
            problems.append("尚未选择真实 Rally 目标睡眠期")
        elif any(stage not in STAGES for stage in targets):
            problems.append("真实 Rally 目标睡眠期包含未知标签")
        if not isinstance(config.trigger_mode, TriggerMode):
            problems.append("触发策略尚未选择")
        interval = config.min_request_interval_seconds
        if (
            interval is None
            or isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not math.isfinite(float(interval))
            or float(interval) < 0
        ):
            problems.append("最小请求间隔须为有限数值且 >=0 秒")
        age = config.max_result_age_seconds
        if (
            age is None
            or isinstance(age, bool)
            or not isinstance(age, (int, float))
            or not math.isfinite(float(age))
            or float(age) <= 0
        ):
            problems.append("最大结果年龄须为有限数值且 >0 秒")
        return tuple(problems)

    @staticmethod
    def _real_model_is_onnx(result: ProcessingResult) -> bool:
        model = result.model
        return bool(
            not model.is_test_double
            and isinstance(model.model_id, str)
            and model.model_id.startswith("onnx-sleep-staging:")
            and isinstance(model.version, str)
            and model.version.startswith("sha256:")
            and bool(model.version.removeprefix("sha256:"))
        )

    def _real_status_locked(self) -> dict[str, object]:
        return {
            "mode": self._control_mode,
            "enabled": self._real_auto_enabled,
            "expected_state": self._real_expected_state,
            "desired_state": self._real_desired_state,
            "confirmed_state": self._real_confirmed_state,
            "confirmed_utc": self._real_confirmed_utc,
            "runtime_state": self._real_runtime_state,
            "last_request": dict(self._real_last_request)
            if self._real_last_request is not None
            else None,
            "last_outcome": dict(self._real_last_outcome)
            if self._real_last_outcome is not None
            else None,
            "independent_stop_required": self._real_independent_stop_required,
            "endpoint": f"{self._real_endpoint.host}:{self._real_endpoint.port}",
            "baseline_ready": self._real_baseline_ready,
            "handshake_ready": self._real_handshake_ready,
            "model_configured": self._real_model_configured,
        }

    def _emit_real_status(self) -> None:
        with self._lock:
            status = self._real_status_locked()
        self.rally_status_changed.emit(status)

    def _set_real_automatic_enabled(self, enabled: bool, *, reason: str = "") -> bool:
        if not enabled:
            self._disable_real_control(reason or "真实 Rally 自动控制已关闭")
            return True
        with self._lock:
            problems: list[str] = []
            if self._shutting_down:
                problems.append("应用正在退出")
            if self._session_id is None or self._real_session_id != self._session_id:
                problems.append("必须先建立当前实时会话")
            if not self._real_handshake_ready:
                problems.append("尚未完成当前 Curry 握手")
            if not self._real_model_configured:
                problems.append("真实 Rally 控制必须显式选择 ONNX 模型")
            problems.extend(self._real_config_issues_locked())
            if self._real_auto_enabled:
                return True
            pipeline = self._pipeline
            if problems:
                detail = "真实 Rally 自动控制未启用：" + "；".join(problems)
            else:
                self._real_auto_enabled = True
                self._real_runtime_state = "STOPPING"
                self._real_expected_state = "STOPPED"
                self._real_desired_state = "STOPPED"
                self._real_confirmed_state = None
                self._real_confirmed_utc = None
                self._real_baseline_ready = False
                self._real_stop_after_current = False
                self._real_independent_stop_required = False
                self._real_close_requested = False
                request = self._new_real_request_locked(
                    RALLY_STOP_COMMAND,
                    "显式启用真实控制；先建立 Stop Stim 安全基线",
                    block_id=None,
                    stage=None,
                    model=None,
                )
                self._real_inflight = request
                detail = "真实 Rally 自动控制已启用；正在发送 Stop Stim 基线，等待精确确认"
        if problems:
            self.automatic_status_changed.emit(False, detail)
            self._emit_real_status()
            return False
        self.automatic_status_changed.emit(True, detail)
        self._remember_control_event(self._build_real_config_event())
        self._submit_real_request(request, pipeline)
        self._emit_real_status()
        return True

    def _disable_real_control(
        self,
        reason: str,
        *,
        close_after: bool = False,
    ) -> None:
        with self._lock:
            self._real_auto_enabled = False
            self._real_close_requested = self._real_close_requested or close_after
            self._real_expected_state = "STOPPED"
            self._real_desired_state = "STOPPED"
            current = self._real_inflight
            pipeline = self._pipeline
            confirmed = self._real_confirmed_state
            if current is not None and current.command == RALLY_START_COMMAND:
                self._real_stop_after_current = True
                self._real_transport.cancel_unsent()
                request = None
            elif current is not None:
                request = None
            elif confirmed == "STOPPED":
                self._real_runtime_state = "STOPPED"
                request = None
            elif self._real_runtime_state == "FAULT/UNKNOWN":
                request = None
            elif self._real_session_id is None and self._session_id is None:
                request = None
            else:
                self._real_runtime_state = "STOPPING"
                request = self._new_real_request_locked(
                    RALLY_STOP_COMMAND,
                    reason,
                    block_id=None,
                    stage=None,
                    model=None,
                )
                self._real_inflight = request
        self.automatic_status_changed.emit(False, reason)
        if request is not None:
            self._submit_real_request(request, pipeline, required_stop=True)
        self._emit_real_status()
        self._refresh_activity()

    def _new_real_request_locked(
        self,
        command: str,
        reason: str,
        *,
        block_id: int | None,
        stage: str | None,
        model: dict[str, object] | None,
    ) -> RallyControlRequest:
        session_id = self._real_session_id or self._session_id
        if session_id is None:
            raise RuntimeError("没有可供 Rally 控制使用的实时会话")
        targets = tuple(
            stage_name
            for stage_name in STAGES
            if stage_name in self._config.target_stages
        )
        return RallyControlRequest(
            request_id=uuid.uuid4().hex,
            session_id=session_id,
            generation=self._real_generation,
            command=command,
            reason=reason,
            block_id=block_id,
            stage=stage,
            model=dict(model) if isinstance(model, dict) else None,
            target_stages=targets,
            desired_state="RUNNING"
            if command == RALLY_START_COMMAND
            else "STOPPED",
            confirmed_state=self._real_confirmed_state,
            runtime_state=self._real_runtime_state,
            created_utc=utc_now_iso(),
            created_monotonic_ns=self._monotonic_ns(),
        )

    def _submit_real_request(
        self,
        request: RallyControlRequest,
        pipeline,
        *,
        lease=None,
        required_stop: bool = False,
    ) -> None:
        """Submit one command while transferring, or acquiring, its lease.

        A control lease is deliberately transferred from an outcome to its
        follow-up/compensation request.  It is not released in the small gap
        between the final outcome event and the next Stop, so the writer
        cannot close underneath the control lifecycle.
        """
        held_lease = lease
        if held_lease is None and pipeline is not None:
            if required_stop:
                acquire = getattr(pipeline, "acquire_control_work", None)
                acquired = acquire() if acquire is not None else pipeline.acquire_external_work()
                if not acquired:
                    self.diagnostic.emit(
                        "会话记录已关闭或失败；必要 Stop 不再等待 writer，继续尽力发送，"
                        "该控制事件可能无法持久化。"
                    )
                else:
                    held_lease = pipeline
            else:
                if not pipeline.acquire_external_work():
                    with self._lock:
                        if self._real_inflight is request:
                            self._real_inflight = None
                    self._on_real_not_sent(
                        request,
                        "会话已开始收尾，Rally 请求未发送",
                        RequestStatus.NOT_SENT,
                        pipeline_override=pipeline,
                    )
                    return
                held_lease = pipeline
        if held_lease is not None:
            with self._lock:
                self._real_leases[request.request_id] = held_lease
        submitted = self._real_transport.submit(request)
        if not submitted:
            self._on_real_not_sent(
                request,
                "真实 Rally 传输忙碌、正在退出或隔离资源已达上限；请求未发送",
                RequestStatus.NOT_SENT,
                pipeline_override=(pipeline if held_lease is None else None),
            )
            return
        with self._lock:
            self._real_last_request = {
                "request_id": request.request_id,
                "command": request.command,
                "status": RequestStatus.WAITING_RESPONSE.value,
                "block_id": request.block_id,
                "model": request.model,
                "created_utc": request.created_utc,
            }
        self.request_changed.emit(
            {
                "mode": "real",
                "request_id": request.request_id,
                "command": request.command,
                "status": RequestStatus.WAITING_RESPONSE.value,
                "message": "Rally 命令已排队；等待精确设备回复",
                "block_id": request.block_id,
                "model": request.model,
            }
        )
        self._refresh_activity()

    @staticmethod
    def _release_real_lease(pipeline) -> None:
        if pipeline is not None:
            pipeline.release_external_work()

    def _real_result_eligibility_reason_locked(
        self,
        context: BlockContext,
        result: ProcessingResult,
        *,
        now_ns: int | None = None,
    ) -> str:
        """Revalidate one result at every real-control decision boundary."""
        if not self._real_model_configured:
            return "当前实时会话没有显式 ONNX 配置"
        if result.status is not ProcessingStatus.SUCCESS:
            return f"当前模型结果不可用：{result.reason or result.status.value}"
        if not self._real_model_is_onnx(result):
            return "当前分期结果不是已确认的 ONNX 模型结果"
        if result.stage not in STAGES:
            return "当前 ONNX 结果缺少合法睡眠期"
        if context.session_id != self._real_session_id or result.session_id != self._real_session_id:
            return "当前结果不属于实时 Rally 会话"
        if context.block_id != result.block_id:
            return "当前 EEG 块与 ONNX 结果 block_id 不一致"
        if (
            not isinstance(context.received_monotonic_ns, int)
            or isinstance(context.received_monotonic_ns, bool)
            or context.received_monotonic_ns <= 0
            or not isinstance(result.started_monotonic_ns, int)
            or isinstance(result.started_monotonic_ns, bool)
            or result.started_monotonic_ns <= 0
            or not isinstance(result.finished_monotonic_ns, int)
            or isinstance(result.finished_monotonic_ns, bool)
            or result.finished_monotonic_ns <= 0
        ):
            return "当前 EEG 块缺少有效接收单调时钟时间"
        configured_age = self._config.max_result_age_seconds
        if (
            configured_age is None
            or isinstance(configured_age, bool)
            or not isinstance(configured_age, (int, float))
            or not math.isfinite(float(configured_age))
            or float(configured_age) <= 0
        ):
            return "当前 ONNX 结果已过期或年龄阈值无效"
        current_ns = self._monotonic_ns() if now_ns is None else now_ns
        age = (current_ns - context.received_monotonic_ns) / 1_000_000_000
        if not math.isfinite(age) or age < 0 or age > float(configured_age):
            return "当前 ONNX 结果已过期或年龄阈值无效"
        return ""

    def _real_cached_result_is_eligible_locked(self, *, now_ns: int) -> bool:
        latest = self._real_latest_valid_result_locked()
        if latest is None or not self._real_baseline_ready:
            return False
        result, context = latest
        if result.block_id != self._real_last_valid_block_id:
            return False
        return not self._real_result_eligibility_reason_locked(
            context, result, now_ns=now_ns
        )

    def tick(self, now_monotonic_ns: int | None = None) -> bool:
        """Run the explicit real-control liveness/age check.

        The controller's existing GUI progress timer calls this method.  It
        deliberately does not invent a new timeout: the configured maximum
        result age remains the only age threshold.  A caller can inject a
        clock and call ``tick`` directly for deterministic tests.
        """
        now_ns = self._monotonic_ns() if now_monotonic_ns is None else now_monotonic_ns
        with self._lock:
            if (
                not self._real_auto_enabled
                or self._control_mode != "real"
                or self._real_latest_valid is None
            ):
                return False
            if self._real_cached_result_is_eligible_locked(now_ns=now_ns):
                return False
        self._handle_real_fault("自最近合格 ONNX 结果后没有新数据，结果年龄已超过配置阈值")
        return True

    def _before_real_send(
        self, request: RallyControlRequest, _worker_now_ns: int
    ) -> tuple[bool, str]:
        """Final worker-thread admission check immediately before ``sendto``."""
        if request.command != RALLY_START_COMMAND:
            return True, ""
        with self._lock:
            if (
                not self._real_auto_enabled
                or self._control_mode != "real"
                or not self._real_baseline_ready
                or self._real_close_requested
                or self._real_shutdown_requested
                or self._real_inflight is not request
                or request.session_id != self._real_session_id
                or request.generation != self._real_generation
            ):
                return False, "发送前复核：真实 Rally Start 已失去当前会话资格"
            latest = self._real_latest_valid_result_locked()
            if latest is None:
                return False, "发送前复核：没有可用的当前 ONNX 结果"
            result, _context = latest
            if result.block_id != request.block_id:
                return False, "发送前复核：Start 对应的 ONNX 结果已被更新"
            if self._real_stop_after_current or self._real_desired_state != "RUNNING":
                return False, "发送前复核：Stop 优先，Start 未发送"
            if not self._real_cached_result_is_eligible_locked(
                now_ns=self._monotonic_ns()
            ):
                return False, "发送前复核：当前 ONNX 结果已过期"
        return True, ""

    def _process_real_result(
        self,
        context: BlockContext,
        result: ProcessingResult,
        *,
        generation: int | None = None,
    ) -> tuple[dict[str, object], ...]:
        with self._lock:
            if (
                not self._real_auto_enabled
                or self._control_mode != "real"
                or context.session_id != self._real_session_id
                or result.session_id != self._real_session_id
                or (
                    generation is not None
                    and int(generation) != self._real_generation
                )
            ):
                return ()
            if result.block_id <= self._real_seen_block_id:
                return ()
            gap = (
                self._real_seen_block_id > 0
                and result.block_id != self._real_seen_block_id + 1
            )
            self._real_seen_block_id = result.block_id
            if gap:
                reason = f"当前实时块序出现缺口：上一块 {result.block_id - 2} 后收到 {result.block_id}"
            else:
                reason = self._real_result_eligibility_reason_locked(
                    context, result, now_ns=self._monotonic_ns()
                )
            if reason:
                # An active real session's model/gap/age fault is not a
                # sleep-stage decision. It invokes one bounded stop path.
                self._real_runtime_state = "FAULT/UNKNOWN"
                self._real_auto_enabled = False
                self._real_expected_state = "STOPPED"
                self._real_desired_state = "STOPPED"
            else:
                stage = result.stage
                assert stage is not None
                self._real_last_valid_block_id = result.block_id
                self._real_latest_valid = (result, context)
                model = result.model.to_dict()
                desired = "RUNNING" if stage in self._config.target_stages else "STOPPED"
                self._real_desired_state = desired
                request = self._plan_real_request_locked(
                    desired,
                    reason=(
                        "当前目标睡眠期，申请 Start Stim"
                        if desired == "RUNNING"
                        else "当前非目标睡眠期，申请 Stop Stim"
                    ),
                    block_id=result.block_id,
                    stage=stage,
                    model=model,
                )
                decision_event = self._build_real_decision_event_locked(
                    result,
                    request,
                    reason=(
                        request.reason
                        if request is not None
                        else (
                            "目标状态与最近确认一致，控制 no-op"
                            if desired == self._real_confirmed_state
                            else "已有 Rally 请求在途，保留最新期望状态"
                        )
                    ),
                )
                submit_pipeline = self._pipeline
                recording_enabled = self._recording_enabled
        if reason:
            self._handle_real_fault(reason)
            return ()
        # When recording is enabled, put the decision into the same bounded
        # writer queue before submitting the UDP command. This preserves the
        # config -> decision -> sent -> outcome order even when Rally replies
        # immediately on the worker thread. With recording disabled, the
        # caller keeps the existing returned-event handoff semantics.
        self._remember_control_event(decision_event)
        if request is not None:
            self._submit_real_request(request, submit_pipeline)
        return (
            ()
            if submit_pipeline is not None and recording_enabled
            else (decision_event,)
        )

    def _plan_real_request_locked(
        self,
        desired: str,
        *,
        reason: str,
        block_id: int | None,
        stage: str | None,
        model: dict[str, object] | None,
    ) -> RallyControlRequest | None:
        current = self._real_inflight
        if current is not None:
            if desired == "STOPPED" and current.command == RALLY_START_COMMAND:
                self._real_stop_after_current = True
                self._real_transport.cancel_unsent()
            return None
        if desired == self._real_confirmed_state:
            self._real_runtime_state = desired
            return None
        command = (
            RALLY_START_COMMAND if desired == "RUNNING" else RALLY_STOP_COMMAND
        )
        request = self._new_real_request_locked(
            command,
            reason,
            block_id=block_id,
            stage=stage,
            model=model,
        )
        self._real_inflight = request
        self._real_expected_state = desired
        self._real_runtime_state = "STARTING" if desired == "RUNNING" else "STOPPING"
        return request

    def _handle_real_fault(self, reason: str) -> None:
        with self._lock:
            current = self._real_inflight
            pipeline = self._pipeline
            if current is not None and current.command == RALLY_START_COMMAND:
                self._real_stop_after_current = True
                self._real_transport.cancel_unsent()
                request = None
            elif current is not None:
                request = None
            elif self._real_confirmed_state == "STOPPED":
                request = None
            else:
                request = self._new_real_request_locked(
                    RALLY_STOP_COMMAND,
                    f"保护性停止：{reason}",
                    block_id=None,
                    stage=None,
                    model=None,
                )
                self._real_inflight = request
                self._real_runtime_state = "STOPPING"
            self._real_auto_enabled = False
            self._real_expected_state = "STOPPED"
            self._real_desired_state = "STOPPED"
        self.automatic_status_changed.emit(False, f"真实控制故障：{reason}")
        if request is not None:
            self._submit_real_request(request, pipeline, required_stop=True)
        self._emit_real_status()

    def _build_real_config_event(self) -> dict[str, object]:
        with self._lock:
            return {
                "event_type": "rally_control",
                "session_id": self._real_session_id or self._session_id,
                "block_id": None,
                "payload": {
                    "schema_version": 1,
                    "phase": "config",
                    "mode": "real",
                    "command": None,
                    "reason": "用户显式确认真实 Rally 模式；协议由操作者在 Rally 中加载",
                    "stage": None,
                    "model": None,
                    "target_stages": [
                        stage for stage in STAGES if stage in self._config.target_stages
                    ],
                    "desired_state": self._real_desired_state,
                    "confirmed_state": self._real_confirmed_state,
                    "runtime_state": self._real_runtime_state,
                    "created_utc": utc_now_iso(),
                    "created_monotonic_ns": self._monotonic_ns(),
                    "outcome": "configured",
                },
            }

    def _build_real_decision_event_locked(
        self,
        result: ProcessingResult,
        request: RallyControlRequest | None,
        *,
        reason: str,
    ) -> dict[str, object]:
        desired = "RUNNING" if result.stage in self._config.target_stages else "STOPPED"
        return {
            "event_type": "rally_control",
            "session_id": result.session_id,
            "block_id": result.block_id,
            "request_id": request.request_id if request is not None else None,
            "payload": {
                "schema_version": 1,
                "phase": "decision",
                "mode": "real",
                "command": request.command if request is not None else None,
                "reason": reason,
                "stage": result.stage,
                "model": result.model.to_dict(),
                "target_stages": [
                    stage for stage in STAGES if stage in self._config.target_stages
                ],
                "desired_state": desired,
                "confirmed_state": self._real_confirmed_state,
                "runtime_state": self._real_runtime_state,
                "created_utc": utc_now_iso(),
                "created_monotonic_ns": self._monotonic_ns(),
                "result_finished_monotonic_ns": result.finished_monotonic_ns,
                "outcome": "planned" if request is not None else "no_op",
            },
        }

    def _build_real_transport_event(
        self,
        request: RallyControlRequest,
        phase: str,
        *,
        outcome: RallyControlOutcome | None = None,
        status: str | None = None,
    ) -> dict[str, object]:
        with self._lock:
            confirmed = self._real_confirmed_state
            runtime = self._real_runtime_state
        response = None
        if outcome is not None:
            response = {
                "text": outcome.raw_text,
                "bytes_b64": outcome.raw_bytes_b64(),
                "source": (
                    {
                        "host": outcome.response_source_host,
                        "port": outcome.response_source_port,
                    }
                    if outcome.response_source_host is not None
                    else None
                ),
            }
        payload: dict[str, object] = {
            "schema_version": 1,
            "phase": phase,
            "mode": "real",
            "command": request.command,
            "reason": request.reason,
            "stage": request.stage,
            "model": request.model,
            "target_stages": list(request.target_stages),
            "desired_state": request.desired_state,
            "confirmed_state": confirmed,
            "runtime_state": runtime,
            "created_utc": request.created_utc,
            "created_monotonic_ns": request.created_monotonic_ns,
            "sent_monotonic_ns": (
                outcome.sent_monotonic_ns if outcome is not None else None
            ),
            "received_monotonic_ns": (
                outcome.received_monotonic_ns if outcome is not None else None
            ),
            "response": response,
            "outcome": status or (outcome.status.value if outcome else "sent"),
        }
        return {
            "event_type": "rally_control",
            "session_id": request.session_id,
            "block_id": request.block_id,
            "request_id": request.request_id,
            "payload": payload,
        }

    def _remember_control_event(self, event: dict[str, object]) -> None:
        with self._lock:
            self._real_control_history.append(dict(event))
            pipeline = self._pipeline
            recording = self._recording_enabled
        if pipeline is not None and recording and event.get("session_id") == pipeline.session_id:
            if not pipeline.enqueue_session_event(event):
                self.diagnostic.emit("Rally 控制事件记录失败；仍继续执行必要的停止收尾")

    def _on_real_sent(self, request: RallyControlRequest, sent_ns: int) -> None:
        with self._lock:
            if self._real_inflight is request:
                self._real_runtime_state = (
                    "STARTING" if request.command == RALLY_START_COMMAND else "STOPPING"
                )
            self._real_last_request = {
                "request_id": request.request_id,
                "command": request.command,
                "status": RequestStatus.WAITING_RESPONSE.value,
                "sent_monotonic_ns": sent_ns,
                "block_id": request.block_id,
                "model": request.model,
            }
        event = self._build_real_transport_event(request, "sent")
        self._remember_control_event(event)
        self.request_changed.emit(
            {
                "mode": "real",
                "request_id": request.request_id,
                "command": request.command,
                "status": RequestStatus.WAITING_RESPONSE.value,
                "message": "UDP 命令已发送；尚未得到 Rally 状态确认",
                "block_id": request.block_id,
                "sent_monotonic_ns": sent_ns,
            }
        )
        self._emit_real_status()

    def _on_real_not_sent(
        self,
        request: RallyControlRequest,
        reason: str,
        status: RequestStatus,
        *,
        pipeline_override=None,
    ) -> None:
        with self._lock:
            self._real_inflight = None
            lease = self._real_leases.pop(request.request_id, None)
            pipeline = lease if lease is not None else pipeline_override
            if pipeline is None:
                pipeline = self._pipeline
            close_after = self._real_close_requested
            expected_stop = request.command == RALLY_STOP_COMMAND
            cancelled_start_for_stop = (
                request.command == RALLY_START_COMMAND and self._real_stop_after_current
            )
            if cancelled_start_for_stop:
                self._real_stop_after_current = False
                self._real_runtime_state = "STOPPING"
                next_request = self._new_real_request_locked(
                    RALLY_STOP_COMMAND,
                    "停止优先：未发送的 Start Stim 已取消，执行 Stop Stim",
                    block_id=None,
                    stage=None,
                    model=None,
                )
                self._real_inflight = next_request
            else:
                next_request = None
        if expected_stop and not cancelled_start_for_stop:
            failure_request, shutdown_after = self._finish_real_failure(
                request, status, reason
            )
            next_request = failure_request
        elif request.command == RALLY_START_COMMAND and not cancelled_start_for_stop:
            failure_request, shutdown_after = self._finish_real_failure(
                request, status, reason
            )
            next_request = failure_request
        else:
            shutdown_after = False
        event = self._build_real_transport_event(
            request, "outcome", status=status.value
        )
        event["payload"]["reason"] = reason
        self._remember_control_event(event)
        if next_request is not None:
            self._submit_real_request(
                next_request,
                pipeline,
                lease=lease,
                required_stop=next_request.command == RALLY_STOP_COMMAND,
            )
        else:
            self._release_real_lease(lease)
        if next_request is None and (close_after or shutdown_after):
            self._real_transport.shutdown()
        self.request_changed.emit(
            {
                "mode": "real",
                "request_id": request.request_id,
                "command": request.command,
                "status": status.value,
                "message": reason,
                "block_id": request.block_id,
            }
        )
        self._emit_real_status()
        self._refresh_activity()

    def _on_real_outcome(
        self, request: RallyControlRequest, outcome: RallyControlOutcome
    ) -> None:
        with self._lock:
            self._real_inflight = None
            lease = self._real_leases.pop(request.request_id, None)
            pipeline = lease if lease is not None else self._pipeline
            current_request = (
                request.generation == self._real_generation
                and request.session_id == self._real_session_id
            )
        if not current_request:
            self._remember_control_event(
                self._build_real_transport_event(request, "outcome", outcome=outcome)
            )
            self._release_real_lease(lease)
            return
        if outcome.status is RequestStatus.API_SUCCESS:
            next_request, shutdown_after = self._finish_real_success(request, outcome)
        else:
            next_request, shutdown_after = self._finish_real_failure(
                request, outcome.status, outcome.message, outcome=outcome
            )
        event = self._build_real_transport_event(request, "outcome", outcome=outcome)
        self._remember_control_event(event)
        if next_request is not None:
            self._submit_real_request(
                next_request,
                pipeline,
                lease=lease,
                required_stop=next_request.command == RALLY_STOP_COMMAND,
            )
        else:
            self._release_real_lease(lease)
            if shutdown_after:
                self._real_transport.shutdown()
        self.request_changed.emit(
            {
                "mode": "real",
                "request_id": request.request_id,
                "command": request.command,
                "status": outcome.status.value,
                "message": outcome.message,
                "response_code": outcome.raw_text,
                "response_source": (
                    f"{outcome.response_source_host}:{outcome.response_source_port}"
                    if outcome.response_source_host
                    else None
                ),
                "block_id": request.block_id,
                "received_monotonic_ns": outcome.received_monotonic_ns,
            }
        )
        self._emit_real_status()
        self._refresh_activity()

    def _finish_real_success(
        self, request: RallyControlRequest, outcome: RallyControlOutcome
    ) -> tuple[RallyControlRequest | None, bool]:
        auto_detail: str | None = None
        disabled_detail: str | None = None
        with self._lock:
            self._real_confirmed_state = (
                "RUNNING" if request.command == RALLY_START_COMMAND else "STOPPED"
            )
            self._real_confirmed_utc = utc_now_iso()
            self._real_last_outcome = {
                "request_id": request.request_id,
                "command": request.command,
                "status": outcome.status.value,
                "message": outcome.message,
                "raw_text": outcome.raw_text,
                "response_source": (
                    f"{outcome.response_source_host}:{outcome.response_source_port}"
                    if outcome.response_source_host
                    else None
                ),
                "received_monotonic_ns": outcome.received_monotonic_ns,
            }
            if request.command == RALLY_STOP_COMMAND:
                self._real_runtime_state = "STOPPED"
                is_baseline = "安全基线" in request.reason
                if is_baseline:
                    self._real_baseline_ready = True
                    self._real_seen_block_id = max(
                        self._real_seen_block_id, self._real_last_valid_block_id
                    )
                    self._real_last_valid_block_id = self._real_seen_block_id
                    if self._real_auto_enabled:
                        auto_detail = (
                            "真实 Rally 自动控制已启用；Stop Stim 基线已精确确认，"
                            "等待下一条合格当前 ONNX 结果"
                        )
                self._real_stop_after_current = False
                follow = (
                    self._real_auto_enabled
                    and not self._real_close_requested
                    and self._real_desired_state == "RUNNING"
                    and not is_baseline
                )
                if follow:
                    now_ns = self._monotonic_ns()
                    if self._real_cached_result_is_eligible_locked(now_ns=now_ns):
                        latest = self._real_latest_valid_result_locked()
                        assert latest is not None
                        result, _context = latest
                        follow_request = self._plan_real_request_locked(
                            "RUNNING",
                            reason="最新合格 ONNX 结果仍为目标期，Stop 完成后申请 Start Stim",
                            block_id=result.block_id,
                            stage=result.stage,
                            model=result.model.to_dict(),
                        )
                    else:
                        self._real_auto_enabled = False
                        self._real_expected_state = "STOPPED"
                        self._real_desired_state = "STOPPED"
                        self._real_runtime_state = "STOPPED"
                        disabled_detail = (
                            "Stop Stim 已确认，但等待期间最近 ONNX 结果已过期；"
                            "真实自动控制已关闭，等待下一次显式启用"
                        )
                        follow_request = None
                else:
                    follow_request = None
            else:
                self._real_runtime_state = "RUNNING"
                follow_request = None
                if (
                    not self._real_auto_enabled
                    or self._real_stop_after_current
                    or self._real_close_requested
                    or self._real_desired_state == "STOPPED"
                ):
                    self._real_runtime_state = "STOPPING"
                    follow_request = self._plan_real_request_locked(
                        "STOPPED",
                        reason=(
                            "Start Stim 后收到停止优先请求，执行一次 Stop Stim"
                        ),
                        block_id=None,
                        stage=None,
                        model=None,
                    )
            shutdown_after = self._real_shutdown_requested
        if auto_detail is not None:
            self.automatic_status_changed.emit(True, auto_detail)
        if disabled_detail is not None:
            self.automatic_status_changed.emit(False, disabled_detail)
        return follow_request, follow_request is None and shutdown_after

    def _finish_real_failure(
        self,
        request: RallyControlRequest,
        status: RequestStatus,
        reason: str,
        *,
        outcome: RallyControlOutcome | None = None,
    ) -> tuple[RallyControlRequest | None, bool]:
        with self._lock:
            self._real_last_outcome = {
                "request_id": request.request_id,
                "command": request.command,
                "status": status.value,
                "message": reason,
                "raw_text": outcome.raw_text if outcome is not None else None,
                "response_source": (
                    f"{outcome.response_source_host}:{outcome.response_source_port}"
                    if outcome is not None and outcome.response_source_host
                    else None
                ),
                "received_monotonic_ns": (
                    outcome.received_monotonic_ns if outcome is not None else None
                ),
            }
            if request.command == RALLY_START_COMMAND:
                self._real_auto_enabled = False
                self._real_baseline_ready = False
                self._real_expected_state = "STOPPED"
                self._real_desired_state = "STOPPED"
                self._real_runtime_state = "FAULT/UNKNOWN"
                # A failed/unknown Start gets exactly one bounded Stop
                # compensation, even when the prior confirmed state was Stop.
                compensation = self._new_real_request_locked(
                    RALLY_STOP_COMMAND,
                    "Start Stim 未获明确成功，执行一次补偿 Stop Stim",
                    block_id=None,
                    stage=None,
                    model=None,
                )
                self._real_inflight = compensation
            else:
                self._real_auto_enabled = False
                self._real_expected_state = "STOPPED"
                self._real_desired_state = "STOPPED"
                self._real_runtime_state = "FAULT/UNKNOWN"
                self._real_independent_stop_required = True
                compensation = None
            shutdown_after = self._real_shutdown_requested
        self.automatic_status_changed.emit(
            False,
            f"真实 Rally {request.command} 未获明确成功：{reason}；不自动重试",
        )
        return compensation, shutdown_after

    def _real_latest_valid_result_locked(self):
        latest = getattr(self, "_real_latest_valid", None)
        return latest

    def _on_real_transport_stopped(self) -> None:
        with self._lock:
            self._real_transport_stopped = True
        self._refresh_activity()
        if self._stopped_signal_sent:
            self.shutdown_completed.emit()

    def begin_session(
        self,
        session_id: str,
        pipeline,
        recording_enabled: bool,
        *,
        generation: int | None = None,
        model_configured: bool = False,
    ) -> None:
        with self._lock:
            self._session_id = session_id
            self._pipeline = pipeline
            self._recording_enabled = recording_enabled
            self._logged_config_versions.clear()
            self._engine.begin_session(session_id)
            self._real_session_id = session_id
            self._real_generation = (
                int(generation) if generation is not None else self._real_generation + 1
            )
            self._real_handshake_ready = False
            self._real_model_configured = bool(model_configured)
            self._real_seen_block_id = 0
            self._real_last_valid_block_id = 0
            self._real_latest_valid = None
            self._real_baseline_ready = False
            self._real_stop_after_current = False
            self._real_close_requested = False
            self._real_independent_stop_required = False
            self._real_confirmed_state = None
            self._real_confirmed_utc = None
            self._real_last_request = None
            self._real_last_outcome = None
            self._real_runtime_state = "DISARMED/UNKNOWN"
            self._real_expected_state = None
            self._real_desired_state = None
            self._real_inflight = None
        self.diagnostic.emit(f"P3/P4-B 决策周期已绑定实时会话 {session_id}；回放不会进入策略。")
        self._emit_real_status()

    def set_session_ready(self, ready: bool = True) -> None:
        with self._lock:
            self._real_handshake_ready = bool(ready)
        self._emit_real_status()

    def finish_session(self, session_id: str) -> None:
        with self._lock:
            if session_id != self._session_id:
                return
            self._automatic_enabled = False
            self._session_id = None
            self._pipeline = None
            self._recording_enabled = False
            self._real_session_id = None
            self._real_handshake_ready = False
            self._real_model_configured = False
            self._real_baseline_ready = False
            self._real_auto_enabled = False
            self._real_expected_state = "STOPPED"
            self._real_desired_state = "STOPPED"
            self._real_runtime_state = "DISARMED/UNKNOWN"
        self.automatic_status_changed.emit(False, "实时会话已结束；自动决策已关闭")
        self._refresh_activity()
        self._emit_real_status()

    def on_session_stopping(self, reason: str = "实时会话正在收尾") -> None:
        """Register the session's control shutdown exactly once.

        This method is safe to call from the Curry worker's ``finally``
        before the pipeline network handoff and again from the queued Qt
        lifecycle callback.  The runtime lock and the single in-flight
        request state make the second call a no-op for an already registered
        Stop or an already confirmed STOPPED session.
        """
        if self.control_mode == "real":
            self._disable_real_control(reason, close_after=False)
        else:
            self.set_automatic_enabled(False, reason=reason)

    def set_automatic_enabled(self, enabled: bool, *, reason: str = "") -> bool:
        if self.control_mode == "real":
            return self._set_real_automatic_enabled(enabled, reason=reason)
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
        self,
        context: BlockContext,
        result: ProcessingResult,
        *,
        generation: int | None = None,
    ) -> tuple[tuple[dict[str, object], ...], DecisionEvaluation]:
        with self._lock:
            real_mode = self._control_mode == "real"
            evaluation = self._engine.evaluate(
                result,
                received_monotonic_ns=context.received_monotonic_ns,
                automatic_enabled=(
                    self._automatic_enabled if not real_mode else False
                ),
                simulator_ready=(
                    self._endpoint is not None and self._owns_endpoint(self._endpoint)
                ),
                request_busy=self._transport.busy,
            )
            if real_mode:
                events: tuple[dict[str, object], ...] = ()
            else:
                version = evaluation.decision.config_version
                if version in self._logged_config_versions:
                    events = evaluation.events[1:]
                else:
                    events = evaluation.events
                    self._logged_config_versions.add(version)
        if real_mode:
            events = self._process_real_result(
                context, result, generation=generation
            )
        self.decision_changed.emit(evaluation.decision)
        self.diagnostic.emit(
            f"block_id={result.block_id}：{evaluation.decision.reason}"
        )
        return tuple(events), evaluation

    def decision_recorded(self, evaluation: object) -> None:
        if self.control_mode == "real":
            return
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
            self._real_shutdown_requested = True
        # This call may leave a sent START waiting for its finite outcome and
        # then schedule exactly one protective STOP.  Do not tear down the
        # real worker before that lifecycle has resolved.
        self._disable_real_control("应用退出；真实控制已关闭", close_after=True)
        if self.control_mode != "real":
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
        with self._lock:
            real_inflight = self._real_inflight
        if real_inflight is None:
            self._real_transport.shutdown()
        self.simulator_status_changed.emit(False, "模拟端已关闭")
        self._refresh_activity()

    def join(self, timeout: float | None = None) -> bool:
        started = time.monotonic()
        p3_done = self._transport.join(timeout)
        if timeout is None:
            real_timeout = None
        else:
            real_timeout = max(0.0, float(timeout) - (time.monotonic() - started))
        real_done = self._real_transport.join(real_timeout)
        return p3_done and real_done

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
            real_stopped = self._real_transport_stopped
        if real_stopped:
            self.shutdown_completed.emit()
        self._refresh_activity()

    def _refresh_activity(self, *, force: bool = False) -> None:
        with self._lock:
            active = (
                self._shutting_down and not self._stopped_signal_sent
            ) or (
                self._simulator is not None and self._simulator.active
            ) or self._transport.busy or self._real_transport.busy or (
                self._real_shutdown_requested and self._real_transport.active
            )
            changed = force or active != self._activity
            self._activity = active
        if changed:
            self.activity_changed.emit(active)
