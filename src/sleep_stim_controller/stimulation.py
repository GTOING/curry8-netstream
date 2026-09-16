"""Pure P3 configuration, policy decisions and Rally wire-format helpers.

Nothing in this module opens sockets or chooses experimental parameters. A
protocol scheme must be explicitly loaded from a user-selected local JSON file.
"""
from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping

from .staging import ProcessingResult, ProcessingStatus, STAGES


REALTIME_CONTROL_TOKEN = "RealTimeControl"
PROTOCOL_SCHEMA_VERSION = 1


class ProtocolValidationError(ValueError):
    """A selected local protocol scheme is incomplete or unsafe to serialize."""


class TriggerMode(StrEnum):
    EACH_MATCHING_BLOCK = "each_matching_block"
    ENTER_TARGET_SET = "enter_target_set"


class RequestStatus(StrEnum):
    NOT_SENT = "not_sent"
    WAITING_RESPONSE = "waiting_response"
    API_SUCCESS = "api_success"
    API_REJECTED = "api_rejected"
    UNKNOWN = "unknown"


RALLY_ERROR_MESSAGES: dict[str, str] = {
    "RALLY_ERROR_SUCCESS": "实时调控 API 报告成功（不证明实际刺激发生）",
    "RALLY_ERROR_INVALID_CHANNEL": "指定通道不在原协议刺激通道中",
    "RALLY_ERROR_INVALID_STATUS": "设备不处于刺激中状态",
    "RALLY_ERROR_INVALID_TDCS_PARAMETER": "tDCS 参数非法",
    "RALLY_ERROR_INVALID_TACS_PARAMETER": "tACS 参数非法",
    "RALLY_ERROR_INVALID_TOTAL_AMPTITUDE": "组合幅值超过接口阈值",
    "RALLY_ERROR_INVALID_RETURN_CHANNEL_SETTING": "接口拒绝修改返回通道",
    "RALLY_ERROR_INVALID_DEVICE_TYPE": "接口报告设备类型不匹配",
    "RALLY_ERROR_STOP_STIM": "接口报告停止刺激失败",
    "RALLY_ERROR_SEND_PROTOCOL": "接口报告协议下发失败",
    "RALLY_ERROR_START_STIM": "接口报告开始刺激失败",
    "RALLY_ERROR_INVALID_STIM_DURATION": "刺激时长非法",
    "RALLY_ERROR_INVALID_FLAT_RISING": "渐升时长非法",
    "RALLY_ERROR_INVALID_FLAT_DECLINE": "渐降时长非法",
    "RALLY_ERROR_INVALID_ORIGIN_PROTOCOL": "原始刺激协议非法",
    "RALLY_ERROR_INVALID_FIELD_STIM_TYPE": "刺激类型字段非法",
    "RALLY_ERROR_INVALID_FIELD_AMPITUDE": "幅值字段非法",
    "RALLY_ERROR_INVALID_FIELD_PHASE": "相位字段非法",
    "RALLY_ERROR_INVALID_FIELD_FREQUENCY": "频率字段非法",
    "RALLY_ERROR_INVALID_FIELD_DCBIAS": "直流偏置字段非法",
    "RALLY_ERROR_INVALID_FIELD_ELECTROD": "电极字段非法",
    "RALLY_ERROR_EXCEPTION_OCCURED": "接口处理异常",
    "RALLY_ERROR_TOTAL_AMPLITUDE_OVER_THRESHOLD": "用户幅值超过接口阈值",
    "RALLY_ERROR_INTERNAL_ERROR": "接口内部错误",
}


@dataclass(frozen=True, slots=True)
class ProtocolScheme:
    name: str
    protocol_version: str
    initial_stimulus_channels: tuple[str, ...]
    return_channels: tuple[str, ...]
    payload_json: str
    source_path: str | None = None

    @property
    def payload(self) -> dict[str, Any]:
        """Return a fresh payload so callers cannot mutate the validated copy."""
        value = json.loads(self.payload_json)
        assert isinstance(value, dict)
        return value

    @property
    def channel_names(self) -> tuple[str, ...]:
        return tuple(channel["N"] for channel in self.payload["CHS"])

    def to_snapshot(self) -> dict[str, object]:
        return {
            "name": self.name,
            "protocol_version": self.protocol_version,
            "initial_stimulus_channels": list(self.initial_stimulus_channels),
            "return_channels": list(self.return_channels),
            "payload": self.payload,
        }


def _finite_number(value: object, field: str) -> float:
    if not _is_finite_number(value):
        raise ProtocolValidationError(f"{field} 必须是有限数值")
    return float(value)


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def parse_protocol_scheme(
    document: Mapping[str, object], *, source_path: str | None = None
) -> ProtocolScheme:
    """Validate the explicit P3 local JSON scheme; never invent defaults."""
    if not isinstance(document, Mapping):
        raise ProtocolValidationError("方案根节点必须是 JSON 对象")
    schema_version = document.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != PROTOCOL_SCHEMA_VERSION
    ):
        raise ProtocolValidationError(
            f"不支持的方案 schema_version：{document.get('schema_version')!r}"
        )
    name = document.get("name")
    version = document.get("protocol_version")
    if not isinstance(name, str) or not name.strip():
        raise ProtocolValidationError("方案 name 不能为空")
    if not isinstance(version, str) or not version.strip():
        raise ProtocolValidationError("protocol_version 不能为空")

    def channel_list(key: str, *, required: bool) -> tuple[str, ...]:
        value = document.get(key)
        if value is None and not required:
            return ()
        if not isinstance(value, list) or (required and not value):
            raise ProtocolValidationError(f"{key} 必须是非空通道列表")
        channels: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ProtocolValidationError(f"{key} 含空白或非字符串通道名")
            channel = item.strip()
            if channel in channels:
                raise ProtocolValidationError(f"{key} 通道名重复：{channel}")
            channels.append(channel)
        return tuple(channels)

    initial = channel_list("initial_stimulus_channels", required=True)
    returns = channel_list("return_channels", required=False)
    if len(initial) > 8:
        raise ProtocolValidationError("模拟初始协议最多包含 8 个刺激通道")
    if set(initial) & set(returns):
        raise ProtocolValidationError("初始刺激通道与返回通道不能重叠")

    payload = document.get("payload")
    if not isinstance(payload, Mapping):
        raise ProtocolValidationError("payload 必须是 JSON 对象")
    allowed_top = {"SD", "FR", "FD", "CHS"}
    if any(not isinstance(key, str) for key in payload):
        raise ProtocolValidationError("payload 字段名必须是字符串")
    if set(payload) != allowed_top:
        missing = sorted(allowed_top - set(payload))
        extra = sorted(set(payload) - allowed_top)
        detail = []
        if missing:
            detail.append("缺少 " + ", ".join(missing))
        if extra:
            detail.append("不支持字段 " + ", ".join(extra))
        raise ProtocolValidationError("payload 字段无效：" + "；".join(detail))
    duration = _finite_number(payload["SD"], "SD")
    rise = _finite_number(payload["FR"], "FR")
    fall = _finite_number(payload["FD"], "FD")
    if duration <= 0:
        raise ProtocolValidationError("SD 必须大于 0 秒")
    if rise < 0 or fall < 0 or rise + fall > duration:
        raise ProtocolValidationError("FR/FD 须 >=0 且 FR+FD<=SD")

    raw_channels = payload["CHS"]
    if not isinstance(raw_channels, list) or not raw_channels:
        raise ProtocolValidationError("payload.CHS 必须是非空列表")
    if len(raw_channels) > 8:
        raise ProtocolValidationError("payload.CHS 最多包含 8 个刺激通道")
    seen: set[str] = set()
    normalized_channels: list[dict[str, object]] = []
    for index, raw_channel in enumerate(raw_channels):
        prefix = f"CHS[{index}]"
        if not isinstance(raw_channel, Mapping):
            raise ProtocolValidationError(f"{prefix} 必须是对象")
        if any(not isinstance(key, str) for key in raw_channel):
            raise ProtocolValidationError(f"{prefix} 字段名必须是字符串")
        channel_name = raw_channel.get("N")
        stim_type = raw_channel.get("T")
        if not isinstance(channel_name, str) or not channel_name.strip():
            raise ProtocolValidationError(f"{prefix}.N 不能为空")
        channel_name = channel_name.strip()
        if channel_name in seen:
            raise ProtocolValidationError(f"CHS 通道名重复：{channel_name}")
        seen.add(channel_name)
        if channel_name not in initial:
            raise ProtocolValidationError(
                f"通道 {channel_name} 不属于明确配置的初始刺激通道集合"
            )
        if channel_name in returns:
            raise ProtocolValidationError(f"返回通道不可修改：{channel_name}")
        if not isinstance(stim_type, str) or stim_type not in {"tD", "tA"}:
            raise ProtocolValidationError(f"{prefix}.T 仅支持 tD/tA")
        required_fields = {"N", "T", "A"}
        if stim_type == "tA":
            required_fields |= {"F", "P", "D"}
        if set(raw_channel) != required_fields:
            missing = sorted(required_fields - set(raw_channel))
            extra = sorted(set(raw_channel) - required_fields)
            detail = []
            if missing:
                detail.append("缺少 " + ", ".join(missing))
            if extra:
                detail.append("不支持字段 " + ", ".join(extra))
            raise ProtocolValidationError(f"{prefix} 字段无效：" + "；".join(detail))
        normalized: dict[str, object] = {"N": channel_name, "T": stim_type}
        for field in sorted(required_fields - {"N", "T"}):
            number = _finite_number(raw_channel[field], f"{prefix}.{field}")
            if field == "A" and number < 0:
                raise ProtocolValidationError(f"{prefix}.A 必须 >=0 μA")
            if field == "F" and number < 0:
                raise ProtocolValidationError(f"{prefix}.F 必须 >=0 Hz")
            normalized[field] = raw_channel[field]
        normalized_channels.append(normalized)

    normalized_payload: dict[str, object] = {
        "SD": payload["SD"],
        "FR": payload["FR"],
        "FD": payload["FD"],
        "CHS": normalized_channels,
    }
    try:
        payload_json = json.dumps(
            normalized_payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ProtocolValidationError(f"payload 无法编码为 JSON：{exc}") from exc
    return ProtocolScheme(
        name=name.strip(),
        protocol_version=version.strip(),
        initial_stimulus_channels=initial,
        return_channels=returns,
        payload_json=payload_json,
        source_path=source_path,
    )


def load_protocol_scheme(path: str | Path) -> ProtocolScheme:
    selected = Path(path).expanduser()
    try:
        document = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolValidationError(f"无法读取协议 JSON：{exc}") from exc
    if not isinstance(document, dict):
        raise ProtocolValidationError("方案根节点必须是 JSON 对象")
    return parse_protocol_scheme(document, source_path=str(selected.resolve()))


def build_rally_message(scheme: ProtocolScheme) -> bytes:
    """Construct the vendor wire message without opening a socket."""
    body = json.dumps(
        scheme.payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return f"{REALTIME_CONTROL_TOKEN} {body}".encode("utf-8")


@dataclass(frozen=True, slots=True)
class ParsedRallyResponse:
    status: RequestStatus
    code: str | None
    message: str
    raw_text: str | None


def parse_rally_response(data: bytes) -> ParsedRallyResponse:
    try:
        response = data.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError:
        return ParsedRallyResponse(
            RequestStatus.UNKNOWN,
            None,
            "响应不是有效 UTF-8，结果未知",
            None,
        )
    if response == "RALLY_ERROR_SUCCESS":
        return ParsedRallyResponse(
            RequestStatus.API_SUCCESS,
            response,
            RALLY_ERROR_MESSAGES[response],
            response,
        )
    if response in RALLY_ERROR_MESSAGES:
        return ParsedRallyResponse(
            RequestStatus.API_REJECTED,
            response,
            RALLY_ERROR_MESSAGES[response],
            response,
        )
    return ParsedRallyResponse(
        RequestStatus.UNKNOWN,
        response or None,
        f"无法解释 Rally 响应，结果未知：{response or '<empty>'}",
        response or None,
    )


@dataclass(frozen=True, slots=True)
class StimulationConfig:
    target_stages: frozenset[str] = frozenset()
    trigger_mode: TriggerMode | None = None
    min_request_interval_seconds: float | None = None
    max_result_age_seconds: float | None = None
    protocol: ProtocolScheme | None = None
    config_version: int = 1

    def issues(self) -> tuple[str, ...]:
        problems: list[str] = []
        if not isinstance(self.target_stages, (set, frozenset)) or any(
            not isinstance(stage, str) for stage in self.target_stages
        ):
            problems.append("目标睡眠期集合格式无效")
            target_stages: set[str] = set()
        else:
            target_stages = set(self.target_stages)
        unknown = target_stages - set(STAGES)
        if unknown:
            problems.append("含未知睡眠期：" + ", ".join(sorted(unknown)))
        if not target_stages:
            problems.append("尚未选择目标睡眠期")
        if self.trigger_mode is None:
            problems.append("触发策略尚未选择")
        elif not isinstance(self.trigger_mode, TriggerMode):
            problems.append("触发策略值无效")
        if self.min_request_interval_seconds is None:
            problems.append("最小请求间隔尚未配置")
        elif not _is_finite_number(self.min_request_interval_seconds) or (
            float(self.min_request_interval_seconds) < 0
        ):
            problems.append("最小请求间隔须为有限数值且 >=0 秒")
        if self.max_result_age_seconds is None:
            problems.append("最大结果年龄尚未配置")
        elif not _is_finite_number(self.max_result_age_seconds) or (
            float(self.max_result_age_seconds) <= 0
        ):
            problems.append("最大结果年龄须为有限数值且 >0 秒")
        if self.protocol is None:
            problems.append("尚未选择经过校验的本地模拟方案")
        elif not isinstance(self.protocol, ProtocolScheme):
            problems.append("本地模拟方案类型无效")
        if (
            isinstance(self.config_version, bool)
            or not isinstance(self.config_version, int)
            or self.config_version <= 0
        ):
            problems.append("配置版本无效")
        return tuple(problems)

    def to_snapshot(self, *, automatic_enabled: bool) -> dict[str, object]:
        return {
            "config_version": self.config_version,
            "automatic_enabled": automatic_enabled,
            "target_stages": [
                stage
                for stage in STAGES
                if isinstance(self.target_stages, (set, frozenset))
                and stage in self.target_stages
            ],
            "trigger_mode": (
                self.trigger_mode.value
                if isinstance(self.trigger_mode, TriggerMode)
                else None
            ),
            "min_request_interval_seconds": (
                float(self.min_request_interval_seconds)
                if _is_finite_number(self.min_request_interval_seconds)
                else None
            ),
            "max_result_age_seconds": (
                float(self.max_result_age_seconds)
                if _is_finite_number(self.max_result_age_seconds)
                else None
            ),
            "protocol": (
                self.protocol.to_snapshot()
                if isinstance(self.protocol, ProtocolScheme)
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class StimulationDecision:
    decision_id: str
    session_id: str
    block_id: int
    config_version: int
    stage: str | None
    model: dict[str, object]
    start_sample: int
    end_sample_exclusive: int
    received_monotonic_ns: int | None
    decided_monotonic_ns: int
    allowed: bool
    reason: str
    request_id: str | None = None
    test_double: bool = False

    def to_event(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "decision_id": self.decision_id,
            "config_version": self.config_version,
            "stage": self.stage,
            "model": self.model,
            "start_sample": self.start_sample,
            "end_sample_exclusive": self.end_sample_exclusive,
            "received_monotonic_ns": self.received_monotonic_ns,
            "decided_monotonic_ns": self.decided_monotonic_ns,
            "allowed": self.allowed,
            "reason": self.reason,
            "test_double": self.test_double,
        }
        return {
            "event_type": "decision",
            "session_id": self.session_id,
            "block_id": self.block_id,
            "request_id": self.request_id,
            "payload": payload,
        }


@dataclass(frozen=True, slots=True)
class StimulusRequest:
    request_id: str
    decision: StimulationDecision
    protocol: ProtocolScheme
    received_monotonic_ns: int
    max_result_age_seconds: float

    @property
    def session_id(self) -> str:
        return self.decision.session_id

    @property
    def block_id(self) -> int:
        return self.decision.block_id

    @property
    def config_version(self) -> int:
        return self.decision.config_version

    def to_sent_event(self) -> dict[str, object]:
        return {
            "event_type": "request_sent",
            "session_id": self.session_id,
            "block_id": self.block_id,
            "request_id": self.request_id,
            "payload": {
                "config_version": self.config_version,
                "status": RequestStatus.WAITING_RESPONSE.value,
                "sent_payload": self.protocol.payload,
                "stage": self.decision.stage,
                "test_double": self.decision.test_double,
            },
        }


@dataclass(frozen=True, slots=True)
class DecisionEvaluation:
    decision: StimulationDecision
    events: tuple[dict[str, object], ...]
    candidate: StimulusRequest | None


class StimulationDecisionEngine:
    """Stateful, deterministic policy evaluator with an injectable clock."""

    def __init__(self, clock_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self._clock_ns = clock_ns
        self._config = StimulationConfig()
        self._session_id: str | None = None
        self._last_block_id = 0
        self._previous_valid_target: bool | None = None
        self._last_sent_monotonic_ns: int | None = None

    @property
    def config(self) -> StimulationConfig:
        return self._config

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def last_block_id(self) -> int:
        return self._last_block_id

    @property
    def last_sent_monotonic_ns(self) -> int | None:
        return self._last_sent_monotonic_ns

    def begin_session(self, session_id: str) -> None:
        if not session_id:
            raise ValueError("session_id 不能为空")
        if session_id != self._session_id:
            self._session_id = session_id
            self._last_block_id = 0
            self._previous_valid_target = None
            self._last_sent_monotonic_ns = None

    def set_config(self, config: StimulationConfig, *, automatic_enabled: bool) -> None:
        if automatic_enabled:
            raise ValueError("自动决策开启时不能更改配置")
        if config.config_version <= self._config.config_version:
            raise ValueError("配置版本必须递增")
        self._config = config
        # Explicit configuration changes start a fresh transition cycle, but
        # never permit old blocks to be replayed or reset the send interval.
        self._previous_valid_target = None

    def mark_request_sent(self, session_id: str, sent_monotonic_ns: int) -> None:
        if session_id == self._session_id:
            self._last_sent_monotonic_ns = sent_monotonic_ns

    def evaluate(
        self,
        result: ProcessingResult,
        *,
        received_monotonic_ns: int | None,
        automatic_enabled: bool,
        simulator_ready: bool,
        request_busy: bool,
        now_monotonic_ns: int | None = None,
    ) -> DecisionEvaluation:
        now_ns = self._clock_ns() if now_monotonic_ns is None else now_monotonic_ns
        allowed = False
        candidate: StimulusRequest | None = None
        reason = ""
        stage = result.stage
        valid_success = (
            result.status is ProcessingStatus.SUCCESS and stage in STAGES
        )

        same_session = result.session_id == self._session_id
        out_of_order = same_session and result.block_id <= self._last_block_id
        if not same_session:
            reason = "结果不属于当前实时会话"
        elif out_of_order:
            reason = (
                "重复分期结果" if result.block_id == self._last_block_id
                else "倒序分期结果"
            )
        else:
            self._last_block_id = result.block_id
            is_target = valid_success and stage in self._config.target_stages
            if valid_success and self._config.trigger_mode is TriggerMode.ENTER_TARGET_SET:
                target_entry = bool(is_target and self._previous_valid_target is not True)
                self._previous_valid_target = bool(is_target)
            elif valid_success:
                target_entry = bool(is_target)
                self._previous_valid_target = bool(is_target)
            else:
                # An unavailable/failed result interrupts eligibility but does
                # not reset whether the last *valid* stage was in the target set.
                target_entry = False

            if result.status is ProcessingStatus.UNAVAILABLE:
                reason = "模型未接入"
            elif result.status is ProcessingStatus.CANCELLED:
                reason = "分期结果已取消"
            elif result.status is not ProcessingStatus.SUCCESS:
                reason = f"分期结果不可用：{result.reason or result.status.value}"
            elif not valid_success:
                reason = "分期结果缺少合法睡眠期"
            elif not self._config.target_stages:
                reason = "尚未选择目标睡眠期"
            elif not is_target:
                reason = f"期别 {stage} 不在目标集合中"
            elif self._config.trigger_mode is None:
                reason = "触发策略尚未选择"
            elif not target_entry:
                reason = "尚未进入目标睡眠期"
            elif not automatic_enabled:
                reason = "自动决策未启用"
            elif self._config.issues():
                reason = "配置不完整：" + "；".join(self._config.issues())
            elif received_monotonic_ns is None or received_monotonic_ns < 0:
                reason = "缺少 EEG 块接收单调时钟时间，拒绝请求"
            elif now_ns < received_monotonic_ns:
                reason = "EEG 块接收时间晚于当前单调时钟，拒绝请求"
            elif (
                (now_ns - received_monotonic_ns) / 1_000_000_000
                > float(self._config.max_result_age_seconds)
            ):
                reason = "分期结果已超过最大允许年龄"
            elif request_busy:
                reason = "已有刺激请求等待响应，不排队补发"
            elif not simulator_ready:
                reason = "本应用模拟端未启动"
            elif (
                self._last_sent_monotonic_ns is not None
                and (now_ns - self._last_sent_monotonic_ns) / 1_000_000_000
                < float(self._config.min_request_interval_seconds)
            ):
                reason = "尚未达到最小请求间隔，不保留候选"
            else:
                allowed = True
                reason = "目标期命中且所有发送条件满足（仅模拟）"

        decision_id = uuid.uuid4().hex
        request_id = uuid.uuid4().hex if allowed else None
        decision = StimulationDecision(
            decision_id=decision_id,
            session_id=result.session_id,
            block_id=result.block_id,
            config_version=self._config.config_version,
            stage=stage,
            model=result.model.to_dict(),
            start_sample=result.start_sample,
            end_sample_exclusive=result.end_sample_exclusive,
            received_monotonic_ns=received_monotonic_ns,
            decided_monotonic_ns=now_ns,
            allowed=allowed,
            reason=reason,
            request_id=request_id,
            test_double=result.model.is_test_double,
        )
        event = decision.to_event()
        snapshot = {
            "event_type": "stimulation_config",
            "session_id": result.session_id,
            "block_id": None,
            "request_id": None,
            "payload": {
                **self._config.to_snapshot(automatic_enabled=automatic_enabled),
                "config_version": self._config.config_version,
            },
        }
        events = (snapshot, event)
        if allowed:
            assert self._config.protocol is not None
            assert self._config.max_result_age_seconds is not None
            assert received_monotonic_ns is not None
            candidate = StimulusRequest(
                request_id=request_id or "",
                decision=decision,
                protocol=self._config.protocol,
                received_monotonic_ns=received_monotonic_ns,
                max_result_age_seconds=self._config.max_result_age_seconds,
            )
        return DecisionEvaluation(decision, events, candidate)
