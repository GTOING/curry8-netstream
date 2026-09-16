"""Single-threaded, on-demand session reader used by offline replay."""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from curry_netstream.models import DataBlock

from .recording import SessionReader


class SessionReplayWorker(QObject):
    opened = Signal(int, object, object)  # generation, overview, error
    block_loaded = Signal(int, int, int, object, object)
    block_error = Signal(int, int, int, str)
    closed = Signal(int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._generation = 0
        self._request_token = 0
        self._pending_request: tuple[int, int] | None = None
        self._stop_requested = False
        self._opened = False
        self._path: str | None = None

    @property
    def active(self) -> bool:
        with self._condition:
            return self._thread is not None and self._thread.is_alive()

    @property
    def request_token(self) -> int:
        with self._condition:
            return self._request_token

    @property
    def generation(self) -> int:
        with self._condition:
            return self._generation

    def open(self, path: str | Path) -> bool:
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._generation += 1
            generation = self._generation
            self._request_token = 0
            self._pending_request = None
            self._stop_requested = False
            self._opened = False
            self._path = str(path)
            thread = threading.Thread(
                target=self._run,
                args=(generation, self._path),
                name="session-replay-reader",
                daemon=False,
            )
            self._thread = thread
            thread.start()
            return True

    def request_block(self, index: int) -> int:
        with self._condition:
            if not self._opened or self._stop_requested:
                return 0
            self._request_token += 1
            token = self._request_token
            self._pending_request = (token, int(index))
            self._condition.notify_all()
            return token

    def close(self) -> None:
        with self._condition:
            if self._thread is None or not self._thread.is_alive():
                return
            self._stop_requested = True
            self._request_token += 1
            self._pending_request = None
            self._condition.notify_all()

    def _run(self, generation: int, path: str | None) -> None:
        reader: SessionReader | None = None
        try:
            try:
                reader = SessionReader(path or "")
                with self._condition:
                    self._opened = True
                self.opened.emit(generation, reader.overview, None)
            except Exception as exc:
                self.opened.emit(generation, None, str(exc) or type(exc).__name__)
            if reader is not None:
                while True:
                    with self._condition:
                        while self._pending_request is None and not self._stop_requested:
                            self._condition.wait()
                        if self._stop_requested:
                            break
                        request = self._pending_request
                        self._pending_request = None
                    if request is None:
                        continue
                    token, index = request
                    try:
                        block, entry = reader.read_block(index)
                        self.block_loaded.emit(
                            generation,
                            token,
                            index,
                            block,
                            entry,
                        )
                    except Exception as exc:
                        self.block_error.emit(
                            generation,
                            token,
                            index,
                            str(exc) or type(exc).__name__,
                        )
        finally:
            with self._condition:
                self._opened = False
                self._pending_request = None
                self._stop_requested = False
            self.closed.emit(generation)
