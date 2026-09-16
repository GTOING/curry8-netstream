from __future__ import annotations

import json
import math
import shutil
import threading
from pathlib import Path

import numpy as np
import pytest

from curry_netstream.models import DataBlock, SessionInfo
from sleep_stim_controller.recording import SessionFormatError, SessionReader
from sleep_stim_controller.staging import (
    BlockContext,
    ModelDescriptor,
    NoModelAdapter,
    PredictionCancelled,
    ProcessingPipeline,
    ProcessingStatus,
    StagePrediction,
    utc_now_iso,
)


def make_context(session_id: str, block_id: int, start_sample: int = 100) -> BlockContext:
    data = np.arange(600, dtype=np.float32).reshape(2, 300) + block_id
    block = DataBlock(
        data=data,
        start_sample=start_sample,
        sample_rate_hz=10.0,
        labels=["C3", "C4"],
    )
    return BlockContext(
        session_id=session_id,
        block_id=block_id,
        block=block,
        received_utc=utc_now_iso(),
        received_monotonic_ns=block_id * 1000,
    )


class AdapterBase:
    descriptor = ModelDescriptor(
        "test-adapter",
        "fixture-1",
        is_test_double=True,
        confidence_meaning="fixture-only score",
    )

    def prepare(self, cancel_event: threading.Event) -> None:
        return None

    def close(self, cancel_event: threading.Event) -> None:
        return None


def start_pipeline(**kwargs):
    ready = threading.Event()
    finished = threading.Event()
    results = []
    ready_errors = []
    outcomes = []
    kwargs.setdefault("on_ready", lambda error: (ready_errors.append(error), ready.set()))
    kwargs.setdefault("on_result", results.append)
    kwargs.setdefault("on_finished", lambda outcome: (outcomes.append(outcome), finished.set()))
    pipeline = ProcessingPipeline(**kwargs)
    pipeline.start()
    assert ready.wait(2)
    return pipeline, ready_errors, results, outcomes, finished


def test_no_model_is_explicit_and_recording_off_creates_no_files(tmp_path: Path) -> None:
    pipeline, ready_errors, results, outcomes, finished = start_pipeline(
        session_id="no-recording",
        host="127.0.0.1",
        port=4455,
        recording_enabled=False,
        recording_root=None,
        model_factory=NoModelAdapter,
    )
    context = make_context("no-recording", 1)
    original = context.block.data.copy()
    assert pipeline.enqueue(context)
    pipeline.finish(cancelled=False, error=None)
    assert finished.wait(2)
    assert ready_errors == [None]
    assert outcomes[0].session_path is None
    assert results[0].status is ProcessingStatus.UNAVAILABLE
    assert results[0].stage is None and results[0].confidence is None
    np.testing.assert_array_equal(context.block.data, original)
    assert list(tmp_path.iterdir()) == []


def test_schema_v1_reader_accepts_model_descriptors_without_configuration(
    tmp_path: Path,
) -> None:
    pipeline, _, _, outcomes, finished = start_pipeline(
        session_id="legacy-model-descriptor",
        host="127.0.0.1",
        port=4455,
        recording_enabled=True,
        recording_root=tmp_path,
        model_factory=NoModelAdapter,
    )
    assert pipeline.enqueue(make_context("legacy-model-descriptor", 1))
    pipeline.finish(cancelled=False, error=None)
    assert finished.wait(2.0)

    session_path = Path(outcomes[0].session_path)
    manifest_path = session_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["model"].pop("configuration", None)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    events_path = session_path / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    result_event = next(event for event in events if event["event_type"] == "processing_result")
    result_event["payload"]["model"].pop("configuration", None)
    events_path.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )

    reader = SessionReader(session_path)
    assert reader.manifest["schema_version"] == 1
    assert not reader.overview.incomplete, reader.overview.issues
    assert reader.entries[0].processing_result["status"] == "unavailable"
    assert "configuration" not in reader.entries[0].processing_result["model"]


