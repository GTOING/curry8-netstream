from __future__ import annotations

import csv
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from curry_netstream.models import DataBlock
from sleep_stim_controller.recording import (
    STAGE_LABELS_CSV_COLUMNS,
    SessionReader,
    SessionWriter,
    export_stage_labels_csv,
)
from sleep_stim_controller.staging import (
    BlockContext,
    ModelDescriptor,
    NoModelAdapter,
    ProcessingPipeline,
    StagePrediction,
    utc_now_iso,
)


LOCAL_ZONE = timezone(timedelta(hours=5, minutes=30))


def make_context(
    session_id: str,
    block_id: int,
    start_sample: int,
    *,
    local_iso: str | None = None,
    received_utc: str | None = None,
) -> BlockContext:
    block = DataBlock(
        data=np.full((2, 300), block_id, dtype=np.float32),
        start_sample=start_sample,
        sample_rate_hz=10.0,
        labels=["C3", "C4"],
    )
    return BlockContext(
        session_id=session_id,
        block_id=block_id,
        block=block,
        received_utc=received_utc or utc_now_iso(),
        received_monotonic_ns=block_id * 1000,
        window_received_local_iso=local_iso,
    )


def test_auto_csv_tracks_controlled_results_and_atomic_rebuild(tmp_path: Path) -> None:
    class ScriptedAdapter:
        descriptor = ModelDescriptor(
            "fixture-stager",
            "fixture-1",
            is_test_double=True,
            confidence_meaning="synthetic fixture score",
        )

        def prepare(self, _cancel_event) -> None:
            return None

        def predict(self, context, _block, _cancel_event):
            if context.block_id == 1:
                return StagePrediction("N2", 0.75)
            raise RuntimeError("controlled failure, with CSV-sensitive punctuation")

        def close(self, _cancel_event) -> None:
            return None

    session_id = "csv-controlled"
    base_local = datetime(2026, 9, 24, 17, 30, 1, 125000, tzinfo=LOCAL_ZONE)
    base_utc = base_local.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    contexts = [
        make_context(
            session_id,
            1,
            9000,
            local_iso=base_local.isoformat(timespec="milliseconds"),
            received_utc=base_utc,
        ),
        make_context(
            session_id,
            2,
            9300,
            local_iso=(base_local + timedelta(seconds=30)).isoformat(
                timespec="milliseconds"
            ),
            received_utc=(base_local + timedelta(seconds=30))
            .astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
        ),
    ]

    ready = threading.Event()
    finished = threading.Event()
    results = []
    statuses: list[str] = []
    outcomes = []
    pipeline = ProcessingPipeline(
        session_id=session_id,
        host="127.0.0.1",
        port=4455,
        recording_enabled=True,
        recording_root=tmp_path,
        stage_csv_enabled=True,
        model_factory=ScriptedAdapter,
        on_ready=lambda error: (assert_no_error(error), ready.set()),
        on_result=results.append,
        on_stage_csv_status=statuses.append,
        on_finished=lambda outcome: (outcomes.append(outcome), finished.set()),
    )
    pipeline.start()
    assert ready.wait(2.0)
    for context in contexts:
        assert pipeline.enqueue(context)
    pipeline.finish(cancelled=False, error=None)
    assert finished.wait(3.0)

    session_path = Path(outcomes[0].session_path)
    csv_path = session_path / "stage_labels.csv"
    raw_csv = csv_path.read_bytes()
    assert raw_csv.startswith(b"\xef\xbb\xbf")
    with csv_path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        rows = list(reader)
        assert tuple(reader.fieldnames or ()) == STAGE_LABELS_CSV_COLUMNS
    assert [row["block_id"] for row in rows] == ["1", "2"]
    assert [row["start_sample"] for row in rows] == ["9000", "9300"]
    assert [row["end_sample_exclusive"] for row in rows] == ["9300", "9600"]
    assert [float(row["relative_start_s"]) for row in rows] == [0.0, 30.0]
    assert [float(row["relative_end_s"]) for row in rows] == [30.0, 60.0]
    assert rows[0]["sample_rate_hz"] == "10"
    assert rows[0]["window_received_local_iso"] == base_local.isoformat(
        timespec="milliseconds"
    )
    assert rows[0]["received_utc"] == base_utc
    assert rows[0]["local_time_source"] == "captured_at_receive"
    assert (rows[0]["stage"], rows[0]["confidence"], rows[0]["status"]) == (
        "N2",
        "0.75",
        "success",
    )
    assert rows[1]["status"] == "failed"
    assert rows[1]["stage"] == rows[1]["confidence"] == ""
    assert rows[1]["reason"] == (
        "预测失败：controlled failure, with CSV-sensitive punctuation"
    )
    assert [result.status.value for result in results] == ["success", "failed"]
    assert "自动 CSV 已写入 2 行" in statuses[-2]
    assert "自动 CSV 已写入 2 行" in statuses[-1]
    assert outcomes[0].error is None

    session_reader = SessionReader(session_path)
    assert [entry.start_sample for entry in session_reader.entries] == [9000, 9300]
    assert [entry.processing_result["status"] for entry in session_reader.entries] == [
        "success",
        "failed",
    ]
    for index, entry in enumerate(session_reader.entries):
        block, _ = session_reader.read_block(index)
        assert block.start_sample == int(rows[index]["start_sample"])
        assert block.start_sample + block.n_samples == int(rows[index]["end_sample_exclusive"])
        assert entry.processing_result["status"] == rows[index]["status"]

    events_before = (session_path / "events.jsonl").read_bytes()
    arrays_before = [(session_path / f"blocks/{index:08d}.npy").read_bytes() for index in (1, 2)]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as corrupted:
        stale_writer = csv.writer(corrupted)
        stale_writer.writerow(STAGE_LABELS_CSV_COLUMNS)
        stale_writer.writerow([rows[0][column] for column in STAGE_LABELS_CSV_COLUMNS])
        stale_writer.writerow([rows[0][column] for column in STAGE_LABELS_CSV_COLUMNS])
        corrupted.write("half,row,from,a,crash")
    destination, exported_rows = export_stage_labels_csv(session_reader)
    assert destination == csv_path
    assert exported_rows == 2
    with csv_path.open(encoding="utf-8-sig", newline="") as source:
        rebuilt = list(csv.DictReader(source))
    assert [row["block_id"] for row in rebuilt] == ["1", "2"]
    assert [float(row["relative_start_s"]) for row in rebuilt] == [0.0, 30.0]
    assert [float(row["relative_end_s"]) for row in rebuilt] == [30.0, 60.0]
    assert (session_path / "events.jsonl").read_bytes() == events_before
    assert [(session_path / f"blocks/{index:08d}.npy").read_bytes() for index in (1, 2)] == arrays_before


