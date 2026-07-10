from __future__ import annotations

from dataclasses import dataclass, field

from keymasq.common.types import JsonObject


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

    def reset(self) -> None:
        self.active_hardware_ids.clear()
        self.suppressed_hardware_ids.clear()

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
