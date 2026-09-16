from __future__ import annotations

import numpy as np
import pytest

from curry_netstream.models import DataBlock, SessionInfo
from sleep_stim_controller.validation import (
    DataBlockValidationError,
    validate_data_block,
)


def make_block(
    *,
    n_channels: int = 2,
    n_samples: int = 300,
    sample_rate_hz: float = 10.0,
    labels: list[str] | None = None,
    values: np.ndarray | None = None,
) -> DataBlock:
    return DataBlock(
        data=np.zeros((n_channels, n_samples), dtype=np.float32)
        if values is None else values,
        start_sample=100,
        sample_rate_hz=sample_rate_hz,
        labels=[f"C{i + 1}" for i in range(n_channels)] if labels is None else labels,
    )


def test_accepts_complete_block_without_mutating_array() -> None:
    values = np.arange(600, dtype=np.float32).reshape(2, 300)
    block = make_block(values=values)
    session = SessionInfo(2, 10.0, ["C1", "C2"])

    assert validate_data_block(block, session=session) is block
    np.testing.assert_array_equal(block.data, values)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"n_samples": 299}, "30 秒完整块"),
        ({"labels": ["C1"]}, "标签数量"),
        (
            {"values": np.array([[0.0] * 300, [np.nan] + [0.0] * 299])},
            "NaN",
        ),
        ({"sample_rate_hz": 0.0}, "采样率必须"),
    ],
)
def test_rejects_invalid_data_at_controller_boundary(kwargs, message) -> None:
    block = make_block(**kwargs)
    with pytest.raises(DataBlockValidationError, match=message):
        validate_data_block(block)


def test_rejects_handshake_mismatch() -> None:
    block = make_block()
    session = SessionInfo(2, 10.0, ["C3", "C4"])
    with pytest.raises(DataBlockValidationError, match="标签与握手信息"):
        validate_data_block(block, session=session)
