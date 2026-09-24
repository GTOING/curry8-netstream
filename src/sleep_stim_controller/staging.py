"""Typed staging boundary and bounded per-session processing worker."""
from __future__ import annotations

import math
import threading
import time
import uuid
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Callable, Protocol

import numpy as np

from curry_netstream.models import DataBlock, SessionInfo


STAGES = ("W", "N1", "N2", "N3", "REM")
DEFAULT_PENDING_BLOCK_LIMIT = 4


class ProcessingStatus(StrEnum):
    UNAVAILABLE = "unavailable"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    model_id: str
    version: str
    is_test_double: bool = False
    confidence_meaning: str | None = None
    configuration: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "version": self.version,
            "is_test_double": self.is_test_double,
            "confidence_meaning": self.confidence_meaning,
            "configuration": deepcopy(self.configuration),
        }


@dataclass(frozen=True, slots=True)
class BlockContext:
    """A validated block and its receive-boundary identity/timestamps."""

    session_id: str
    block_id: int
    block: DataBlock
    received_utc: str
    received_monotonic_ns: int
    window_received_local_iso: str | None = None

    @property
    def start_sample(self) -> int:
        return int(self.block.start_sample)

    @property
    def end_sample_exclusive(self) -> int:
        return self.start_sample + int(self.block.n_samples)

    def model_input_copy(self) -> DataBlock:
        """Isolate model-side mutation from the raw block used for persistence."""
        source = self.block
        return DataBlock(
            data=np.array(source.data, copy=True, order="K"),
            start_sample=source.start_sample,
            sample_rate_hz=source.sample_rate_hz,
            labels=list(source.labels),
            events=list(source.events),
        )

    def model_context_copy(self) -> "BlockContext":
        return BlockContext(
            session_id=self.session_id,
            block_id=self.block_id,
            block=self.model_input_copy(),
            received_utc=self.received_utc,
            received_monotonic_ns=self.received_monotonic_ns,
            window_received_local_iso=self.window_received_local_iso,
        )


@dataclass(frozen=True, slots=True)
class StagePrediction:
    stage: str
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    session_id: str
    block_id: int
    start_sample: int
    end_sample_exclusive: int
    model: ModelDescriptor
    status: ProcessingStatus
    stage: str | None
    confidence: float | None
    started_monotonic_ns: int
    finished_monotonic_ns: int
    elapsed_ms: float
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "block_id": self.block_id,
            "start_sample": self.start_sample,
            "end_sample_exclusive": self.end_sample_exclusive,
            "model": self.model.to_dict(),
            "status": self.status.value,
            "stage": self.stage,
            "confidence": self.confidence,
            "started_monotonic_ns": self.started_monotonic_ns,
            "finished_monotonic_ns": self.finished_monotonic_ns,
            "elapsed_ms": self.elapsed_ms,
            "reason": self.reason,
        }


class PredictionCancelled(Exception):
    """A cooperative model adapter stopped prediction on request."""


class ModelAdapter(Protocol):
    """Small future-model contract; no model files or framework are implied."""

    @property
    def descriptor(self) -> ModelDescriptor: ...

    def prepare(self, cancel_event: threading.Event) -> None: ...

    def predict(
        self, block: BlockContext, data: DataBlock, cancel_event: threading.Event
    ) -> StagePrediction | None: ...

    def close(self, cancel_event: threading.Event) -> None: ...


class NoModelAdapter:
    """Production default: make unavailability explicit, never synthesize W."""

    descriptor = ModelDescriptor("none", "not-connected", is_test_double=False)

    def prepare(self, cancel_event: threading.Event) -> None:
        return None

    def predict(
        self, block: BlockContext, data: DataBlock, cancel_event: threading.Event
    ) -> None:
        return None

    def close(self, cancel_event: threading.Event) -> None:
        return None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True, slots=True)
class PipelineOutcome:
    session_id: str
    accepted_blocks: int
    rejected_blocks: int
    saved_blocks: int
    completed_results: int
    unprocessed_blocks: int
    cancelled: bool
    error: str | None
    session_path: str | None
    stream_assembly: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class _QueuedSessionEvent:
    event: dict[str, object]


