from __future__ import annotations

from sleep_stim_controller.latest import LatestItemBuffer


def test_latest_item_buffer_is_bounded_and_counts_replacements() -> None:
    buffer = LatestItemBuffer[int]()
    buffer.put(1)
    buffer.put(2)
    buffer.put(3)

    assert buffer.take() == 3
    stats = buffer.stats()
    assert stats.replaced_count == 2
    assert stats.delivered_count == 1
    assert not stats.pending
