"""ONNX sleep-staging adapter and continuous EEG preprocessing."""
from __future__ import annotations

import hashlib
import math
import threading
from collections import deque
from fractions import Fraction
from pathlib import Path
from typing import Protocol

import numpy as np
import onnxruntime as ort
from curry_netstream.models import DataBlock
from scipy.signal import (
    butter,
    iirnotch,
    lfilter,
    lfilter_zi,
    resample_poly,
    sosfilt,
    sosfilt_zi,
)

from .staging import (
    BlockContext,
    ModelDescriptor,
    PredictionCancelled,
    StagePrediction,
)


TARGET_SAMPLE_RATE_HZ = 100.0
EPOCH_SECONDS = 30
EPOCH_SAMPLES = int(TARGET_SAMPLE_RATE_HZ * EPOCH_SECONDS)
STAGE_LABELS = ("W", "N1", "N2", "N3", "REM")
DEFAULT_MODEL_FILENAME = "litesleepnet_edf20_fp32_6000.onnx"


def default_model_path() -> Path:
    return Path(__file__).with_name("models") / DEFAULT_MODEL_FILENAME


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def build_context_window(
    history: list[np.ndarray] | tuple[np.ndarray, ...] | deque[np.ndarray],
    current_epoch: np.ndarray,
    input_points: int,
) -> np.ndarray:
    """Build oldest-to-current context, repeating the earliest epoch at start."""
    current = np.asarray(current_epoch, dtype=np.float32)
    if current.shape != (EPOCH_SAMPLES,):
        raise ValueError(
            f"当前 epoch 必须是 ({EPOCH_SAMPLES},)，实际为 {current.shape}"
        )
    if input_points <= 0 or input_points % EPOCH_SAMPLES:
        raise ValueError(
            f"模型时间长度必须是 {EPOCH_SAMPLES} 的正整数倍，实际为 {input_points}"
        )
    required_previous = input_points // EPOCH_SAMPLES - 1
    prior = [np.asarray(item, dtype=np.float32) for item in history]
    if any(item.shape != (EPOCH_SAMPLES,) for item in prior):
        raise ValueError("历史 epoch 形状无效")
    prior = prior[-required_previous:] if required_previous else []
    if len(prior) < required_previous:
        earliest = prior[0] if prior else current
        prior = [earliest] * (required_previous - len(prior)) + prior
    return np.ascontiguousarray(np.concatenate([*prior, current]), dtype=np.float32)


class _SessionInput(Protocol):
    name: str
    shape: list[object]
    type: str


class _SessionOutput(Protocol):
    name: str
    shape: list[object]
    type: str


def validate_onnx_contract(session) -> tuple[str, str, int]:
    inputs: list[_SessionInput] = session.get_inputs()
    outputs: list[_SessionOutput] = session.get_outputs()
    if len(inputs) != 1:
        raise ValueError(f"ONNX 必须只有一个输入，实际为 {len(inputs)}")
    if len(outputs) != 1:
        raise ValueError(f"ONNX 必须只有一个输出，实际为 {len(outputs)}")
    model_input = inputs[0]
    model_output = outputs[0]
    if model_input.type != "tensor(float)":
        raise ValueError(f"ONNX 输入必须是 float32，实际为 {model_input.type}")
    if model_output.type != "tensor(float)":
        raise ValueError(f"ONNX 输出必须是 float32，实际为 {model_output.type}")
    if len(model_input.shape) != 3 or model_input.shape[1] != 1:
        raise ValueError(
            "ONNX 输入必须是 [batch, 1, time]，"
            f"实际为 {model_input.shape}"
        )
    input_points = model_input.shape[2]
    if isinstance(input_points, bool) or not isinstance(input_points, int):
        raise ValueError("ONNX 时间长度必须是固定整数，不能是动态维度")
    if input_points <= 0 or input_points % EPOCH_SAMPLES:
        raise ValueError(
            f"ONNX 时间长度必须是 {EPOCH_SAMPLES} 的正整数倍，"
            f"实际为 {input_points}"
        )
    if len(model_output.shape) != 2 or model_output.shape[1] != len(STAGE_LABELS):
        raise ValueError(
            f"ONNX 输出必须是 [batch, {len(STAGE_LABELS)}]，"
            f"实际为 {model_output.shape}"
        )
    return model_input.name, model_output.name, input_points


