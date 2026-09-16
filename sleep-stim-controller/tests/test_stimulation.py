from __future__ import annotations

import math

import pytest

from sleep_stim_controller.staging import (
    ModelDescriptor,
    ProcessingResult,
    ProcessingStatus,
)
from sleep_stim_controller.stimulation import (
    REALTIME_CONTROL_TOKEN,
    ProtocolValidationError,
    RequestStatus,
    StimulationConfig,
    StimulationDecisionEngine,
    TriggerMode,
    build_rally_message,
    parse_protocol_scheme,
    parse_rally_response,
)


NOW = 50_000_000_000


def protocol_document() -> dict[str, object]:
    # Fixture-only parameters: never loaded by the desktop application.
    return {
        "schema_version": 1,
        "name": "fixture protocol",
        "protocol_version": "fixture-1",
        "initial_stimulus_channels": ["C3", "C4"],
        "return_channels": ["Ref"],
        "payload": {
            "SD": 1.0,
            "FR": 0.0,
            "FD": 0.0,
            "CHS": [{"N": "C3", "T": "tD", "A": 0.5}],
        },
    }


def valid_config(
    *,
    mode: TriggerMode = TriggerMode.EACH_MATCHING_BLOCK,
    targets: frozenset[str] = frozenset({"N2", "N3"}),
    interval: float = 0.0,
    age: float = 5.0,
    version: int = 2,
) -> StimulationConfig:
    return StimulationConfig(
        target_stages=targets,
        trigger_mode=mode,
        min_request_interval_seconds=interval,
        max_result_age_seconds=age,
        protocol=parse_protocol_scheme(protocol_document()),
        config_version=version,
    )


def result(
    block_id: int,
    stage: str | None,
    *,
    session_id: str = "session-a",
    status: ProcessingStatus = ProcessingStatus.SUCCESS,
    test_double: bool = True,
) -> ProcessingResult:
    return ProcessingResult(
        session_id=session_id,
        block_id=block_id,
        start_sample=(block_id - 1) * 300,
        end_sample_exclusive=block_id * 300,
        model=ModelDescriptor(
            "fixture-model", "fixture-1", is_test_double=test_double
        ),
        status=status,
        stage=stage if status is ProcessingStatus.SUCCESS else None,
        confidence=None,
        started_monotonic_ns=NOW,
        finished_monotonic_ns=NOW + 1,
        elapsed_ms=0.000001,
        reason=None if status is ProcessingStatus.SUCCESS else "fixture unavailable",
    )


def make_engine(config: StimulationConfig | None = None) -> StimulationDecisionEngine:
    engine = StimulationDecisionEngine(clock_ns=lambda: NOW)
    engine.begin_session("session-a")
    engine.set_config(config or valid_config(), automatic_enabled=False)
    return engine


def evaluate(
    engine: StimulationDecisionEngine,
    item: ProcessingResult,
    *,
    received: int | None = NOW - 1_000_000_000,
    now: int = NOW,
    busy: bool = False,
    enabled: bool = True,
    simulator_ready: bool = True,
):
    return engine.evaluate(
        item,
        received_monotonic_ns=received,
        automatic_enabled=enabled,
        simulator_ready=simulator_ready,
        request_busy=busy,
        now_monotonic_ns=now,
    )


