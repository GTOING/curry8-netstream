"""数据模型 —— 对应 User Guide p.253 中 Curry 发给客户端的各项内容。"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(slots=True)
class Event:
    """块内事件（对应 inevents）。"""

    sample: int  # 采样点号（绝对或块内，取决于协议校准）
    type: int    # 事件类型码（见 User Guide 事件码表 p.553 起）


@dataclass(slots=True)
class SessionInfo:
    """一次会话的元信息，通常由 INFO 消息一次性下发。"""

    n_channels: int
    sample_rate_hz: float
    labels: list[str]


@dataclass(slots=True)
class DataBlock:
    """一块连续 EEG 数据。"""

    data: np.ndarray            # 形状 = (n_channels, n_samples), float32
    start_sample: int           # instartsample：本块绝对起始采样点
    sample_rate_hz: float       # insampleratehz
    labels: list[str]           # inlabels
    events: list[Event] = field(default_factory=list)  # inevents

    @property
    def n_channels(self) -> int:
        return int(self.data.shape[0])

    @property
    def n_samples(self) -> int:
        return int(self.data.shape[1]) if self.data.ndim == 2 else 0