class StreamingEegPreprocessor:
    """Select one µV channel, filter continuously, and produce 100-Hz epochs."""

    def __init__(self, channel_name: str) -> None:
        channel = channel_name.strip()
        if not channel:
            raise ValueError("模型通道名不能为空")
        self.channel_name = channel
        self.reset()

    def reset(self) -> None:
        self._source_rate_hz: float | None = None
        self._expected_start_sample: int | None = None
        self._notch_coefficients: tuple[np.ndarray, np.ndarray] | None = None
        self._notch_state: np.ndarray | None = None
        self._bandpass_sos: np.ndarray | None = None
        self._bandpass_state: np.ndarray | None = None

    def _channel_index(self, block: DataBlock) -> int:
        wanted = self.channel_name.casefold()
        matches = [
            index
            for index, label in enumerate(block.labels)
            if str(label).strip().casefold() == wanted
        ]
        if not matches:
            raise ValueError(
                f"找不到模型通道 {self.channel_name!r}；"
                f"可用通道为 {list(block.labels)!r}"
            )
        if len(matches) != 1:
            raise ValueError(f"模型通道 {self.channel_name!r} 在输入中不唯一")
        return matches[0]

    def _prepare_filters(self, source_rate_hz: float, first_value: float) -> None:
        if not math.isfinite(source_rate_hz) or source_rate_hz <= 70.0:
            raise ValueError(
                "源采样率必须高于 70 Hz，才能应用 0.3–35 Hz 带通；"
                f"实际为 {source_rate_hz!r}"
            )
        self._source_rate_hz = source_rate_hz
        # At exactly 100 Hz, 50 Hz is Nyquist and is already excluded by the
        # 35-Hz low-pass edge. Apply the explicit notch whenever it is
        # representable below Nyquist.
        if source_rate_hz > 100.0 + 1e-9:
            b_notch, a_notch = iirnotch(50.0, Q=30.0, fs=source_rate_hz)
            self._notch_coefficients = (b_notch, a_notch)
            self._notch_state = lfilter_zi(b_notch, a_notch) * first_value
        self._bandpass_sos = butter(
            4,
            (0.3, 35.0),
            btype="bandpass",
            fs=source_rate_hz,
            output="sos",
        )
        self._bandpass_state = sosfilt_zi(self._bandpass_sos) * first_value

    def process(self, context: BlockContext, block: DataBlock) -> np.ndarray:
        source_rate_hz = float(block.sample_rate_hz)
        if self._expected_start_sample is not None and (
            context.start_sample != self._expected_start_sample
        ):
            expected = self._expected_start_sample
            self.reset()
            raise ValueError(
                f"模型预处理检测到 EEG 样本不连续：预期 {expected}，"
                f"实际 {context.start_sample}；历史已重置"
            )
        if self._source_rate_hz is not None and not math.isclose(
            source_rate_hz,
            self._source_rate_hz,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            previous = self._source_rate_hz
            self.reset()
            raise ValueError(
                f"模型预处理期间采样率从 {previous:g} 变为 "
                f"{source_rate_hz:g} Hz；历史已重置"
            )

        channel = np.asarray(
            block.data[self._channel_index(block)], dtype=np.float64
        )
        if channel.ndim != 1 or channel.size == 0:
            raise ValueError("模型通道数据必须是一维非空数组")
        expected_source_samples = round(source_rate_hz * EPOCH_SECONDS)
        if channel.size != expected_source_samples:
            raise ValueError(
                f"模型输入必须是完整 {EPOCH_SECONDS} 秒："
                f"实际 {channel.size} 点，预期 {expected_source_samples} 点"
            )
        if not np.isfinite(channel).all():
            raise ValueError("模型通道包含 NaN 或 Inf")

        if self._source_rate_hz is None:
            self._prepare_filters(source_rate_hz, float(channel[0]))

        filtered = channel
        if self._notch_coefficients is not None:
            assert self._notch_state is not None
            filtered, self._notch_state = lfilter(
                *self._notch_coefficients,
                filtered,
                zi=self._notch_state,
            )
        assert self._bandpass_sos is not None
        assert self._bandpass_state is not None
        filtered, self._bandpass_state = sosfilt(
            self._bandpass_sos,
            filtered,
            zi=self._bandpass_state,
        )

        if math.isclose(
            source_rate_hz,
            TARGET_SAMPLE_RATE_HZ,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            epoch = filtered
        else:
            ratio = Fraction(TARGET_SAMPLE_RATE_HZ / source_rate_hz).limit_denominator(
                10000
            )
            represented = source_rate_hz * ratio.numerator / ratio.denominator
            if not math.isclose(
                represented,
                TARGET_SAMPLE_RATE_HZ,
                rel_tol=0.0,
                abs_tol=1e-9,
            ):
                raise ValueError(
                    f"无法精确把 {source_rate_hz:g} Hz 重采样到 "
                    f"{TARGET_SAMPLE_RATE_HZ:g} Hz"
                )
            epoch = resample_poly(filtered, ratio.numerator, ratio.denominator)

        if epoch.shape != (EPOCH_SAMPLES,):
            raise ValueError(
                f"重采样后必须得到 {EPOCH_SAMPLES} 点，实际为 {epoch.shape}"
            )
        if not np.isfinite(epoch).all():
            raise ValueError("模型预处理结果包含 NaN 或 Inf")
        self._expected_start_sample = context.end_sample_exclusive
        # Input is already µV by the user-approved contract. No z-score or
        # silent amplitude normalization is applied here.
        return np.ascontiguousarray(epoch, dtype=np.float32)


class OnnxSleepStagingAdapter:
    """CPU ONNX adapter producing W/N1/N2/N3/REM for the current 30-s block."""

    def __init__(self, model_path: str | Path, channel_name: str) -> None:
        self.model_path = Path(model_path).expanduser().resolve(strict=True)
        self.channel_name = channel_name.strip()
        if not self.channel_name:
            raise ValueError("模型通道名不能为空")
        self.model_sha256 = sha256_file(self.model_path)
        self.descriptor = ModelDescriptor(
            model_id=f"onnx-sleep-staging:{self.model_path.stem}",
            version=f"sha256:{self.model_sha256[:16]}",
            is_test_double=False,
            confidence_meaning="maximum softmax probability over W/N1/N2/N3/REM",
        )
        self._session = None
        self._input_name: str | None = None
        self._output_name: str | None = None
        self._input_points: int | None = None
        self._history: deque[np.ndarray] = deque()
        self._preprocessor = StreamingEegPreprocessor(self.channel_name)

    @property
    def input_points(self) -> int | None:
        return self._input_points

    @property
    def context_epochs(self) -> int | None:
        if self._input_points is None:
            return None
        return self._input_points // EPOCH_SAMPLES

    def prepare(self, cancel_event: threading.Event) -> None:
        if cancel_event.is_set():
            raise PredictionCancelled("模型加载已取消")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        session = ort.InferenceSession(
            str(self.model_path),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        input_name, output_name, input_points = validate_onnx_contract(session)
        warmup = np.zeros((1, 1, input_points), dtype=np.float32)
        output = np.asarray(session.run([output_name], {input_name: warmup})[0])
        if output.shape != (1, len(STAGE_LABELS)) or not np.isfinite(output).all():
            raise ValueError(
                "ONNX 预热输出必须是有限的 "
                f"(1, {len(STAGE_LABELS)})，实际为 {output.shape}"
            )
        self._session = session
        self._input_name = input_name
        self._output_name = output_name
        self._input_points = input_points
        self._history = deque(maxlen=max(0, input_points // EPOCH_SAMPLES - 1))

    def predict(
        self,
        block: BlockContext,
        data: DataBlock,
        cancel_event: threading.Event,
    ) -> StagePrediction:
        if cancel_event.is_set():
            raise PredictionCancelled("模型预测已取消")
        if (
            self._session is None
            or self._input_name is None
            or self._output_name is None
            or self._input_points is None
        ):
            raise RuntimeError("ONNX 模型尚未 prepare")
        epoch = self._preprocessor.process(block, data)
        model_window = build_context_window(
            self._history,
            epoch,
            self._input_points,
        )
        if self._history.maxlen:
            self._history.append(epoch.copy())
        if cancel_event.is_set():
            raise PredictionCancelled("模型预测已取消")
        tensor = np.ascontiguousarray(model_window[None, None, :], dtype=np.float32)
        logits = np.asarray(
            self._session.run(
                [self._output_name],
                {self._input_name: tensor},
            )[0],
            dtype=np.float64,
        )
        if logits.shape != (1, len(STAGE_LABELS)) or not np.isfinite(logits).all():
            raise ValueError(
                f"ONNX 输出必须是有限的 (1, {len(STAGE_LABELS)})，"
                f"实际为 {logits.shape}"
            )
        shifted = logits[0] - float(np.max(logits[0]))
        probabilities = np.exp(shifted)
        probabilities /= probabilities.sum()
        index = int(np.argmax(probabilities))
        return StagePrediction(
            stage=STAGE_LABELS[index],
            confidence=float(probabilities[index]),
        )

    def close(self, cancel_event: threading.Event) -> None:
        self._session = None
        self._history.clear()
        self._preprocessor.reset()
