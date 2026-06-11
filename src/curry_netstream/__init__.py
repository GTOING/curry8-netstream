"""Curry 8 NetStreaming 客户端骨架（只做客户端）。

模块：
  protocol     —— ★唯一"校准点"：帧头结构 / 消息码 / encode·decode
  models       —— DataBlock / Event / SessionInfo 数据类
  client       —— CurryClient：TCP 连接、收包重组、分发、发控制指令
  logging_util —— 日志 + hexdump
"""
from .client import CurryClient
from .models import DataBlock, Event, SessionInfo
from .protocol import FrameHeader, MessageCode

__all__ = [
    "CurryClient",
    "DataBlock",
    "Event",
    "SessionInfo",
    "FrameHeader",
    "MessageCode",
]
