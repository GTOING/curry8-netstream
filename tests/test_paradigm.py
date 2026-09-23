from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from sleep_stim_controller.paradigm import (
    ParadigmRuntimeManager,
    ParadigmValidationError,
    load_paradigm_package,
)
from sleep_stim_controller.recording import SessionReader, SessionWriter
from sleep_stim_controller.staging import ModelDescriptor


FIXTURE = Path(__file__).parent / "fixtures" / "issue6_paradigm_test_only"


def _copy_package(tmp_path: Path) -> Path:
    target = tmp_path / "package"
    shutil.copytree(FIXTURE, target)
    return target


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=True), encoding="utf-8")


def test_synthetic_package_is_complete_canonical_and_test_only() -> None:
    snapshot = load_paradigm_package(FIXTURE)
    assert snapshot.classification == "synthetic/test-only"
    assert snapshot.real_armable is False
    assert snapshot.stage_mapping == {
        "W": "A",
        "N1": "B",
        "N2": "C",
        "N3": None,
        "REM": None,
    }
    assert snapshot.protocol_for_stage("W").key == "A"
    assert snapshot.protocol_for_stage("N3") is None
    assert snapshot.sha256 == load_paradigm_package(FIXTURE).sha256
    assert len(snapshot.sha256) == 64
    assert "TEST_RETURN" in snapshot.protocols[0].return_channels
    assert "\"SD\":101" in snapshot.protocols[0].payload_json


def test_snapshot_freezes_protocol_content_after_disk_change(tmp_path: Path) -> None:
    package = _copy_package(tmp_path)
    snapshot = load_paradigm_package(package)
    manager = ParadigmRuntimeManager()
    manager.select(snapshot)
    manager.begin_session()
    original = manager.session_snapshot.protocol_map["A"].payload_json

    document = _read(package / "protocols" / "A.json")
    document["payload"]["SD"] = 33
    _write(package / "protocols" / "A.json", document)
    assert manager.session_snapshot.protocol_map["A"].payload_json == original
    assert load_paradigm_package(package).sha256 != snapshot.sha256
    with pytest.raises(RuntimeError, match="冻结"):
        manager.select(load_paradigm_package(package))


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda root: (root / "protocols" / "B.json").unlink(), "缺失或路径越界"),
        (
            lambda root: _set_manifest(root, "A", "../outside.json"),
            "越界",
        ),
        (
            lambda root: _set_protocol(root, lambda d: d["initial_stimulus_channels"].append("TEST_STIM_A")),
            "重复",
        ),
        (
            lambda root: _set_protocol(root, lambda d: d["payload"]["CHS"][0].update({"A": float("inf")})),
            "非有限数",
        ),
        (
            lambda root: _set_protocol(root, lambda d: d["payload"]["CHS"][0].update({"N": "TEST_RETURN"})),
            "不属于|返回通道",
        ),
        (
            lambda root: _set_manifest_schema(root, 4),
            "schema_version",
        ),
    ],
)
def test_invalid_package_is_rejected(tmp_path: Path, mutate, message: str) -> None:
    package = _copy_package(tmp_path)
    mutate(package)
    with pytest.raises(ParadigmValidationError, match=message):
        load_paradigm_package(package)


def test_missing_sd_conversion_loads_but_can_never_arm_real(tmp_path: Path) -> None:
    package = _copy_package(tmp_path)
    path = package / "protocols" / "B.json"
    document = _read(path)
    document.pop("sd_conversion")
    _write(path, document)
    snapshot = load_paradigm_package(package)
    assert snapshot.real_armable is False
    assert snapshot.protocol_map["B"].can_estimate_expiry is False


def test_json_duplicate_keys_are_rejected(tmp_path: Path) -> None:
    package = _copy_package(tmp_path)
    path = package / "paradigm.json"
    raw = path.read_text(encoding="utf-8")
    raw = raw.replace('"version": "test-only-1",', '"version": "test-only-1",\n  "version": "shadow",')
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ParadigmValidationError, match="重复 JSON 字段"):
        load_paradigm_package(package)