def test_injected_model_status_validation_and_raw_block_isolation() -> None:
    class ScriptedAdapter(AdapterBase):
        def __init__(self) -> None:
            self.calls = 0

        def predict(self, block, data, cancel_event):
            self.calls += 1
            if self.calls == 1:
                block.block.data[0, 0] = -12345
                return StagePrediction("N2", 0.75)
            if self.calls == 2:
                raise RuntimeError("fixture failure")
            if self.calls == 3:
                return StagePrediction("AWAKE")
            return StagePrediction("REM", math.inf)

    adapter = ScriptedAdapter()
    pipeline, ready_errors, results, outcomes, finished = start_pipeline(
        session_id="model-tests",
        host="127.0.0.1",
        port=4455,
        recording_enabled=False,
        recording_root=None,
        model_factory=lambda: adapter,
    )
    contexts = [make_context("model-tests", i, 100 + 300 * (i - 1)) for i in range(1, 5)]
    originals = [context.block.data.copy() for context in contexts]
    for context in contexts:
        assert pipeline.enqueue(context)
    pipeline.finish(cancelled=False, error=None)
    assert finished.wait(2)
    assert ready_errors == [None]
    assert [result.status for result in results] == [
        ProcessingStatus.SUCCESS,
        ProcessingStatus.FAILED,
        ProcessingStatus.FAILED,
        ProcessingStatus.FAILED,
    ]
    assert results[0].stage == "N2" and results[0].confidence == 0.75
    assert all(result.stage is None and result.confidence is None for result in results[1:])
    assert all(result.session_id == "model-tests" for result in results)
    assert [result.block_id for result in results] == [1, 2, 3, 4]
    for context, original in zip(contexts, originals, strict=True):
        np.testing.assert_array_equal(context.block.data, original)


def test_cooperative_cancel_saves_accepted_prefix_and_records_cancelled(tmp_path) -> None:
    started = threading.Event()

    class SlowAdapter(AdapterBase):
        def predict(self, block, data, cancel_event):
            started.set()
            if cancel_event.wait(2):
                raise PredictionCancelled("cancel fixture")
            return StagePrediction("W")

    pipeline, _, results, outcomes, finished = start_pipeline(
        session_id="cancel-test",
        host="127.0.0.1",
        port=4455,
        recording_enabled=True,
        recording_root=tmp_path,
        model_factory=SlowAdapter,
        pending_limit=2,
    )
    assert pipeline.enqueue(make_context("cancel-test", 1))
    assert started.wait(2)
    assert pipeline.enqueue(make_context("cancel-test", 2, 400))
    pipeline.finish(cancelled=True, error=None)
    assert finished.wait(2)
    assert [result.status for result in results] == [
        ProcessingStatus.CANCELLED,
        ProcessingStatus.CANCELLED,
    ]
    assert all(result.stage is None and result.confidence is None for result in results)
    assert outcomes[0].cancelled
    reader = SessionReader(pipeline.session_path)
    assert not reader.overview.incomplete
    assert len(reader.entries) == 2
    assert all(
        entry.processing_result["status"] == "cancelled"
        for entry in reader.entries
    )


def test_processing_backlog_is_bounded_and_counted() -> None:
    started = threading.Event()
    release = threading.Event()

    class SlowAdapter(AdapterBase):
        def predict(self, block, data, cancel_event):
            started.set()
            release.wait(2)
            return StagePrediction("N1")

    pipeline, _, results, outcomes, finished = start_pipeline(
        session_id="bounded",
        host="127.0.0.1",
        port=4455,
        recording_enabled=False,
        recording_root=None,
        model_factory=SlowAdapter,
        pending_limit=1,
    )
    assert pipeline.enqueue(make_context("bounded", 1))
    assert started.wait(2)
    assert pipeline.enqueue(make_context("bounded", 2, 400))
    assert not pipeline.enqueue(make_context("bounded", 3, 700))
    pipeline.finish(cancelled=False, error=RuntimeError("queue full"))
    release.set()
    assert finished.wait(2)
    assert outcomes[0].accepted_blocks == 2
    assert outcomes[0].rejected_blocks == 1
    assert outcomes[0].unprocessed_blocks == 0
    assert outcomes[0].error == "queue full"
    assert [result.block_id for result in results] == [1, 2]


