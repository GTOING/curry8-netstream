"""Versioned per-session writer and read-only, prefix-tolerant replay reader."""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from curry_netstream.models import DataBlock, SessionInfo

from .staging import (
    STAGES,
    BlockContext,
    ModelDescriptor,
    ProcessingResult,
    utc_now_iso,
)
from .rally_control_schema import validate_rally_control_event
from .paradigm_control_schema import validate_paradigm_control_event


SCHEMA_VERSION = 1


class SessionFormatError(ValueError):
    """A recording is unsupported or structurally corrupt."""


class SessionWriter:
    """Single-thread-owned append writer for one live session."""

    def __init__(
        self,
        path: Path,
        *,
        session_id: str,
        host: str,
        port: int,
        descriptor: ModelDescriptor,
    ) -> None:
        self.path = path
        self.session_id = session_id
        self._sequence = 0
        self._saved_blocks = 0
        self._processing_results = 0
        self._manifest: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "source": "live",
            "created_utc": utc_now_iso(),
            "ended_utc": None,
            "status": "recording",
            "recording_enabled": True,
            "connection": {"host": host, "port": port},
            "handshake": None,
            "model": descriptor.to_dict(),
            "application_version": "0.1.0",
            "configuration_snapshot": {"host": host, "port": port},
            "end_reason": None,
            "counts": {
                "accepted_blocks": 0,
                "saved_blocks": 0,
                "processing_results": 0,
                "rejected_blocks": 0,
                "unprocessed_blocks": 0,
            },
        }
        self._write_manifest()
        self._events = (path / "events.jsonl").open("a", encoding="utf-8", newline="\n")

    @classmethod
    def create(
        cls,
        root: str | Path,
        *,
        session_id: str,
        host: str,
        port: int,
        descriptor: ModelDescriptor,
    ) -> "SessionWriter":
        parent = Path(root).expanduser()
        if not parent.is_dir():
            raise FileNotFoundError(f"保存父目录不存在或不是目录：{parent}")
        path = parent / f"session_{session_id}"
        path.mkdir(exist_ok=False)
        (path / "blocks").mkdir()
        return cls(
            path,
            session_id=session_id,
            host=host,
            port=port,
            descriptor=descriptor,
        )

    def set_model(self, descriptor: ModelDescriptor) -> None:
        self._manifest["model"] = descriptor.to_dict()
        self._write_manifest()

    def update_handshake(self, info: SessionInfo) -> None:
        self._manifest["handshake"] = {
            "n_channels": int(info.n_channels),
            "sample_rate_hz": float(info.sample_rate_hz),
            "labels": [str(label) for label in info.labels],
            "units": "unknown",
        }
        self._write_manifest()

    def save_block(self, context: BlockContext) -> None:
        block = context.block
        data = np.asarray(block.data)
        self._manifest["counts"]["accepted_blocks"] = max(
            int(self._manifest["counts"]["accepted_blocks"]), context.block_id
        )
        self._write_manifest()
        relative_path = f"blocks/{context.block_id:08d}.npy"
        destination = self.path / relative_path
        temporary = destination.with_name(
            f".{destination.name}.tmp-{uuid.uuid4().hex}"
        )
        with temporary.open("xb") as output:
            np.save(output, data, allow_pickle=False)
            output.flush()
        os.replace(temporary, destination)
        payload = {
            "path": relative_path,
            "start_sample": context.start_sample,
            "end_sample_exclusive": context.end_sample_exclusive,
            "received_utc": context.received_utc,
            "received_monotonic_ns": context.received_monotonic_ns,
            "labels": [str(label) for label in block.labels],
            "sample_rate_hz": float(block.sample_rate_hz),
            "shape": list(data.shape),
            "dtype": data.dtype.str,
            "units": "unknown",
        }
        self._append_event(
            "block_saved",
            block_id=context.block_id,
            utc_timestamp=context.received_utc,
            monotonic_ns=context.received_monotonic_ns,
            payload=payload,
        )
        self._saved_blocks += 1
        self._manifest["counts"]["saved_blocks"] = self._saved_blocks
        self._write_manifest()

    def save_processing_result(self, result: ProcessingResult) -> None:
        self._append_event(
            "processing_result",
            block_id=result.block_id,
            utc_timestamp=utc_now_iso(),
            monotonic_ns=result.finished_monotonic_ns,
            payload=result.to_dict(),
        )
        self._processing_results += 1
        self._manifest["counts"]["processing_results"] = self._processing_results
        self._write_manifest()

    def append_extension_event(self, event: dict[str, object]) -> None:
        """Append one optional P3 event through this existing single writer."""
        event_type = event.get("event_type")
        session_id = event.get("session_id")
        block_id = event.get("block_id")
        request_id = event.get("request_id")
        payload = event.get("payload")
        if event_type == "rally_control":
            validate_rally_control_event(event)
        elif event_type == "paradigm_control":
            validate_paradigm_control_event(event)
        elif event_type not in {
            "stimulation_config",
            "decision",
            "request_sent",
            "request_outcome",
        }:
            raise ValueError(f"不支持的附加会话事件：{event_type!r}")
        if session_id != self.session_id:
            raise ValueError("附加会话事件 session_id 不匹配")
        if block_id is not None and (
            isinstance(block_id, bool) or not isinstance(block_id, int) or block_id <= 0
        ):
            raise ValueError("附加会话事件 block_id 无效")
        if request_id is not None and (
            not isinstance(request_id, str) or not request_id.strip()
        ):
            raise ValueError("附加会话事件 request_id 无效")
        if not isinstance(payload, dict):
            raise ValueError("附加会话事件 payload 必须是对象")
        if event_type == "stimulation_config" and block_id is not None:
            raise ValueError("stimulation_config 不应关联 EEG block")
        if event_type not in {"stimulation_config", "rally_control", "paradigm_control"} and block_id is None:
            raise ValueError(f"{event_type} 必须关联 EEG block")
        try:
            json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"附加会话事件 payload 不可序列化：{exc}") from exc
        self._append_event(
            str(event_type),
            block_id=block_id,
            utc_timestamp=utc_now_iso(),
            monotonic_ns=time.monotonic_ns(),
            payload=payload,
            request_id=request_id,
        )

    def finish(
        self,
        *,
        status: str,
        reason: str,
        accepted_blocks: int,
        rejected_blocks: int,
        completed_results: int,
        unprocessed_blocks: int,
        stream_assembly: dict[str, object] | None = None,
    ) -> None:
        if status not in {"closed", "failed"}:
            raise ValueError(f"unsupported final session status: {status}")
        finish_payload: dict[str, object] = {"status": status, "reason": reason}
        if stream_assembly is not None:
            try:
                json.dumps(stream_assembly, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"stream_assembly 摘要不可序列化：{exc}") from exc
            finish_payload["stream_assembly"] = dict(stream_assembly)
        try:
            self._append_event(
                "session_finished",
                block_id=None,
                utc_timestamp=utc_now_iso(),
                monotonic_ns=None,
                payload=finish_payload,
            )
            self._events.flush()
        finally:
            self._events.close()
        self._manifest.update(
            {
                "status": status,
                "ended_utc": utc_now_iso(),
                "end_reason": reason,
                "counts": {
                    "accepted_blocks": accepted_blocks,
                    "saved_blocks": self._saved_blocks,
                    "processing_results": completed_results,
                    "rejected_blocks": rejected_blocks,
                    "unprocessed_blocks": unprocessed_blocks,
                },
            }
        )
        self._write_manifest()

    def _append_event(
        self,
        event_type: str,
        *,
        block_id: int | None,
        utc_timestamp: str,
        monotonic_ns: int | None,
        payload: dict[str, object],
        request_id: str | None = None,
    ) -> None:
        self._sequence += 1
        event = {
            "sequence": self._sequence,
            "event_type": event_type,
            "session_id": self.session_id,
            "block_id": block_id,
            "utc_timestamp": utc_timestamp,
            "monotonic_ns": monotonic_ns,
            "payload": payload,
        }
        if request_id is not None:
            event["request_id"] = request_id
        self._events.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
        self._events.flush()

    def _write_manifest(self) -> None:
        temporary = self.path / f".manifest.{uuid.uuid4().hex}.tmp"
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(
                self._manifest,
                output,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
            )
            output.write("\n")
            output.flush()
        os.replace(temporary, self.path / "manifest.json")