class ProcessingPipeline:
    """One sequential worker for optional raw saving followed by prediction.

    The queue bound applies to waiting blocks; at most one additional block is
    in flight. The worker owns its writer and model lifecycle from startup to
    close, so neither disk access nor prediction runs in the Qt/network thread.
    """

    def __init__(
        self,
        *,
        session_id: str,
        host: str,
        port: int,
        recording_enabled: bool,
        recording_root: str | Path | None,
        stage_csv_enabled: bool = False,
        model_factory: Callable[[], ModelAdapter] = NoModelAdapter,
        pending_limit: int = DEFAULT_PENDING_BLOCK_LIMIT,
        on_ready: Callable[[BaseException | None], None] | None = None,
        on_result: Callable[[ProcessingResult], None] | None = None,
        on_stimulation_result: Callable[
            [BlockContext, ProcessingResult], tuple[tuple[dict[str, object], ...], object]
        ] | None = None,
        on_stimulation_decision_recorded: Callable[[object], None] | None = None,
        on_fatal: Callable[[BaseException], None] | None = None,
        on_finished: Callable[[PipelineOutcome], None] | None = None,
        on_stage_csv_status: Callable[[str], None] | None = None,
        session_event_limit: int = 64,
    ) -> None:
        if pending_limit < 1:
            raise ValueError("pending_limit 必须大于 0")
        if session_event_limit < 1:
            raise ValueError("session_event_limit 必须大于 0")
        if recording_enabled and recording_root is None:
            raise ValueError("启用记录时必须选择保存目录")
        self.session_id = session_id
        self.host = host
        self.port = int(port)
        self.recording_enabled = recording_enabled
        self.recording_root = Path(recording_root) if recording_root else None
        self.stage_csv_enabled = bool(stage_csv_enabled)
        if self.stage_csv_enabled and not self.recording_enabled:
            raise ValueError("启用分期 CSV 时必须启用会话记录")
        self.model_factory = model_factory
        self.pending_limit = pending_limit
        self.on_ready = on_ready
        self.on_result = on_result
        self.on_stimulation_result = on_stimulation_result
        self.on_stimulation_decision_recorded = on_stimulation_decision_recorded
        self.on_fatal = on_fatal
        self.on_finished = on_finished
        self.on_stage_csv_status = on_stage_csv_status
        self.session_event_limit = session_event_limit

        self.cancel_predictions = threading.Event()
        self._condition = threading.Condition()
        self._pending: deque[BlockContext] = deque()
        self._session_events: deque[_QueuedSessionEvent] = deque()
        self._event_reservations: set[str] = set()
        self._external_work = 0
        self._external_error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"eeg-processing-{session_id[:8]}",
            daemon=False,
        )
        self._ready = False
        self._finish_requested = False
        self._finish_cancelled = False
        self._finish_error: BaseException | None = None
        self._session_info: SessionInfo | None = None
        self._session_info_revision = 0
        self._accepted_blocks = 0
        self._rejected_blocks = 0
        self._saved_blocks = 0
        self._completed_results = 0
        self._unprocessed_blocks = 0
        self._session_path: str | None = None
        self._stream_assembly: dict[str, object] | None = None
        self._network_handoff_required = False
        self._network_handoff_received = False
        self._network_end_cancelled = False
        self._network_end_error: BaseException | None = None
        # This is the control-lease admission barrier, not merely a marker
        # written after SessionWriter.finish().  Once the worker has drained
        # the final accepted event/lease set, it flips this under
        # ``_condition`` before entering writer.finish(), so a late required
        # Stop can still be attempted but cannot be mistaken for a persistable
        # lifecycle.
        self._closed = False

    def start(self) -> None:
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    @property
    def pending_count(self) -> int:
        with self._condition:
            return len(self._pending)

    @property
    def session_path(self) -> str | None:
        return self._session_path

    def enqueue(self, context: BlockContext) -> bool:
        with self._condition:
            if self._finish_requested or len(self._pending) >= self.pending_limit:
                self._rejected_blocks += 1
                return False
            self._pending.append(context)
            self._accepted_blocks += 1
            self._condition.notify()
            return True

    def acquire_external_work(self) -> bool:
        """Keep session_finished behind one asynchronous P3 request lifecycle."""
        with self._condition:
            if self._finish_requested or self._external_error is not None:
                return False
            self._external_work += 1
            self._condition.notify_all()
            return True

    def acquire_control_work(self) -> bool:
        """Reserve a necessary control-stop lifecycle during session close.

        This is intentionally separate from :meth:`acquire_external_work`.
        Normal asynchronous work must stop at ``finish`` or an external
        recording error, while a protective Rally Stop may still need to keep
        the writer alive long enough to persist its final outcome.  If the
        writer has already closed, the caller may still make a best-effort
        network stop without a lease; this method then returns ``False``.
        """
        with self._condition:
            if self._closed:
                return False
            self._external_work += 1
            self._condition.notify_all()
            return True

    def release_external_work(self) -> None:
        with self._condition:
            if self._external_work > 0:
                self._external_work -= 1
            self._condition.notify_all()

    def reserve_session_event(self) -> str | None:
        """Reserve writer capacity before a UDP send is allowed to happen."""
        if not self.recording_enabled:
            return "unrecorded"
        failure: BaseException | None = None
        with self._condition:
            if self._closed or self._external_error is not None:
                return None
            if (
                len(self._session_events) + len(self._event_reservations)
                >= self.session_event_limit
            ):
                failure = RuntimeError(
                    f"刺激会话事件队列已满（上限 {self.session_event_limit}）；"
                    "自动决策已停止，实时接收将收尾"
                )
                self._external_error = failure
            else:
                token = uuid.uuid4().hex
                self._event_reservations.add(token)
                self._condition.notify_all()
                return token
        self._report_external_failure(failure)
        return None

    def cancel_session_event_reservation(self, token: str | None) -> None:
        if not token or token == "unrecorded":
            return
        with self._condition:
            self._event_reservations.discard(token)
            self._condition.notify_all()

    def commit_session_event_reservation(
        self, token: str | None, event: dict[str, object]
    ) -> bool:
        if not self.recording_enabled:
            return True
        if not token or token == "unrecorded":
            return False
        with self._condition:
            if token not in self._event_reservations:
                return False
            if self._closed or self._external_error is not None:
                # The outbound request may already have crossed the socket
                # boundary. Release its writer-capacity reservation even when
                # the failed session can no longer persist request_sent.
                self._event_reservations.remove(token)
                self._condition.notify_all()
                return False
            if not self._validate_session_event(event):
                failure = ValueError("P3 会话事件与当前会话不匹配")
                self._external_error = failure
            else:
                self._event_reservations.remove(token)
                self._session_events.append(_QueuedSessionEvent(dict(event)))
                self._condition.notify_all()
                return True
        self._report_external_failure(failure)
        return False

    def enqueue_session_event(self, event: dict[str, object]) -> bool:
        """Bounded, non-blocking event handoff to the existing P2 writer."""
        if not self.recording_enabled:
            return True
        failure: BaseException | None = None
        with self._condition:
            if self._closed or self._external_error is not None:
                return False
            if not self._validate_session_event(event):
                failure = ValueError("P3 会话事件与当前会话不匹配")
                self._external_error = failure
            elif (
                len(self._session_events) + len(self._event_reservations)
                >= self.session_event_limit
            ):
                failure = RuntimeError(
                    f"刺激会话事件队列已满（上限 {self.session_event_limit}）；"
                    "自动决策已停止，实时接收将收尾"
                )
                self._external_error = failure
            else:
                self._session_events.append(_QueuedSessionEvent(dict(event)))
                self._condition.notify_all()
                return True
        self._report_external_failure(failure)
        return False

    def _validate_session_event(self, event: dict[str, object]) -> bool:
        if event.get("event_type") in {"rally_control", "paradigm_control"}:
            try:
                if event.get("event_type") == "rally_control":
                    from .rally_control_schema import validate_rally_control_event

                    validate_rally_control_event(event)
                else:
                    from .paradigm_control_schema import (
                        validate_paradigm_control_event,
                    )

                    validate_paradigm_control_event(event)
            except (TypeError, ValueError):
                return False
            return event.get("session_id") == self.session_id
        return (
            event.get("session_id") == self.session_id
            and event.get("event_type")
            in {"stimulation_config", "decision", "request_sent", "request_outcome"}
            and isinstance(event.get("payload"), dict)
        )

    def _report_external_failure(self, error: BaseException | None) -> None:
        if error is not None and self.on_fatal is not None:
            self.on_fatal(error)

    def set_session_info(self, info: SessionInfo) -> None:
        with self._condition:
            self._session_info = SessionInfo(
                info.n_channels, info.sample_rate_hz, list(info.labels)
            )
            self._session_info_revision += 1
            self._condition.notify()

    def cancel_active_prediction(self) -> None:
        self.cancel_predictions.set()
        with self._condition:
            self._condition.notify_all()

    def mark_network_started(self) -> None:
        """Require a network-end handoff before this pipeline can close."""
        with self._condition:
            self._network_handoff_required = True
            self._condition.notify_all()

    def finish(
        self,
        *,
        cancelled: bool,
        error: BaseException | None,
    ) -> None:
        with self._condition:
            self._finish_requested = True
            self._finish_cancelled = self._finish_cancelled or cancelled
            self._finish_error = self._finish_error or error
            if cancelled:
                self.cancel_predictions.set()
            self._condition.notify_all()

    def set_stream_assembly(self, summary: dict[str, object] | None) -> None:
        """Freeze network-thread diagnostics for the single session writer."""
        with self._condition:
            if not self._network_handoff_received:
                self._stream_assembly = dict(summary) if summary is not None else None
            self._condition.notify_all()

    def handoff_network_end(
        self,
        summary: dict[str, object] | None,
        *,
        cancelled: bool,
        error: BaseException | None,
    ) -> bool:
        """Commit the frozen network result before the single writer closes.

        The network worker calls this directly from its ``finally`` block. A
        later Qt lifecycle callback may call it again as a safe fallback, but
        only the first call can publish the summary or end status.
        """
        with self._condition:
            if self._network_handoff_received:
                return False
            self._network_handoff_received = True
            self._stream_assembly = dict(summary) if summary is not None else None
            self._network_end_cancelled = cancelled
            self._network_end_error = error
            self._finish_requested = True
            self._finish_cancelled = self._finish_cancelled or cancelled
            self._finish_error = self._finish_error or error
            if cancelled:
                self.cancel_predictions.set()
            self._condition.notify_all()
            return True

    def _signal_ready(self, error: BaseException | None) -> None:
        if self._ready:
            return
        self._ready = True
        if self.on_ready is not None:
            self.on_ready(error)

    def _run(self) -> None:
        from .recording import SessionWriter

        writer = None
        adapter: ModelAdapter | None = None
        descriptor = NoModelAdapter.descriptor
        prepare_error: str | None = None
        fatal_error: BaseException | None = None
        latest_info_revision = -1
        in_flight: BlockContext | None = None
        try:
            if self.recording_enabled:
                assert self.recording_root is not None
                self._session_path = str(
                    self.recording_root.expanduser() / f"session_{self.session_id}"
                )
                writer = SessionWriter.create(
                    self.recording_root,
                    session_id=self.session_id,
                    host=self.host,
                    port=self.port,
                    descriptor=descriptor,
                    stage_csv_enabled=self.stage_csv_enabled,
                )
                self._publish_stage_csv_status(writer)
            adapter = self.model_factory()
            descriptor = adapter.descriptor
            if writer is not None:
                writer.set_model(descriptor)
            try:
                adapter.prepare(self.cancel_predictions)
            except Exception as exc:  # failed preparation is per-block failed
                prepare_error = f"模型准备失败：{exc}"
            descriptor = adapter.descriptor
            if writer is not None:
                writer.set_model(descriptor)
            self._signal_ready(None)
            while True:
                queued_event: _QueuedSessionEvent | None = None
                context: BlockContext | None = None
                with self._condition:
                    while True:
                        if self._external_error is not None:
                            fatal_error = self._external_error
                            self._pending.clear()
                            self._session_events.clear()
                            self.cancel_predictions.set()
                            break
                        if self._session_events:
                            queued_event = self._session_events.popleft()
                            context = None
                            break
                        if self._pending:
                            context = self._pending.popleft()
                            in_flight = context
                            break
                        if (
                            self._finish_requested
                            and self._external_work == 0
                            and not self._event_reservations
                            and (
                                not self._network_handoff_required
                                or self._network_handoff_received
                            )
                        ):
                            # This is the atomic handoff from accepting
                            # necessary control lifecycles to closing the
                            # writer.  acquire_control_work() uses this same
                            # condition, so a late EOF callback cannot acquire
                            # a lease after the final event-drain point.
                            self._closed = True
                            self._condition.notify_all()
                            context = None
                            break
                        self._condition.wait()
                    done = (
                        self._finish_requested
                        and context is None
                        and queued_event is None
                        and self._external_work == 0
                        and not self._event_reservations
                        and (
                            not self._network_handoff_required
                            or self._network_handoff_received
                        )
                    )
                    info = self._session_info
                    info_revision = self._session_info_revision
                if fatal_error is not None:
                    if self.on_fatal is not None:
                        self.on_fatal(fatal_error)
                    break
                if writer is not None and info is not None and info_revision != latest_info_revision:
                    writer.update_handshake(info)
                    latest_info_revision = info_revision
                if done:
                    break
                if queued_event is not None:
                    if writer is not None:
                        writer.append_extension_event(queued_event.event)
                    continue
                if context is None:
                    continue
                try:
                    if writer is not None:
                        writer.save_block(context)
                        self._saved_blocks += 1
                    result = self._process_one(
                        context,
                        adapter,
                        descriptor,
                        prepare_error,
                    )
                    if writer is not None:
                        writer.set_model(result.model)
                        writer.save_processing_result(result)
                        self._publish_stage_csv_status(writer)
                    self._completed_results += 1
                    in_flight = None
                    if self.on_result is not None:
                        self.on_result(result)
                    if self.on_stimulation_result is not None:
                        events, evaluation = self.on_stimulation_result(context, result)
                        if writer is not None:
                            for event in events:
                                writer.append_extension_event(event)
                        if self.on_stimulation_decision_recorded is not None:
                            self.on_stimulation_decision_recorded(evaluation)
                except Exception as exc:
                    fatal_error = exc
                    with self._condition:
                        if in_flight is not None:
                            self._unprocessed_blocks += 1
                            in_flight = None
                        self._unprocessed_blocks += len(self._pending)
                        self._pending.clear()
                    self.cancel_predictions.set()
                    if self.on_fatal is not None:
                        self.on_fatal(exc)
                    break
        except Exception as exc:
            fatal_error = exc
            if in_flight is not None:
                self._unprocessed_blocks += 1
                in_flight = None
            self._signal_ready(exc)
            if self.on_fatal is not None:
                self.on_fatal(exc)
        finally:
            if adapter is not None:
                try:
                    adapter.close(self.cancel_predictions)
                except Exception as exc:
                    fatal_error = fatal_error or exc
                    if self.on_fatal is not None:
                        self.on_fatal(exc)
            with self._condition:
                if fatal_error is not None:
                    # No writer loop remains to drain events after this
                    # point. Mark the recording/processing failure before
                    # clearing the queue so a late control callback is
                    # rejected as missing evidence rather than being
                    # accepted into an undrained queue.
                    self._external_error = self._external_error or fatal_error
                    if in_flight is not None:
                        self._unprocessed_blocks += 1
                    self._unprocessed_blocks += len(self._pending)
                    self._pending.clear()
                    self._session_events.clear()
                # A processing/recording failure can reach this finally block
                # before the TCP worker returns. Keep the writer open until
                # that worker publishes its frozen summary. Controller paths
                # that never start networking do not set this requirement.
                while (
                    self._network_handoff_required
                    and not self._network_handoff_received
                ):
                    self._condition.wait()
                # A sent request must resolve or become unknown before the
                # writer is closed, including after an earlier recording error.
                while self._external_work or self._event_reservations:
                    self._condition.wait(0.05)
                # Fatal/recording-error paths bypass the normal ``done``
                # branch above.  Apply the same admission barrier after their
                # accepted work and reservations have drained, before the
                # writer is asked to append session_finished.
                self._closed = True
                self._condition.notify_all()
                accepted = self._accepted_blocks
                rejected = self._rejected_blocks
                unprocessed = self._unprocessed_blocks
                finish_error = self._finish_error
                stream_assembly = (
                    dict(self._stream_assembly)
                    if self._stream_assembly is not None
                    else None
                )
            final_error = fatal_error or finish_error
            cancelled = (
                self._finish_cancelled
                and fatal_error is None
                and finish_error is None
            )
            if writer is not None:
                try:
                    writer.finish(
                        status="failed" if final_error is not None else "closed",
                        reason=(str(final_error) if final_error else (
                            "用户主动断开" if cancelled else "接收结束"
                        )),
                        accepted_blocks=accepted,
                        rejected_blocks=rejected,
                        completed_results=self._completed_results,
                        unprocessed_blocks=unprocessed,
                        stream_assembly=stream_assembly,
                    )
                except Exception as exc:
                    final_error = final_error or exc
                    if self.on_fatal is not None:
                        self.on_fatal(exc)
                finally:
                    self._publish_stage_csv_status(writer)
            with self._condition:
                self._closed = True
                self._condition.notify_all()
            if not self._ready:
                self._signal_ready(final_error or RuntimeError("处理线程未能启动"))
            if self.on_finished is not None:
                self.on_finished(
                    PipelineOutcome(
                        session_id=self.session_id,
                        accepted_blocks=accepted,
                        rejected_blocks=rejected,
                        saved_blocks=self._saved_blocks,
                        completed_results=self._completed_results,
                        unprocessed_blocks=unprocessed,
                        cancelled=cancelled,
                        error=str(final_error) if final_error is not None else None,
                        session_path=self._session_path,
                        stream_assembly=stream_assembly,
                    )
                )

    def _publish_stage_csv_status(self, writer) -> None:
        if self.on_stage_csv_status is not None and writer.stage_csv_enabled:
            try:
                self.on_stage_csv_status(writer.stage_csv_status())
            except Exception:
                # UI/status reporting is secondary to the archive and control
                # lifecycle; a broken observer must not stop the writer.
                pass

    def _process_one(
        self,
        context: BlockContext,
        adapter: ModelAdapter,
        descriptor: ModelDescriptor,
        prepare_error: str | None,
    ) -> ProcessingResult:
        started = time.monotonic_ns()
        stage: str | None = None
        confidence: float | None = None
        reason: str | None = None
        status = ProcessingStatus.FAILED
        if self.cancel_predictions.is_set():
            status = ProcessingStatus.CANCELLED
            reason = "用户已请求取消分期"
        elif prepare_error is not None:
            status = ProcessingStatus.FAILED
            reason = prepare_error
        else:
            try:
                model_context = context.model_context_copy()
                prediction = adapter.predict(
                    model_context, model_context.block, self.cancel_predictions
                )
                if self.cancel_predictions.is_set():
                    status = ProcessingStatus.CANCELLED
                    reason = "用户已请求取消分期"
                elif prediction is None:
                    status = ProcessingStatus.UNAVAILABLE
                    reason = "模型未接入"
                elif not isinstance(prediction, StagePrediction):
                    raise ValueError("模型输出必须是 StagePrediction")
                elif prediction.stage not in STAGES:
                    raise ValueError(f"非法睡眠期：{prediction.stage!r}")
                else:
                    value = prediction.confidence
                    if value is not None:
                        if isinstance(value, bool):
                            raise ValueError("confidence 不能是布尔值")
                        value = float(value)
                        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                            raise ValueError("confidence 必须是 0–1 有限数值")
                        if (
                            not descriptor.confidence_meaning
                            or not descriptor.confidence_meaning.strip()
                        ):
                            raise ValueError("模型置信度缺少已声明的语义")
                    status = ProcessingStatus.SUCCESS
                    stage = prediction.stage
                    confidence = value
            except PredictionCancelled as exc:
                status = ProcessingStatus.CANCELLED
                reason = str(exc) or "模型预测已取消"
            except Exception as exc:  # one model failure cannot stop later blocks
                status = ProcessingStatus.FAILED
                reason = f"预测失败：{exc}"
        finished = time.monotonic_ns()
        if status is not ProcessingStatus.SUCCESS:
            stage = None
            confidence = None
        descriptor = adapter.descriptor
        return ProcessingResult(
            session_id=context.session_id,
            block_id=context.block_id,
            start_sample=context.start_sample,
            end_sample_exclusive=context.end_sample_exclusive,
            model=descriptor,
            status=status,
            stage=stage,
            confidence=confidence,
            started_monotonic_ns=started,
            finished_monotonic_ns=finished,
            elapsed_ms=max(0.0, (finished - started) / 1_000_000.0),
            reason=reason,
        )
