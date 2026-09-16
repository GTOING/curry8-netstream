"""Protocol tests, including two real captured Curry CTRL headers."""
from __future__ import annotations

import struct
import unittest

import numpy as np

from curry_netstream.protocol import (
    BASIC_INFO_FORMAT,
    BASIC_INFO_SIZE,
    CHANNEL_INFO_FORMAT,
    CHANNEL_INFO_SIZE,
    CTRL_FROM_CLIENT,
    HEADER_FORMAT,
    HEADER_SIZE,
    ID_CTRL,
    BasicInfo,
    FrameHeader,
    decode_channel_info,
    decode_eeg_payload,
    encode_control,
)

REAL_CTRL_MSG_1 = bytes.fromhex("4354524c00010002000000000000000000000000")
REAL_CTRL_MSG_2 = bytes.fromhex("4354524c00010001000000000000000000000000")


def make_channel_info(channel_id: int, label: str) -> bytes:
    raw_label = label.encode("utf-16-le")[:78] + b"\x00\x00"
    raw_label = raw_label.ljust(80, b"\x00")
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


class ProtocolTests(unittest.TestCase):
    def test_protocol_struct_sizes(self) -> None:
        self.assertEqual(HEADER_SIZE, struct.calcsize(HEADER_FORMAT))
        self.assertEqual(HEADER_SIZE, 20)
        self.assertEqual(BASIC_INFO_SIZE, 24)
        self.assertEqual(CHANNEL_INFO_SIZE, 144)

    def test_decode_real_capture(self) -> None:
        h1 = FrameHeader.unpack(REAL_CTRL_MSG_1)
        self.assertEqual((h1.chid, h1.code, h1.request), (ID_CTRL, 1, 2))
        self.assertEqual((h1.sample, h1.data_size), (0, 0))

        h2 = FrameHeader.unpack(REAL_CTRL_MSG_2)
        self.assertEqual((h2.chid, h2.code, h2.request), (ID_CTRL, 1, 1))
        self.assertEqual(h2.data_size, 0)

    def test_header_roundtrip(self) -> None:
        original = FrameHeader(b"DATA", 2, 1, 1000, 256, 0)
        decoded = FrameHeader.unpack(original.pack())
        self.assertEqual(decoded, original)

    def test_encode_client_control(self) -> None:
        header = FrameHeader.unpack(encode_control(request=8))
        self.assertEqual(header.chid, ID_CTRL)
        self.assertEqual(header.code, CTRL_FROM_CLIENT)
        self.assertEqual(header.request, 8)
        self.assertEqual(header.data_size, 0)

    def test_unpack_rejects_wrong_header_size(self) -> None:
        with self.assertRaises(ValueError):
            FrameHeader.unpack(b"\x00" * (HEADER_SIZE - 1))

    def test_decode_basic_info(self) -> None:
        payload = struct.pack(BASIC_INFO_FORMAT, 24, 2, 500, 4, 1, 0)
        info = BasicInfo.unpack(payload)
        self.assertEqual(info.n_eeg_channels, 2)
        self.assertEqual(info.sample_rate_hz, 500)
        self.assertEqual(info.data_size, 4)
        self.assertTrue(info.allow_client_control_amp)
        self.assertFalse(info.allow_client_control_recording)

    def test_decode_channel_info(self) -> None:
        channels = decode_channel_info(
            make_channel_info(1, "C3") + make_channel_info(2, "C4")
        )
        self.assertEqual([item.channel_id for item in channels], [1, 2])
        self.assertEqual([item.label for item in channels], ["C3", "C4"])

    def test_decode_sample_major_eeg(self) -> None:
        # Wire order: sample0(C3,C4), sample1(C3,C4), sample2(C3,C4).
        wire_values = np.array([1, 10, 2, 20, 3, 30], dtype="<f4")
        eeg = decode_eeg_payload(wire_values.tobytes(), n_channels=2)
        self.assertEqual(eeg.shape, (2, 3))
        np.testing.assert_array_equal(eeg[0], [1, 2, 3])
        np.testing.assert_array_equal(eeg[1], [10, 20, 30])

    def test_decode_complete_30_second_block(self) -> None:
        n_channels, sample_rate_hz, seconds = 2, 500, 30
        n_samples = sample_rate_hz * seconds
        wire_values = np.arange(n_samples * n_channels, dtype="<f4")
        eeg = decode_eeg_payload(wire_values.tobytes(), n_channels)
        self.assertEqual(eeg.shape, (n_channels, n_samples))
        self.assertEqual(eeg[0, 0], 0.0)
        self.assertEqual(eeg[1, 0], 1.0)
        self.assertEqual(eeg[0, 1], 2.0)

    def test_decode_eeg_rejects_incomplete_sample(self) -> None:
        payload = np.array([1, 2, 3], dtype="<f4").tobytes()
        with self.assertRaises(ValueError):
            decode_eeg_payload(payload, n_channels=2)


if __name__ == "__main__":
    unittest.main()