def test_protocol_builder_uses_exact_token_and_utf8_json_without_local_id() -> None:
    scheme = parse_protocol_scheme(protocol_document())
    message = build_rally_message(scheme)
    assert message.startswith((REALTIME_CONTROL_TOKEN + " ").encode())
    assert "C3" in message.decode("utf-8")
    assert b"request_id" not in message
    assert scheme.channel_names == ("C3",)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda doc: doc.update(schema_version=True), "schema_version"),
        (lambda doc: doc["payload"].update(SD=math.inf), "有限数值"),
        (lambda doc: doc["payload"].update(FR=-1), "FR/FD"),
        (lambda doc: doc["payload"].update(FD=2), "FR/FD"),
        (
            lambda doc: doc["payload"]["CHS"][0].update(N="Ref"),
            "初始刺激通道集合",
        ),
        (
            lambda doc: doc["payload"]["CHS"][0].update(T=[]),
            "tD/tA",
        ),
        (
            lambda doc: doc["payload"]["CHS"][0].update(A=True),
            "有限数值",
        ),
        (
            lambda doc: doc["payload"]["CHS"][0].update(
                {"F": 10.0, "P": 0.0, "D": 0.0, "T": "tA"}
            ),
            None,
        ),
    ],
)
def test_protocol_schema_validation(mutate, message) -> None:
    document = protocol_document()
    mutate(document)
    if message is None:
        scheme = parse_protocol_scheme(document)
        assert scheme.payload["CHS"][0]["T"] == "tA"
    else:
        with pytest.raises(ProtocolValidationError, match=message):
            parse_protocol_scheme(document)


def test_protocol_rejects_duplicate_and_return_channels() -> None:
    document = protocol_document()
    document["payload"]["CHS"].append({"N": "C3", "T": "tD", "A": 1.0})
    with pytest.raises(ProtocolValidationError, match="重复"):
        parse_protocol_scheme(document)

    document = protocol_document()
    document["initial_stimulus_channels"] = ["C3", "Ref"]
    with pytest.raises(ProtocolValidationError, match="不能重叠"):
        parse_protocol_scheme(document)


def test_default_config_is_explicitly_incomplete_and_numeric_errors_are_safe() -> None:
    config = StimulationConfig()
    issues = config.issues()
    assert "尚未选择目标睡眠期" in issues
    assert "触发策略尚未选择" in issues
    assert "最小请求间隔尚未配置" in issues
    assert "最大结果年龄尚未配置" in issues
    assert "尚未选择经过校验的本地模拟方案" in issues
    malformed = StimulationConfig(
        target_stages=frozenset({"N9"}),
        min_request_interval_seconds="invalid",  # type: ignore[arg-type]
        max_result_age_seconds=math.nan,
    )
    assert any("未知睡眠期" in issue for issue in malformed.issues())
    assert any("最小请求间隔" in issue for issue in malformed.issues())
    assert any("最大结果年龄" in issue for issue in malformed.issues())
    snapshot = malformed.to_snapshot(automatic_enabled=False)
    assert snapshot["min_request_interval_seconds"] is None
    assert snapshot["max_result_age_seconds"] is None


def test_each_matching_block_multiselect_and_suppression_are_explicit() -> None:
    engine = make_engine(valid_config(targets=frozenset({"N2", "N3"})))
    first = evaluate(engine, result(1, "N2"))
    second = evaluate(engine, result(2, "N3"))
    outside = evaluate(engine, result(3, "N1"))
    assert first.decision.allowed and first.candidate is not None
    assert second.decision.allowed and second.candidate is not None
    assert not outside.decision.allowed
    assert "不在目标集合" in outside.decision.reason
    assert first.decision.test_double is True
    assert first.decision.start_sample == 0
    assert first.decision.end_sample_exclusive == 300


def test_enter_target_set_initial_entry_internal_transition_and_reentry() -> None:
    engine = make_engine(
        valid_config(mode=TriggerMode.ENTER_TARGET_SET)
    )
    assert evaluate(engine, result(1, "N2")).decision.allowed
    internal = evaluate(engine, result(2, "N3"))
    assert not internal.decision.allowed
    assert internal.decision.reason == "尚未进入目标睡眠期"
    assert not evaluate(engine, result(3, "N1")).decision.allowed
    assert evaluate(engine, result(4, "N3")).decision.allowed


def test_failed_result_does_not_reset_previous_valid_target_membership() -> None:
    engine = make_engine(valid_config(mode=TriggerMode.ENTER_TARGET_SET))
    assert evaluate(engine, result(1, "N2")).decision.allowed
    unavailable = evaluate(
        engine,
        result(2, None, status=ProcessingStatus.UNAVAILABLE),
    )
    assert unavailable.decision.reason == "模型未接入"
    after = evaluate(engine, result(3, "N3"))
    assert not after.decision.allowed
    assert after.decision.reason == "尚未进入目标睡眠期"


