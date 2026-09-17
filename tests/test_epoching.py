from __future__ import annotations

import numpy as np
import pytest

from curry_netstream.models import DataBlock, Event, SessionInfo
from sleep_stim_controller.epoching import (
    ThirtySecondEpochAssembler,
    UnsupportedStreamEventsError,
)
from sleep_stim_controller.validation import DataBlockValidationError


SESSION = SessionInfo(2, 10.0, ["C3", "C4"])


def packet(start: int, data: np.ndarray, *, labels=None, rate=10.0, events=None):
    return DataBlock(
        data=np.asarray(data, dtype=np.float32),
        start_sample=start,
        sample_rate_hz=rate,
        labels=list(SESSION.labels if labels is None else labels),
        events=[] if events is None else list(events),
    )


def test_variable_packets_cross_boundaries_without_overlap_or_mutation() -> None:
    source = np.arange(2 * 950, dtype=np.float32).reshape(2, 950)
    sizes = [37, 113, 251, 549]
    assembler = ThirtySecondEpochAssembler(SESSION)
    outputs = []
    offset = 0
    for size in sizes:
        outputs.extend(
            assembler.push(packet(1000 + offset, source[:, offset : offset + size]))
        )
        offset += size

    assert [block.start_sample for block in outputs] == [1000, 1300, 1600]
    assert [block.n_samples for block in outputs] == [300, 300, 300]
    for index, block in enumerate(outputs):
        np.testing.assert_array_equal(block.data, source[:, index * 300 : (index + 1) * 300])
    first_copy = outputs[0].data.copy()
    # The last packet crossed two window boundaries; neither completed output
    # may be changed by later writes or by the residual tail.
    np.testing.assert_array_equal(outputs[0].data, first_copy)
    snapshot = assembler.snapshot()
    assert snapshot.received_packets == 4
    assert snapshot.received_samples == 950
    assert snapshot.completed_windows == 3
    assert snapshot.accepted_windows == 0
    assert snapshot.partial_samples == 50
    assert snapshot.partial_start_sample == 1900
    assembler.mark_accepted_window()
    assembler.mark_accepted_window()
    assert assembler.snapshot().accepted_windows == 2


@pytest.mark.parametrize("sizes", [[299], [300], [301], [600], [73, 227, 600]])
def test_window_boundary_counts_and_tail(sizes: list[int]) -> None:
    assembler = ThirtySecondEpochAssembler(SESSION)
    offset = 0
    outputs = []
    for size in sizes:
        values = np.full((2, size), offset, dtype=np.float32)
        outputs.extend(assembler.push(packet(50 + offset, values)))
        offset += size
    expected_count, remainder = divmod(sum(sizes), 300)
    assert len(outputs) == expected_count
    assert assembler.snapshot().partial_samples == remainder
    assert assembler.snapshot().partial_start_sample == (
        50 + expected_count * 300 if remainder else None
    )


def test_single_packet_can_yield_multiple_windows() -> None:
    assembler = ThirtySecondEpochAssembler(SESSION)
    data = np.arange(2 * 750, dtype=np.float32).reshape(2, 750)
    outputs = list(assembler.push(packet(7, data)))
    assert [item.start_sample for item in outputs] == [7, 307]
    np.testing.assert_array_equal(outputs[1].data, data[:, 300:600])
    assert assembler.snapshot().partial_samples == 150


@pytest.mark.parametrize(
    ("bad_packet", "message"),
    [
        (packet(102, np.zeros((2, 10))), "采样不连续"),
        (packet(100, np.array([[np.nan] * 10, [0.0] * 10])), "NaN"),
        (packet(100, np.array([[np.inf] * 10, [0.0] * 10])), "Inf"),
        (packet(100, np.zeros((2, 10)), labels=["C3", "C4", "Ref"]), "标签数量"),
        (packet(100, np.zeros((2, 10)), rate=11.0), "采样率"),
    ],
)
def test_invalid_packet_does_not_pollute_following_contiguous_packet(
    bad_packet: DataBlock, message: str
) -> None:
    assembler = ThirtySecondEpochAssembler(SESSION)
    if message == "采样不连续":
        list(assembler.push(packet(100, np.zeros((2, 1)))))
    with pytest.raises(DataBlockValidationError, match=message):
        list(assembler.push(bad_packet))
    good = packet(101 if message == "采样不连续" else 100, np.zeros((2, 10)))
    assert list(assembler.push(good)) == []
    assert assembler.snapshot().received_packets == (2 if message == "采样不连续" else 1)
    assert assembler.snapshot().received_samples == (11 if message == "采样不连续" else 10)


@pytest.mark.parametrize("bad_start", [100, 99])
def test_duplicate_and_out_of_order_packets_do_not_advance_continuity(
    bad_start: int,
) -> None:
    assembler = ThirtySecondEpochAssembler(SESSION)
    list(assembler.push(packet(100, np.zeros((2, 1)))))
    with pytest.raises(DataBlockValidationError, match="采样不连续"):
        list(assembler.push(packet(bad_start, np.zeros((2, 1)))))
    assert assembler.snapshot().received_packets == 1
    list(assembler.push(packet(101, np.zeros((2, 1)))))
    assert assembler.snapshot().received_samples == 2


def test_events_are_explicitly_rejected_and_finite_start_is_strict() -> None:
    assembler = ThirtySecondEpochAssembler(SESSION)
    with pytest.raises(UnsupportedStreamEventsError, match="事件语义尚未支持"):
        list(assembler.push(packet(100, np.zeros((2, 10)), events=[Event(100, 1)])))

    fractional = DataBlock(
        data=np.zeros((2, 10), dtype=np.float32),
        start_sample=100.5,
        sample_rate_hz=10.0,
        labels=["C3", "C4"],
    )
    with pytest.raises(DataBlockValidationError, match="start_sample"):
        list(assembler.push(fractional))


def test_invalid_window_rate_and_finalize_release_tail() -> None:
    with pytest.raises(ValueError, match="整数个"):
        ThirtySecondEpochAssembler(SessionInfo(2, 10.01, ["C3", "C4"]))
    assembler = ThirtySecondEpochAssembler(SESSION)
    list(assembler.push(packet(900, np.ones((2, 7)))))
    snapshot = assembler.finalize()
    assert snapshot.partial_samples == 7
    assert snapshot.partial_start_sample == 900
    assert assembler.finalize() == snapshot
    with pytest.raises(RuntimeError, match="已结束"):
        list(assembler.push(packet(907, np.ones((2, 1)))))