def test_old_v1_missing_local_time_exports_verified_prefix_only(tmp_path: Path) -> None:
    session_id = "legacy-csv"
    ready = threading.Event()
    finished = threading.Event()
    outcomes = []
    pipeline = ProcessingPipeline(
        session_id=session_id,
        host="127.0.0.1",
        port=4455,
        recording_enabled=True,
        recording_root=tmp_path,
        model_factory=NoModelAdapter,
        on_ready=lambda error: (assert_no_error(error), ready.set()),
        on_finished=lambda outcome: (outcomes.append(outcome), finished.set()),
    )
    pipeline.start()
    assert ready.wait(2.0)
    assert pipeline.enqueue(make_context(session_id, 1, 2500))
    assert pipeline.enqueue(make_context(session_id, 2, 2800))
    pipeline.finish(cancelled=False, error=None)
    assert finished.wait(3.0)
    session_path = Path(outcomes[0].session_path)

    event_path = session_path / "events.jsonl"
    events = [json.loads(line) for line in event_path.read_text("utf-8").splitlines()]
    second_result_seen = False
    prefix_events = []
    for event in events:
        if event["event_type"] == "processing_result" and event["block_id"] == 2:
            second_result_seen = True
        if not second_result_seen:
            prefix_events.append(event)
    event_path.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in prefix_events),
        encoding="utf-8",
    )
    manifest_path = session_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["counts"]["processing_results"] = 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    reader = SessionReader(session_path)
    assert reader.overview.incomplete
    destination, row_count = export_stage_labels_csv(reader)
    assert row_count == 1
    assert destination == session_path / "stage_labels.csv"
    with destination.open(encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    assert len(rows) == 1
    assert rows[0]["start_sample"] == "2500"
    assert rows[0]["end_sample_exclusive"] == "2800"
    assert rows[0]["relative_start_s"] == "0"
    assert rows[0]["relative_end_s"] == "30"
    assert rows[0]["status"] == "unavailable"
    assert rows[0]["stage"] == rows[0]["confidence"] == ""
    assert rows[0]["window_received_local_iso"] == ""
    assert rows[0]["local_time_source"] == "unavailable"
    assert rows[0]["received_utc"] == reader.entries[0].received_utc


def test_csv_failure_is_nonfatal_and_preserves_processing_result(
    monkeypatch, tmp_path: Path
) -> None:
    def fail_append(_writer, _result) -> None:
        raise OSError("injected CSV disk failure")

    monkeypatch.setattr(SessionWriter, "_append_stage_csv_result", fail_append)
    ready = threading.Event()
    finished = threading.Event()
    statuses: list[str] = []
    results = []
    outcomes = []
    pipeline = ProcessingPipeline(
        session_id="csv-write-failure",
        host="127.0.0.1",
        port=4455,
        recording_enabled=True,
        recording_root=tmp_path,
        stage_csv_enabled=True,
        model_factory=NoModelAdapter,
        on_ready=lambda error: (assert_no_error(error), ready.set()),
        on_result=results.append,
        on_stage_csv_status=statuses.append,
        on_finished=lambda outcome: (outcomes.append(outcome), finished.set()),
    )
    pipeline.start()
    assert ready.wait(2.0)
    assert pipeline.enqueue(make_context("csv-write-failure", 1, 800))
    pipeline.finish(cancelled=False, error=None)
    assert finished.wait(3.0)

    session_path = Path(outcomes[0].session_path)
    reader = SessionReader(session_path)
    assert outcomes[0].error is None
    assert results[0].status.value == "unavailable"
    assert reader.entries[0].processing_result["status"] == "unavailable"
    assert (session_path / "stage_labels.csv").read_text("utf-8-sig").splitlines() == [
        ",".join(STAGE_LABELS_CSV_COLUMNS)
    ]
    manifest = json.loads((session_path / "manifest.json").read_text("utf-8"))
    assert manifest["stage_csv"]["status"] == "failed"
    assert "injected CSV disk failure" in manifest["stage_csv"]["error"]
    assert any("JSONL/NPY 仍继续记录" in status for status in statuses)


def assert_no_error(error) -> None:
    assert error is None
