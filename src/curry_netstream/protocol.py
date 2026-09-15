"""Curry 8 NetStreaming wire protocol.

The layout in this module is derived from the local Curry reference client in
``reference/P300Speller-pyQt_v202410/scutbci-pyqt-p300``. Multi-byte fields in
the 20-byte message header use network byte order; DATA payload structures and
uncompressed EEG float samples use little-endian byte order.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

# Message header: id, code, request, start sample, payload size and
# uncompressed payload size. This matches the captured CTRL packets in tests.
HEADER_FORMAT = ">4sHHIII"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

ID_CTRL = b"CTRL"
ID_DATA = b"DATA"
VALID_IDS = frozenset((ID_CTRL, ID_DATA))

# CTRL packet codes.
CTRL_FROM_SERVER = 1
CTRL_FROM_CLIENT = 2

# Server notifications (CTRL_FROM_SERVER).
SERVER_ACQUISITION_START = 1
SERVER_ACQUISITION_STOP = 2
SERVER_IMPEDANCE_START = 3
SERVER_IMPEDANCE_STOP = 4
SERVER_RECORDING_START = 5
SERVER_RECORDING_STOP = 6

# Client requests (CTRL_FROM_CLIENT).
REQUEST_VERSION = 1
REQUEST_CHANNEL_INFO = 3
REQUEST_STATUS_AMP = 4
REQUEST_BASIC_INFO = 6
REQUEST_STREAMING_START = 8
REQUEST_STREAMING_STOP = 9
REQUEST_AMP_CONNECT = 10
REQUEST_AMP_DISCONNECT = 11
REQUEST_IMPEDANCE_START = 12
REQUEST_IMPEDANCE_STOP = 13
REQUEST_RECORDING_START = 14
REQUEST_RECORDING_STOP = 15
REQUEST_DELAY = 16
REQUEST_SET_RECORDING_PATH = 17

# DATA packet codes.
DATA_INFO = 1
DATA_EEG = 2
DATA_EVENTS = 3
DATA_IMPEDANCES = 4

# DATA_INFO request/subtype values.
INFO_VERSION = 1
INFO_BASIC_INFO = 2
INFO_CHANNEL_INFO = 4
INFO_STATUS_AMP = 7
INFO_TIME = 9

# DATA_EEG / DATA_EVENTS request/subtype values.
DATA_TYPE_FLOAT32 = 1
DATA_TYPE_FLOAT32_ZIP = 2
DATA_TYPE_EVENT_LIST = 3

BASIC_INFO_FORMAT = "<iiiiII"
BASIC_INFO_SIZE = struct.calcsize(BASIC_INFO_FORMAT)

CHANNEL_INFO_FORMAT = "<I80siiIdddiifiii"
CHANNEL_INFO_SIZE = struct.calcsize(CHANNEL_INFO_FORMAT)

SAMPLE_DTYPE = np.dtype("<f4")


@dataclass(slots=True)
class FrameHeader:
    """One fixed-size Curry NetStreaming message header."""

    chid: bytes
    code: int
    request: int
    sample: int
    size1: int
    size2: int

    @property
    def data_size(self) -> int:
        """Number of payload bytes that follow the header on the wire."""
        return self.size1

    @property
    def uncompressed_size(self) -> int:
        return self.size2

    def pack(self) -> bytes:
        return struct.pack(
            HEADER_FORMAT,
            self.chid[:4].ljust(4, b"\x00"),
            self.code & 0xFFFF,
            self.request & 0xFFFF,
            self.sample,
            self.size1,
            self.size2,
        )

    @classmethod
    def unpack(cls, raw: bytes) -> "FrameHeader":
        if len(raw) != HEADER_SIZE:
            raise ValueError(f"帧头长度应为 {HEADER_SIZE}，实际 {len(raw)}")
        chid, code, request, sample, size1, size2 = struct.unpack(
            HEADER_FORMAT, raw
        )
        return cls(chid, code, request, sample, size1, size2)


@dataclass(frozen=True, slots=True)
class BasicInfo:
    """Acquisition configuration returned by INFO_BASIC_INFO."""

    struct_size: int
    n_eeg_channels: int
    sample_rate_hz: int
    data_size: int
    allow_client_control_amp: bool
    allow_client_control_recording: bool

    @classmethod
    def unpack(cls, payload: bytes) -> "BasicInfo":
        if len(payload) != BASIC_INFO_SIZE:
            raise ValueError(
                f"BasicInfo 长度应为 {BASIC_INFO_SIZE}，实际 {len(payload)}"
            )
        values = struct.unpack(BASIC_INFO_FORMAT, payload)
        result = cls(
            struct_size=values[0],
            n_eeg_channels=values[1],
            sample_rate_hz=values[2],
            data_size=values[3],
            allow_client_control_amp=bool(values[4]),
            allow_client_control_recording=bool(values[5]),
        )
        if result.n_eeg_channels <= 0:
            raise ValueError(f"无效 EEG 通道数: {result.n_eeg_channels}")
        if result.sample_rate_hz <= 0:
            raise ValueError(f"无效采样率: {result.sample_rate_hz}")
        if result.data_size <= 0:
            raise ValueError(f"无效样本数据大小: {result.data_size}")
        return result


@dataclass(frozen=True, slots=True)
class ChannelInfo:
    """Useful fields from one 144-byte NetStreamingChannelInfo record."""

    channel_id: int
    label: str
    channel_type: int
    device_type: int
    eeg_group: int
    additional_scale: float
    bipolar_reference_channel: int

    @classmethod
    def unpack(cls, payload: bytes) -> "ChannelInfo":
        if len(payload) != CHANNEL_INFO_SIZE:
            raise ValueError(
                f"ChannelInfo 长度应为 {CHANNEL_INFO_SIZE}，实际 {len(payload)}"
            )
        values = struct.unpack(CHANNEL_INFO_FORMAT, payload)
        label = values[1].decode("utf-16-le", errors="replace")
        label = label.split("\x00", 1)[0]
        return cls(
            channel_id=values[0],
            label=label,
            channel_type=values[2],
            device_type=values[3],
            eeg_group=values[4],
            bipolar_reference_channel=values[9],
            additional_scale=values[10],
        )


def decode_channel_info(payload: bytes) -> list[ChannelInfo]:
    """Decode all fixed-size channel records in an INFO_CHANNEL_INFO body."""
    if not payload or len(payload) % CHANNEL_INFO_SIZE != 0:
        raise ValueError(
            f"通道信息长度必须是 {CHANNEL_INFO_SIZE} 的正整数倍，实际 {len(payload)}"
        )
    return [
        ChannelInfo.unpack(payload[offset : offset + CHANNEL_INFO_SIZE])
        for offset in range(0, len(payload), CHANNEL_INFO_SIZE)
    ]


def decode_eeg_payload(payload: bytes, n_channels: int) -> np.ndarray:
    """Decode sample-major little-endian float32 EEG to ``[channel, sample]``.

    Curry sends one complete time sample at a time: all channel values for
    sample 0, followed by all channel values for sample 1, and so on.
    """
    if n_channels <= 0:
        raise ValueError(f"EEG 通道数必须大于 0，实际 {n_channels}")
    if not payload:
        raise ValueError("EEG payload 为空")
    if len(payload) % SAMPLE_DTYPE.itemsize != 0:
        raise ValueError(
            f"EEG payload 长度 {len(payload)} 不是 float32 大小的整数倍"
        )
    flat = np.frombuffer(payload, dtype=SAMPLE_DTYPE)
    if flat.size % n_channels != 0:
        raise ValueError(
            f"EEG 样本值数量 {flat.size} 不能被通道数 {n_channels} 整除"
        )
    return np.ascontiguousarray(flat.reshape(-1, n_channels).T, dtype=np.float32)


def encode_control(request: int, code: int = CTRL_FROM_CLIENT) -> bytes:
    """Encode a header-only client request."""
    return FrameHeader(
        chid=ID_CTRL,
        code=code,
        request=request,
        sample=0,
        size1=0,
        size2=0,
    ).pack()
