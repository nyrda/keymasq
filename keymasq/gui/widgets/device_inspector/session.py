from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from .model import Payload

ResponseCallback = Callable[[Payload | None], bool | None]
EventCallback = Callable[[Payload], bool | None]


class Request(Protocol):
    def __call__(
        self,
        payload: Payload,
        callback: ResponseCallback,
        timeout: float = 5.0,
    ) -> object: ...


Register = Callable[[str, EventCallback], None]


@dataclass
class InspectorSession:
    """Owns inspector IPC registration and its start/stop transaction."""

    hardware_id: str
    request: Request
    register: Register
    unregister: Register
    closing: bool = False
    finalized: bool = False
    stop_sent: bool = False
    _callbacks: dict[str, EventCallback] = field(default_factory=dict)

    def start(
        self,
        callbacks: Mapping[str, EventCallback],
        response: ResponseCallback,
    ) -> None:
        self._callbacks = dict(callbacks)
        for event_name, callback in self._callbacks.items():
            self.register(event_name, callback)
        self.request(
            {"command": "start_device_inspector", "hardware_id": self.hardware_id},
            response,
            timeout=6.0,
        )

    def request_snapshot(self, response: ResponseCallback) -> None:
        if self.closing:
            return
        self.request(
            {
                "command": "get_device_inspector_snapshot",
                "hardware_id": self.hardware_id,
            },
            response,
            timeout=3.0,
        )

    def set_suppressed(
        self,
        suppressed: bool,
        response: ResponseCallback,
        *,
        reason: str = "manual",
    ) -> None:
        command = (
            "enable_device_inspector_suppression"
            if suppressed
            else "disable_device_inspector_suppression"
        )
        payload: Payload = {"command": command, "hardware_id": self.hardware_id}
        if not suppressed:
            payload["reason"] = reason
        self.request(payload, response, timeout=3.0)

    def finalize(self) -> bool:
        """Unregister and stop once; return whether this call finalized the session."""

        if self.finalized:
            return False
        self.finalized = True
        self.closing = True
        for event_name, callback in self._callbacks.items():
            self.unregister(event_name, callback)
        self._callbacks.clear()
        self.stop()
        return True

    def stop(self) -> None:
        if self.stop_sent:
            return
        self.stop_sent = True
        self.request(
            {"command": "stop_device_inspector", "hardware_id": self.hardware_id},
            _ignore_response,
            timeout=1.5,
        )


def _ignore_response(_result: Payload | None) -> bool:
    return False
