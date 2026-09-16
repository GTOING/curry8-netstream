"""Synthetic end-to-end handshake and fragmented TCP receive test."""
from __future__ import annotations

import socket
import struct
import unittest
from collections import deque

import numpy as np

from curry_netstream.client import CurryClient
from curry_netstream.protocol import (
    BASIC_INFO_FORMAT,
    CHANNEL_INFO_FORMAT,
    CTRL_FROM_CLIENT,
    DATA_EEG,
    DATA_INFO,
    DATA_TYPE_FLOAT32,
    ID_DATA,
    INFO_BASIC_INFO,
    INFO_CHANNEL_INFO,
    REQUEST_BASIC_INFO,
    REQUEST_CHANNEL_INFO,
    REQUEST_STREAMING_START,
    REQUEST_STREAMING_STOP,
    FrameHeader,
)


def frame(code: int, request: int, payload: bytes, sample: int = 0) -> bytes:
    return FrameHeader(
        chid=ID_DATA,
        code=code,
        request=request,
        sample=sample,
        size1=len(payload),
        size2=0,
    ).pack() + payload


def channel_record(channel_id: int, label: str) -> bytes:
    raw_label = (label.encode("utf-16-le") + b"\x00\x00").ljust(80, b"\x00")
    return struct.pack(
        CHANNEL_INFO_FORMAT,
        channel_id,
        raw_label,
        0,
        0,
        0,
        0.0,
        0.0,
        0.0,
        0,
        -1,
        1.0,
        0,
        0,
        0,
    )


class ScriptedSocket:
    def __init__(self, parts: list[bytes | BaseException]) -> None:
        self.parts = deque(parts)
        self.sent: list[bytes] = []

    def recv(self, size: int) -> bytes:
        if not self.parts:
            return b""
        item = self.parts.popleft()
        if isinstance(item, BaseException):
            raise item
        result = item[:size]
        if len(item) > size:
            self.parts.appendleft(item[size:])
        return result

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def close(self) -> None:
        pass


class ClientTests(unittest.TestCase):
    def test_fragmented_handshake_and_eeg_block(self) -> None:
        basic = struct.pack(BASIC_INFO_FORMAT, 24, 2, 500, 4, 1, 1)
        channels = channel_record(1, "C3") + channel_record(2, "C4")
        samples = np.array([1, 10, 2, 20, 3, 30], dtype="<f4").tobytes()
        samples_2 = np.array([4, 40, 5, 50, 6, 60], dtype="<f4").tobytes()
        incoming = (
            frame(DATA_INFO, INFO_BASIC_INFO, basic)
            + frame(DATA_INFO, INFO_CHANNEL_INFO, channels)
            + frame(DATA_EEG, DATA_TYPE_FLOAT32, samples, sample=15000)
            + frame(DATA_EEG, DATA_TYPE_FLOAT32, samples_2, sample=15003)
        )

        # Split inside the first header and inject a timeout after a partial read.
        fake = ScriptedSocket([incoming[:7], socket.timeout(), incoming[7:]])
        client = CurryClient("unused", 4455)
        client._sock = fake  # type: ignore[assignment]
        blocks = []

        client.stream(blocks.append, max_blocks=2)

        self.assertEqual(len(blocks), 2)
        block = blocks[0]
        self.assertEqual(block.start_sample, 15000)
        self.assertEqual(block.sample_rate_hz, 500.0)
        self.assertEqual(block.labels, ["C3", "C4"])
        self.assertEqual(block.data.shape, (2, 3))
        np.testing.assert_array_equal(block.data[0], [1, 2, 3])
        np.testing.assert_array_equal(block.data[1], [10, 20, 30])
        self.assertEqual(blocks[1].start_sample, 15003)
        np.testing.assert_array_equal(blocks[1].data[0], [4, 5, 6])

        sent_headers = [FrameHeader.unpack(raw) for raw in fake.sent]
        self.assertTrue(all(item.code == CTRL_FROM_CLIENT for item in sent_headers))
        self.assertEqual(
            [item.request for item in sent_headers],
            [
                REQUEST_BASIC_INFO,
                REQUEST_CHANNEL_INFO,
                REQUEST_STREAMING_START,
                REQUEST_STREAMING_STOP,
            ],
        )


if __name__ == "__main__":
    unittest.main()
