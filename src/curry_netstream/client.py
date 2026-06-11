"""Curry 8 NetStreaming TCP 客户端（只做客户端，不含模拟服务器）。

职责：
  - 连接 Curry(Server) 的 NetStreaming 端口
  - 按帧头声明的长度重组每一帧
  - 把数据帧解析为 DataBlock；信息帧解析为 SessionInfo
  - 发送控制指令（start/stop recording、impedance）
  - 可把原始字节流落盘，便于拿到真实数据后逆向校准协议
"""
from __future__ import annotations

import logging
import socket
from typing import BinaryIO, Callable, Optional

import numpy as np

from .logging_util import hexdump
from .models import DataBlock, SessionInfo
from .protocol import (
    HEADER_SIZE,
    SAMPLE_DTYPE,
    FrameHeader,
    MessageCode,
    encode_control,
)

log = logging.getLogger("curry.client")

DataCallback = Callable[[DataBlock], None]


class CurryClient:
    """连接单个 Curry NetStreaming Server 的客户端。

    用法::

        with CurryClient("127.0.0.1", 4455) as c:
            c.start_impedance()              # 可选：发控制指令
            c.stream(on_data, max_blocks=20) # 阻塞式收数据
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float = 5.0,
        dump_path: Optional[str] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._dump_path = dump_path
        self._sock: Optional[socket.socket] = None
        self._dump: Optional[BinaryIO] = None
        self._session: Optional[SessionInfo] = None
        self._running = False

    # ------------------------------------------------------------------ #
    # 连接管理
    # ------------------------------------------------------------------ #
    def connect(self) -> None:
        log.info("连接 Curry NetStreaming Server %s:%d ...", self.host, self.port)
        self._sock = socket.create_connection(
            (self.host, self.port), timeout=self.timeout
        )
        self._sock.settimeout(self.timeout)
        if self._dump_path:
            self._dump = open(self._dump_path, "wb")
            log.info("原始字节将写入 %s（逆向协议用）", self._dump_path)
        log.info("已连接。")

    def close(self) -> None:
        self._running = False
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        if self._dump is not None:
            self._dump.close()
            self._dump = None
        log.info("连接已关闭。")

    def __enter__(self) -> "CurryClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 底层收包
    # ------------------------------------------------------------------ #
    def _recv_exact(self, n: int) -> bytes:
        """精确读取 n 字节（不足则继续读，直到读满或连接断开）。"""
        if self._sock is None:
            raise ConnectionError("未连接")
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("服务器关闭了连接")
            buf += chunk
        data = bytes(buf)
        if self._dump is not None:
            self._dump.write(data)
        return data

    def _read_frame(self) -> tuple[FrameHeader, bytes]:
        raw_header = self._recv_exact(HEADER_SIZE)
        header = FrameHeader.unpack(raw_header)
        log.debug(
            "帧头 code=%s sample=%d n_items=%d data_size=%d | %s",
            header.code, header.sample, header.n_items, header.data_size,
            hexdump(raw_header),
        )
        payload = self._recv_exact(header.data_size) if header.data_size else b""
        if payload:
            log.debug("payload %s", hexdump(payload))
        return header, payload

    # ------------------------------------------------------------------ #
    # 帧分发（PLACEHOLDER：payload 解析逻辑待官方 demo 校准）
    # ------------------------------------------------------------------ #
    def _handle_info(self, header: FrameHeader, payload: bytes) -> None:
        """INFO 消息：取采样率、通道标签。

        PLACEHOLDER：真实布局未知。此处假设 payload 为 UTF-8 文本：
            第一行 = 采样率(Hz)
            第二行 = 逗号分隔的通道标签
        """
        sr, labels = 0.0, []
        try:
            text = payload.decode("utf-8", "replace")
            sr_line, _, labels_line = text.partition("\n")
            sr = float(sr_line.strip() or 0)
            labels = [s for s in labels_line.split(",") if s]
        except Exception:  # noqa: BLE001 —— 协议未校准时容错
            log.debug("INFO payload 无法按假设格式解析，按空会话处理")
        self._session = SessionInfo(
            n_channels=len(labels) or header.n_items,
            sample_rate_hz=sr,
            labels=labels,
        )
        log.info(
            "会话信息: %d 通道 @ %.1f Hz",
            self._session.n_channels, self._session.sample_rate_hz,
        )

    def _handle_data(self, header: FrameHeader, payload: bytes) -> DataBlock:
        """DATA 消息：把 payload 解析为 (n_channels, n_samples) 的 float32。

        PLACEHOLDER：假设 payload 全是 float32 样本、行优先 [ch0 全部样本, ch1 ...]，
        且不含内嵌事件。校准时改这里即可。
        """
        sess = self._session
        n_ch = sess.n_channels if sess else 0
        arr = np.frombuffer(payload, dtype=SAMPLE_DTYPE)
        if n_ch > 0 and arr.size % n_ch == 0:
            data = arr.reshape(n_ch, -1)
        else:
            # 未知通道数：退化成单行，方便先把字节看清楚再校准
            data = arr.reshape(1, -1)
        return DataBlock(
            data=np.ascontiguousarray(data, dtype=np.float32),
            start_sample=header.sample,
            sample_rate_hz=sess.sample_rate_hz if sess else 0.0,
            labels=list(sess.labels) if sess else [],
            events=[],
        )

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def stream(
        self,
        on_data: DataCallback,
        *,
        max_blocks: Optional[int] = None,
    ) -> None:
        """阻塞式接收。每收到一块数据回调 on_data(DataBlock)。"""
        self._running = True
        n = 0
        while self._running:
            try:
                header, payload = self._read_frame()
            except (socket.timeout, TimeoutError):
                log.warning("接收超时，继续等待…")
                continue
            except ConnectionError as exc:
                log.error("连接中断: %s", exc)
                break

            code = header.code
            if code == MessageCode.INFO:
                self._handle_info(header, payload)
            elif code == MessageCode.DATA:
                on_data(self._handle_data(header, payload))
                n += 1
                if max_blocks is not None and n >= max_blocks:
                    log.info("已达 max_blocks=%d，停止接收。", max_blocks)
                    break
            elif code == MessageCode.IMPEDANCE:
                log.info("收到阻抗结果帧 (n_items=%d)", header.n_items)
            else:
                log.warning(
                    "未知消息码 %d（payload %dB）—— 协议可能尚未校准",
                    code, len(payload),
                )

    # ------------------------------------------------------------------ #
    # 控制流（客户端 -> 服务器）
    # ------------------------------------------------------------------ #
    def _send(self, data: bytes) -> None:
        if self._sock is None:
            raise ConnectionError("未连接")
        self._sock.sendall(data)

    def start_recording(self) -> None:
        log.info("-> 发送 开始录制")
        self._send(encode_control(MessageCode.CTRL_START_RECORDING))

    def stop_recording(self) -> None:
        log.info("-> 发送 停止录制")
        self._send(encode_control(MessageCode.CTRL_STOP_RECORDING))

    def start_acquisition(self) -> None:
        log.info("-> 发送 开始采集")
        self._send(encode_control(MessageCode.CTRL_START_ACQUISITION))

    def stop_acquisition(self) -> None:
        log.info("-> 发送 停止采集")
        self._send(encode_control(MessageCode.CTRL_STOP_ACQUISITION))

    def start_impedance(self) -> None:
        log.info("-> 发送 阻抗检测")
        self._send(encode_control(MessageCode.CTRL_START_IMPEDANCE))
