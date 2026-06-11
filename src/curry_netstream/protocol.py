"""Curry 8 NetStreaming 协议层 —— 整套代码里【唯一的"校准点"】。

✅ 帧头结构：已用真实抓包 capture.bin 校准（不再是纯假设）。
   实际收到的字节（连接后服务器发来的两条控制消息）：

       43 54 52 4c 00 01 00 02 00 00 00 00 00 00 00 00 00 00 00 00
        C  T  R  L  ^code  ^req  ^^^^^^^^ sample/size1/size2 全 0 ^^^^^^

   => 帧头 = 20 字节、**大端 (network byte order)**，布局如下：
        char     id[4]    # 4 字节 ASCII 块标识，已确认有 "CTRL"
        uint16   code     # 消息码
        uint16   request  # 请求/子码
        uint32   sample   # 起始采样点 / 块号 (instartsample)
        uint32   size1    # 紧随其后的 payload 字节数（未压缩）
        uint32   size2    # 压缩大小 / 预留

⚠️ 仍待确认（需要看到真实 DATA 块才能定）：
   - DATA / INFO 块用的 id 是什么（目前只观察到 "CTRL"）
   - payload 里 float 样本的字节序（大端还是小端）、是否内嵌事件
   - 各 code / request 的语义、以及"请求开始推流"该发什么
   抓到一帧 DATA 后，基本只改本文件即可。

参考：CURRY 8 User Guide p.253-257《14.2.1.1 Configure as NetStreaming Server or Client》。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

# --- 帧头格式（已用 capture.bin 校准）---------------------------------------
# 大端；4 字节 ASCII id + uint16 code + uint16 request + 3 × uint32
HEADER_FORMAT = ">4sHHIII"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # = 20 字节

# 已知/推测的 4 字节块标识 (id) ------------------------------------------------
ID_CTRL = b"CTRL"   # ✅ 已抓包确认：控制 / 状态 / 握手消息
# 下面两个是推测，等抓到真实 DATA 块时按日志里出现的真实 id 修正：
ID_DATA = b"DATA"   # ❓ 推测：EEG 数据块
ID_INFO = b"INFO"   # ❓ 推测：会话信息（采样率/通道）

# payload 里 raw float 的字节序（❓待 DATA 抓包确认；EEG 数值是否合理可反推）
SAMPLE_DTYPE = "<f4"  # 先按小端 float32；若解出来是垃圾值就改成 ">f4"


@dataclass(slots=True)
class FrameHeader:
    """一个 NetStreaming 帧的固定 20 字节头部（大端）。"""

    chid: bytes      # 4 字节块标识，如 b"CTRL"
    code: int        # uint16
    request: int     # uint16
    sample: int      # uint32：起始采样 / 块号
    size1: int       # uint32：payload 字节数（未压缩）
    size2: int       # uint32：压缩大小 / 预留

    @property
    def data_size(self) -> int:
        """紧随帧头之后应读取的 payload 字节数。"""
        # 非压缩格式下用 size1；压缩格式(本骨架暂不处理)再议。
        return self.size1

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
        chid, code, request, sample, size1, size2 = struct.unpack(HEADER_FORMAT, raw)
        return cls(chid, code, request, sample, size1, size2)


def encode_control(code: int, request: int = 0, chid: bytes = ID_CTRL) -> bytes:
    """编码一条「客户端 -> 服务器」的控制消息（仅 20 字节帧头、无 payload）。

    ⚠️ code / request 的具体取值仍待确认（需 Neuroscan demo 或试验）。
    本函数只保证按真实的 20 字节大端结构打包；语义留待校准。
    """
    return FrameHeader(
        chid=chid, code=code, request=request, sample=0, size1=0, size2=0
    ).pack()
