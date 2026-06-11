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

import numpy as np

from .logging_util import hexdump
from .models import DataBlock, SessionInfo
from .protocol import (
    HEADER_SIZE,
    ID_CTRL,
    ID_INFO,
    SAMPLE_DTYPE,
    FrameHeader,
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
        『开始采集 + 开始录制』控制指令，再继续监听 —— 用来一次验证
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
                log.info("=== peek 阶段 B：发送 开始采集 + 开始录制，再继续监听 ===")
                try:
                    self.start_acquisition()
                    self.start_recording()
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
            "帧头 id=%r code=%d request=%d sample=%d data_size=%d | %s",
            header.chid.rstrip(b"\x00"), header.code, header.request,
            header.sample, header.data_size, hexdump(raw_header),
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
            n_channels=len(labels),
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

            cid = header.chid.rstrip(b"\x00")
            if cid == ID_CTRL:
                # 控制/状态/握手消息（已抓包确认的类型，通常无 payload）
                log.info(
                    "CTRL 控制消息: code=%d request=%d (payload %dB)",
                    header.code, header.request, len(payload),
                )
            elif cid == ID_INFO:
                self._handle_info(header, payload)
            elif payload:
                # 带 payload 的非控制帧：先当数据块试解。
                # 注意把真实 id 打出来，以便确认 DATA 块到底用什么标识。
                log.info("数据帧 id=%r（%d 字节 payload）", cid, len(payload))
                on_data(self._handle_data(header, payload))
                n += 1
                if max_blocks is not None and n >= max_blocks:
                    log.info("已达 max_blocks=%d，停止接收。", max_blocks)
                    break
            else:
                log.warning(
                    "未知帧 id=%r code=%d request=%d（无 payload）—— 待校准",
                    cid, header.code, header.request,
                )

    # ------------------------------------------------------------------ #
    # 控制流（客户端 -> 服务器）
    #
    # ⚠️ 控制码 (code/request) 尚未确认：手册要求向 Neuroscan 索取 demo 才有
    #    规范。下面的取值是【占位/试验用】，仅保证按真实的 20 字节大端结构发送。
    #    抓到一次"发了 X 之后服务器开始推流"的成功样本后，再把这些码改对。
    # ------------------------------------------------------------------ #
    # (code, request) —— 占位值，待校准
    _CTRL_TBD = {
        "start_acquisition": (1, 10),
        "stop_acquisition": (1, 11),
        "start_recording": (1, 20),
        "stop_recording": (1, 21),
        "start_impedance": (1, 30),
    }

    def _send(self, data: bytes) -> None:
        if self._sock is None:
            raise ConnectionError("未连接")
        self._sock.sendall(data)

    def send_control(self, code: int, request: int = 0) -> None:
        """发送任意控制码（用于试验/逆向）。"""
        log.info("-> 发送控制: code=%d request=%d", code, request)
        self._send(encode_control(code, request))

    def _send_named(self, name: str) -> None:
        code, request = self._CTRL_TBD[name]
        log.info("-> 发送 %s （控制码待确认: code=%d request=%d）", name, code, request)
        self._send(encode_control(code, request))

    def start_recording(self) -> None:
        self._send_named("start_recording")

    def stop_recording(self) -> None:
        self._send_named("stop_recording")

    def start_acquisition(self) -> None:
        self._send_named("start_acquisition")

    def stop_acquisition(self) -> None:
        self._send_named("stop_acquisition")

    def start_impedance(self) -> None:
        self._send_named("start_impedance")
