"""协议层单元测试 —— 用构造字节验证解析逻辑，无需任何服务器或硬件。

这是『只做客户端』时的主要调试入口：可在此打断点，逐步检查
帧头打包/解包、控制指令编码、float32 payload 整形等逻辑。
"""
from __future__ import annotations

import struct

import numpy as np
import pytest

from curry_netstream.protocol import (
    HEADER_FORMAT,
    HEADER_SIZE,
    SAMPLE_DTYPE,
    FrameHeader,
    MessageCode,
    encode_control,
)


def test_header_size_constant() -> None:
    assert HEADER_SIZE == struct.calcsize(HEADER_FORMAT)
    assert HEADER_SIZE == 32


def test_header_roundtrip() -> None:
    h = FrameHeader(
        device_id=b"DEV",
        code=int(MessageCode.DATA),
        request=7,
        sample=1000,
        n_items=64,
        data_size=256,
        reserved=0,
    )
    raw = h.pack()
    assert len(raw) == HEADER_SIZE

    back = FrameHeader.unpack(raw)
    assert back.code == int(MessageCode.DATA)
    assert back.request == 7
    assert back.sample == 1000
    assert back.n_items == 64
    assert back.data_size == 256
    assert back.device_id.rstrip(b"\x00") == b"DEV"


def test_encode_control_is_header_only() -> None:
    raw = encode_control(MessageCode.CTRL_STOP_RECORDING, request=3)
    assert len(raw) == HEADER_SIZE

    h = FrameHeader.unpack(raw)
    assert h.code == int(MessageCode.CTRL_STOP_RECORDING)
    assert h.request == 3
    assert h.data_size == 0


def test_unpack_rejects_wrong_size() -> None:
    with pytest.raises(ValueError):
        FrameHeader.unpack(b"\x00" * (HEADER_SIZE - 1))


def test_data_payload_reshape() -> None:
    """模拟一块 4 通道 × 5 采样的 float32 payload，验证整形逻辑。"""
    n_ch, n_samp = 4, 5
    samples = np.arange(n_ch * n_samp, dtype=np.float32)
    payload = samples.tobytes()

    arr = np.frombuffer(payload, dtype=SAMPLE_DTYPE)
    assert arr.size == n_ch * n_samp

    data = arr.reshape(n_ch, -1)
    assert data.shape == (n_ch, n_samp)
    np.testing.assert_array_equal(data[0], [0, 1, 2, 3, 4])
    np.testing.assert_array_equal(data[3], [15, 16, 17, 18, 19])