@dataclass(frozen=True, slots=True)
class ReplayBlockEntry:
    block_id: int
    path: Path | None
    start_sample: int
    end_sample_exclusive: int
    received_utc: str
    received_monotonic_ns: int
    labels: tuple[str, ...]
    sample_rate_hz: float
    shape: tuple[int, ...]
    dtype: str
    processing_result: dict[str, Any] | None
    issue: str | None = None
    stimulation_events: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ReplayOverview:
    path: str
    session_id: str
    status: str
    block_count: int
    incomplete: bool
    issues: tuple[str, ...]
    control_events: tuple[dict[str, Any], ...] = ()


class SessionReader:
    """Read only event-indexed, verified blocks without loading a full session."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve(strict=True)
        if not self.path.is_dir():
            raise SessionFormatError("所选路径不是会话目录")
        try:
            manifest = json.loads((self.path / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SessionFormatError(f"无法读取 manifest.json：{exc}") from exc
        if not isinstance(manifest, dict):
            raise SessionFormatError("manifest.json 必须是 JSON 对象")
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise SessionFormatError(
                f"不支持的会话 schema_version：{manifest.get('schema_version')!r}"
            )
        session_id = manifest.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise SessionFormatError("manifest 缺少有效 session_id")
        if manifest.get("source") != "live" or manifest.get("recording_enabled") is not True:
            raise SessionFormatError("manifest 来源或 recording_enabled 标记无效")
        self.manifest = manifest
        self.session_finished_payload: dict[str, Any] | None = None
        self.control_events: tuple[dict[str, Any], ...] = ()
        self.issues: list[str] = []
        self.incomplete = manifest.get("status") == "recording"
        if manifest.get("status") not in {"recording", "closed", "failed"}:
            self.incomplete = True
            self.issues.append(f"未知会话状态：{manifest.get('status')!r}")
        elif manifest.get("status") == "recording":
            self.issues.append("manifest 未终结；会话可能被中断")
        elif manifest.get("status") == "failed":
            self.incomplete = True
            self.issues.append(f"会话以失败状态结束：{manifest.get('end_reason') or '原因未知'}")
        if manifest.get("status") in {"closed", "failed"} and not manifest.get("ended_utc"):
            self.incomplete = True
            self.issues.append("最终状态缺少 ended_utc")
        if not isinstance(manifest.get("counts"), dict):
            self.incomplete = True
            self.issues.append("manifest 缺少有效计数")
        self.entries = self._read_event_index()
        self.overview = ReplayOverview(
            path=str(self.path),
            session_id=session_id,
            status=str(manifest.get("status", "unknown")),
            block_count=len(self.entries),
            incomplete=self.incomplete,
            issues=tuple(self.issues),
            control_events=self.control_events,
        )

    def _read_event_index(self) -> tuple[ReplayBlockEntry, ...]:
        events_path = self.path / "events.jsonl"
        try:
            raw = events_path.read_bytes()
        except OSError as exc:
            raise SessionFormatError(f"无法读取 events.jsonl：{exc}") from exc
        lines = raw.splitlines(keepends=True)
        if lines and not lines[-1].endswith((b"\n", b"\r")):
            self.incomplete = True
            self.issues.append("events.jsonl 末行截断；仅显示此前完整事件")
            lines = lines[:-1]
        saved: dict[int, ReplayBlockEntry] = {}
        results: dict[int, dict[str, Any]] = {}
        stimulation_events: dict[int, list[dict[str, Any]]] = {}
        stimulation_configs: dict[int, dict[str, Any]] = {}
        control_events: list[dict[str, Any]] = []
        decisions_by_request: dict[str, dict[str, Any]] = {}
        sent_requests: set[str] = set()
        request_outcomes: set[str] = set()
        decision_ids: set[str] = set()
        saved_before_result: set[int] = set()
        finish_payload: dict[str, Any] | None = None
        expected_sequence = 1
        last_saved_block_id = 0
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SessionFormatError(
                    f"events.jsonl 第 {line_number} 行损坏："
                    f"{getattr(exc, 'msg', str(exc))}"
                ) from exc
            if not isinstance(event, dict):
                raise SessionFormatError(f"events.jsonl 第 {line_number} 行不是对象")
            sequence = event.get("sequence")
            if (
                not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or sequence != expected_sequence
            ):
                raise SessionFormatError(
                    f"events.jsonl 第 {line_number} 行序号错误："
                    f"预期 {expected_sequence}，实际 {event.get('sequence')!r}"
                )
            expected_sequence += 1
            if event.get("session_id") != self.manifest.get("session_id"):
                raise SessionFormatError(f"events.jsonl 第 {line_number} 行 session_id 不匹配")
            event_type = event.get("event_type")
            if finish_payload is not None:
                raise SessionFormatError(
                    f"events.jsonl 第 {line_number} 行出现在 session_finished 之后"
                )
            block_id = event.get("block_id")
            payload = event.get("payload")
            if event_type in {"rally_control", "paradigm_control"}:
                try:
                    if event_type == "rally_control":
                        validate_rally_control_event(event)
                    else:
                        validate_paradigm_control_event(event)
                except ValueError as exc:
                    raise SessionFormatError(
                        f"events.jsonl 第 {line_number} 行控制事件无效：{exc}"
                    ) from exc
                if event.get("session_id") != self.manifest.get("session_id"):
                    raise SessionFormatError(
                        f"events.jsonl 第 {line_number} 行 Rally 控制事件 session_id 不匹配"
                    )
                event_copy = dict(event)
                control_events.append(event_copy)
                if isinstance(event.get("block_id"), int):
                    stimulation_events.setdefault(int(event["block_id"]), []).append(event_copy)
                continue
            if event_type == "block_saved":
                if (
                    not isinstance(block_id, int)
                    or isinstance(block_id, bool)
                    or block_id <= 0
                    or not isinstance(payload, dict)
                ):
                    raise SessionFormatError(f"events.jsonl 第 {line_number} 行 block_saved 字段无效")
                if block_id in saved:
                    raise SessionFormatError(f"events.jsonl 第 {line_number} 行重复 block_id={block_id}")
                if block_id <= last_saved_block_id:
                    raise SessionFormatError(
                        f"events.jsonl 第 {line_number} 行 block_id 未递增"
                    )
                last_saved_block_id = block_id
                saved[block_id] = self._entry_from_event(block_id, payload, line_number)
                saved_before_result.add(block_id)
            elif event_type == "processing_result":
                if (
                    not isinstance(block_id, int)
                    or isinstance(block_id, bool)
                    or block_id <= 0
                    or not isinstance(payload, dict)
                ):
                    raise SessionFormatError(f"events.jsonl 第 {line_number} 行 processing_result 字段无效")
                if payload.get("session_id") != self.manifest.get("session_id") or payload.get("block_id") != block_id:
                    raise SessionFormatError(f"events.jsonl 第 {line_number} 行处理结果关联不匹配")
                if block_id not in saved_before_result:
                    raise SessionFormatError(
                        f"events.jsonl 第 {line_number} 行处理结果早于对应 block_saved"
                    )
                if block_id in results:
                    raise SessionFormatError(f"events.jsonl 第 {line_number} 行重复处理结果 block_id={block_id}")
                status = payload.get("status")
                stage = payload.get("stage")
                confidence = payload.get("confidence")
                if status not in {"unavailable", "success", "failed", "cancelled"}:
                    raise SessionFormatError(f"events.jsonl 第 {line_number} 行处理状态无效")
                if status == "success":
                    if stage not in STAGES:
                        raise SessionFormatError(f"events.jsonl 第 {line_number} 行睡眠期无效")
                    if confidence is not None:
                        if isinstance(confidence, bool):
                            raise SessionFormatError(
                                f"events.jsonl 第 {line_number} 行 confidence 不能是布尔值"
                            )
                        try:
                            confidence_value = float(confidence)
                        except (TypeError, ValueError) as exc:
                            raise SessionFormatError(
                                f"events.jsonl 第 {line_number} 行 confidence 无效"
                            ) from exc
                        if not np.isfinite(confidence_value) or not 0 <= confidence_value <= 1:
                            raise SessionFormatError(
                                f"events.jsonl 第 {line_number} 行 confidence 越界"
                            )
                        model = payload.get("model")
                        if (
                            not isinstance(model, dict)
                            or not isinstance(model.get("confidence_meaning"), str)
                            or not model["confidence_meaning"].strip()
                        ):
                            raise SessionFormatError(
                                f"events.jsonl 第 {line_number} 行缺少 confidence 语义说明"
                            )
                elif stage is not None or confidence is not None:
                    raise SessionFormatError(
                        f"events.jsonl 第 {line_number} 行非成功结果不能包含期别/置信度"
                    )
                results[block_id] = payload
            elif event_type in {
                "stimulation_config",
                "decision",
                "request_sent",
                "request_outcome",
            }:
                request_id = event.get("request_id")
                if not isinstance(payload, dict):
                    raise SessionFormatError(
                        f"events.jsonl 第 {line_number} 行 {event_type} payload 无效"
                    )
                if event_type == "stimulation_config":
                    version = payload.get("config_version")
                    if (
                        block_id is not None
                        or request_id is not None
                        or isinstance(version, bool)
                        or not isinstance(version, int)
                        or version <= 0
                    ):
                        raise SessionFormatError(
                            f"events.jsonl 第 {line_number} 行刺激配置快照字段无效"
                        )
                    if version in stimulation_configs:
                        raise SessionFormatError(
                            f"events.jsonl 第 {line_number} 行重复刺激配置版本 {version}"
                        )
                    stimulation_configs[version] = payload
                else:
                    if (
                        not isinstance(block_id, int)
                        or isinstance(block_id, bool)
                        or block_id <= 0
                        or block_id not in saved
                        or block_id not in results
                    ):
                        raise SessionFormatError(
                            f"events.jsonl 第 {line_number} 行 {event_type} 未关联已记录处理块"
                        )
                    version = payload.get("config_version")
                    if (
                        isinstance(version, bool)
                        or not isinstance(version, int)
                        or version <= 0
                    ):
                        raise SessionFormatError(
                            f"events.jsonl 第 {line_number} 行 {event_type} 配置版本无效"
                        )
                    if version not in stimulation_configs:
                        raise SessionFormatError(
                            f"events.jsonl 第 {line_number} 行 {event_type} 缺少先行配置快照"
                        )
                    if event_type == "decision":
                        decision_id = payload.get("decision_id")
                        allowed = payload.get("allowed")
                        reason = payload.get("reason")
                        model = payload.get("model")
                        result = results[block_id]
                        block = saved[block_id]
                        if (
                            not isinstance(decision_id, str)
                            or not decision_id
                            or decision_id in decision_ids
                            or not isinstance(allowed, bool)
                            or not isinstance(reason, str)
                            or not reason.strip()
                            or not isinstance(model, dict)
                            or payload.get("stage") != result.get("stage")
                            or isinstance(payload.get("start_sample"), bool)
                            or not isinstance(payload.get("start_sample"), int)
                            or payload.get("start_sample") != block.start_sample
                            or isinstance(payload.get("end_sample_exclusive"), bool)
                            or not isinstance(payload.get("end_sample_exclusive"), int)
                            or payload.get("end_sample_exclusive")
                            != block.end_sample_exclusive
                            or payload.get("test_double")
                            is not bool(model.get("is_test_double", False))
                        ):
                            raise SessionFormatError(
                                f"events.jsonl 第 {line_number} 行决策事件字段/块关联无效"
                            )
                        if allowed and result.get("status") != "success":
                            raise SessionFormatError(
                                f"events.jsonl 第 {line_number} 行非成功分期不能允许请求"
                            )
                        if allowed:
                            if not isinstance(request_id, str) or not request_id.strip():
                                raise SessionFormatError(
                                    f"events.jsonl 第 {line_number} 行允许决策缺少 request_id"
                                )
                            if request_id in decisions_by_request:
                                raise SessionFormatError(
                                    f"events.jsonl 第 {line_number} 行重复 request_id"
                                )
                            decisions_by_request[request_id] = {
                                "block_id": block_id,
                                "config_version": version,
                                "decision_id": decision_id,
                                "stage": payload.get("stage"),
                                "test_double": payload.get("test_double"),
                            }
                        elif request_id is not None:
                            raise SessionFormatError(
                                f"events.jsonl 第 {line_number} 行抑制决策不应含 request_id"
                            )
                        decision_ids.add(decision_id)
                    elif event_type == "request_sent":
                        if not isinstance(request_id, str) or not request_id.strip():
                            raise SessionFormatError(
                                f"events.jsonl 第 {line_number} 行 request_sent request_id 无效"
                            )
                        decision = decisions_by_request.get(request_id)
                        if (
                            decision is None
                            or decision["block_id"] != block_id
                            or decision["config_version"] != version
                            or payload.get("status") != "waiting_response"
                            or not isinstance(payload.get("sent_payload"), dict)
                            or payload.get("stage") != decision["stage"]
                            or payload.get("test_double") is not decision["test_double"]
                            or request_id in sent_requests
                        ):
                            raise SessionFormatError(
                                f"events.jsonl 第 {line_number} 行 request_sent 无匹配决策"
                            )
                        sent_requests.add(request_id)
                    else:
                        if not isinstance(request_id, str) or not request_id.strip():
                            raise SessionFormatError(
                                f"events.jsonl 第 {line_number} 行 request_outcome request_id 无效"
                            )
                        decision = decisions_by_request.get(request_id)
                        status_value = payload.get("status")
                        send_attempted = payload.get("send_attempted")
                        if (
                            decision is None
                            or decision["block_id"] != block_id
                            or decision["config_version"] != version
                            or status_value
                            not in {"not_sent", "api_success", "api_rejected", "unknown"}
                            or request_id in request_outcomes
                            or not isinstance(payload.get("message"), str)
                            or not isinstance(send_attempted, bool)
                        ):
                            raise SessionFormatError(
                                f"events.jsonl 第 {line_number} 行 request_outcome 无匹配决策"
                            )
                        if status_value in {"api_success", "api_rejected"} and request_id not in sent_requests:
                            raise SessionFormatError(
                                f"events.jsonl 第 {line_number} 行 API 结果前缺少 request_sent"
                            )
                        if status_value == "not_sent" and (
                            request_id in sent_requests or send_attempted
                        ):
                            raise SessionFormatError(
                                f"events.jsonl 第 {line_number} 行已发送请求不能标记为未发送"
                            )
                        if (
                            status_value == "unknown"
                            and request_id not in sent_requests
                            and not send_attempted
                        ):
                            raise SessionFormatError(
                                f"events.jsonl 第 {line_number} 行未知结果缺少发送证据"
                            )
                        request_outcomes.add(request_id)
                    stimulation_events.setdefault(block_id, []).append(event)
            elif event_type == "session_finished":
                if finish_payload is not None or block_id is not None or not isinstance(payload, dict):
                    raise SessionFormatError(
                        f"events.jsonl 第 {line_number} 行 session_finished 字段无效"
                    )
                finish_payload = payload
        entries: list[ReplayBlockEntry] = []
        for block_id, entry in sorted(saved.items()):
            result = results.get(block_id)
            if result is None:
                self.incomplete = True
                self.issues.append(f"block_id={block_id} 缺少 processing_result")
            entries.append(
                ReplayBlockEntry(
                    block_id=entry.block_id,
                    path=entry.path,
                    start_sample=entry.start_sample,
                    end_sample_exclusive=entry.end_sample_exclusive,
                    received_utc=entry.received_utc,
                    received_monotonic_ns=entry.received_monotonic_ns,
                    labels=entry.labels,
                    sample_rate_hz=entry.sample_rate_hz,
                    shape=entry.shape,
                    dtype=entry.dtype,
                    processing_result=result,
                    issue=entry.issue,
                    stimulation_events=tuple(stimulation_events.get(block_id, ())),
                )
            )
        unfinished_requests = set(decisions_by_request) - request_outcomes
        if unfinished_requests:
            self.incomplete = True
            self.issues.append(
                "存在未收口的刺激请求：" + ", ".join(sorted(unfinished_requests)[:3])
            )
        status = self.manifest.get("status")
        if status in {"closed", "failed"} and finish_payload is None:
            self.incomplete = True
            self.issues.append("缺少 session_finished 终态事件")
        elif finish_payload is not None and finish_payload.get("status") != status:
            self.incomplete = True
            self.issues.append("manifest 状态与 session_finished 事件不一致")
        counts = self.manifest.get("counts")
        if isinstance(counts, dict):
            if counts.get("saved_blocks") != len(saved):
                self.incomplete = True
                self.issues.append("manifest saved_blocks 计数与事件索引不一致")
            if counts.get("processing_results") != len(results):
                self.incomplete = True
                self.issues.append("manifest processing_results 计数与事件索引不一致")
        self.session_finished_payload = finish_payload
        self.control_events = tuple(control_events)
        return tuple(entries)

    def _entry_from_event(
        self, block_id: int, payload: dict[str, Any], line_number: int
    ) -> ReplayBlockEntry:
        relative = payload.get("path")
        issue: str | None = None
        block_path: Path | None = None
        if not isinstance(relative, str):
            issue = f"events.jsonl 第 {line_number} 行数据路径无效"
        else:
            candidate = Path(relative)
            if candidate.is_absolute():
                issue = f"events.jsonl 第 {line_number} 行数据路径不是相对路径"
            else:
                try:
                    resolved = (self.path / candidate).resolve(strict=True)
                    resolved.relative_to(self.path)
                    if not resolved.is_file():
                        raise OSError("不是普通文件")
                    block_path = resolved
                except (OSError, RuntimeError, ValueError):
                    issue = f"block_id={block_id} 数据文件缺失或路径越界：{relative}"
        try:
            start = int(payload["start_sample"])
            end = int(payload["end_sample_exclusive"])
            sample_rate = float(payload["sample_rate_hz"])
            labels = tuple(str(item) for item in payload["labels"])
            shape = tuple(int(value) for value in payload["shape"])
            dtype = str(payload["dtype"])
            received_utc = str(payload["received_utc"])
            received_ns = int(payload["received_monotonic_ns"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionFormatError(
                f"events.jsonl 第 {line_number} 行块元数据无效：{exc}"
            ) from exc
        if len(shape) != 2 or shape[0] != len(labels) or end - start != shape[1]:
            raise SessionFormatError(f"events.jsonl 第 {line_number} 行块形状/样本区间不匹配")
        if not np.isfinite(sample_rate) or sample_rate <= 0:
            raise SessionFormatError(f"events.jsonl 第 {line_number} 行采样率无效")
        if block_path is None:
            self.incomplete = True
            self.issues.append(issue or f"block_id={block_id} 数据文件不可读")
        return ReplayBlockEntry(
            block_id=block_id,
            path=block_path,
            start_sample=start,
            end_sample_exclusive=end,
            received_utc=received_utc,
            received_monotonic_ns=received_ns,
            labels=labels,
            sample_rate_hz=sample_rate,
            shape=shape,
            dtype=dtype,
            processing_result=None,
            issue=issue,
        )

    def read_block(self, index: int) -> tuple[DataBlock, ReplayBlockEntry]:
        if not 0 <= index < len(self.entries):
            raise IndexError("回放块索引越界")
        entry = self.entries[index]
        if entry.issue is not None or entry.path is None:
            raise SessionFormatError(entry.issue or f"block_id={entry.block_id} 无有效数据路径")
        try:
            data = np.load(entry.path, allow_pickle=False)
        except (OSError, ValueError) as exc:
            raise SessionFormatError(f"block_id={entry.block_id} .npy 读取失败：{exc}") from exc
        if not isinstance(data, np.ndarray) or data.dtype.str != entry.dtype:
            raise SessionFormatError(f"block_id={entry.block_id} dtype 与事件记录不匹配")
        if tuple(data.shape) != entry.shape:
            raise SessionFormatError(f"block_id={entry.block_id} shape 与事件记录不匹配")
        if data.dtype.hasobject:
            raise SessionFormatError(f"block_id={entry.block_id} 禁止 object dtype")
        return (
            DataBlock(
                data=data,
                start_sample=entry.start_sample,
                sample_rate_hz=entry.sample_rate_hz,
                labels=list(entry.labels),
            ),
            entry,
        )
