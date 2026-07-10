"""Resolved combo payloads and their stable reconciliation signatures."""

import json
from typing import TYPE_CHECKING

from keymasq.session.profile.types import ResolvedCombo

from ..common import JsonObject
from .action import combo_action_signature_payload, combo_action_to_payload

if TYPE_CHECKING:
    from ..core import SessionManager


def signature(manager: "SessionManager", combos: list[ResolvedCombo]) -> str:
    payload: list[dict[str, object]] = []
    for combo in combos:
        if combo.action is None:
            continue
        action_data = combo_action_signature_payload(
            manager,
            combo.action,
            step_count=len(combo.steps),
        )
        if action_data is None:
            continue
        steps: list[dict[str, object]] = []
        for step in combo.steps:
            events = [
                {
                    "hardware_id": str(event.hardware_id or ""),
                    "source": str(event.source or ""),
                    "evdev": str(event.evdev or ""),
                }
                for event in step.events
                if event.evdev
            ]
            if not events:
                continue
            events.sort(
                key=lambda event: (
                    str(event["hardware_id"]),
                    str(event["source"]),
                    str(event["evdev"]),
                )
            )
            step_payload: dict[str, object] = {"events": events}
            if step.timeout_ms is not None:
                step_payload["timeout_ms"] = int(step.timeout_ms)
            steps.append(step_payload)
        if not steps:
            continue
        payload.append(
            {
                "id": combo.id,
                "name": combo.name,
                "profile_name": combo.profile_name,
                "steps": steps,
                "action": action_data,
                "match_across_devices": bool(combo.match_across_devices),
                **({"recall_trigger_keys": True} if combo.recall_trigger_keys else {}),
                **(
                    {"restore_trigger_keys": list(combo.restore_trigger_keys)}
                    if combo.restore_trigger_keys
                    else {}
                ),
            }
        )
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def serialize_all(
    manager: "SessionManager",
    combos: list[ResolvedCombo],
) -> list[JsonObject]:
    payload: list[JsonObject] = []
    for combo in combos:
        combo_payload = serialize(manager, combo)
        if combo_payload is not None:
            payload.append(combo_payload)
    return payload


def serialize(
    manager: "SessionManager",
    combo: ResolvedCombo,
) -> JsonObject | None:
    if combo.action is None:
        return None
    action_data = combo_action_to_payload(
        manager,
        combo.action,
        step_count=len(combo.steps),
    )
    if action_data is None:
        return None
    steps: list[dict[str, object]] = []
    for step in combo.steps:
        events: list[dict[str, str]] = []
        for event in step.events:
            if not event.evdev:
                continue
            event_data = {"evdev": event.evdev}
            if event.hardware_id:
                event_data["hardware_id"] = event.hardware_id
            if event.source:
                event_data["source"] = event.source
            events.append(event_data)
        if events:
            step_payload: dict[str, object] = {"events": events}
            if step.timeout_ms is not None:
                step_payload["timeout_ms"] = int(step.timeout_ms)
            steps.append(step_payload)
    if not steps:
        return None
    return {
        "id": combo.id,
        "name": combo.name,
        "profile_name": combo.profile_name,
        "steps": steps,
        "action": action_data,
        "match_across_devices": bool(combo.match_across_devices),
        **({"recall_trigger_keys": True} if combo.recall_trigger_keys else {}),
        **(
            {"restore_trigger_keys": list(combo.restore_trigger_keys)}
            if combo.restore_trigger_keys
            else {}
        ),
    }
