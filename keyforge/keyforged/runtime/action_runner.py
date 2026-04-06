import asyncio
from collections.abc import Awaitable, Callable
from typing import TypedDict

from keyforge.common.ipc import CommandType
from keyforge.common.models import ActionType, MappingAction

type JsonObject = dict[str, object]
type BroadcastCallback = Callable[[CommandType, JsonObject], Awaitable[None]]
type FireAndObserve = Callable[[Awaitable[object], str], asyncio.Task[object]]


class MacroPlaybackRequest(TypedDict):
    macro_events: list[JsonObject]
    macro_name: str
    replay_mouse_movement: bool
    replay_mouse_clicks: bool
    speed: float
    loop_mode: str
    loop_count: int
    move_to_start: bool
    start_x: int
    start_y: int
    block_mouse_movement: bool
    source_device: str
    source_button: str
    trigger_value: int


def build_action_trigger_payload(
    action: MappingAction,
    *,
    source_device: str,
    source_button: str,
) -> JsonObject | None:
    if action.action_type == ActionType.EXEC:
        if action.exec_ref is None:
            return None
        return {
            "action_type": "exec",
            "exec_ref": action.exec_ref,
            "source_device": source_device,
            "source_button": source_button,
        }

    if action.action_type == ActionType.COMPOSITOR_DISPATCH:
        return {
            "action_type": "compositor_dispatch",
            "compositor": action.compositor_id or "",
            "dispatcher": action.compositor_dispatcher or "",
            "args": action.compositor_args or "",
            "source_device": source_device,
            "source_button": source_button,
        }

    if action.action_type in {
        ActionType.START_MACRO_RECORDING,
        ActionType.STOP_MACRO_RECORDING,
        ActionType.CANCEL_MACRO_PLAYBACK,
    }:
        return {
            "action_type": action.action_type.value,
            "source_device": source_device,
            "source_button": source_button,
        }

    if action.action_type in {
        ActionType.PROFILE_ENABLE,
        ActionType.PROFILE_DISABLE,
        ActionType.PROFILE_TOGGLE,
    }:
        return {
            "action_type": action.action_type.value,
            "profile_name": action.profile_name or action.target or "",
            "source_device": source_device,
            "source_button": source_button,
        }

    return None


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
    command_type: type[CommandType],
    label: str,
) -> bool:
    if broadcast_callback is None or data is None:
        return False
    fire_and_observe_fn(
        broadcast_callback(command_type.ACTION_TRIGGER, data),
        label,
    )
    return True
