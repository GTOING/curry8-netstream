"""P1 data-boundary validation for Curry EEG blocks."""
from __future__ import annotations

import math

import numpy as np
from curry_netstream.models import DataBlock, SessionInfo


P1_BLOCK_SECONDS = 30.0


class DataBlockValidationError(ValueError):
    """A Curry block cannot be delivered to the P1 display."""


def validate_data_block(
    block: DataBlock,
    *,
    session: SessionInfo | None = None,
    expected_seconds: float = P1_BLOCK_SECONDS,
) -> DataBlock:
    """Validate one complete block without changing its data.

    The decoded Curry ``DataBlock`` remains the source of truth. This
    function only checks the P1 contract and returns the same object so the
    caller can keep the original array for diagnostics/display handoff.
    """
    if not isinstance(block, DataBlock):
        raise DataBlockValidationError("接收到的对象不是 Curry DataBlock")

    data = np.asarray(block.data)
    if data.ndim != 2:
        raise DataBlockValidationError(
            f"EEG 数据必须是二维数组，实际维度为 {data.ndim}"
        )
    n_channels, n_samples = (int(data.shape[0]), int(data.shape[1]))
    if n_channels <= 0 or n_samples <= 0:
        raise DataBlockValidationError(
            f"EEG 数据不能为空，实际形状为 {tuple(data.shape)}"
        )

    labels = list(block.labels)
    if len(labels) != n_channels:
        raise DataBlockValidationError(
            f"通道标签数量 {len(labels)} 与 EEG 通道数 {n_channels} 不匹配"
        )
    if any(not str(label).strip() for label in labels):
        raise DataBlockValidationError("EEG 通道标签不能有空值")

    try:
        sample_rate_hz = float(block.sample_rate_hz)
    except (TypeError, ValueError) as exc:
        raise DataBlockValidationError("采样率不是有效数字") from exc
    if not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0:
        raise DataBlockValidationError(
            f"采样率必须为有限正数，实际为 {block.sample_rate_hz!r}"
        )
    if not math.isfinite(expected_seconds) or expected_seconds <= 0:
        raise ValueError("expected_seconds 必须为有限正数")

    expected_float = sample_rate_hz * expected_seconds
    expected_samples = round(expected_float)
    if not math.isclose(
        expected_float,
        expected_samples,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise DataBlockValidationError(
            f"采样率 {sample_rate_hz:g} 无法形成整数个 {expected_seconds:g} 秒样本"
        )
    if n_samples != expected_samples:
        duration = n_samples / sample_rate_hz
        raise DataBlockValidationError(
            f"EEG 数据不是 {expected_seconds:g} 秒完整块："
            f"每通道 {n_samples} 个样本，预期 {expected_samples}（实际 {duration:.3f} 秒）"
        )

    if not np.isfinite(data).all():
        raise DataBlockValidationError("EEG 数据包含 NaN 或 Inf 非有限值")

    if session is not None:
        if session.n_channels != n_channels:
            raise DataBlockValidationError(
                f"EEG 通道数 {n_channels} 与握手信息 {session.n_channels} 不匹配"
            )
        if len(session.labels) != session.n_channels:
            raise DataBlockValidationError("握手通道标签不完整，拒绝交付 EEG")
        if labels != list(session.labels):
            raise DataBlockValidationError("EEG 通道标签与握手信息不一致")
        if not math.isclose(
            sample_rate_hz,
            float(session.sample_rate_hz),
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise DataBlockValidationError(
                f"EEG 采样率 {sample_rate_hz:g} 与握手信息 "
                f"{float(session.sample_rate_hz):g} 不一致"
            )

    return block
