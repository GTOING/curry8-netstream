"""Bounded latest-item handoff between the Curry worker and Qt thread."""
from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Generic, TypeVar


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class LatestBufferStats:
    """Counters useful for showing whether display updates were replaced."""

    replaced_count: int
    delivered_count: int
    pending: bool


class LatestItemBuffer(Generic[T]):
    """Keep at most one item waiting for the consumer.

    A producer can replace an item that has not reached the GUI yet. The
    replacement count is cumulative and deliberately separate from the
    number of accepted EEG blocks, so this display optimization cannot be
    mistaken for a data retention policy in later stages.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._pending: T | None = None
        self._replaced_count = 0
        self._delivered_count = 0

    def put(self, item: T) -> None:
        with self._lock:
            if self._pending is not None:
                self._replaced_count += 1
            self._pending = item

    def take(self) -> T | None:
        with self._lock:
            item = self._pending
            self._pending = None
            if item is not None:
                self._delivered_count += 1
            return item

    def clear(self) -> None:
        with self._lock:
            self._pending = None

    def stats(self) -> LatestBufferStats:
        with self._lock:
            return LatestBufferStats(
                replaced_count=self._replaced_count,
                delivered_count=self._delivered_count,
                pending=self._pending is not None,
            )
