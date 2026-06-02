from typing import TYPE_CHECKING

from keymasq.session.profiles import ResolvedCombo

from .common import JsonObject
from .payloads import serialize_mapping_action

if TYPE_CHECKING:
    from .core import SessionManager


def build_combo_inspector_snapshot(manager: "SessionManager") -> JsonObject:
    return {
        "status": "ok",
        "active_profiles": list(manager.profile_state.active_profile_names),
        "combos": [
            _serialize_combo(manager, combo, index)
            for index, combo in enumerate(manager.profile_state.resolved_combos)
        ],
    }


def _serialize_combo(
    manager: "SessionManager",
    combo: ResolvedCombo,
    index: int,
) -> JsonObject:
    return {
        "id": combo.id,
        "name": combo.name,
        "profile_name": combo.profile_name,
        "order": index,
        "steps": [
            {
                **(
                    {"timeout_ms": int(step.timeout_ms)}
                    if step.timeout_ms is not None
                    else {}
                ),
                "events": [
                    {
                        "evdev": event.evdev,
                        "hardware_id": event.hardware_id or "",
                        "source": event.source or "",
                        "device_name": _device_name(manager, event.hardware_id),
                    }
                    for event in step.events
                    if event.evdev
                ],
            }
            for step in combo.steps
            if any(event.evdev for event in step.events)
        ],
        "action": serialize_mapping_action(combo.action),
        "recall_trigger_keys": bool(combo.recall_trigger_keys),
        "restore_trigger_keys": list(combo.restore_trigger_keys),
        "match_across_devices": bool(combo.match_across_devices),
    }


def _device_name(manager: "SessionManager", hardware_id: str | None) -> str:
    if not hardware_id:
        return ""
    hardware = manager.hardware.get_hardware(hardware_id)
    return hardware.name if hardware is not None else hardware_id
