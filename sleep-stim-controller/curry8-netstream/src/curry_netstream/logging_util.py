"""日志与调试辅助。"""
from __future__ import annotations

import logging


def setup_logging(level: str = "INFO") -> None:
    """配置根日志器。level 取 DEBUG/INFO/WARNING/ERROR。"""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def hexdump(data: bytes, max_bytes: int = 64) -> str:
    """把字节串渲染成可读的十六进制（DEBUG 级排查协议用）。"""
    chunk = data[:max_bytes]
    hexs = " ".join(f"{b:02x}" for b in chunk)
    suffix = f" …(+{len(data) - max_bytes}B)" if len(data) > max_bytes else ""
    return f"[{len(data)}B] {hexs}{suffix}"
