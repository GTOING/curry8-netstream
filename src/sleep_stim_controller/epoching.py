"""Bounded sample-accurate assembly of Curry packets into analysis windows."""
from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Integral

import numpy as np

from curry_netstream.models import DataBlock, SessionInfo

from .validation import DataBlockValidationError, validate_stream_block


@dataclass(frozen=True, slots=True)
class EpochAssemblySnapshot:
    """Thread-safe value object describing one assembler's current progress."""

    window_seconds: float
    window_samples: int
    received_packets: int
    received_samples: int
    completed_windows: int
    accepted_windows: int
    partial_samples: int
    partial_start_sample: int | None

    @property
    def pending_samples(self) -> int:
        """Alias used by progress consumers for the current partial window."""
        return self.partial_samples

    @property
    def pending_seconds(self) -> float:
        return self.pending_samples / self.window_samples * self.window_seconds

    def to_dict(self) -> dict[str, object]:
        return {
            "window_seconds": self.window_seconds,
            "window_samples": self.window_samples,
            "received_packets": self.received_packets,
            "received_samples": self.received_samples,
            "completed_windows": self.completed_windows,
            "accepted_windows": self.accepted_windows,
            "partial_samples": self.partial_samples,
            "partial_start_sample": self.partial_start_sample,
        }


class UnsupportedStreamEventsError(DataBlockValidationError):
    """Event-bearing live packets cannot be assigned to analysis windows yet."""


class ThirtySecondEpochAssembler:
    """Assemble contiguous decoded packets into non-overlapping fixed windows.

    The object is intentionally owned by one network worker.  It retains at
    most one partially-filled window; a completed output receives the old
    buffer and the assembler allocates a fresh one before accepting more
    samples, so later packets cannot mutate an already-delivered window.
    """

    def __init__(
        self,
        session: SessionInfo,
        window_seconds: float = 30.0,
    ) -> None:
        if not math.isfinite(float(window_seconds)) or float(window_seconds) <= 0:
            raise ValueError("window_seconds 必须为有限正数")
        if session.n_channels <= 0 or len(session.labels) != session.n_channels:
            raise ValueError("握手信息的通道数与标签不一致")
        try:
            sample_rate_hz = float(session.sample_rate_hz)
        except (TypeError, ValueError) as exc:
            raise ValueError("握手采样率不是有效数字") from exc
        if not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0:
            raise ValueError("握手采样率必须为有限正数")
        window_float = sample_rate_hz * float(window_seconds)
        window_samples = round(window_float)
        if window_samples <= 0 or not math.isclose(
            window_float, window_samples, rel_tol=0.0, abs_tol=1e-9
        ):
            raise ValueError(
                f"采样率 {sample_rate_hz:g} 无法形成整数个 "
                f"{float(window_seconds):g} 秒窗口"
            )
        self.session = SessionInfo(
            int(session.n_channels), sample_rate_hz, list(session.labels)
        )
        self.window_seconds = float(window_seconds)
        self.window_samples = int(window_samples)
        self._buffer: np.ndarray | None = None
        self._window_start_sample: int | None = None
        self._expected_next_sample: int | None = None
        self._filled_samples = 0
        self._received_packets = 0
        self._received_samples = 0
        self._completed_windows = 0
        self._accepted_windows = 0
        self._closed = False
        self._final_snapshot: EpochAssemblySnapshot | None = None

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Curry 分析窗口累积器已结束")

    def push(self, block: DataBlock):
        """Validate and consume one packet, yielding complete windows lazily."""
        self._ensure_open()
        validate_stream_block(block, session=self.session)
        if block.events:
            raise UnsupportedStreamEventsError(
                "实时流事件语义尚未支持，拒绝带事件的网络包"
            )
        start_sample = block.start_sample
        if isinstance(start_sample, bool) or not isinstance(start_sample, Integral):
            # validate_stream_block already reports this, but keeping the
            # local value as an int below makes the state invariant explicit.
            raise DataBlockValidationError("start_sample 必须是非负整数")
        start_sample = int(start_sample)
        if (
            self._expected_next_sample is not None
            and start_sample != self._expected_next_sample
        ):
            raise DataBlockValidationError(
                f"EEG 采样不连续: 预期 start_sample={self._expected_next_sample}，"
                f"实际 {start_sample}"
            )

        data = np.asarray(block.data)
        try:
            data_float32 = np.asarray(data, dtype=np.float32)
        except (TypeError, ValueError) as exc:
            raise DataBlockValidationError("EEG 数据无法转换为 float32") from exc
        if self._expected_next_sample is None:
            self._expected_next_sample = start_sample
            self._window_start_sample = start_sample
        self._received_packets += 1
        self._received_samples += int(data_float32.shape[1])

        offset = 0
        packet_samples = int(data_float32.shape[1])
        while offset < packet_samples:
            if self._buffer is None:
                self._buffer = np.empty(
                    (self.session.n_channels, self.window_samples),
                    dtype=np.float32,
                )
            remaining = self.window_samples - self._filled_samples
            take = min(remaining, packet_samples - offset)
            self._buffer[:, self._filled_samples : self._filled_samples + take] = (
                data_float32[:, offset : offset + take]
            )
            self._filled_samples += take
            offset += take
            assert self._expected_next_sample is not None
            self._expected_next_sample += take
            if self._filled_samples != self.window_samples:
                continue

            assert self._window_start_sample is not None
            completed = DataBlock(
                data=self._buffer,
                start_sample=self._window_start_sample,
                sample_rate_hz=self.session.sample_rate_hz,
                labels=list(self.session.labels),
                events=[],
            )
            self._completed_windows += 1
            self._buffer = None
            self._filled_samples = 0
            self._window_start_sample = self._expected_next_sample
            yield completed

    def mark_accepted_window(self) -> None:
        """Record that the existing bounded processing queue accepted output."""
        self._ensure_open()
        if self._accepted_windows >= self._completed_windows:
            raise RuntimeError("不能接纳尚未由累积器完成的窗口")
        self._accepted_windows += 1

    def snapshot(self) -> EpochAssemblySnapshot:
        if self._final_snapshot is not None:
            return self._final_snapshot
        return EpochAssemblySnapshot(
            window_seconds=self.window_seconds,
            window_samples=self.window_samples,
            received_packets=self._received_packets,
            received_samples=self._received_samples,
            completed_windows=self._completed_windows,
            accepted_windows=self._accepted_windows,
            partial_samples=self._filled_samples,
            partial_start_sample=(
                self._window_start_sample if self._filled_samples else None
            ),
        )

    def finalize(self) -> EpochAssemblySnapshot:
        """Freeze diagnostics and release the residual waveform buffer."""
        if self._final_snapshot is None:
            self._final_snapshot = self.snapshot()
        self._buffer = None
        self._filled_samples = 0
        self._window_start_sample = None
        self._closed = True
        return self._final_snapshot


# A shorter name is convenient for callers and keeps the public contract
# discoverable without changing the fixed 30-second production default.
EpochAssembler = ThirtySecondEpochAssembler
