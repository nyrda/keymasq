from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

import evdev

from keymasq.common.types import JsonObject

INSPECTOR_UPDATE_INTERVAL_S = 1 / 60


def normalize_hardware_id(hardware_id: str) -> str:
    normalized = str(hardware_id or "").strip()
    if not normalized:
        raise ValueError("hardware_id required")
    return normalized


@dataclass(frozen=True)
class InspectorTransition:
    hardware_id: str
    active: bool
    suppressed: bool
    reset_runtime: bool = False

    def response(self, *, reason: str | None = None) -> JsonObject:
        payload: JsonObject = {
            "status": "ok",
            "hardware_id": self.hardware_id,
            "active": self.active,
            "suppressed": self.suppressed,
        }
        if reason is not None:
            payload["reason"] = reason
        return payload


@dataclass
class DeviceInspectorState:
    active_hardware_ids: set[str] = field(default_factory=set)
    suppressed_hardware_ids: set[str] = field(default_factory=set)
    event_sequence: int = 0
    _pending_axes: dict[tuple[str, str, str, str], JsonObject] = field(default_factory=dict)
    _flush_handle: asyncio.TimerHandle | None = None

    def reset(self) -> None:
        self._cancel_flush()
        self._pending_axes.clear()
        self.active_hardware_ids.clear()
        self.suppressed_hardware_ids.clear()

    def queue_event(self, payload: JsonObject, emit: Callable[[JsonObject], None]) -> None:
        hardware_id = str(payload.get("hardware_id", ""))
        if not self.is_active(hardware_id):
            return
        event_type, code = payload.get("type"), payload.get("code")
        if (
            event_type == evdev.ecodes.EV_ABS
            and isinstance(code, int)
            and code < evdev.ecodes.ABS_MT_SLOT
        ) or (
            event_type == evdev.ecodes.EV_SYN and code == evdev.ecodes.SYN_REPORT
        ):
            key = (hardware_id, str(payload.get("path", "")), str(event_type), str(code))
            # Retain the last value in arrival order, including the frame boundary.
            self._pending_axes.pop(key, None)
            self._pending_axes[key] = payload
            if self._flush_handle is None:
                self._flush_handle = asyncio.get_running_loop().call_later(
                    INSPECTOR_UPDATE_INTERVAL_S, self.flush_events, emit
                )
            return

        # Preserve button edges, relative movement, and exceptional SYN reports.
        # Flush preceding axis values so their order relative to these is retained.
        source = (hardware_id, str(payload.get("path", "")))
        for key in list(self._pending_axes):
            if key[:2] == source:
                self._emit_event(self._pending_axes.pop(key), emit)
        self._emit_event(payload, emit)

    def flush_events(self, emit: Callable[[JsonObject], None]) -> None:
        self._cancel_flush()
        pending = list(self._pending_axes.values())
        self._pending_axes.clear()
        for payload in pending:
            self._emit_event(payload, emit)

    def _emit_event(self, payload: JsonObject, emit: Callable[[JsonObject], None]) -> None:
        event_payload = self.event_payload(payload)
        if event_payload is not None:
            emit(event_payload)

    def _cancel_flush(self) -> None:
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None

    def is_active(self, hardware_id: str) -> bool:
        return str(hardware_id or "").strip() in self.active_hardware_ids

    def is_suppressed(self, hardware_id: str) -> bool:
        return str(hardware_id or "").strip() in self.suppressed_hardware_ids

    def suppressed_snapshot(self) -> set[str]:
        return set(self.suppressed_hardware_ids)

    def event_payload(self, payload: JsonObject) -> JsonObject | None:
        hardware_id = str(payload.get("hardware_id", "") or "").strip()
        if not hardware_id or not self.is_active(hardware_id):
            return None
        self.event_sequence += 1
        return {**payload, "sequence": self.event_sequence}

    def status_payload(self, hardware_id: str, reason: str) -> JsonObject:
        normalized = str(hardware_id or "").strip()
        return {
            "hardware_id": normalized,
            "active": self.is_active(normalized),
            "suppressed": self.is_suppressed(normalized),
            "reason": str(reason or ""),
        }

    def start(self, hardware_id: str) -> InspectorTransition:
        normalized = normalize_hardware_id(hardware_id)
        self.active_hardware_ids.add(normalized)
        return self._transition(normalized)

    def stop(self, hardware_id: str) -> InspectorTransition:
        normalized = normalize_hardware_id(hardware_id)
        self._pending_axes = {
            key: payload for key, payload in self._pending_axes.items() if key[0] != normalized
        }
        if not self._pending_axes:
            self._cancel_flush()
        reset_runtime = normalized in self.suppressed_hardware_ids
        self.suppressed_hardware_ids.discard(normalized)
        self.active_hardware_ids.discard(normalized)
        return self._transition(normalized, reset_runtime=reset_runtime)

    def enable_suppression(self, hardware_id: str) -> InspectorTransition:
        normalized = normalize_hardware_id(hardware_id)
        self.active_hardware_ids.add(normalized)
        self.suppressed_hardware_ids.add(normalized)
        return self._transition(normalized, reset_runtime=True)

    def disable_suppression(self, hardware_id: str) -> InspectorTransition:
        normalized = normalize_hardware_id(hardware_id)
        reset_runtime = normalized in self.suppressed_hardware_ids
        self.suppressed_hardware_ids.discard(normalized)
        return self._transition(normalized, reset_runtime=reset_runtime)

    def _transition(
        self,
        hardware_id: str,
        *,
        reset_runtime: bool = False,
    ) -> InspectorTransition:
        return InspectorTransition(
            hardware_id=hardware_id,
            active=self.is_active(hardware_id),
            suppressed=self.is_suppressed(hardware_id),
            reset_runtime=reset_runtime,
        )
