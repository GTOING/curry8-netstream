"""Curry 8 NetStreaming 客户端骨架（只做客户端）。

模块：
  protocol     —— ★唯一"校准点"：帧头结构 / 消息码 / encode·decode
  models       —— DataBlock / Event / SessionInfo 数据类
  client       —— CurryClient：TCP 连接、收包重组、分发、发控制指令
  logging_util —— 日志 + hexdump
"""
from .client import CurryClient, CurryClientCancelled, StreamResult
from .models import DataBlock, Event, SessionInfo
from .protocol import (
    BasicInfo,
    ChannelInfo,
    FrameHeader,
    decode_channel_info,
    decode_eeg_payload,
    encode_control,
)

__all__ = [
    "CurryClient",
    "CurryClientCancelled",
    "StreamResult",
    "DataBlock",
    "Event",
    "SessionInfo",
    "BasicInfo",
    "ChannelInfo",
    "FrameHeader",
    "decode_channel_info",
    "decode_eeg_payload",
    "encode_control",
]
