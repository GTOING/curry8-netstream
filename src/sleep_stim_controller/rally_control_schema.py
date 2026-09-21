"""Shared validation for optional P4-B Rally control events."""
from __future__ import annotations

from typing import Mapping


RALLY_CONTROL_EVENT_TYPE = "rally_control"
RALLY_CONTROL_PHASES = {"config", "decision", "sent", "outcome"}
RALLY_CONTROL_COMMANDS = {None, "Start Stim", "Stop Stim"}
RALLY_CONTROL_STATES = {None, "RUNNING", "STOPPED", "UNKNOWN"}
RALLY_CONTROL_RUNTIME_STATES = {
    "DISARMED/UNKNOWN",
    "STOPPED",
    "STARTING",
    "RUNNING",
    "STOPPING",
    "FAULT/UNKNOWN",
}
RALLY_CONTROL_OUTCOMES = {
    "configured",
    "planned",
    "no_op",
    "sent",
    "not_sent",
    "api_success",
    "api_rejected",
    "unknown",
}


def validate_rally_control_event(event: Mapping[str, object]) -> None:
    """Raise ``ValueError`` unless *event* satisfies the shared v1 shape."""

    if not isinstance(event, Mapping) or event.get("event_type") != RALLY_CONTROL_EVENT_TYPE:
        raise ValueError("Rally 控制事件类型无效")
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("Rally 控制事件 session_id 无效")
    block_id = event.get("block_id")
    if block_id is not None and (
        isinstance(block_id, bool) or not isinstance(block_id, int) or block_id <= 0
    ):
        raise ValueError("Rally 控制事件 block_id 无效")
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("Rally 控制事件 payload 必须是对象")
    phase = payload.get("phase")
    if phase not in RALLY_CONTROL_PHASES:
        raise ValueError("Rally 控制事件 phase 无效")
    if payload.get("schema_version") != 1:
        raise ValueError("Rally 控制事件 schema_version 必须为 1")
    if payload.get("mode") != "real":
        raise ValueError("Rally 控制事件 mode 必须为 real")
    command = payload.get("command")
    if command not in RALLY_CONTROL_COMMANDS:
        raise ValueError("Rally 控制事件 command 无效")
    if phase == "config" and command is not None:
        raise ValueError("Rally 控制配置事件 command 必须为 null")
    if phase in {"sent", "outcome"} and command not in {"Start Stim", "Stop Stim"}:
        raise ValueError("Rally 控制决策/传输事件 command 无效")
    request_id = event.get("request_id")
    if phase in {"sent", "outcome"}:
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError(f"Rally 控制 {phase} 事件必须有 request_id")
    elif request_id is not None and (
        not isinstance(request_id, str) or not request_id.strip()
    ):
        raise ValueError("Rally 控制事件 request_id 无效")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Rally 控制事件 reason 不能为空")
    stage = payload.get("stage")
    if stage is not None and stage not in {"W", "N1", "N2", "N3", "REM"}:
        raise ValueError("Rally 控制事件 stage 无效")
    model = payload.get("model")
    if model is not None and not isinstance(model, Mapping):
        raise ValueError("Rally 控制事件 model 必须为对象或 null")
    targets = payload.get("target_stages")
    if not isinstance(targets, (list, tuple)) or any(
        item not in {"W", "N1", "N2", "N3", "REM"} for item in targets
    ):
        raise ValueError("Rally 控制事件 target_stages 无效")
    if payload.get("desired_state") not in RALLY_CONTROL_STATES:
        raise ValueError("Rally 控制事件 desired_state 无效")
    if payload.get("confirmed_state") not in RALLY_CONTROL_STATES:
        raise ValueError("Rally 控制事件 confirmed_state 无效")
    if payload.get("runtime_state") not in RALLY_CONTROL_RUNTIME_STATES:
        raise ValueError("Rally 控制事件 runtime_state 无效")
    _positive_int(payload.get("created_monotonic_ns"), "created_monotonic_ns")
    created_utc = payload.get("created_utc")
    if not isinstance(created_utc, str) or not created_utc.strip():
        raise ValueError("Rally 控制事件 created_utc 无效")
    outcome = payload.get("outcome")
    if outcome not in RALLY_CONTROL_OUTCOMES:
        raise ValueError("Rally 控制事件 outcome 无效")
    for field in ("sent_monotonic_ns", "received_monotonic_ns"):
        value = payload.get(field)
        if value is not None:
            _positive_int(value, field)
    response = payload.get("response")
    if response is not None:
        if not isinstance(response, Mapping):
            raise ValueError("Rally 控制事件 response 必须为对象或 null")
        source = response.get("source")
        if source is not None:
            if not isinstance(source, Mapping):
                raise ValueError("Rally 响应来源必须为对象或 null")
            if source.get("host") != "127.0.0.1":
                raise ValueError("Rally 响应来源必须是 127.0.0.1")
            port = source.get("port")
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError("Rally 响应来源端口无效")
        raw_b64 = response.get("bytes_b64")
        if raw_b64 is not None and (not isinstance(raw_b64, str) or not raw_b64):
            raise ValueError("Rally 原始字节记录无效")


def _positive_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Rally 控制事件 {name} 必须为正整数")