def test_storage_failure_preserves_prefix_and_reports_unprocessed(monkeypatch, tmp_path) -> None:
    from sleep_stim_controller.recording import SessionWriter

    def fail_save(self, context):
        raise OSError("simulated disk full")

    monkeypatch.setattr(SessionWriter, "save_block", fail_save)
    fatal = []
    pipeline, ready_errors, results, outcomes, finished = start_pipeline(
        session_id="storage-failure",
        host="127.0.0.1",
        port=4455,
        recording_enabled=True,
        recording_root=tmp_path,
        model_factory=NoModelAdapter,
        on_fatal=fatal.append,
    )
    path = Path(pipeline.session_path)
    assert pipeline.enqueue(make_context("storage-failure", 1))
    pipeline.finish(cancelled=False, error=None)
    assert finished.wait(2)
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["counts"]["accepted_blocks"] == 1
    assert manifest["counts"]["saved_blocks"] == 0
    assert manifest["counts"]["unprocessed_blocks"] == 1
    assert not results
    assert "simulated disk full" in outcomes[0].error
    assert fatal


def make_saved_session(root: Path, *, n_blocks: int = 2) -> Path:
    session_id = "reader-session"
    pipeline, ready_errors, _, outcomes, finished = start_pipeline(
        session_id=session_id,
        host="127.0.0.1",
        port=4455,
        recording_enabled=True,
        recording_root=root,
        model_factory=NoModelAdapter,
    )
    pipeline.set_session_info(SessionInfo(2, 10.0, ["C3", "C4"]))
    originals = []
    for block_id in range(1, n_blocks + 1):
        context = make_context(session_id, block_id, 100 + (block_id - 1) * 300)
        originals.append(context.block.data.copy())
        assert pipeline.enqueue(context)
    pipeline.finish(cancelled=False, error=None)
    assert finished.wait(2)
    assert ready_errors == [None]
    assert outcomes[0].saved_blocks == n_blocks
    return Path(pipeline.session_path)


