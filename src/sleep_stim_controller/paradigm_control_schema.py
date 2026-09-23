"""Validation for optional Issue 6 paradigm-control session events."""
from __future__ import annotations

import json
from typing import Mapping


PARADIGM_CONTROL_EVENT_TYPE = "paradigm_control"
PARADIGM_CONTROL_PHASES = {"config", "decision", "sent", "outcome"}


def validate_paradigm_control_event(event: Mapping[str, object]) -> None:
    if not isinstance(event, Mapping) or event.get("event_type") != PARADIGM_CONTROL_EVENT_TYPE:
        raise ValueError("范式控制事件类型无效")
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("范式控制事件 session_id 无效")
    block_id = event.get("block_id")
    if block_id is not None and (
        isinstance(block_id, bool) or not isinstance(block_id, int) or block_id <= 0
    ):
        raise ValueError("范式控制事件 block_id 无效")
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("范式控制事件 payload 必须是对象")
    if payload.get("schema_version") != 1 or isinstance(payload.get("schema_version"), bool):
        raise ValueError("范式控制事件 schema_version 必须为 1")
    if payload.get("phase") not in PARADIGM_CONTROL_PHASES:
        raise ValueError("范式控制事件 phase 无效")
    if payload.get("mode") != "real" or payload.get("profile") != "paradigm":
        raise ValueError("范式控制事件 mode/profile 无效")
    if not isinstance(payload.get("reason"), str) or not payload.get("reason", "").strip():
        raise ValueError("范式控制事件 reason 不能为空")
    for key in ("created_utc",):
        if not isinstance(payload.get(key), str) or not payload.get(key, "").strip():
            raise ValueError(f"范式控制事件 {key} 无效")
    _positive_int(payload.get("created_monotonic_ns"), "created_monotonic_ns")
    if payload.get("physical_output_confirmed") not in {None, False}:
        raise ValueError("软件记录不得声称物理输出已确认")
    desired = payload.get("desired_protocol")
    confirmed = payload.get("api_confirmed_protocol")
    for field, value in (("desired_protocol", desired), ("api_confirmed_protocol", confirmed)):
        if value is not None and value not in {"A", "B", "C"}:
            raise ValueError(f"范式控制事件 {field} 无效")
    phase = payload.get("phase")
    action = payload.get("action")
    if action is not None and action not in {"no_op", "apply", "start", "stop", "fault", "expired"}:
        raise ValueError("范式控制事件 action 无效")
    request_id = event.get("request_id")
    if phase in {"sent", "outcome"}:
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError(f"范式控制 {phase} 事件必须有 request_id")
    elif request_id is not None and (not isinstance(request_id, str) or not request_id.strip()):
        raise ValueError("范式控制事件 request_id 无效")
    if phase == "config":
        snapshot = payload.get("paradigm_snapshot")
        if not isinstance(snapshot, Mapping):
            raise ValueError("范式配置事件必须保存完整 paradigm_snapshot")
        for key in ("paradigm_canonical_json", "paradigm_sha256", "protocols"):
            if key not in snapshot:
                raise ValueError(f"范式配置快照缺少 {key}")
        if not isinstance(snapshot.get("protocols"), list) or len(snapshot["protocols"]) != 3:
            raise ValueError("范式配置快照必须内嵌 A/B/C 协议")
    if phase in {"sent", "outcome"}:
        if payload.get("operation") not in {"start", "apply", "stop"}:
            raise ValueError("范式控制传输 operation 无效")
    response = payload.get("response")
    if response is not None:
        if not isinstance(response, Mapping):
            raise ValueError("范式控制 response 必须是对象")
        source = response.get("source")
        if source is not None:
            if not isinstance(source, Mapping) or source.get("host") != "127.0.0.1":
                raise ValueError("Rally response source 必须是 loopback")
            _port(source.get("port"))
        raw = response.get("bytes_b64")
        if raw is not None and (not isinstance(raw, str) or not raw):
            raise ValueError("范式控制 response 原始字节无效")
    try:
        json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"范式控制事件含不可序列化或非有限值：{exc}") from exc


def _positive_int(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"范式控制事件 {label} 必须为正整数")


def _port(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError("范式控制事件来源端口无效")
