"""Shared runtime context and release-policy helpers for combo actions."""

from __future__ import annotations

from typing import cast

from keymasq.common.devices import resolve_evdev_code, resolve_evdev_event_type
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.keymasqd.combo_engine import RuntimeComboBinding
from keymasq.keymasqd.runtime.action.state import ActionRuntimeContext
from keymasq.keymasqd.runtime.combo.state import ComboManager, ComboRuntimeDeps
from keymasq.keymasqd.runtime.grabbed_device.types import ActionExecutionDeps, EvdevModule
from keymasq.keymasqd.runtime.mouse_actions import resolve_mouse_output_target


def action_execution_deps(deps: ComboRuntimeDeps) -> ActionExecutionDeps:
    return ActionExecutionDeps(
        asyncio_mod=deps.asyncio_mod,
        fire_and_observe_fn=deps.fire_and_observe_fn,
        evdev_mod=cast(EvdevModule, deps.evdev_mod),
        uinput_writer=deps.uinput_writer,
    )


def action_runtime(
    manager: ComboManager,
    combo_id: str,
    action: MappingAction,
    trigger_binding: RuntimeComboBinding,
    *,
    trigger_name: str,
) -> ActionRuntimeContext:
    return ActionRuntimeContext(
        path=f"combo:{combo_id}:{trigger_name}",
        hardware_id=(
            "combo" if action.action_type == ActionType.MACRO else trigger_binding.hardware_id
        ),
        uinput=None,
        keyboard_uinput=manager.output_state.keyboard_uinput,
        mouse_uinput=manager.output_state.mouse_uinput,
        gamepad_uinput=manager.output_state.gamepad_uinput,
        broadcast_callback=manager.broadcast_callback,
        cursor_position_setter=manager.set_cursor_position,
        natural_mouse_mover=manager.move_cursor_natural,
        macro_player=manager.play_macro,
        emergency_resetter=manager.emergency_reset,
        repeat_state=manager.repeat_state,
        gamepad_output_resolver=lambda output_id, context: manager.resolve_gamepad_output(
            output_id,
            context=context,
        ),
    )


class SyntheticEvent:
    """Minimal evdev-shaped event used to execute combo actions."""

    def __init__(self, event_type: int | None, code: int | None, value: int) -> None:
        self.type = int(event_type or 0)
        self.code = int(code or 0)
        self.value = int(value)


def synthetic_event(
    trigger_binding: RuntimeComboBinding | None,
    value: int,
) -> SyntheticEvent:
    evdev_name = trigger_binding.evdev if trigger_binding is not None else None
    return SyntheticEvent(
        resolve_evdev_event_type(evdev_name),
        resolve_evdev_code(evdev_name),
        value,
    )


def action_needs_release(action: MappingAction) -> bool:
    if action.action_type in {
        ActionType.KEYBOARD,
        ActionType.GAMEPAD,
        ActionType.GAMEPAD_AXIS,
        ActionType.REPEAT,
    }:
        return True
    if action.action_type == ActionType.MOUSE:
        target = resolve_mouse_output_target(action.target)
        return bool(
            action.tap_enabled
            or action.rapidfire_enabled
            or (target is not None and not target.is_relative)
        )
    if action.action_type in {
        ActionType.MOUSE_MOVE_REL,
        ActionType.MOUSE_MOVE_ABS,
        ActionType.MOUSE_MOVE_NATURAL_ABS,
    }:
        return bool(action.tap_enabled or action.rapidfire_enabled)
    if action.action_type == ActionType.MACRO:
        # Even a Once parent can contain children that follow trigger release.
        return True
    return action.action_type in {
        ActionType.EXEC,
        ActionType.COMPOSITOR_DISPATCH,
        ActionType.MPRIS,
        ActionType.START_MACRO_RECORDING,
        ActionType.STOP_MACRO_RECORDING,
        ActionType.PLAY_MACRO_SLOT,
        ActionType.PROFILE_ENABLE,
        ActionType.PROFILE_DISABLE,
        ActionType.PROFILE_TOGGLE,
    }


def uses_tap_task(action: MappingAction | None) -> bool:
    return bool(action and action.tap_enabled)


def profile_action_tracks_trigger(action: MappingAction) -> bool:
    return (
        action.action_type in {ActionType.PROFILE_ENABLE, ActionType.PROFILE_TOGGLE}
        and action.profile_deactivation is not None
        and action.profile_deactivation.on_trigger_end
    )
