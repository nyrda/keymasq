from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from keymasq.common.ipc import CommandType
from keymasq.common.model.actions import (
    MappingAction,
    normalize_macro_loop_stop_behavior,
    normalize_mpris_command,
    profile_deactivation_policy_to_dict,
)
from keymasq.common.model.core import ActionType
from keymasq.common.types import JsonObject
from keymasq.keymasqd.runtime.action.state import (
    BroadcastCallback,
    MacroPlaybackRequest,
)

type FireAndObserve = Callable[[Awaitable[object], str], asyncio.Task[object]]


def build_action_trigger_payload(
    action: MappingAction,
    *,
    source_device: str,
    source_button: str,
    trigger_id: str | None = None,
) -> JsonObject | None:
    base_payload: JsonObject = {
        "source_device": source_device,
        "source_button": source_button,
    }
    if trigger_id:
        base_payload["trigger_id"] = trigger_id
    if action.source_profile_name:
        base_payload["source_profile_name"] = action.source_profile_name

    if action.action_type == ActionType.EXEC:
        if action.exec_ref is None:
            return None
        return {"action_type": "exec", "exec_ref": action.exec_ref, **base_payload}

    if action.action_type == ActionType.COMPOSITOR_DISPATCH:
        return {
            "action_type": "compositor_dispatch",
            "compositor": action.compositor_id or "",
            "dispatcher": action.compositor_dispatcher or "",
            "args": action.compositor_args or "",
            **base_payload,
        }

    if action.action_type == ActionType.MPRIS:
        return {
            "action_type": "mpris",
            "command": normalize_mpris_command(action.mpris_command),
            **base_payload,
        }

    if action.action_type in {
        ActionType.START_MACRO_RECORDING,
        ActionType.STOP_MACRO_RECORDING,
        ActionType.PLAY_MACRO_SLOT,
        ActionType.CANCEL_MACRO_PLAYBACK,
        ActionType.EMERGENCY_RESET,
    }:
        payload: JsonObject = {"action_type": action.action_type.value, **base_payload}
        if action.action_type in {
            ActionType.START_MACRO_RECORDING,
            ActionType.STOP_MACRO_RECORDING,
            ActionType.PLAY_MACRO_SLOT,
        }:
            payload["recording_slot"] = int(action.macro_recording_slot)
        return payload

    if action.action_type in {
        ActionType.PROFILE_ENABLE,
        ActionType.PROFILE_DISABLE,
        ActionType.PROFILE_TOGGLE,
    }:
        payload = {
            "action_type": action.action_type.value,
            "profile_name": action.profile_name or action.target or "",
            **base_payload,
        }
        deactivation = profile_deactivation_policy_to_dict(action.profile_deactivation)
        if deactivation is not None and action.action_type != ActionType.PROFILE_DISABLE:
            payload["deactivation"] = deactivation
        return payload

    return None


def source_trigger_id(source_device: str, source_button: str) -> str:
    return f"{source_device}:{source_button}"


def build_macro_playback_request(
    action: MappingAction,
    *,
    source_device: str,
    source_button: str,
    trigger_value: int,
    include_macro_events: bool = True,
) -> MacroPlaybackRequest | None:
    if not (action.macro_events or action.macro_name):
        return None

    return {
        "macro_events": (action.macro_events or []) if include_macro_events else [],
        "macro_name": action.macro_name or "",
        "replay_mouse_movement": action.macro_replay_mouse_movement,
        "replay_mouse_clicks": action.macro_replay_mouse_clicks,
        "speed": action.macro_speed,
        "loop_mode": action.macro_loop_mode,
        "loop_count": action.macro_loop_count,
        "loop_stop_behavior": normalize_macro_loop_stop_behavior(action.macro_loop_stop_behavior),
        "move_to_start": action.macro_move_to_start,
        "start_x": action.macro_start_x,
        "start_y": action.macro_start_y,
        "block_mouse_movement": action.macro_block_mouse_movement,
        "source_device": source_device,
        "source_button": source_button,
        "trigger_value": trigger_value,
    }


def is_hold_macro_action(action: MappingAction) -> bool:
    return str(action.macro_loop_mode or "none").lower() == "hold"


def dispatch_action_trigger(
    broadcast_callback: BroadcastCallback | None,
    data: JsonObject | None,
    *,
    fire_and_observe_fn: FireAndObserve,
    label: str,
) -> bool:
    if broadcast_callback is None or data is None:
        return False
    fire_and_observe_fn(
        broadcast_callback(CommandType.ACTION_TRIGGER, data),
        label,
    )
    return True