def test_duplicate_and_out_of_order_results_do_not_mutate_policy_state() -> None:
    engine = make_engine(valid_config(mode=TriggerMode.ENTER_TARGET_SET))
    assert evaluate(engine, result(1, "N2")).decision.allowed
    duplicate = evaluate(engine, result(1, "N1"))
    assert duplicate.decision.reason == "重复分期结果"
    out_of_order = evaluate(engine, result(0, "N1"))
    assert out_of_order.decision.reason == "倒序分期结果"
    assert engine.last_block_id == 1
    assert evaluate(engine, result(2, "N3")).decision.reason == "尚未进入目标睡眠期"
    other_session = evaluate(engine, result(1, "N2", session_id="session-b"))
    assert other_session.decision.reason == "结果不属于当前实时会话"
    assert engine.last_block_id == 2


def test_configuration_change_starts_transition_cycle_but_not_block_replay() -> None:
    engine = make_engine(valid_config(mode=TriggerMode.ENTER_TARGET_SET))
    assert evaluate(engine, result(1, "N2")).decision.allowed
    engine.set_config(
        valid_config(mode=TriggerMode.ENTER_TARGET_SET, version=3),
        automatic_enabled=False,
    )
    duplicate = evaluate(engine, result(1, "N2"))
    assert not duplicate.decision.allowed
    assert duplicate.decision.reason == "重复分期结果"
    assert evaluate(engine, result(2, "N3")).decision.allowed
    engine.begin_session("session-b")
    other = result(1, "N2", session_id="session-b")
    assert evaluate(engine, other).decision.allowed


def test_age_busy_interval_and_missing_time_are_never_queued() -> None:
    engine = make_engine(valid_config(age=2.0, interval=3.0))
    stale = evaluate(engine, result(1, "N2"), received=NOW - 3_000_000_000)
    assert not stale.decision.allowed
    assert "超过最大允许年龄" in stale.decision.reason
    missing = evaluate(make_engine(valid_config(age=2.0)), result(1, "N2"), received=None)
    assert not missing.decision.allowed
    assert "缺少 EEG 块接收" in missing.decision.reason
    busy = evaluate(make_engine(valid_config(age=2.0)), result(1, "N2"), busy=True)
    assert not busy.decision.allowed and "已有刺激请求" in busy.decision.reason
    no_sim = evaluate(
        make_engine(valid_config(age=2.0)),
        result(1, "N2"),
        simulator_ready=False,
    )
    assert not no_sim.decision.allowed and "模拟端未启动" in no_sim.decision.reason

    engine = make_engine(valid_config(age=2.0, interval=3.0))
    first = evaluate(engine, result(1, "N2"), received=NOW)
    assert first.decision.allowed
    engine.mark_request_sent("session-a", NOW)
    suppressed = evaluate(
        engine,
        result(2, "N2"),
        received=NOW + 1_000_000_000,
        now=NOW + 1_000_000_000,
    )
    assert not suppressed.decision.allowed
    assert "最小请求间隔" in suppressed.decision.reason
    later = evaluate(
        engine,
        result(3, "N2"),
        received=NOW + 3_000_000_000,
        now=NOW + 3_000_000_000,
    )
    assert later.decision.allowed


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (b"RALLY_ERROR_SUCCESS", RequestStatus.API_SUCCESS),
        (b"RALLY_ERROR_START_STIM", RequestStatus.API_REJECTED),
        (b"RALLY_ERROR_NOT_A_REAL_CODE", RequestStatus.UNKNOWN),
        (b"\xff", RequestStatus.UNKNOWN),
        (b"", RequestStatus.UNKNOWN),
    ],
)
def test_response_parser_classifies_success_rejection_and_unknown(response, status) -> None:
    assert parse_rally_response(response).status is status