def test_session_v1_roundtrip_raw_blocks_order_and_results(tmp_path: Path) -> None:
    session_path = make_saved_session(tmp_path)
    reader = SessionReader(session_path)
    assert reader.manifest["schema_version"] == 1
    assert reader.manifest["status"] == "closed"
    assert reader.manifest["recording_enabled"] is True
    assert reader.manifest["handshake"]["units"] == "unknown"
    assert [entry.block_id for entry in reader.entries] == [1, 2]
    assert not reader.overview.incomplete
    for index, entry in enumerate(reader.entries):
        block, found = reader.read_block(index)
        assert found.block_id == entry.block_id
        assert block.start_sample == 100 + 300 * index
        assert block.labels == ["C3", "C4"]
        assert block.sample_rate_hz == 10.0
        assert block.data.dtype == np.float32
        np.testing.assert_array_equal(
            block.data,
            np.arange(600, dtype=np.float32).reshape(2, 300) + (index + 1),
        )
        assert entry.processing_result["status"] == "unavailable"
        assert entry.processing_result["stage"] is None
    events = [
        json.loads(line)
        for line in (session_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert [event["event_type"] for event in events] == [
        "block_saved",
        "processing_result",
        "block_saved",
        "processing_result",
        "session_finished",
    ]


def test_reader_marks_open_prefix_truncation_and_missing_result_incomplete(tmp_path: Path) -> None:
    session_path = make_saved_session(tmp_path)
    truncated = tmp_path / "truncated"
    shutil.copytree(session_path, truncated)
    with (truncated / "events.jsonl").open("ab") as stream:
        stream.write(b'{"sequence":')
    reader = SessionReader(truncated)
    assert reader.overview.incomplete
    assert any("末行截断" in issue for issue in reader.overview.issues)
    assert len(reader.entries) == 2

    open_session = tmp_path / "open-session"
    shutil.copytree(session_path, open_session)
    open_manifest_path = open_session / "manifest.json"
    open_manifest = json.loads(open_manifest_path.read_text(encoding="utf-8"))
    open_manifest["status"] = "recording"
    open_manifest["ended_utc"] = None
    open_manifest_path.write_text(json.dumps(open_manifest), encoding="utf-8")
    open_reader = SessionReader(open_session)
    assert open_reader.overview.incomplete
    assert any("未终结" in issue for issue in open_reader.issues)

    unfinished = tmp_path / "unfinished"
    shutil.copytree(session_path, unfinished)
    all_lines = (unfinished / "events.jsonl").read_bytes().splitlines(keepends=True)
    (unfinished / "events.jsonl").write_bytes(all_lines[0])
    incomplete_reader = SessionReader(unfinished)
    assert incomplete_reader.overview.incomplete
    assert len(incomplete_reader.entries) == 1
    assert incomplete_reader.entries[0].processing_result is None
    assert any("缺少 processing_result" in issue for issue in incomplete_reader.issues)


def test_reader_reports_missing_corrupt_and_escaping_data_files(tmp_path: Path) -> None:
    session_path = make_saved_session(tmp_path)

    missing = tmp_path / "missing"
    shutil.copytree(session_path, missing)
    (missing / "blocks/00000001.npy").unlink()
    missing_reader = SessionReader(missing)
    assert missing_reader.overview.incomplete
    assert "缺失或路径越界" in missing_reader.entries[0].issue
    with pytest.raises(SessionFormatError, match="缺失或路径越界"):
        missing_reader.read_block(0)

    corrupt = tmp_path / "corrupt"
    shutil.copytree(session_path, corrupt)
    (corrupt / "blocks/00000001.npy").write_bytes(b"not an npy file")
    with pytest.raises(SessionFormatError, match="npy 读取失败"):
        SessionReader(corrupt).read_block(0)

    escaping = tmp_path / "escaping"
    shutil.copytree(session_path, escaping)
    event_file = escaping / "events.jsonl"
    events = [json.loads(line) for line in event_file.read_text(encoding="utf-8").splitlines()]
    events[0]["payload"]["path"] = "../outside.npy"
    event_file.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )
    escape_reader = SessionReader(escaping)
    assert escape_reader.overview.incomplete
    assert escape_reader.entries[0].path is None
    with pytest.raises(SessionFormatError, match="路径不是相对路径|缺失或路径越界"):
        escape_reader.read_block(0)


def test_reader_rejects_unsupported_schema_and_malformed_middle_event(tmp_path: Path) -> None:
    session_path = make_saved_session(tmp_path)
    unsupported = tmp_path / "unsupported"
    shutil.copytree(session_path, unsupported)
    manifest_path = unsupported / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 99
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SessionFormatError, match="不支持的会话 schema_version"):
        SessionReader(unsupported)

    malformed = tmp_path / "malformed"
    shutil.copytree(session_path, malformed)
    event_path = malformed / "events.jsonl"
    lines = event_path.read_text(encoding="utf-8").splitlines()
    lines[1] = "{bad json}"
    event_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(SessionFormatError, match="第 2 行损坏"):
        SessionReader(malformed)


def test_p3_event_reservation_overflow_fails_writer_without_hanging(tmp_path) -> None:
    fatal = []
    pipeline, _, _, outcomes, finished = start_pipeline(
        session_id="p3-event-overflow",
        host="127.0.0.1",
        port=4455,
        recording_enabled=True,
        recording_root=tmp_path,
        model_factory=NoModelAdapter,
        session_event_limit=1,
        on_fatal=fatal.append,
    )
    reservation = pipeline.reserve_session_event()
    assert reservation is not None
    assert pipeline.reserve_session_event() is None
    assert any("事件队列已满" in str(error) for error in fatal)
    pipeline.cancel_session_event_reservation(reservation)
    pipeline.finish(cancelled=False, error=None)
    assert finished.wait(2.0)
    assert not pipeline.is_alive()
    assert outcomes[0].error is not None
    assert "事件队列已满" in outcomes[0].error
    reader = SessionReader(pipeline.session_path)
    assert reader.overview.status == "failed"
    assert reader.overview.incomplete
