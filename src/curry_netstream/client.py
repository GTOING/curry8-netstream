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
import time
from typing import BinaryIO, Callable, Optional

from .logging_util import hexdump
from .models import DataBlock, SessionInfo
from .protocol import (
    CTRL_FROM_CLIENT,
    DATA_EEG,
    DATA_EVENTS,
    DATA_IMPEDANCES,
    DATA_INFO,
    DATA_TYPE_EVENT_LIST,
    DATA_TYPE_FLOAT32,
    DATA_TYPE_FLOAT32_ZIP,
    HEADER_SIZE,
    ID_CTRL,
    ID_DATA,
    INFO_BASIC_INFO,
    INFO_CHANNEL_INFO,
    INFO_STATUS_AMP,
    INFO_TIME,
    INFO_VERSION,
    REQUEST_AMP_CONNECT,
    REQUEST_AMP_DISCONNECT,
    REQUEST_BASIC_INFO,
    REQUEST_CHANNEL_INFO,
    REQUEST_IMPEDANCE_START,
    REQUEST_IMPEDANCE_STOP,
    REQUEST_RECORDING_START,
    REQUEST_RECORDING_STOP,
    REQUEST_STREAMING_START,
    REQUEST_STREAMING_STOP,
    VALID_IDS,
    BasicInfo,
    ChannelInfo,
    FrameHeader,
    decode_channel_info,
    decode_eeg_payload,
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
        max_payload_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.max_payload_bytes = max_payload_bytes
        self._dump_path = dump_path
        self._sock: Optional[socket.socket] = None
        self._dump: Optional[BinaryIO] = None
        self._basic_info: Optional[BasicInfo] = None
        self._channel_info: list[ChannelInfo] = []
        self._session: Optional[SessionInfo] = None
        self._running = False
        self._streaming_requested = False
        self._next_sample: Optional[int] = None

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
                if self._streaming_requested:
                    try:
                        self.stop_streaming()
                    except OSError:
                        pass
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
    # 诊断：纯原始字节窥探（不分帧）
    # ------------------------------------------------------------------ #
    def peek_raw(
        self,
        duration: float = 10.0,
        *,
        chunk: int = 4096,
        send_start: bool = False,
    ) -> int:
        """连接后只读原始字节、不做任何分帧，持续 duration 秒。

        用来回答最关键的问题：**服务器到底有没有在发数据？**
          - 全程 0 字节   -> 服务器静默：采集没跑，或需要客户端先握手/请求
          - 收到了字节     -> 有数据流，问题在我们的分帧/解析假设
        返回累计收到的字节数。

        send_start=True 时，会在被动监听一半时长后，主动发送
        『开始推流』控制指令，再继续监听 —— 用来一次验证
        『是否需要客户端触发』这个假设（日志会清楚标出两个阶段）。
        """
        if self._sock is None:
            raise ConnectionError("未连接")
        self._sock.settimeout(1.0)
        deadline = time.monotonic() + duration
        half = time.monotonic() + duration / 2
        total = 0
        sent = False
        log.info("=== peek 阶段 A：被动监听（不发任何东西）===")
        while time.monotonic() < deadline:
            if send_start and not sent and time.monotonic() >= half:
                log.info("=== peek 阶段 B：发送 开始推流，再继续监听 ===")
                try:
                    self.start_streaming()
                except OSError as exc:
                    log.error("发送控制指令失败: %s", exc)
                sent = True
            try:
                data = self._sock.recv(chunk)
            except (socket.timeout, TimeoutError):
                log.info("…等待中，累计收到 %d 字节（这 1 秒没有新数据）", total)
                continue
            if not data:
                log.warning("服务器主动关闭了连接（共收到 %d 字节）", total)
                break
            total += len(data)
            if self._dump is not None:
                self._dump.write(data)
            log.info("收到 %d 字节（累计 %d）: %s", len(data), total, hexdump(data))
        verdict = "服务器静默（0 字节）" if total == 0 else f"有数据流（{total} 字节）"
        log.info("peek 结束：%.1f 秒内 -> %s", duration, verdict)
        return total

    # ------------------------------------------------------------------ #
    # 底层收包
    # ------------------------------------------------------------------ #
    def _recv_exact(self, n: int) -> bytes:
        """精确读取 n 字节；超时后保留已收到的半包并继续等待。"""
        if self._sock is None:
            raise ConnectionError("未连接")
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self._sock.recv(n - len(buf))
            except (socket.timeout, TimeoutError):
                log.debug("等待剩余字节：已收到 %d/%d", len(buf), n)
                continue
            if not chunk:
                raise ConnectionError("服务器关闭了连接")
            buf += chunk
            if self._dump is not None:
                self._dump.write(chunk)
        return bytes(buf)

    def _read_frame(self) -> tuple[FrameHeader, bytes]:
        raw_header = self._recv_exact(HEADER_SIZE)
        header = FrameHeader.unpack(raw_header)
        if header.chid not in VALID_IDS:
            raise ValueError(f"未知 Curry 帧标识: {header.chid!r}")
        if header.data_size > self.max_payload_bytes:
            raise ValueError(
                f"payload 过大: {header.data_size} > {self.max_payload_bytes}"
            )
        log.debug(
            "帧头 id=%r code=%d request=%d sample=%d data_size=%d | %s",
            header.chid.rstrip(b"\x00"), header.code, header.request,
            header.sample, header.data_size, hexdump(raw_header),
        )
        payload = self._recv_exact(header.data_size) if header.data_size else b""
        if payload:
            log.debug("payload %s", hexdump(payload))
        return header, payload

    # ------------------------------------------------------------------ #
    # DATA_Info / DATA_Eeg 解码
    # ------------------------------------------------------------------ #
    def _handle_info(self, header: FrameHeader, payload: bytes) -> None:
        """Handle one DATA_Info subtype and advance the Curry handshake."""
        if header.request == INFO_BASIC_INFO:
            self._basic_info = BasicInfo.unpack(payload)
            self._session = SessionInfo(
                n_channels=self._basic_info.n_eeg_channels,
                sample_rate_hz=float(self._basic_info.sample_rate_hz),
                labels=[],
            )
            log.info(
                "基础信息: %d EEG 通道 @ %d Hz, data_size=%d",
                self._basic_info.n_eeg_channels,
                self._basic_info.sample_rate_hz,
                self._basic_info.data_size,
            )
            self.request_channel_info()
        elif header.request == INFO_CHANNEL_INFO:
            if self._basic_info is None or self._session is None:
                raise ValueError("收到通道信息前尚未收到 BasicInfo")
            self._channel_info = decode_channel_info(payload)
            n_eeg = self._basic_info.n_eeg_channels
            if len(self._channel_info) < n_eeg:
                raise ValueError(
                    f"通道信息只有 {len(self._channel_info)} 条，少于 EEG 通道数 {n_eeg}"
                )
            labels = [item.label for item in self._channel_info[:n_eeg]]
            self._session = SessionInfo(
                n_channels=n_eeg,
                sample_rate_hz=float(self._basic_info.sample_rate_hz),
                labels=labels,
            )
            log.info("通道信息就绪: %s", ", ".join(labels))
            self.start_streaming()
        elif header.request == INFO_VERSION and len(payload) == 4:
            version = int.from_bytes(payload, "little")
            log.info("NetStreaming 协议版本: %d", version)
        elif header.request == INFO_STATUS_AMP and len(payload) == 4:
            status = int.from_bytes(payload, "little")
            log.info("放大器状态: %d", status)
        elif header.request == INFO_TIME and len(payload) == 4:
            timestamp = int.from_bytes(payload, "little")
            log.debug("服务器时间包: %d", timestamp)
        else:
            log.warning(
                "未处理 DATA_Info subtype=%d payload=%dB",
                header.request,
                len(payload),
            )

    def _handle_data(self, header: FrameHeader, payload: bytes) -> DataBlock:
        """Decode one uncompressed Curry EEG body to [channel, sample]."""
        sess = self._session
        if sess is None or sess.n_channels <= 0 or sess.sample_rate_hz <= 0:
            raise ValueError("会话信息未就绪，无法可靠解码 EEG")
        data = decode_eeg_payload(payload, sess.n_channels)
        if self._next_sample is not None and header.sample != self._next_sample:
            raise ValueError(
                f"EEG 采样不连续: 预期 start_sample={self._next_sample}，"
                f"实际 {header.sample}"
            )
        self._next_sample = header.sample + data.shape[1]
        return DataBlock(
            data=data,
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
        """完成握手并阻塞接收；每个 EEG 包回调一个 DataBlock。"""
        self._running = True
        n = 0
        self.request_basic_info()
        while self._running:
            try:
                header, payload = self._read_frame()
            except (ConnectionError, OSError) as exc:
                log.error("连接中断: %s", exc)
                break
            except ValueError as exc:
                log.error("协议错误，停止接收: %s", exc)
                raise

            if header.chid == ID_CTRL:
                log.info(
                    "CTRL 控制消息: code=%d request=%d (payload %dB)",
                    header.code, header.request, len(payload),
                )
            elif header.chid == ID_DATA and header.code == DATA_INFO:
                self._handle_info(header, payload)
            elif header.chid == ID_DATA and header.code == DATA_EEG:
                if header.request == DATA_TYPE_FLOAT32:
                    block = self._handle_data(header, payload)
                    on_data(block)
                    n += 1
                    if max_blocks is not None and n >= max_blocks:
                        log.info("已达 max_blocks=%d，停止接收。", max_blocks)
                        break
                elif header.request == DATA_TYPE_FLOAT32_ZIP:
                    raise ValueError(
                        "不支持压缩 EEG；请在 Curry 中选择未压缩 float32"
                    )
                else:
                    log.warning("不支持的 EEG 数据类型: %d", header.request)
            elif header.chid == ID_DATA and header.code == DATA_EVENTS:
                if header.request == DATA_TYPE_EVENT_LIST:
                    log.debug("忽略事件包: %dB（当前目标仅 EEG）", len(payload))
            elif header.chid == ID_DATA and header.code == DATA_IMPEDANCES:
                log.debug("忽略阻抗包: %dB", len(payload))
            else:
                log.warning(
                    "未处理帧 id=%r code=%d request=%d payload=%dB",
                    header.chid, header.code, header.request, len(payload),
                )

        if self._streaming_requested and self._sock is not None:
            try:
                self.stop_streaming()
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    # 控制流（客户端 -> 服务器）
    # ------------------------------------------------------------------ #
    def _send(self, data: bytes) -> None:
        if self._sock is None:
            raise ConnectionError("未连接")
        self._sock.sendall(data)

    def send_control(self, request: int, code: int = CTRL_FROM_CLIENT) -> None:
        """Send one header-only Curry control request."""
        log.info("-> 发送控制: code=%d request=%d", code, request)
        self._send(encode_control(request=request, code=code))

    def request_basic_info(self) -> None:
        self.send_control(REQUEST_BASIC_INFO)

    def request_channel_info(self) -> None:
        self.send_control(REQUEST_CHANNEL_INFO)

    def start_streaming(self) -> None:
        self._next_sample = None
        self.send_control(REQUEST_STREAMING_START)
        self._streaming_requested = True

    def stop_streaming(self) -> None:
        self.send_control(REQUEST_STREAMING_STOP)
        self._streaming_requested = False
        self._next_sample = None

    def start_recording(self) -> None:
        self.send_control(REQUEST_RECORDING_START)

    def stop_recording(self) -> None:
        self.send_control(REQUEST_RECORDING_STOP)

    def start_acquisition(self) -> None:
        self.send_control(REQUEST_AMP_CONNECT)

    def stop_acquisition(self) -> None:
        self.send_control(REQUEST_AMP_DISCONNECT)

    def start_impedance(self) -> None:
        self.send_control(REQUEST_IMPEDANCE_START)

    def stop_impedance(self) -> None:
        self.send_control(REQUEST_IMPEDANCE_STOP)
