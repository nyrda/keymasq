from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any

import evdev

from keymasq.common.devices import high_res_wheel_low_res_code, normalize_wheel_value
from keymasq.common.models import (
    REPEAT_CATEGORY_GAMEPAD,
    REPEAT_CATEGORY_KEYBOARD,
    REPEAT_CATEGORY_MACRO,
    REPEAT_CATEGORY_MOUSE,
    REPEAT_CATEGORY_SPECIAL,
    ActionType,
    MappingAction,
    SuperkeyMode,
    normalize_repeat_categories,
)
from keymasq.keymasqd.runtime.mouse_actions import resolve_mouse_output_target

REPEAT_HISTORY_LIMIT = 32
SUPERKEY_SLOT_TAP = "tap"
SUPERKEY_SLOT_DOUBLE_TAP = "double_tap"
SUPERKEY_SLOT_HOLD = "hold"
SUPERKEY_SLOT_TAP_HOLD = "tap_hold"
SUPERKEY_SLOT_OVERLOAD = "overload"
SUPERKEY_REPEAT_SLOTS = frozenset(
    {
        SUPERKEY_SLOT_TAP,
        SUPERKEY_SLOT_DOUBLE_TAP,
        SUPERKEY_SLOT_HOLD,
        SUPERKEY_SLOT_TAP_HOLD,
        SUPERKEY_SLOT_OVERLOAD,
    }
)

_SPECIAL_REPEAT_ACTION_TYPES = frozenset(
    {
        ActionType.EXEC,
        ActionType.COMPOSITOR_DISPATCH,
        ActionType.START_MACRO_RECORDING,
        ActionType.STOP_MACRO_RECORDING,
        ActionType.CANCEL_MACRO_PLAYBACK,
        ActionType.PROFILE_ENABLE,
        ActionType.PROFILE_DISABLE,
        ActionType.PROFILE_TOGGLE,
    }
)


@dataclass(frozen=True)
class RepeatHistoryEntry:
    category: str
    action: MappingAction
    source_device: str = ""
    source_button: str = ""
    superkey_slot: str | None = None


@dataclass
class RepeatRuntimeState:
    history: deque[RepeatHistoryEntry] = field(
        default_factory=lambda: deque(maxlen=REPEAT_HISTORY_LIMIT)
    )


def repeat_category_for_action(action: MappingAction) -> str | None:
    action_type = action.action_type
    if action_type in {ActionType.REPEAT, ActionType.SUPPRESS, ActionType.PASSTHROUGH}:
        return None
    if action_type == ActionType.KEYBOARD:
        return REPEAT_CATEGORY_KEYBOARD
    if action_type == ActionType.MOUSE:
        return REPEAT_CATEGORY_MOUSE
    if action_type in {ActionType.MOUSE_MOVE_REL, ActionType.MOUSE_MOVE_ABS}:
        return REPEAT_CATEGORY_SPECIAL
    if action_type in {ActionType.GAMEPAD, ActionType.GAMEPAD_AXIS}:
        return REPEAT_CATEGORY_GAMEPAD
    if action_type == ActionType.MACRO:
        return REPEAT_CATEGORY_MACRO
    if action_type in _SPECIAL_REPEAT_ACTION_TYPES:
        return REPEAT_CATEGORY_SPECIAL
    return None


def action_supports_repeat_rapidfire(action: MappingAction) -> bool:
    if action.action_type in {ActionType.KEYBOARD, ActionType.GAMEPAD}:
        return True
    if action.action_type != ActionType.MOUSE:
        return False
    target = resolve_mouse_output_target(action.target)
    if target is None:
        return False
    if not target.is_relative:
        return True
    return int(target.code) in {int(evdev.ecodes.REL_WHEEL), int(evdev.ecodes.REL_HWHEEL)}


def repeat_execution_action(
    repeat_action: MappingAction,
    action: MappingAction,
) -> MappingAction:
    rapidfire_enabled = bool(
        repeat_action.rapidfire_enabled and action_supports_repeat_rapidfire(action)
    )
    return replace(
        action,
        rapidfire_enabled=rapidfire_enabled,
        rapidfire_hold_ms=int(repeat_action.rapidfire_hold_ms),
        rapidfire_wait_ms=int(repeat_action.rapidfire_wait_ms),
        tap_enabled=False,
    )


def remember_action(
    repeat_state: RepeatRuntimeState | None,
    action: MappingAction,
    *,
    source_device: str = "",
    source_button: str = "",
) -> None:
    if repeat_state is None:
        return
    category = repeat_category_for_action(action)
    if category is None:
        return
    repeat_state.history.append(
        RepeatHistoryEntry(
            category=category,
            action=replace(
                action,
                rapidfire_enabled=False,
                tap_enabled=False,
            ),
            source_device=str(source_device or ""),
            source_button=str(source_button or ""),
        )
    )


def remember_superkey_path(
    repeat_state: RepeatRuntimeState | None,
    action: MappingAction,
    slot: str,
    *,
    source_device: str = "",
    source_button: str = "",
) -> None:
    if repeat_state is None:
        return
    if action.action_type != ActionType.SUPERKEY or action.superkey_config is None:
        return
    normalized_slot = str(slot or "").strip()
    if normalized_slot not in SUPERKEY_REPEAT_SLOTS:
        return
    if normalized_slot == SUPERKEY_SLOT_OVERLOAD:
        if action.superkey_config.mode != SuperkeyMode.OVERLOAD:
            return
    elif action.superkey_config.mode == SuperkeyMode.OVERLOAD:
        return
    repeat_state.history.append(
        RepeatHistoryEntry(
            category=REPEAT_CATEGORY_SPECIAL,
            action=replace(
                action,
                rapidfire_enabled=False,
                tap_enabled=False,
            ),
            source_device=str(source_device or ""),
            source_button=str(source_button or ""),
            superkey_slot=normalized_slot,
        )
    )


