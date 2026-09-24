"""Curry session lifecycle and P2 bounded processing/persistence handoff."""
from __future__ import annotations

import ipaddress
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QTimer, QObject, Signal, Slot

from curry_netstream.client import CurryClient, CurryClientCancelled, StreamResult
from curry_netstream.models import DataBlock, SessionInfo

from .epoching import EpochAssemblySnapshot, ThirtySecondEpochAssembler
from .latest import LatestBufferStats, LatestItemBuffer
from .staging import (
    DEFAULT_PENDING_BLOCK_LIMIT,
    BlockContext,
    ModelAdapter,
    NoModelAdapter,
    PipelineOutcome,
    ProcessingPipeline,
    ProcessingResult,
)
from .stimulation_runtime import StimulationRuntime
from .validation import validate_data_block


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    STREAMING = "streaming"
    STOPPING = "stopping"
    ERROR = "error"


class ProcessingBacklogError(RuntimeError):
    """Validated input could not be accepted by the bounded P2 queue."""


@dataclass(frozen=True, slots=True)
class QueuedBlock:
    """One accepted block waiting for the GUI's bounded display handoff."""

    number: int
    block: DataBlock
    generation: int = 0
    historical: bool = False
    history_reason: str | None = None


class CurrySessionController(QObject):
    """Own one Curry worker and one sequential processing/session writer."""

    state_changed = Signal(str, str)
    busy_changed = Signal(bool)
    error_changed = Signal(str)
    session_changed = Signal(object)
    session_started = Signal()
    session_finished = Signal(bool, object)
    diagnostics_changed = Signal(str)
    assembly_progress_changed = Signal(object)
    processing_changed = Signal(object)
    recording_changed = Signal(str)
    stage_csv_changed = Signal(str)

    _pipeline_ready_signal = Signal(int, object)
    _network_finished_signal = Signal(int, bool, object)
    _pipeline_result_signal = Signal(int, object)
    _pipeline_fatal_signal = Signal(int, object)
    _pipeline_finished_signal = Signal(int, object)
    _stage_csv_status_signal = Signal(int, str)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        model_factory: Callable[[], ModelAdapter] = NoModelAdapter,
        pending_block_limit: int = DEFAULT_PENDING_BLOCK_LIMIT,
        stimulation_runtime: StimulationRuntime | None = None,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(parent)
        self._lock = threading.RLock()
        self._state = ConnectionState.DISCONNECTED
        self._generation = 0
        self._thread: threading.Thread | None = None
        self._client: CurryClient | None = None
        self._cancel_event: threading.Event | None = None
        self._pipeline: ProcessingPipeline | None = None
        self._model_factory = model_factory
        self._pending_block_limit = pending_block_limit
        self._stimulation_runtime = stimulation_runtime
        self._wall_clock = wall_clock or (lambda: datetime.now().astimezone())
        self._session: SessionInfo | None = None
        self._assembler: ThirtySecondEpochAssembler | None = None
        self._assembly_snapshot: EpochAssemblySnapshot | None = None
        self._assembly_progress_dirty = False
        self._session_id: str | None = None
        self._session_path: str | None = None
        self._recording_enabled = False
        self._stage_csv_enabled = False
        self._stage_csv_path: str | None = None
        self._latest = LatestItemBuffer[QueuedBlock]()
        self._accepted_blocks = 0
        self._validated_blocks = 0
        self._last_error = ""
        self._latest_processing: ProcessingResult | None = None
        self._session_terminal = False
        self._terminal_cancelled = False
        self._terminal_error: BaseException | None = None
        self._network_started = False
        self._network_done = False
        self._network_cancelled = False
        self._network_error: BaseException | None = None
        self._pipeline_done = False
        self._pipeline_outcome: PipelineOutcome | None = None
        self._fatal_error: BaseException | None = None
        self._replay_active = False
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(200)
        self._progress_timer.timeout.connect(self._flush_assembly_progress)

        self._pipeline_ready_signal.connect(self._on_pipeline_ready)
        self._network_finished_signal.connect(self._on_network_finished)
        self._pipeline_result_signal.connect(self._on_processing_result)
        self._pipeline_fatal_signal.connect(self._on_pipeline_fatal)
        self._pipeline_finished_signal.connect(self._on_pipeline_finished)
        self._stage_csv_status_signal.connect(self._on_stage_csv_status)

    @property
    def state(self) -> ConnectionState:
        with self._lock:
            return self._state

    @property
    def session(self) -> SessionInfo | None:
        with self._lock:
            return self._session

    @property
    def session_id(self) -> str | None:
        with self._lock:
            return self._session_id

    @property
    def latest_processing(self) -> ProcessingResult | None:
        with self._lock:
            return self._latest_processing

    @property
    def recording_enabled(self) -> bool:
        with self._lock:
            return self._recording_enabled

    @property
    def session_path(self) -> str | None:
        with self._lock:
            return self._session_path

    @property
    def pipeline_outcome(self) -> PipelineOutcome | None:
        with self._lock:
            return self._pipeline_outcome

    @property
    def assembly_snapshot(self) -> EpochAssemblySnapshot | None:
        """Return the latest immutable network/window progress snapshot."""
        with self._lock:
            return self._assembly_snapshot

    def is_busy(self) -> bool:
        with self._lock:
            return self._state in {
                ConnectionState.CONNECTING,
                ConnectionState.STREAMING,
                ConnectionState.STOPPING,
            }

    def worker_alive(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def processing_worker_alive(self) -> bool:
        with self._lock:
            return self._pipeline is not None and self._pipeline.is_alive()

    def resources_released(self) -> bool:
        with self._lock:
            return (
                self._thread is None
                and self._client is None
                and self._cancel_event is None
                and self._pipeline is None
            )

    def set_replay_active(self, active: bool) -> bool:
        with self._lock:
            if active and self.is_busy():
                return False
            self._replay_active = active
            # The display handoff is live-session state. Never carry a
            # disconnected session's pending block across either replay
            # boundary, where it could be mislabeled as replay data.
            self._latest.clear()
        if active and self._stimulation_runtime is not None:
            self._stimulation_runtime.on_session_stopping(
                "进入离线回放；自动决策已关闭，回放不运行策略或通信"
            )
        return True

    def connect(
        self,
        host: str,
        port: int,
        *,
        recording_enabled: bool = False,
        recording_root: str | Path | None = None,
        stage_csv_enabled: bool = False,
        model_factory: Callable[[], ModelAdapter] | None = None,
    ) -> bool:
        """Prepare the processing owner, then start networking off the GUI thread."""
        with self._lock:
            if self._replay_active or self._state in {
                ConnectionState.CONNECTING,
                ConnectionState.STREAMING,
                ConnectionState.STOPPING,
            }:
                return False
        host = host.strip()
        try:
            parsed = ipaddress.ip_address(host)
            if parsed.version != 4:
                raise ValueError("只支持 IPv4 地址")
            if not 1 <= int(port) <= 65535:
                raise ValueError("端口必须在 1–65535 范围内")
            if recording_enabled and not recording_root:
                raise ValueError("启用记录前请先选择保存父目录")
            if stage_csv_enabled and not recording_enabled:
                raise ValueError("启用分期 CSV 前必须启用会话记录")
        except (TypeError, ValueError) as exc:
            self._set_error_state(str(exc))
            return False

        with self._lock:
            self._generation += 1
            generation = self._generation
            session_id = uuid.uuid4().hex
            stage_csv_path = (
                str(Path(recording_root).expanduser() / f"session_{session_id}" / "stage_labels.csv")
                if stage_csv_enabled and recording_root is not None
                else None
            )
            cancel_event = threading.Event()
            client = CurryClient(
                host,
                int(port),
                timeout=3.0,
                receive_timeout=0.25,
            )
            self._client = client
            self._cancel_event = cancel_event
            self._pipeline = None
            self._thread = None
            self._session = None
            self._assembler = None
            self._assembly_snapshot = None
            self._assembly_progress_dirty = False
            self._session_id = session_id
            self._session_path = None
            self._recording_enabled = recording_enabled
            self._stage_csv_enabled = stage_csv_enabled
            self._stage_csv_path = stage_csv_path
            self._accepted_blocks = 0
            self._validated_blocks = 0
            self._latest_processing = None
            self._latest.clear()
            self._last_error = ""
            self._session_terminal = False
            self._terminal_cancelled = False
            self._terminal_error = None
            self._network_started = False
            self._network_done = False
            self._network_cancelled = False
            self._network_error = None
            self._pipeline_done = False
            self._pipeline_outcome = None
            self._fatal_error = None
            self._state = ConnectionState.CONNECTING
            self._progress_timer.start()
            effective_model_factory = (
                model_factory if model_factory is not None else self._model_factory
            )
            model_configured = effective_model_factory is not NoModelAdapter
            pipeline = ProcessingPipeline(
                session_id=session_id,
                host=host,
                port=int(port),
                recording_enabled=recording_enabled,
                recording_root=recording_root,
                stage_csv_enabled=stage_csv_enabled,
                model_factory=effective_model_factory,
                pending_limit=self._pending_block_limit,
                on_ready=lambda error: self._pipeline_ready_signal.emit(
                    generation, error
                ),
                on_result=lambda result: self._pipeline_result_signal.emit(
                    generation, result
                ),
                on_stimulation_result=(
                    (
                        lambda context, result, current_generation=generation: (
                            self._stimulation_runtime.process_block(
                                context, result, generation=current_generation
                            )
                        )
                    )
                    if self._stimulation_runtime is not None
                    else None
                ),
                on_stimulation_decision_recorded=(
                    self._stimulation_runtime.decision_recorded
                    if self._stimulation_runtime is not None
                    else None
                ),
                on_fatal=lambda error: self._on_pipeline_fatal_from_worker(
                    generation, error
                ),
                on_finished=lambda outcome: self._pipeline_finished_signal.emit(
                    generation, outcome
                ),
                on_stage_csv_status=lambda status: self._stage_csv_status_signal.emit(
                    generation, status
                ),
            )
            self._pipeline = pipeline
            if self._stimulation_runtime is not None:
                self._stimulation_runtime.begin_session(
                    session_id,
                    pipeline,
                    recording_enabled,
                    generation=generation,
                    model_configured=model_configured,
                )

        self.busy_changed.emit(True)
        self.error_changed.emit("")
        self.session_changed.emit(None)
        self.processing_changed.emit(None)
        self.session_started.emit()
        self.state_changed.emit(
            ConnectionState.CONNECTING.value,
            "正在初始化分期处理与会话记录",
        )
        self.recording_changed.emit(
            "正在创建新会话目录" if recording_enabled else "未保存（记录已关闭）"
        )
        self.stage_csv_changed.emit(
            f"正在创建：{stage_csv_path}"
            if stage_csv_enabled and stage_csv_path is not None
            else "未启用"
        )
        self.diagnostics_changed.emit(
            "本次会话已创建。\n"
            f"分期/记录队列最多等待 {self._pending_block_limit} 个完整分析窗口，"
            "另有至多一个正在处理的窗口；界面摘要交接独立且可替换。"
        )
        pipeline.start()
        return True

    def disconnect(self) -> bool:
        """Cancel receive/model cooperatively; let accepted blocks save first."""
        with self._lock:
            if self._state not in {
                ConnectionState.CONNECTING,
                ConnectionState.STREAMING,
            }:
                return False
            event = self._cancel_event
            client = self._client
            pipeline = self._pipeline
            self._state = ConnectionState.STOPPING
            network_started = self._network_started
            if not network_started:
                self._network_done = True
                self._network_cancelled = True
        self.state_changed.emit(
            ConnectionState.STOPPING.value,
            "正在停止接收；已接纳数据将先保存，分期会协作取消",
        )
        if self._stimulation_runtime is not None:
            self._stimulation_runtime.on_session_stopping(
                "实时会话停止；不再创建新刺激请求"
            )
        if event is not None:
            event.set()
        if pipeline is not None:
            pipeline.cancel_active_prediction()
            if not network_started:
                pipeline.handoff_network_end(
                    None,
                    cancelled=True,
                    error=None,
                )
                pipeline.finish(cancelled=True, error=None)
        if client is not None:
            client.cancel()
        self._maybe_finalize(self._generation)
        return True

    def clear_error(self) -> None:
        with self._lock:
            self._last_error = ""
        self.error_changed.emit("")

    def take_latest_block(self) -> tuple[QueuedBlock, LatestBufferStats] | None:
        with self._lock:
            if self._replay_active:
                return None
            item = self._latest.take()
            if item is None:
                return None
            terminal = (
                item.generation == self._generation and self._session_terminal
            )
            if terminal and not item.historical:
                if self._terminal_cancelled:
                    reason = "用户主动断开"
                elif self._terminal_error is not None:
                    reason = f"会话错误：{self._terminal_error}"
                else:
                    reason = "会话已结束"
                item = replace(item, historical=True, history_reason=reason)
        return item, self._latest.stats()

    def buffer_stats(self) -> LatestBufferStats:
        return self._latest.stats()

    def diagnostics_text(self) -> str:
        with self._lock:
            state = self._state.value
            session = self._session
            accepted = self._accepted_blocks
            validated = self._validated_blocks
            error = self._last_error
            pipeline = self._pipeline
            session_path = self._session_path
            latest_result = self._latest_processing
            recording_enabled = self._recording_enabled
            assembly = self._assembly_snapshot
        stats = self._latest.stats()
        lines = [
            f"连接状态：{state}",
            f"校验通过的完整分析窗口：{validated}",
            f"处理队列已接纳的窗口：{accepted}",
            f"已交付界面摘要：{stats.delivered_count}",
            f"有界摘要交接替换过期更新：{stats.replaced_count}（不代表原始 EEG 丢失）",
            f"当前待展示块：{'有' if stats.pending else '无'}",
        ]
        if assembly is None:
            lines.append("Curry 网络包/30 秒窗口：尚未完成握手")
        else:
            lines.extend(
                [
                    f"已校验网络包：{assembly.received_packets} 个，"
                    f"{assembly.received_samples} 个样本",
                    f"完整窗口：{assembly.completed_windows} 个，"
                    f"已接纳：{assembly.accepted_windows} 个",
                    f"当前累计：{assembly.partial_samples} 点，"
                    f"{assembly.pending_seconds:.1f} / "
                    f"{assembly.window_seconds:g} 秒",
                ]
            )
        if pipeline is not None:
            lines.append(
                f"处理队列待办：{pipeline.pending_count}/{pipeline.pending_limit}"
            )
        if session is not None:
            lines.extend(
                [
                    f"会话通道：{session.n_channels}",
                    f"会话采样率：{session.sample_rate_hz:g} Hz",
                    f"会话标签：{', '.join(session.labels)}",
                ]
            )
        if latest_result is not None:
            lines.append(
                f"最新处理结果：block_id={latest_result.block_id} "
                f"status={latest_result.status.value}"
            )
        lines.append(
            f"记录状态：{'已启用' if recording_enabled else '未保存（记录已关闭）'}"
        )
        if session_path:
            lines.append(f"会话路径：{session_path}")
        if error:
            lines.append(f"最后错误：{error}")
        return "\n".join(lines)

    def _set_error_state(self, text: str) -> None:
        with self._lock:
            self._state = ConnectionState.ERROR
            self._last_error = text
        self.state_changed.emit(ConnectionState.ERROR.value, "参数错误，未建立连接")
        self.error_changed.emit(text)

    @Slot(int, str)
    def _on_stage_csv_status(self, generation: int, status: str) -> None:
        with self._lock:
            if generation != self._generation:
                return
        self.stage_csv_changed.emit(status)

    @Slot(int, object)
    def _on_pipeline_ready(self, generation: int, error: BaseException | None) -> None:
        network_start_error: BaseException | None = None
        network_not_started_cancelled = False
        with self._lock:
            if generation != self._generation:
                return
            pipeline = self._pipeline
            client = self._client
            event = self._cancel_event
            stage_csv_enabled = self._stage_csv_enabled
            if pipeline is not None and pipeline.session_path:
                self._session_path = pipeline.session_path
                if error is None:
                    self.recording_changed.emit(f"记录中：{pipeline.session_path}")
                else:
                    self.recording_changed.emit(
                        f"会话初始化失败；目标目录（若已创建则保留）：{pipeline.session_path}"
                    )
            if error is not None:
                self._network_done = True
                self._network_error = error
                if stage_csv_enabled:
                    self.stage_csv_changed.emit(
                        f"CSV/会话创建失败，采集未开始：{error}"
                    )
            elif event is not None and event.is_set():
                self._network_done = True
                self._network_cancelled = True
                network_not_started_cancelled = True
            elif client is not None:
                worker = threading.Thread(
                    target=self._run_session,
                    args=(generation, client, event, pipeline),
                    name="curry-session-worker",
                    daemon=False,
                )
                self._thread = worker
                self._network_started = True
                if pipeline is not None:
                    pipeline.mark_network_started()
                try:
                    worker.start()
                except BaseException as exc:
                    network_start_error = exc
                    self._thread = None
                    self._network_started = False
                    self._network_done = True
                    self._network_error = exc
                else:
                    return
        finish_error = error or network_start_error
        finish_cancelled = network_not_started_cancelled and finish_error is None
        if pipeline is not None:
            pipeline.handoff_network_end(
                None,
                cancelled=finish_cancelled,
                error=finish_error,
            )
            pipeline.finish(cancelled=finish_cancelled, error=finish_error)
        if finish_error is not None:
            self.error_changed.emit(str(finish_error) or type(finish_error).__name__)
            self.state_changed.emit(
                ConnectionState.STOPPING.value,
                "会话初始化失败，正在收尾",
            )
        self._maybe_finalize(generation)

    def _run_session(
        self,
        generation: int,
        client: CurryClient,
        cancel_event: threading.Event | None,
        pipeline: ProcessingPipeline | None,
    ) -> None:
        error: BaseException | None = None
        cancelled = False
        try:
            self._worker_status(generation, "connecting")
            client.connect(cancel_event=cancel_event)
            self._worker_status(generation, "handshaking")
            result: StreamResult = client.stream(
                lambda block: self._accept_block(generation, cancel_event, block),
                cancel_event=cancel_event,
                on_status=lambda status: self._client_status(generation, status),
                on_session=lambda session: self._accept_session(generation, session),
            )
            error = result.error
            cancelled = result.cancelled
            if cancel_event is not None and cancel_event.is_set() and error is None:
                cancelled = True
        except CurryClientCancelled:
            cancelled = True
        except (ConnectionError, OSError) as exc:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
            else:
                error = exc
        except Exception as exc:  # protocol, validation, or bounded-queue failures
            error = exc
        finally:
            try:
                client.close()
            except (OSError, ValueError) as exc:
                if not cancelled and error is None:
                    error = exc
            if self._stimulation_runtime is not None:
                # Natural EOF and network failures must register the real
                # control shutdown from this worker before the pipeline is
                # allowed to pass its final handoff barrier.  The Qt callback
                # below repeats this idempotently as a lifecycle fallback.
                self._stimulation_runtime.on_session_stopping(
                    "接收结束；自动决策已关闭"
                )
            assembly = self._finalize_assembly(generation)
            if pipeline is not None:
                pipeline.handoff_network_end(
                    assembly.to_dict() if assembly is not None else None,
                    cancelled=cancelled,
                    error=error,
                )
            self._network_finished_signal.emit(generation, cancelled, error)

    def _worker_status(self, generation: int, status: str) -> None:
        if not self._is_current(generation):
            return
        if status == "connecting":
            detail = "正在建立 TCP 连接"
        else:
            detail = "正在等待 BasicInfo / ChannelInfo 握手"
        self._set_worker_state(generation, ConnectionState.CONNECTING, detail)

    def _client_status(self, generation: int, status: str) -> None:
        if status == "streaming":
            self._set_worker_state(
                generation,
                ConnectionState.STREAMING,
                "握手完成，已连接并接收；正在按采样点累积 30 秒窗口",
            )
        elif status == "stopping":
            self._set_worker_state(
                generation,
                ConnectionState.STOPPING,
                "正在发送停止推流并清理连接",
            )
        elif status in {"handshaking", "waiting_basic_info", "waiting_channel_info"}:
            details = {
                "handshaking": "正在等待 Curry 握手",
                "waiting_basic_info": "等待 BasicInfo",
                "waiting_channel_info": "已收到 BasicInfo，等待 ChannelInfo",
            }
            self._set_worker_state(
                generation, ConnectionState.CONNECTING, details[status]
            )

    def _set_worker_state(
        self,
        generation: int,
        state: ConnectionState,
        detail: str,
    ) -> None:
        with self._lock:
            if generation != self._generation:
                return
            if self._state == ConnectionState.STOPPING and state != ConnectionState.STOPPING:
                return
            self._state = state
        self.state_changed.emit(state.value, detail)

    def _accept_session(self, generation: int, session: SessionInfo) -> None:
        with self._lock:
            if generation != self._generation:
                return
            assembler = ThirtySecondEpochAssembler(session)
            self._session = session
            self._assembler = assembler
            self._assembly_snapshot = assembler.snapshot()
            self._assembly_progress_dirty = True
            pipeline = self._pipeline
        if pipeline is not None:
            pipeline.set_session_info(session)
        if self._stimulation_runtime is not None:
            self._stimulation_runtime.set_session_ready(True)
        self.session_changed.emit(session)
        self.assembly_progress_changed.emit(assembler.snapshot())
        self.diagnostics_changed.emit(self.diagnostics_text())

    def _accept_block(
        self,
        generation: int,
        cancel_event: threading.Event | None,
        block: DataBlock,
    ) -> None:
        # Capture the packet-arrival boundary before validation, accumulation,
        # queueing or model work. All windows completed by this packet share
        # this timestamp, which is the last contributing packet for them.
        received_at_local = self._wall_clock()
        if received_at_local.tzinfo is None or received_at_local.utcoffset() is None:
            raise ValueError("本机墙上时钟必须返回带 UTC 偏移的 datetime")
        received_local_iso = received_at_local.isoformat(timespec="milliseconds")
        received_utc = (
            received_at_local.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        received_monotonic_ns = time.monotonic_ns()
        with self._lock:
            if generation != self._generation or (cancel_event and cancel_event.is_set()):
                return
            session = self._session
            assembler = self._assembler
            pipeline = self._pipeline
        if session is None or assembler is None:
            raise RuntimeError("Curry 握手尚未准备好窗口累积器")
        if pipeline is None:
            raise RuntimeError("P2 处理线程尚未准备好，不能接纳 EEG 窗口")

        windows = assembler.push(block)
        try:
            for window in windows:
                self._accept_epoch(
                    generation,
                    cancel_event,
                    session,
                    assembler,
                    pipeline,
                    window,
                    received_utc,
                    received_local_iso,
                    received_monotonic_ns,
                )
        except ProcessingBacklogError:
            # Drain the current packet's lazy iterator so the residual state
            # is truthful before the session is stopped for overflow.
            for _ in windows:
                pass
            raise
        finally:
            self._refresh_assembly_snapshot(generation, assembler)

    def _accept_epoch(
        self,
        generation: int,
        cancel_event: threading.Event | None,
        session: SessionInfo,
        assembler: ThirtySecondEpochAssembler,
        pipeline: ProcessingPipeline,
        block: DataBlock,
        received_utc: str,
        received_local_iso: str,
        received_monotonic_ns: int,
    ) -> None:
        validate_data_block(block, session=session)
        with self._lock:
            if generation != self._generation or (cancel_event and cancel_event.is_set()):
                return
            self._validated_blocks += 1
            block_id = self._validated_blocks
            session_id = self._session_id
        if session_id is None:
            raise RuntimeError("当前会话标识尚未准备好，不能接纳 EEG 窗口")
        context = BlockContext(
            session_id=session_id,
            block_id=block_id,
            block=block,
            received_utc=received_utc,
            received_monotonic_ns=received_monotonic_ns,
            window_received_local_iso=received_local_iso,
        )
        if not pipeline.enqueue(context):
            error = ProcessingBacklogError(
                f"分期/记录队列已满（最多等待 {pipeline.pending_limit} 个完整窗口）；"
                f"已接纳前 {block_id - 1} 个窗口，本窗口未接纳，采集已停止"
            )
            with self._lock:
                self._fatal_error = error
            pipeline.cancel_active_prediction()
            raise error
        assembler.mark_accepted_window()
        with self._lock:
            self._accepted_blocks += 1
        self._latest.put(QueuedBlock(block_id, block, generation=generation))

    def _refresh_assembly_snapshot(
        self,
        generation: int,
        assembler: ThirtySecondEpochAssembler,
    ) -> None:
        snapshot = assembler.snapshot()
        with self._lock:
            if generation != self._generation or self._assembler is not assembler:
                return
            self._assembly_snapshot = snapshot
            self._assembly_progress_dirty = True

    def _finalize_assembly(self, generation: int) -> EpochAssemblySnapshot | None:
        with self._lock:
            if generation != self._generation or self._assembler is None:
                return None
            assembler = self._assembler
        snapshot = assembler.finalize()
        with self._lock:
            if generation != self._generation or self._assembler is not assembler:
                return None
            self._assembly_snapshot = snapshot
            self._assembly_progress_dirty = True
        self.assembly_progress_changed.emit(snapshot)
        return snapshot

    @Slot()
    def _flush_assembly_progress(self) -> None:
        if self._stimulation_runtime is not None:
            self._stimulation_runtime.tick()
        with self._lock:
            if not self._assembly_progress_dirty:
                return
            snapshot = self._assembly_snapshot
            self._assembly_progress_dirty = False
        if snapshot is not None:
            self.assembly_progress_changed.emit(snapshot)
            self.diagnostics_changed.emit(self.diagnostics_text())

    @Slot(int, bool, object)
    def _on_network_finished(
        self,
        generation: int,
        cancelled: bool,
        error: BaseException | None,
    ) -> None:
        with self._lock:
            if generation != self._generation:
                return
            fatal_error = self._fatal_error
            pipeline = self._pipeline
            assembly = self._assembly_snapshot
            self._network_done = True
            self._network_cancelled = cancelled
            self._network_error = fatal_error or error
            network_error = self._network_error
        if self._stimulation_runtime is not None:
            self._stimulation_runtime.on_session_stopping(
                "接收结束；自动决策已关闭"
            )
        if pipeline is not None:
            # The network worker normally committed this before emitting the
            # Qt lifecycle signal. Keep this idempotent fallback for startup
            # failures or custom clients that return through the signal path.
            pipeline.handoff_network_end(
                assembly.to_dict() if assembly is not None else None,
                cancelled=cancelled,
                error=network_error,
            )
            pipeline.finish(cancelled=cancelled, error=network_error)
        self._maybe_finalize(generation)

    def _on_pipeline_fatal_from_worker(
        self, generation: int, error: BaseException
    ) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._fatal_error = self._fatal_error or error
            event = self._cancel_event
            client = self._client
            if self._state != ConnectionState.STOPPING:
                self._state = ConnectionState.STOPPING
        if self._stimulation_runtime is not None:
            self._stimulation_runtime.on_session_stopping(
                "处理或记录错误；自动决策已关闭"
            )
        if event is not None:
            event.set()
        if client is not None:
            client.cancel()
        self._pipeline_fatal_signal.emit(generation, error)

    @Slot(int, object)
    def _on_pipeline_fatal(self, generation: int, error: BaseException) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._last_error = str(error) or type(error).__name__
        self.state_changed.emit(ConnectionState.STOPPING.value, "处理/记录失败，正在停止并保留已写前缀")
        self.error_changed.emit(self._last_error)
        self.recording_changed.emit(f"记录/处理失败：{self._last_error}")
        self.diagnostics_changed.emit(self.diagnostics_text())

    @Slot(int, object)
    def _on_processing_result(self, generation: int, result: ProcessingResult) -> None:
        with self._lock:
            if generation != self._generation or result.session_id != self._session_id:
                return
            self._latest_processing = result
        self.processing_changed.emit(result)
        self.diagnostics_changed.emit(self.diagnostics_text())

    @Slot(int, object)
    def _on_pipeline_finished(self, generation: int, outcome: PipelineOutcome) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._pipeline_done = True
            self._pipeline_outcome = outcome
            if outcome.error and self._fatal_error is None:
                self._fatal_error = RuntimeError(outcome.error)
            self._session_path = outcome.session_path or self._session_path
        if self._stimulation_runtime is not None:
            self._stimulation_runtime.finish_session(outcome.session_id)
        if self._recording_enabled:
            if outcome.error:
                self.recording_changed.emit(
                    f"记录未完整：已保存 {outcome.saved_blocks} 块；{outcome.error}；"
                    f"目标目录：{outcome.session_path or '未创建'}"
                )
            else:
                self.recording_changed.emit(
                    f"会话已关闭：保存 {outcome.saved_blocks} 块；"
                    f"{outcome.session_path or '路径不可用'}"
                )
        self._maybe_finalize(generation)

    def _maybe_finalize(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation or not (self._network_done and self._pipeline_done):
                return
            network = self._thread
            pipeline = self._pipeline
            if (network is not None and network.is_alive()) or (
                pipeline is not None and pipeline.is_alive()
            ):
                QTimer.singleShot(5, lambda: self._maybe_finalize(generation))
                return
            outcome = self._pipeline_outcome
            error = self._network_error or self._fatal_error
            if error is None and outcome is not None and outcome.error:
                error = RuntimeError(outcome.error)
            cancelled = self._network_cancelled or bool(outcome and outcome.cancelled)
            self._thread = None
            self._client = None
            self._cancel_event = None
            self._pipeline = None
            self._progress_timer.stop()
            self._session_terminal = True
            self._terminal_cancelled = cancelled
            self._terminal_error = error
            if error is not None:
                self._state = ConnectionState.ERROR
                self._last_error = str(error) or type(error).__name__
                detail = "接收/处理失败，后台资源已清理"
            else:
                self._state = ConnectionState.DISCONNECTED
                detail = "已断开，网络、处理线程与写者均已退出"
                self._last_error = ""
        if error is not None:
            self.error_changed.emit(str(error) or type(error).__name__)
        self.state_changed.emit(self._state.value, detail)
        self.busy_changed.emit(False)
        self.session_finished.emit(cancelled, error)
        self.diagnostics_changed.emit(self.diagnostics_text())

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation
