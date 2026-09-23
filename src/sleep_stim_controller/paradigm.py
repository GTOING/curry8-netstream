"""Versioned, immutable Rally paradigm packages.
Package loading is deliberately side-effect free: it never opens a socket and
never supplies a production default.  A session keeps the loaded snapshot even
if its source files are subsequently edited or removed.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .stimulation import ProtocolValidationError, parse_protocol_scheme
from .staging import STAGES


PARADIGM_SCHEMA_VERSION = 1
PARADIGM_STAGE_MAPPING = {
    "W": "A",
    "N1": "B",
    "N2": "C",
    "N3": None,
    "REM": None,
}
SYNTHETIC_CLASSIFICATION = "synthetic/test-only"
APPROVED_CLASSIFICATION = "approved-production"


class ParadigmValidationError(ValueError):
    """A selected versioned paradigm package is incomplete or unsafe."""


@dataclass(frozen=True, slots=True)
class ParadigmProtocol:
    key: str
    name: str
    version: str
    rally_base_protocol: str
    classification: str
    initial_stimulus_channels: tuple[str, ...]
    return_channels: tuple[str, ...]
    payload_json: str
    payload_sha256: str
    sha256: str
    canonical_json: str
    sd_seconds_per_unit: float | None
    sd_conversion_source: str | None
    source_relative_path: str

    @property
    def payload(self) -> dict[str, Any]:
        value = json.loads(self.payload_json)
        assert isinstance(value, dict)
        return value

    @property
    def can_estimate_expiry(self) -> bool:
        return self.sd_seconds_per_unit is not None

    def to_snapshot(self) -> dict[str, object]:
        return {
            "key": self.key,
            "name": self.name,
            "version": self.version,
            "rally_base_protocol": self.rally_base_protocol,
            "classification": self.classification,
            "initial_stimulus_channels": list(self.initial_stimulus_channels),
            "return_channels": list(self.return_channels),
            "payload": self.payload,
            "payload_canonical_json": self.payload_json,
            "payload_sha256": self.payload_sha256,
            "protocol_canonical_json": self.canonical_json,
            "protocol_sha256": self.sha256,
            "sd_seconds_per_unit": self.sd_seconds_per_unit,
            "sd_conversion_source": self.sd_conversion_source,
            "source_relative_path": self.source_relative_path,
        }


@dataclass(frozen=True, slots=True)
class ParadigmSnapshot:
    name: str
    version: str
    classification: str
    decision_strategy: str
    required_rally_base_protocol: str
    model_input_channels: tuple[str, ...]
    stage_mapping_json: str
    protocols: tuple[ParadigmProtocol, ...]
    paradigm_canonical_json: str
    sha256: str
    source_directory: str

    @property
    def stage_mapping(self) -> dict[str, str | None]:
        return json.loads(self.stage_mapping_json)

    @property
    def protocol_map(self) -> dict[str, ParadigmProtocol]:
        return {protocol.key: protocol for protocol in self.protocols}

    @property
    def real_armable(self) -> bool:
        return (
            self.classification == APPROVED_CLASSIFICATION
            and all(protocol.can_estimate_expiry for protocol in self.protocols)
        )

    def protocol_for_stage(self, stage: str) -> ParadigmProtocol | None:
        key = self.stage_mapping.get(stage)
        return self.protocol_map.get(key) if key is not None else None

    def to_snapshot(self) -> dict[str, object]:
        """Return the complete self-contained session evidence snapshot."""
        paradigm = json.loads(self.paradigm_canonical_json)
        return {
            "name": self.name,
            "version": self.version,
            "classification": self.classification,
            "decision_strategy": self.decision_strategy,
            "required_rally_base_protocol": self.required_rally_base_protocol,
            "model_input_channels": list(self.model_input_channels),
            "stage_mapping": self.stage_mapping,
            "paradigm": paradigm,
            "paradigm_canonical_json": self.paradigm_canonical_json,
            "paradigm_sha256": self.sha256,
            "protocols": [protocol.to_snapshot() for protocol in self.protocols],
        }


class ParadigmRuntimeManager:
    """Own the selected package and freeze one immutable copy per session."""

    def __init__(self) -> None:
        self._selected: ParadigmSnapshot | None = None
        self._session: ParadigmSnapshot | None = None

    @property
    def selected(self) -> ParadigmSnapshot | None:
        return self._selected

    @property
    def session_snapshot(self) -> ParadigmSnapshot | None:
        return self._session

    def select(self, snapshot: ParadigmSnapshot) -> None:
        if not isinstance(snapshot, ParadigmSnapshot):
            raise TypeError("必须先加载并验证 ParadigmSnapshot")
        if self._session is not None:
            raise RuntimeError("当前会话已冻结范式快照")
        self._selected = snapshot

    def begin_session(self) -> ParadigmSnapshot | None:
        if self._session is not None:
            raise RuntimeError("范式 session 快照已存在")
        self._session = self._selected
        return self._session

    def end_session(self) -> None:
        self._session = None

    @staticmethod
    def decision(snapshot: ParadigmSnapshot, stage: str) -> str | None:
        if stage not in STAGES:
            raise ValueError(f"未知睡眠期：{stage!r}")
        return snapshot.stage_mapping[stage]


def load_paradigm_package(directory: str | Path) -> ParadigmSnapshot:
    """Read and strictly validate ``paradigm.json`` and every referenced file."""
    root = Path(directory).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ParadigmValidationError("范式包路径必须是目录")
    manifest_path = root / "paradigm.json"
    manifest = _read_json(manifest_path)
    allowed = {
        "schema_version",
        "name",
        "version",
        "classification",
        "approval_reference",
        "decision_strategy",
        "required_rally_base_protocol",
        "model_input_channels",
        "stage_mapping",
        "protocols",
    }
    _exact_fields(manifest, allowed, "paradigm.json")
    _version(manifest.get("schema_version"), "paradigm.json schema_version")
    name = _nonempty(manifest.get("name"), "范式 name")
    version = _nonempty(manifest.get("version"), "范式 version")
    classification = manifest.get("classification")
    if classification not in {SYNTHETIC_CLASSIFICATION, APPROVED_CLASSIFICATION}:
        raise ParadigmValidationError("classification 必须明确标记合成测试或经批准生产包")
    if classification == APPROVED_CLASSIFICATION:
        _nonempty(manifest.get("approval_reference"), "approved-production approval_reference")
    elif "approval_reference" in manifest:
        raise ParadigmValidationError("synthetic/test-only 范式不能携带生产批准声明")
    strategy = manifest.get("decision_strategy")
    if strategy != "immediate_each_eligible_epoch":
        raise ParadigmValidationError("仅支持 immediate_each_eligible_epoch 决策策略")
    base = _nonempty(
        manifest.get("required_rally_base_protocol"),
        "required_rally_base_protocol",
    )
    channels = _string_list(manifest.get("model_input_channels"), "model_input_channels")
    if not channels:
        raise ParadigmValidationError("model_input_channels 不能为空")
    stage_mapping = manifest.get("stage_mapping")
    if not isinstance(stage_mapping, dict) or stage_mapping != PARADIGM_STAGE_MAPPING:
        raise ParadigmValidationError("stage_mapping 必须精确为 W→A、N1→B、N2→C、N3/REM→Stop")
    refs = manifest.get("protocols")
    if not isinstance(refs, dict) or set(refs) != {"A", "B", "C"}:
        raise ParadigmValidationError("protocols 必须精确引用 A、B、C 三个 JSON 文件")

    protocols: list[ParadigmProtocol] = []
    resolved_refs: set[Path] = set()
    for key in ("A", "B", "C"):
        relative = refs[key]
        if not isinstance(relative, str) or not relative.strip() or "\\" in relative:
            raise ParadigmValidationError(f"协议 {key} 引用必须是非空 POSIX 相对路径")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise ParadigmValidationError(f"协议 {key} 引用存在绝对路径或目录越界")
        try:
            path = (root / Path(*pure.parts)).resolve(strict=True)
            path.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ParadigmValidationError(f"协议 {key} 引用缺失或路径越界：{relative}") from exc
        if not path.is_file() or path in resolved_refs:
            raise ParadigmValidationError(f"协议 {key} 引用不是唯一普通文件：{relative}")
        resolved_refs.add(path)
        protocols.append(_load_protocol(path, relative, key, base, classification))

    first = protocols[0]
    for protocol in protocols[1:]:
        if protocol.rally_base_protocol != base:
            raise ParadigmValidationError(f"协议 {protocol.key} 与 required Rally 基础协议不一致")
        if protocol.initial_stimulus_channels != first.initial_stimulus_channels:
            raise ParadigmValidationError("A/B/C 的基础刺激通道声明不兼容")
        if protocol.return_channels != first.return_channels:
            raise ParadigmValidationError("A/B/C 修改了基础协议返回通道声明")

    canonical_obj = {
        "schema_version": PARADIGM_SCHEMA_VERSION,
        "name": name,
        "version": version,
        "classification": classification,
        **({"approval_reference": manifest["approval_reference"]} if classification == APPROVED_CLASSIFICATION else {}),
        "decision_strategy": strategy,
        "required_rally_base_protocol": base,
        "model_input_channels": list(channels),
        "stage_mapping": dict(PARADIGM_STAGE_MAPPING),
        "protocols": {protocol.key: protocol.source_relative_path for protocol in protocols},
    }
    canonical = _canonical_json(
        {
            "paradigm": canonical_obj,
            "protocols": {
                protocol.key: json.loads(protocol.canonical_json)
                for protocol in protocols
            },
        }
    )
    return ParadigmSnapshot(
        name=name,
        version=version,
        classification=classification,
        decision_strategy=strategy,
        required_rally_base_protocol=base,
        model_input_channels=channels,
        stage_mapping_json=_canonical_json(PARADIGM_STAGE_MAPPING),
        protocols=tuple(protocols),
        paradigm_canonical_json=canonical,
        sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        source_directory=str(root),
    )


def _load_protocol(
    path: Path,
    relative: str,
    key: str,
    required_base: str,
    classification: str,
) -> ParadigmProtocol:
    document = _read_json(path)
    allowed = {
        "schema_version",
        "name",
        "protocol_version",
        "classification",
        "rally_base_protocol",
        "initial_stimulus_channels",
        "return_channels",
        "sd_conversion",
        "payload",
    }
    _exact_fields(document, allowed, relative)
    _version(document.get("schema_version"), f"{relative} schema_version")
    name = _nonempty(document.get("name"), f"{relative} name")
    version = _nonempty(document.get("protocol_version"), f"{relative} protocol_version")
    if document.get("classification") != classification:
        raise ParadigmValidationError(f"{relative} classification 与范式不一致")
    base = _nonempty(document.get("rally_base_protocol"), f"{relative} rally_base_protocol")
    if base != required_base:
        raise ParadigmValidationError(f"{relative} 未声明要求的同一 Rally 基础协议")
    initial = _string_list(document.get("initial_stimulus_channels"), f"{relative} initial_stimulus_channels")
    returns = _string_list(document.get("return_channels"), f"{relative} return_channels")
    if not initial:
        raise ParadigmValidationError(f"{relative} initial_stimulus_channels 不能为空")
    payload = document.get("payload")
    if not isinstance(payload, Mapping):
        raise ParadigmValidationError(f"{relative} payload 必须是对象")
    scheme_doc = {
        "schema_version": 1,
        "name": name,
        "protocol_version": version,
        "initial_stimulus_channels": list(initial),
        "return_channels": list(returns),
        "payload": dict(payload),
    }
    try:
        scheme = parse_protocol_scheme(scheme_doc, source_path=relative)
    except ProtocolValidationError as exc:
        raise ParadigmValidationError(f"{relative} Rally payload 无效：{exc}") from exc
    if scheme.channel_names and set(scheme.channel_names) & set(returns):
        raise ParadigmValidationError(f"{relative} 尝试修改返回通道")

    conversion = document.get("sd_conversion")
    seconds_per_unit: float | None = None
    conversion_source: str | None = None
    if conversion is not None:
        if not isinstance(conversion, dict) or set(conversion) != {"seconds_per_sd_unit", "source"}:
            raise ParadigmValidationError(f"{relative} sd_conversion 字段无效")
        raw_factor = conversion.get("seconds_per_sd_unit")
        if isinstance(raw_factor, bool) or not isinstance(raw_factor, (int, float)):
            raise ParadigmValidationError(f"{relative} SD 换算系数必须是有限正数")
        seconds_per_unit = float(raw_factor)
        if not math.isfinite(seconds_per_unit) or seconds_per_unit <= 0:
            raise ParadigmValidationError(f"{relative} SD 换算系数必须是有限正数")
        conversion_source = _nonempty(conversion.get("source"), f"{relative} SD 换算来源")
    elif classification == APPROVED_CLASSIFICATION:
        raise ParadigmValidationError(f"{relative} 缺少可核实 SD 单位换算，不能真实自动启用")

    payload_json = _canonical_json(scheme.payload)
    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    normalized_protocol: dict[str, object] = {
        "schema_version": 1,
        "name": name,
        "protocol_version": version,
        "classification": classification,
        "rally_base_protocol": base,
        "initial_stimulus_channels": list(scheme.initial_stimulus_channels),
        "return_channels": list(scheme.return_channels),
        "payload": json.loads(payload_json),
    }
    if conversion is not None:
        normalized_protocol["sd_conversion"] = {
            "seconds_per_sd_unit": seconds_per_unit,
            "source": conversion_source,
        }
    canonical_protocol = _canonical_json(normalized_protocol)
    protocol_hash = hashlib.sha256(canonical_protocol.encode("utf-8")).hexdigest()
    return ParadigmProtocol(
        key=key,
        name=name,
        version=version,
        rally_base_protocol=base,
        classification=classification,
        initial_stimulus_channels=scheme.initial_stimulus_channels,
        return_channels=scheme.return_channels,
        payload_json=payload_json,
        payload_sha256=payload_hash,
        sha256=protocol_hash,
        canonical_json=canonical_protocol,
        sd_seconds_per_unit=seconds_per_unit,
        sd_conversion_source=conversion_source,
        source_relative_path=relative,
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"非有限数 {value}")),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ParadigmValidationError(f"无法读取有效 JSON {path.name}：{exc}") from exc
    if not isinstance(value, dict):
        raise ParadigmValidationError(f"{path.name} 根节点必须是 JSON 对象")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"重复 JSON 字段：{key}")
        value[key] = item
    return value


def _exact_fields(document: Mapping[str, object], expected: set[str], label: str) -> None:
    fields = set(document)
    missing = expected - fields
    extra = fields - expected
    # approval_reference and sd_conversion are conditionally optional.
    missing -= {"approval_reference", "sd_conversion"}
    if missing or extra:
        raise ParadigmValidationError(
            f"{label} 字段不匹配；缺少={sorted(missing)}，多余={sorted(extra)}"
        )


def _version(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value != 1:
        raise ParadigmValidationError(f"{label} 仅支持整数 1")


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ParadigmValidationError(f"{label} 不能为空")
    return value.strip()


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ParadigmValidationError(f"{label} 必须是字符串列表")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ParadigmValidationError(f"{label} 含空白或非字符串项目")
        normalized = item.strip()
        if normalized in result:
            raise ParadigmValidationError(f"{label} 含重复项目：{normalized}")
        result.append(normalized)
    return tuple(result)


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ParadigmValidationError(f"不能规范化 JSON：{exc}") from exc
