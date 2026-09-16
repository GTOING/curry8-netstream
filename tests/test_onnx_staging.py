from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import numpy as np
import pytest
from curry_netstream.models import DataBlock

import sleep_stim_controller.onnx_staging as onnx_staging
from sleep_stim_controller.onnx_staging import (
    EPOCH_SAMPLES,
    STAGE_LABELS,
    OnnxSleepStagingAdapter,
    StreamingEegPreprocessor,
    build_context_window,
    default_model_path,
    sha256_file,
    validate_onnx_contract,
)
from sleep_stim_controller.staging import BlockContext


EXPECTED_MODEL_SHA256 = (
    "E256DEF7BA8454314D8D53B5FF2DB5680CFE5237088A19A2FE59052C20ACF943"
)


def make_context(
    *,
    block_id: int = 1,
    start_sample: int = 0,
    sample_rate_hz: float = 100.0,
    channel_name: str = "Fpz-Cz",
    amplitude: float = 20.0,
) -> BlockContext:
    sample_count = round(sample_rate_hz * 30)
    timeline = np.arange(sample_count, dtype=np.float64) / sample_rate_hz
    signal = amplitude * np.sin(2 * np.pi * 10.0 * timeline)
    block = DataBlock(
        data=signal.astype(np.float32)[None, :],
        start_sample=start_sample,
        sample_rate_hz=sample_rate_hz,
        labels=[channel_name],
    )
    return BlockContext(
        session_id="onnx-test",
        block_id=block_id,
        block=block,
        received_utc="2026-09-16T00:00:00.000Z",
        received_monotonic_ns=time.monotonic_ns(),
    )


def test_packaged_model_hash_contract_and_cpu_inference() -> None:
    model_path = default_model_path()
    assert model_path.is_file()
    assert sha256_file(model_path) == EXPECTED_MODEL_SHA256
    adapter = OnnxSleepStagingAdapter(model_path, "Fpz-Cz")
    cancelled = threading.Event()
    adapter.prepare(cancelled)
    try:
        assert adapter.input_points == 6000
        assert adapter.context_epochs == 2
        context = make_context()
        prediction = adapter.predict(context, context.block, cancelled)
        assert prediction.stage in STAGE_LABELS
        assert prediction.confidence is not None
        assert 0.0 <= prediction.confidence <= 1.0
    finally:
        adapter.close(cancelled)


def test_context_window_uses_model_length_and_repeats_earliest() -> None:
    epoch0 = np.full(EPOCH_SAMPLES, 1.0, dtype=np.float32)
    epoch1 = np.full(EPOCH_SAMPLES, 2.0, dtype=np.float32)
    epoch2 = np.full(EPOCH_SAMPLES, 3.0, dtype=np.float32)
    np.testing.assert_array_equal(
        build_context_window([], epoch0, 3000),
        epoch0,
    )
    np.testing.assert_array_equal(
        build_context_window([], epoch0, 6000),
        np.concatenate([epoch0, epoch0]),
    )
    np.testing.assert_array_equal(
        build_context_window([epoch0], epoch1, 9000),
        np.concatenate([epoch0, epoch0, epoch1]),
    )
    np.testing.assert_array_equal(
        build_context_window([epoch0, epoch1], epoch2, 9000),
        np.concatenate([epoch0, epoch1, epoch2]),
    )
    with pytest.raises(ValueError, match="3000"):
        build_context_window([], epoch0, 4500)


def test_preprocessor_selects_exact_channel_preserves_scale_and_resamples() -> None:
    one = StreamingEegPreprocessor("Fpz-Cz")
    two = StreamingEegPreprocessor("Fpz-Cz")
    context_one = make_context(amplitude=10.0)
    context_two = make_context(amplitude=20.0)
    output_one = one.process(context_one, context_one.block)
    output_two = two.process(context_two, context_two.block)
    assert output_one.shape == (3000,)
    assert output_two.shape == (3000,)
    assert output_two.std() == pytest.approx(2.0 * output_one.std(), rel=1e-5)

    resampler = StreamingEegPreprocessor("Fpz-Cz")
    context_200 = make_context(sample_rate_hz=200.0)
    assert resampler.process(context_200, context_200.block).shape == (3000,)

    missing = StreamingEegPreprocessor("Fpz-Cz")
    wrong = make_context(channel_name="C3")
    with pytest.raises(ValueError, match="找不到模型通道"):
        missing.process(wrong, wrong.block)


def test_preprocessor_rejects_discontinuity_and_resets_history() -> None:
    preprocessor = StreamingEegPreprocessor("Fpz-Cz")
    first = make_context(start_sample=0)
    preprocessor.process(first, first.block)
    discontinuous = make_context(block_id=2, start_sample=4000)
    with pytest.raises(ValueError, match="样本不连续"):
        preprocessor.process(discontinuous, discontinuous.block)
    # The failed discontinuity resets state, so a later block can establish a
    # new explicit preprocessing segment rather than using stale filter state.
    assert preprocessor.process(discontinuous, discontinuous.block).shape == (3000,)


@dataclass
class _Meta:
    name: str
    shape: list[object]
    type: str = "tensor(float)"


class _FakeSession:
    def __init__(self, input_points: int = 9000) -> None:
        self.input_points = input_points
        self.inputs: list[np.ndarray] = []

    def get_inputs(self):
        return [_Meta("input", ["batch_size", 1, self.input_points])]

    def get_outputs(self):
        return [_Meta("output", ["batch_size", 5])]

    def run(self, output_names, feeds):
        self.inputs.append(np.array(feeds["input"], copy=True))
        return [np.array([[0.0, 0.0, 5.0, 0.0, 0.0]], dtype=np.float32)]


def test_adapter_uses_detected_9000_context_and_maps_logits(monkeypatch, tmp_path) -> None:
    model_path = tmp_path / "fixture.onnx"
    model_path.write_bytes(b"fixture")
    fake = _FakeSession(9000)
    monkeypatch.setattr(onnx_staging.ort, "InferenceSession", lambda *a, **k: fake)
    adapter = OnnxSleepStagingAdapter(model_path, "Fpz-Cz")
    cancelled = threading.Event()
    adapter.prepare(cancelled)
    first = make_context(block_id=1, start_sample=0, amplitude=10.0)
    second = make_context(block_id=2, start_sample=3000, amplitude=20.0)
    prediction1 = adapter.predict(first, first.block, cancelled)
    prediction2 = adapter.predict(second, second.block, cancelled)
    assert adapter.context_epochs == 3
    assert prediction1.stage == prediction2.stage == "N2"
    assert prediction1.confidence is not None and prediction1.confidence > 0.97
    # call 0 is prepare() warm-up; calls 1 and 2 are the two predictions.
    first_input = fake.inputs[1][0, 0]
    second_input = fake.inputs[2][0, 0]
    assert first_input.shape == (9000,)
    np.testing.assert_array_equal(first_input[:3000], first_input[3000:6000])
    np.testing.assert_array_equal(first_input[3000:6000], first_input[6000:])
    np.testing.assert_array_equal(second_input[:3000], second_input[3000:6000])
    assert not np.array_equal(second_input[3000:6000], second_input[6000:])


def test_onnx_contract_rejects_wrong_time_and_output_shapes() -> None:
    with pytest.raises(ValueError, match="3000"):
        validate_onnx_contract(_FakeSession(4500))

    bad = _FakeSession(3000)
    bad.get_outputs = lambda: [_Meta("output", ["batch_size", 4])]
    with pytest.raises(ValueError, match="5"):
        validate_onnx_contract(bad)
