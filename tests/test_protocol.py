"""协议层单元测试 —— 含【真实抓包】回归用例，无需任何服务器或硬件。

这是调试入口：可在此打断点，逐步检查 20 字节大端帧头的打包/解包、
控制消息编码、float32 payload 整形等逻辑。
"""
from __future__ import annotations

import struct

import numpy as np
import pytest

from curry_netstream.protocol import (
    HEADER_FORMAT,
    HEADER_SIZE,
    ID_CTRL,
    SAMPLE_DTYPE,
    FrameHeader,
    encode_control,
)

# 来自真实抓包 capture.bin：连接 Curry NetStreaming Server 后收到的第一条消息
REAL_CTRL_MSG_1 = bytes.fromhex("4354524c00010002000000000000000000000000")
REAL_CTRL_MSG_2 = bytes.fromhex("4354524c00010001000000000000000000000000")


def test_header_size_is_20() -> None:
    assert HEADER_SIZE == struct.calcsize(HEADER_FORMAT)
    assert HEADER_SIZE == 20


def test_decode_real_capture() -> None:
    """用真实字节验证帧头解析（回归用例）。"""
    h1 = FrameHeader.unpack(REAL_CTRL_MSG_1)
    assert h1.chid == ID_CTRL  # b"CTRL"
    assert h1.code == 1
    assert h1.request == 2
    assert h1.sample == 0
    assert h1.data_size == 0  # 纯控制消息，无 payload

    h2 = FrameHeader.unpack(REAL_CTRL_MSG_2)
    assert h2.chid == ID_CTRL
    assert h2.code == 1
    assert h2.request == 1
    assert h2.data_size == 0


def test_header_roundtrip() -> None:
    h = FrameHeader(
        chid=b"DATA", code=2, request=0, sample=1000, size1=256, size2=0
    )
    raw = h.pack()
    assert len(raw) == HEADER_SIZE

    back = FrameHeader.unpack(raw)
    assert back.chid == b"DATA"
    assert back.code == 2
    assert back.sample == 1000
    assert back.data_size == 256


def test_encode_control_is_header_only() -> None:
    raw = encode_control(code=1, request=20)
    assert len(raw) == HEADER_SIZE

    h = FrameHeader.unpack(raw)
    assert h.chid == ID_CTRL
    assert h.code == 1
    assert h.request == 20
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
