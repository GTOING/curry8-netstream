"""Curry 8 NetStreaming 协议层 —— 整套代码里【唯一的"校准点"】。

⚠️ 重要前提
    raw float 数据包的精确字节布局，在 CURRY 8 User Guide 中并没有给出。
    手册 p.254 / p.256 明确写道：协议规范与可运行的 C++ / MATLAB demo 需要向
    curry8help@neuroscan.com 索取。因此——

      * 本文件中的帧头结构(HEADER_FORMAT)、字段顺序、消息码(MessageCode)取值
        全部是【基于公开实现的合理假设 / PLACEHOLDER】。
      * 拿到官方 demo 后，通常【只需修改本文件】即可让 client.py 正常工作，
        其余模块无需改动。

参考：User Guide p.253-257《14.2.1.1 Configure as NetStreaming Server or Client》。
每个数据块由 Curry(Server) 发出，含以下内容（手册原文变量名）：
    indat            波形数据       -> DataBlock.data
    inlabels         通道标签列表   -> 经 INFO 消息获取 -> SessionInfo.labels
    insampleratehz   采样率(Hz)     -> 经 INFO 消息获取 -> SessionInfo.sample_rate_hz
    instartsample    绝对起始采样   -> FrameHeader.sample
    inevents         块内事件       -> DATA payload 尾部（本骨架暂留空，待校准）
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

# --- 帧头格式（PLACEHOLDER，待官方 demo 校准）--------------------------------
# 假设布局：小端(little-endian)；8 字节标识 + 6 个 uint32。
#   device_id[8] : 设备/块标识（ASCII，右侧补 \x00）
#   code         : 消息码，见 MessageCode
#   request      : 请求号 / 序号
#   sample       : 绝对起始采样点 (对应 instartsample)
#   n_items      : 数据项数（DATA 时=通道数×采样数；其它消息含义不同）
#   data_size    : 紧随帧头之后的 payload 字节数
#   reserved     : 预留字段
HEADER_FORMAT = "<8sIIIIII"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # = 8 + 6*4 = 32 字节

# 假设：非压缩 raw float = 小端 float32。压缩格式(Compressed)不在骨架范围内。
SAMPLE_DTYPE = "<f4"


class MessageCode(IntEnum):
    """消息码（PLACEHOLDER，待官方 demo 校准）。"""

    # ---- Server -> Client ----
    INFO = 1        # 会话信息：采样率 / 通道数 / 通道标签
    DATA = 2        # 一块波形数据(+事件)
    IMPEDANCE = 3   # 阻抗结果

    # ---- Client -> Server（控制流）----
    # 对应手册 "Allow Client to control amplifier" / "start/pause recording"
    CTRL_START_RECORDING = 101
    CTRL_STOP_RECORDING = 102
    CTRL_START_ACQUISITION = 103
    CTRL_STOP_ACQUISITION = 104
    CTRL_START_IMPEDANCE = 105


@dataclass(slots=True)
class FrameHeader:
    """一个 NetStreaming 帧的固定长度头部。"""

    device_id: bytes
    code: int
    request: int
    sample: int
    n_items: int
    data_size: int
    reserved: int = 0

    def pack(self) -> bytes:
        """序列化为 HEADER_SIZE 字节。"""
        return struct.pack(
            HEADER_FORMAT,
            self.device_id[:8].ljust(8, b"\x00"),
            self.code,
            self.request,
            self.sample,
            self.n_items,
            self.data_size,
            self.reserved,
        )

    @classmethod
    def unpack(cls, raw: bytes) -> "FrameHeader":
        """从 HEADER_SIZE 字节反序列化。长度不符会抛 ValueError。"""
        if len(raw) != HEADER_SIZE:
            raise ValueError(f"帧头长度应为 {HEADER_SIZE}，实际 {len(raw)}")
        dev, code, req, sample, n_items, data_size, reserved = struct.unpack(
            HEADER_FORMAT, raw
        )
        return cls(dev, code, req, sample, n_items, data_size, reserved)


def encode_control(code: MessageCode, request: int = 0) -> bytes:
    """编码一条「客户端 -> 服务器」的控制指令（仅帧头、无 payload）。

    用于 start/stop recording、impedance 等远程控制。
    """
    return FrameHeader(
        device_id=b"CTRL",
        code=int(code),
        request=request,
        sample=0,
        n_items=0,
        data_size=0,
    ).pack()
