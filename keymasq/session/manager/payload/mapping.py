"""Device mapping payloads, stable signatures, and logging views."""

import json
from typing import TYPE_CHECKING, cast

from keymasq.session.profile.types import ResolvedDeviceProfile

from ..common import JsonObject, json_object
from .action import action_signature_payload, mapping_action_payload

if TYPE_CHECKING:
    from ..core import SessionManager


def update_needed(
    manager: "SessionManager",
    hardware_id: str,
    resolved: ResolvedDeviceProfile,
) -> bool:
    return manager.profile_state.last_sent_mapping_signatures.get(hardware_id, "") != signature(
        manager, resolved, hardware_id
    )


def signature(
    manager: "SessionManager",
    resolved: ResolvedDeviceProfile,
    hardware_id: str,
) -> str:
    mapping: dict[str, dict[str, object]] = {}
    for button_id in sorted(resolved.mappings):
        mapping[button_id] = action_signature_payload(
            manager,
            resolved.mappings[button_id],
            hardware_id,
        )
    return json.dumps(mapping, sort_keys=True, separators=(",", ":"))


def serialize(
    manager: "SessionManager",
    resolved: ResolvedDeviceProfile,
    hardware_id: str,
) -> JsonObject:
    """Build a daemon-facing source-button to action mapping."""
    if hardware_id not in manager.exec_state.device_exec_refs:
        manager.exec_state.device_exec_refs[hardware_id] = set()

    mapping: dict[str, dict[str, object]] = {}
    for button_id, action in resolved.mappings.items():
        mapping[button_id] = mapping_action_payload(manager, action, hardware_id)
    return cast(JsonObject, mapping)


def log_view(mapping: JsonObject) -> JsonObject:
    """Elide bulky inline macro events while retaining useful mapping context."""
    view: JsonObject = {}
    for button_id, action_data in mapping.items():
        action_data_dict = json_object(action_data)
        if action_data_dict is None:
            view[button_id] = action_data
            continue

        data = dict(action_data_dict)
        events = data.get("macro_events")
        if isinstance(events, list):
            event_items = cast(list[object], events)
            data["macro_events"] = f"<{len(event_items)} events>"
        view[button_id] = data

    return view
