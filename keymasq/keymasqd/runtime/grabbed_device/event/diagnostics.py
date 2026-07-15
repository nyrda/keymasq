"""Diagnostics timing, labels, and logging for grabbed events."""

import logging

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.keymasqd.runtime.grabbed_device.types import (
    EvdevModule,
    GrabbedDeviceRuntime,
    InputEventLike,
    TimeModule,
)


def record_diagnostics(
    device_runtime: GrabbedDeviceRuntime,
    label: str,
    started_ns: int,
    *,
    time_mod: TimeModule,
) -> None:
    if device_runtime.diagnostics_recorder is None:
        return
    device_runtime.diagnostics_recorder(
        label,
        (time_mod.perf_counter_ns() - started_ns) / 1000.0,
    )


def action_diagnostic_label(action: MappingAction, *, combo_consumed: bool) -> str:
    prefix = "combo_release_action" if combo_consumed else "action"
    return f"{prefix}_{action.action_type.value}"


def passthrough_diagnostic_label(
    *,
    combo_passthrough_requested: bool,
    mapped_route: bool,
) -> str:
    if combo_passthrough_requested:
        return "combo_passthrough"
    return "passthrough_mapped" if mapped_route else "passthrough_fast"


def log_raw_hardware_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    *,
    evdev_mod: EvdevModule,
    log: logging.Logger,
) -> None:
    if device_runtime.verbosity < 3:
        return
    if event.type == evdev_mod.ecodes.EV_SYN:
        return
    if event.type == evdev_mod.ecodes.EV_REL and event.code in (
        evdev_mod.ecodes.REL_X,
        evdev_mod.ecodes.REL_Y,
    ):
        return
    log.debug(
        "[hw %s %s] type=%s code=%s name=%s value=%s",
        device_runtime.hardware_id,
        device_runtime.interface_id,
        event.type,
        event.code,
        event_name,
        event.value,
    )


def log_mapped_action(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction | None,
    event: InputEventLike,
    event_name: str,
    *,
    evdev_mod: EvdevModule,
    log: logging.Logger,
) -> None:
    if device_runtime.verbosity < 2:
        return
    if event.type == evdev_mod.ecodes.EV_REL and event.code in (
        evdev_mod.ecodes.REL_X,
        evdev_mod.ecodes.REL_Y,
    ):
        return
    if action is None:
        log.debug(
            "[%s] %s (%s) -> PASSTHROUGH value=%s",
            device_runtime.hardware_id,
            event_name,
            event.code,
            event.value,
        )
        return
    if action.action_type == ActionType.SUPPRESS:
        log.debug(
            "[%s] %s (%s) -> SUPPRESS value=%s",
            device_runtime.hardware_id,
            event_name,
            event.code,
            event.value,
        )
        return
    if action.action_type in (
        ActionType.KEYBOARD,
        ActionType.MOUSE,
        ActionType.GAMEPAD,
        ActionType.GAMEPAD_AXIS,
    ):
        target = action.target or "?"
        mods: list[str] = []
        if action.rapidfire_enabled:
            mods.append(f"rf:{action.rapidfire_hold_ms}/{action.rapidfire_wait_ms}")
        if action.tap_enabled:
            mods.append(f"tap:{action.tap_hold_ms}")
        mod_str = f" [{', '.join(mods)}]" if mods else ""
        log.debug(
            "[%s] %s (%s) -> %s:%s%s value=%s",
            device_runtime.hardware_id,
            event_name,
            event.code,
            action.action_type.value,
            f"{target}={int(action.axis_value)}"
            if action.action_type == ActionType.GAMEPAD_AXIS
            else target,
            mod_str,
            event.value,
        )
        return
    if action.action_type in (
        ActionType.MOUSE_MOVE_REL,
        ActionType.MOUSE_MOVE_ABS,
        ActionType.MOUSE_MOVE_NATURAL_ABS,
    ):
        log.debug(
            "[%s] %s (%s) -> %s x=%s y=%s value=%s",
            device_runtime.hardware_id,
            event_name,
            event.code,
            action.action_type.value,
            int(action.move_x),
            int(action.move_y),
            event.value,
        )
        return
    if action.action_type == ActionType.EXEC:
        log.debug(
            "[%s] %s (%s) -> EXEC %s value=%s",
            device_runtime.hardware_id,
            event_name,
            event.code,
            action.cmd or "",
            event.value,
        )
        return
    if action.action_type == ActionType.SUPERKEY:
        sk_name = action.superkey_config.name if action.superkey_config else "?"
        log.debug(
            "[%s] %s (%s) -> SUPERKEY:%s value=%s",
            device_runtime.hardware_id,
            event_name,
            event.code,
            sk_name,
            event.value,
        )