def test_session_writer_reader_roundtrip_keeps_full_paradigm_snapshot(tmp_path: Path) -> None:
    snapshot = load_paradigm_package(FIXTURE)
    writer = SessionWriter.create(
        tmp_path,
        session_id="paradigm-roundtrip",
        host="127.0.0.1",
        port=4455,
        descriptor=ModelDescriptor("fixture", "test-only"),
    )
    writer.append_extension_event(
        {
            "event_type": "paradigm_control",
            "session_id": "paradigm-roundtrip",
            "block_id": None,
            "payload": {
                "schema_version": 1,
                "phase": "config",
                "mode": "real",
                "profile": "paradigm",
                "reason": "test-only snapshot",
                "operator_confirmed": True,
                "paradigm_snapshot": snapshot.to_snapshot(),
                "model_input_channels": list(snapshot.model_input_channels),
                "required_rally_base_protocol": snapshot.required_rally_base_protocol,
                "desired_protocol": None,
                "api_confirmed_protocol": None,
                "desired_state": "STOPPED",
                "confirmed_state": None,
                "runtime_state": "ARMED/IDLE",
                "physical_output_confirmed": False,
                "created_utc": "2026-09-23T00:00:00Z",
                "created_monotonic_ns": 1,
                "outcome": "configured",
            },
        }
    )
    writer.append_extension_event(
        {
            "event_type": "paradigm_control",
            "session_id": "paradigm-roundtrip",
            "block_id": None,
            "payload": {
                "schema_version": 1,
                "phase": "decision",
                "mode": "real",
                "profile": "paradigm",
                "reason": "synthetic no-op decision",
                "action": "no_op",
                "desired_protocol": "A",
                "api_confirmed_protocol": "A",
                "desired_state": "RUNNING",
                "confirmed_state": "RUNNING",
                "runtime_state": "RUNNING",
                "physical_output_confirmed": False,
                "created_utc": "2026-09-23T00:00:01Z",
                "created_monotonic_ns": 2,
                "outcome": "no_op",
            },
        }
    )
    writer.finish(
        status="closed",
        reason="fixture",
        accepted_blocks=0,
        rejected_blocks=0,
        completed_results=0,
        unprocessed_blocks=0,
    )
    reader = SessionReader(writer.path)
    assert len(reader.control_events) == 2
    saved = reader.control_events[0]["payload"]["paradigm_snapshot"]
    assert saved["paradigm_sha256"] == snapshot.sha256
    assert len(saved["protocols"]) == 3
    assert all(protocol["protocol_canonical_json"] for protocol in saved["protocols"])
    assert reader.control_events[1]["payload"]["action"] == "no_op"


def test_paradigm_events_cannot_claim_physical_output_confirmation(tmp_path: Path) -> None:
    writer = SessionWriter.create(
        tmp_path,
        session_id="paradigm-physical-claim",
        host="127.0.0.1",
        port=4455,
        descriptor=ModelDescriptor("fixture", "test-only"),
    )
    event = {
        "event_type": "paradigm_control",
        "session_id": "paradigm-physical-claim",
        "block_id": None,
        "payload": {
            "schema_version": 1,
            "phase": "decision",
            "mode": "real",
            "profile": "paradigm",
            "reason": "invalid claim",
            "desired_protocol": None,
            "api_confirmed_protocol": None,
            "physical_output_confirmed": True,
            "created_utc": "2026-09-23T00:00:00Z",
            "created_monotonic_ns": 1,
        },
    }
    with pytest.raises(ValueError, match="物理输出已确认"):
        writer.append_extension_event(event)
    writer.finish(
        status="closed",
        reason="fixture",
        accepted_blocks=0,
        rejected_blocks=0,
        completed_results=0,
        unprocessed_blocks=0,
    )


def _set_manifest(root: Path, key: str, value: str) -> None:
    path = root / "paradigm.json"
    document = _read(path)
    document["protocols"][key] = value
    _write(path, document)


def _set_protocol(root: Path, update) -> None:
    path = root / "protocols" / "A.json"
    document = _read(path)
    update(document)
    _write(path, document)


def _set_manifest_schema(root: Path, value: int) -> None:
    path = root / "paradigm.json"
    document = _read(path)
    document["schema_version"] = value
    _write(path, document)
