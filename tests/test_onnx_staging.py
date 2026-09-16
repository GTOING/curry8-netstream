from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import numpy as np
import pytest
from curry_netstream.models import DataBlock

import sleep_stim_controller.onnx_staging as onnx_staging
from sleep_stim_controller.recording import SessionReader
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
from sleep_stim_controller.staging import (
    BlockContext,
    ProcessingPipeline,
    ProcessingStatus,
)


EXPECTED_MODEL_SHA256 = (
    "E256DEF7BA8454314D8D53B5FF2DB5680CFE5237088A19A2FE59052C20ACF943"
)


def make_context(
    *,
    session_id: str = "onnx-test",
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
        session_id=session_id,
        block_id=block_id,
        block=block,
        received_utc="2026-09-16T00:00:00.000Z",
        received_monotonic_ns=time.monotonic_ns(),
    )


def make_multichannel_context(
    *,
    session_id: str,
    block_id: int,
    labels: tuple[str, ...],
    amplitudes: tuple[float, ...],
    start_sample: int = 0,
) -> BlockContext:
    sample_rate_hz = 100.0
    timeline = np.arange(EPOCH_SAMPLES, dtype=np.float64) / sample_rate_hz
    channels = [
        amplitude * np.sin(2 * np.pi * 10.0 * timeline)
        for amplitude in amplitudes
    ]
    block = DataBlock(
        data=np.asarray(channels, dtype=np.float32),
        start_sample=start_sample,
        sample_rate_hz=sample_rate_hz,
        labels=list(labels),
    )
    return BlockContext(
        session_id=session_id,
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
    def __init__(self, input_points: int = 9000, failures=()) -> None:
        self.input_points = input_points
        self.failures = set(failures)
        self.inputs: list[np.ndarray] = []

    def get_inputs(self):
        return [_Meta("input", ["batch_size", 1, self.input_points])]

    def get_outputs(self):
        return [_Meta("output", ["batch_size", 5])]

    def run(self, output_names, feeds):
        self.inputs.append(np.array(feeds["input"], copy=True))
        if len(self.inputs) - 1 in self.failures:
            raise RuntimeError("fixture inference failure")
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


@pytest.mark.parametrize("input_points", [6000, 9000])
@pytest.mark.parametrize("break_kind", ["sample_gap", "sample_rate"])
def test_adapter_discards_old_epoch_context_after_segment_break(
    monkeypatch, tmp_path, input_points, break_kind
) -> None:
    model_path = tmp_path / f"fixture-{input_points}.onnx"
    model_path.write_bytes(b"fixture")
    fake = _FakeSession(input_points)
    monkeypatch.setattr(onnx_staging.ort, "InferenceSession", lambda *a, **k: fake)
    adapter = OnnxSleepStagingAdapter(model_path, "Fpz-Cz")
    cancelled = threading.Event()
    adapter.prepare(cancelled)
    try:
        first = make_context(block_id=1, start_sample=0, amplitude=10.0)
        adapter.predict(first, first.block, cancelled)

        if break_kind == "sample_gap":
            broken = make_context(block_id=2, start_sample=4000, amplitude=40.0)
            resumed_start = 7000
            resumed_rate = 100.0
        else:
            broken = make_context(
                block_id=2,
                start_sample=3000,
                sample_rate_hz=200.0,
                amplitude=40.0,
            )
            resumed_start = 9000
            resumed_rate = 200.0

        with pytest.raises(ValueError, match="不连续|采样率"):
            adapter.predict(broken, broken.block, cancelled)

        resumed = make_context(
            block_id=3,
            start_sample=resumed_start,
            sample_rate_hz=resumed_rate,
            amplitude=80.0,
        )
        following = make_context(
            block_id=4,
            start_sample=resumed_start + round(resumed_rate * 30),
            sample_rate_hz=resumed_rate,
            amplitude=100.0,
        )
        adapter.predict(resumed, resumed.block, cancelled)
        adapter.predict(following, following.block, cancelled)

        old_window = fake.inputs[1][0, 0]
        resumed_window = fake.inputs[-2][0, 0]
        following_window = fake.inputs[-1][0, 0]
        context_epochs = input_points // EPOCH_SAMPLES
        resumed_epochs = resumed_window.reshape(context_epochs, EPOCH_SAMPLES)
        assert all(
            np.array_equal(resumed_epochs[0], epoch) for epoch in resumed_epochs[1:]
        )
        assert not np.array_equal(resumed_epochs[0], old_window[-EPOCH_SAMPLES:])

        following_epochs = following_window.reshape(context_epochs, EPOCH_SAMPLES)
        if context_epochs == 2:
            np.testing.assert_array_equal(following_epochs[0], resumed_epochs[0])
            assert not np.array_equal(following_epochs[1], following_epochs[0])
        else:
            np.testing.assert_array_equal(following_epochs[0], resumed_epochs[0])
            np.testing.assert_array_equal(following_epochs[1], resumed_epochs[0])
            assert not np.array_equal(following_epochs[2], following_epochs[1])
    finally:
        adapter.close(cancelled)


def test_inference_failure_keeps_valid_eeg_epoch_in_context(monkeypatch, tmp_path) -> None:
    model_path = tmp_path / "inference-failure.onnx"
    model_path.write_bytes(b"fixture")
    fake = _FakeSession(6000, failures={2})
    monkeypatch.setattr(onnx_staging.ort, "InferenceSession", lambda *a, **k: fake)
    adapter = OnnxSleepStagingAdapter(model_path, "Fpz-Cz")
    cancelled = threading.Event()
    adapter.prepare(cancelled)
    try:
        first = make_context(block_id=1, start_sample=0, amplitude=10.0)
        failed_inference = make_context(block_id=2, start_sample=3000, amplitude=30.0)
        following = make_context(block_id=3, start_sample=6000, amplitude=60.0)
        adapter.predict(first, first.block, cancelled)
        with pytest.raises(RuntimeError, match="fixture inference failure"):
            adapter.predict(failed_inference, failed_inference.block, cancelled)
        adapter.predict(following, following.block, cancelled)
        failed_epoch = fake.inputs[2][0, 0, -EPOCH_SAMPLES:]
        following_window = fake.inputs[3][0, 0]
        np.testing.assert_array_equal(following_window[:EPOCH_SAMPLES], failed_epoch)
    finally:
        adapter.close(cancelled)


@pytest.mark.parametrize(
    ("channel_name", "expected_index"), [("C3", 0), ("C4", 1)]
)
def test_recorded_model_configuration_uses_selected_channel_and_prepared_shape(
    monkeypatch, tmp_path, channel_name, expected_index
) -> None:
    model_path = tmp_path / "recorded-fixture.onnx"
    model_path.write_bytes(b"fixture")
    fake = _FakeSession(6000)
    monkeypatch.setattr(onnx_staging.ort, "InferenceSession", lambda *a, **k: fake)
    session_id = f"onnx-recording-{channel_name}"
    context = make_multichannel_context(
        session_id=session_id,
        block_id=1,
        labels=("C3", "C4"),
        amplitudes=(15.0, 45.0),
    )
    ready = threading.Event()
    finished = threading.Event()
    results = []
    outcomes = []
    pipeline = ProcessingPipeline(
        session_id=session_id,
        host="127.0.0.1",
        port=4455,
        recording_enabled=True,
        recording_root=tmp_path,
        model_factory=lambda: OnnxSleepStagingAdapter(model_path, channel_name),
        on_ready=lambda error: (assert_no_error(error), ready.set()),
        on_result=results.append,
        on_finished=lambda outcome: (outcomes.append(outcome), finished.set()),
    )
    pipeline.start()
    assert ready.wait(3.0)
    assert pipeline.enqueue(context)
    pipeline.finish(cancelled=False, error=None)
    assert finished.wait(3.0)
    assert results[0].status is ProcessingStatus.SUCCESS

    reader = SessionReader(outcomes[0].session_path)
    assert not reader.overview.incomplete, reader.overview.issues
    configuration = reader.manifest["model"]["configuration"]
    assert configuration["model_content_identity"]["digest"] == sha256_file(model_path)
    assert configuration["input_points"] == 6000
    assert configuration["context_epochs"] == 2
    assert configuration["channel_selection"] == {
        "requested_label": channel_name,
        "match_rule": "trim_whitespace_and_casefold_then_exact_unique_match",
        "resolved_label": channel_name,
        "resolved_index": expected_index,
        "resolved_from_block_id": 1,
    }
    assert configuration["label_mapping"] == [
        {"index": 0, "label": "W"},
        {"index": 1, "label": "N1"},
        {"index": 2, "label": "N2"},
        {"index": 3, "label": "N3"},
        {"index": 4, "label": "REM"},
    ]
    assert configuration["preprocessing"]["input_units"] == "µV"
    assert configuration["preprocessing"]["target_sample_rate_hz"] == 100.0
    assert configuration["preprocessing"]["notch"]["applied_to_observed_input"] is False
    recorded_result = reader.entries[0].processing_result
    assert recorded_result["model"]["configuration"] == configuration


def test_failed_prepare_records_error_without_claiming_model_dimensions(
    monkeypatch, tmp_path
) -> None:
    model_path = tmp_path / "bad-load.onnx"
    model_path.write_bytes(b"fixture")

    def fail_to_load(*_args, **_kwargs):
        raise RuntimeError("fixture model load failure")

    monkeypatch.setattr(onnx_staging.ort, "InferenceSession", fail_to_load)
    session_id = "onnx-prepare-failure"
    context = make_context(session_id=session_id, block_id=1, start_sample=0)
    ready = threading.Event()
    finished = threading.Event()
    outcomes = []
    pipeline = ProcessingPipeline(
        session_id=session_id,
        host="127.0.0.1",
        port=4455,
        recording_enabled=True,
        recording_root=tmp_path,
        model_factory=lambda: OnnxSleepStagingAdapter(model_path, "Fpz-Cz"),
        on_ready=lambda error: (assert_no_error(error), ready.set()),
        on_finished=lambda outcome: (outcomes.append(outcome), finished.set()),
    )
    pipeline.start()
    assert ready.wait(3.0)
    assert pipeline.enqueue(context)
    pipeline.finish(cancelled=False, error=None)
    assert finished.wait(3.0)

    reader = SessionReader(outcomes[0].session_path)
    configuration = reader.manifest["model"]["configuration"]
    assert configuration["preparation"] == {
        "status": "failed",
        "error": "fixture model load failure",
    }
    assert configuration["input_points"] is None
    assert configuration["context_epochs"] is None
    assert reader.entries[0].processing_result["status"] == "failed"
    assert "fixture model load failure" in reader.entries[0].processing_result["reason"]


def assert_no_error(error) -> None:
    assert error is None


def test_onnx_contract_rejects_wrong_time_and_output_shapes() -> None:
    with pytest.raises(ValueError, match="3000"):
        validate_onnx_contract(_FakeSession(4500))

    bad = _FakeSession(3000)
    bad.get_outputs = lambda: [_Meta("output", ["batch_size", 4])]
    with pytest.raises(ValueError, match="5"):
        validate_onnx_contract(bad)
