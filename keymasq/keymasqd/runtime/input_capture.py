"""An exclusive setup reader attached to an already owned input stream."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from keymasq.keymasqd.runtime.grabbed_device.types import InputEventLike


class InputCaptureStream:
    def __init__(self) -> None:
        self._consumer: Callable[[InputEventLike], None] | None = None
        self._lock = Lock()

    def attach(self, consumer: Callable[[InputEventLike], None]) -> None:
        with self._lock:
            if self._consumer is not None:
                raise RuntimeError("This interface already has an active capture")
            self._consumer = consumer

    def detach(self, consumer: Callable[[InputEventLike], None]) -> None:
        with self._lock:
            if self._consumer is consumer:
                self._consumer = None

    def feed(self, event: InputEventLike) -> bool:
        with self._lock:
            consumer = self._consumer
            if consumer is None:
                return False
            consumer(event)
            return True