def select_repeated_action(
    repeat_state: RepeatRuntimeState | None,
    repeat_action: MappingAction,
) -> MappingAction | None:
    entry = select_repeated_entry(repeat_state, repeat_action)
    return entry.action if entry is not None else None


def select_repeated_entry(
    repeat_state: RepeatRuntimeState | None,
    repeat_action: MappingAction,
) -> RepeatHistoryEntry | None:
    if repeat_state is None:
        return None
    allowed = set(normalize_repeat_categories(repeat_action.repeat_categories))
    if not allowed:
        return None
    for entry in reversed(repeat_state.history):
        if entry.action.action_type == ActionType.REPEAT:
            continue
        if entry.category in allowed:
            return replace(entry, action=repeat_execution_action(repeat_action, entry.action))
    return None


def refresh_repeated_exec_source(
    repeat_state: RepeatRuntimeState | None,
    repeated_entry: RepeatHistoryEntry,
) -> None:
    if repeat_state is None or repeated_entry.action.action_type != ActionType.EXEC:
        return
    repeated_exec_ref = repeated_entry.action.exec_ref
    if not repeat_state.history:
        return
    latest = repeat_state.history[-1]
    if latest.action.action_type != ActionType.EXEC or latest.action.exec_ref != repeated_exec_ref:
        return
    repeat_state.history[-1] = replace(
        latest,
        source_device=repeated_entry.source_device,
        source_button=repeated_entry.source_button,
    )


def forget_exec_actions(
    repeat_state: RepeatRuntimeState | None,
    *,
    source_device: str | None = None,
    source_button_prefix: str | None = None,
    exclude_source_button_prefix: str | None = None,
) -> None:
    if repeat_state is None:
        return
    normalized_source_device = str(source_device or "").strip()
    normalized_source_button_prefix = str(source_button_prefix or "").strip()
    normalized_exclude_source_button_prefix = str(exclude_source_button_prefix or "").strip()
    if not (
        normalized_source_device
        or normalized_source_button_prefix
        or normalized_exclude_source_button_prefix
    ):
        return
    retained: list[RepeatHistoryEntry] = []
    for entry in repeat_state.history:
        if entry.action.action_type != ActionType.EXEC:
            retained.append(entry)
            continue
        if normalized_exclude_source_button_prefix and entry.source_button.startswith(
            normalized_exclude_source_button_prefix
        ):
            retained.append(entry)
            continue
        if normalized_source_device and entry.source_device != normalized_source_device:
            retained.append(entry)
            continue
        if normalized_source_button_prefix and not entry.source_button.startswith(
            normalized_source_button_prefix
        ):
            retained.append(entry)
            continue
    repeat_state.history.clear()
    repeat_state.history.extend(retained)


def remember_passthrough_event(
    repeat_state: RepeatRuntimeState | None,
    device_runtime: Any,
    event: Any,
    event_name: str,
    *,
    evdev_mod: Any,
) -> None:
    if repeat_state is None:
        return
    event_type = int(event.type)
    event_code = int(event.code)
    event_value = int(event.value)
    normalized_name = str(event_name or "").lower()

    if event_type == int(evdev_mod.ecodes.EV_KEY):
        if event_value != 1:
            return
        if normalized_name.startswith("key_"):
            action = MappingAction(action_type=ActionType.KEYBOARD, target=normalized_name)
        elif normalized_name.startswith("btn_"):
            if _device_is_gamepad(device_runtime):
                action = MappingAction(
                    action_type=ActionType.GAMEPAD,
                    target=normalized_name,
                    output_id=str(getattr(device_runtime, "hardware_id", "") or "") or None,
                )
            else:
                action = MappingAction(action_type=ActionType.MOUSE, target=normalized_name)
        else:
            return
        remember_action(
            repeat_state,
            action,
            source_device=getattr(device_runtime, "hardware_id", ""),
            source_button=normalized_name,
        )
        return

    if event_type != int(evdev_mod.ecodes.EV_REL):
        return

    low_res_event_code = high_res_wheel_low_res_code(event_code) or event_code
    if low_res_event_code == int(evdev_mod.ecodes.REL_WHEEL):
        wheel_name = "rel_wheel"
    elif low_res_event_code == int(evdev_mod.ecodes.REL_HWHEEL):
        wheel_name = "rel_hwheel"
    elif event_code in {int(evdev_mod.ecodes.REL_X), int(evdev_mod.ecodes.REL_Y)}:
        return
    else:
        return

    normalized_value = normalize_wheel_value(event_value)
    if normalized_value is None:
        return
    remember_action(
        repeat_state,
        MappingAction(action_type=ActionType.MOUSE, target=f"{wheel_name}:{normalized_value}"),
        source_device=getattr(device_runtime, "hardware_id", ""),
        source_button=f"{wheel_name}:{normalized_value}",
    )


def _device_is_gamepad(device_runtime: Any) -> bool:
    device_types = getattr(device_runtime, "device_types", []) or []
    return "gamepad" in {str(device_kind or "").strip().lower() for device_kind in device_types}
