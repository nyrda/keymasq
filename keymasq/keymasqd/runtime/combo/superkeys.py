"""Combo-triggered superkey machine construction and output tracking."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.common.model.superkeys import combo_effective_superkey_config
from keymasq.keymasqd.combo_engine import RuntimeComboBinding
from keymasq.keymasqd.runtime import adapters
from keymasq.keymasqd.runtime.action.triggers import dispatch_action_trigger
from keymasq.keymasqd.runtime.combo.execution import action_execution_deps
from keymasq.keymasqd.runtime.combo.recall import combo_step_count, ordered_unique_bindings
from keymasq.keymasqd.runtime.combo.state import ComboManager, ComboRuntimeDeps
from keymasq.keymasqd.runtime.grabbed_device.outputs import track_refcounted_output_bucket
from keymasq.keymasqd.runtime.grabbed_device.types import NaturalMouseMover
from keymasq.keymasqd.runtime.repeat import remember_superkey_path
from keymasq.keymasqd.superkey_state import SuperkeyConfig, SuperkeyMachine


def track_output(
    manager: ComboManager,
    action_type: str,
    code: int,
    value: int,
) -> bool:
    return track_refcounted_output_bucket(
        manager.combo_state.superkey_output_refcounts,
        manager.combo_state.held_output_keys,
        action_type,
        code,
        value,
    )


def effective_config(
    manager: ComboManager,
    combo_id: str,
    action: MappingAction,
) -> SuperkeyConfig | None:
    config = cast(SuperkeyConfig | None, action.superkey_config)
    if config is None:
        return None
    return combo_effective_superkey_config(
        config,
        step_count=combo_step_count(manager, combo_id),
    )


def releases_outputs_on_cancel(manager: ComboManager, combo_id: str) -> bool:
    return any(
        combo.id == combo_id and combo.release_outputs_on_cancel
        for combo in manager.combo_state.active_combos
    )


async def build_machine(
    manager: ComboManager,
    combo_id: str,
    action: MappingAction,
    trigger_binding: RuntimeComboBinding,
    trigger_bindings: Sequence[RuntimeComboBinding],
    *,
    deps: ComboRuntimeDeps,
    machine_type: type[SuperkeyMachine] = SuperkeyMachine,
) -> SuperkeyMachine | None:
    config = effective_config(manager, combo_id, action)
    if config is None:
        return None

    trigger_name = f"combo:{combo_id}"
    cancel_macro_playback = (
        manager.cancel_macro_playback_and_release_outputs
        if releases_outputs_on_cancel(manager, combo_id)
        else manager.cancel_macro_playback
    )
    machine_bindings = tuple(ordered_unique_bindings(trigger_bindings or (trigger_binding,)))
    existing = manager.combo_state.superkey_machines.get(combo_id)
    if existing is not None:
        existing_bindings = manager.combo_state.superkey_machine_bindings.get(combo_id)
        if (
            existing.config == config
            and existing.event_name == trigger_name
            and existing_bindings == machine_bindings
        ):
            return existing
        await existing.stop()
        manager.combo_state.superkey_machines.pop(combo_id, None)
        manager.combo_state.superkey_machine_bindings.pop(combo_id, None)

    async def broadcast(data: dict[str, object]) -> None:
        payload = dict(data)
        action_type = str(payload.get("action_type", "") or "")
        if action_type == ActionType.CANCEL_MACRO_PLAYBACK.value:
            await cancel_macro_playback()
            return
        if action_type == ActionType.EMERGENCY_RESET.value:
            deps.fire_and_observe_fn(
                manager.emergency_reset(),
                "combo emergency runtime reset",
            )
            return
        payload.setdefault("source_device", trigger_binding.hardware_id)
        payload.setdefault("source_button", trigger_name)
        dispatch_action_trigger(
            manager.broadcast_callback,
            payload,
            fire_and_observe_fn=deps.fire_and_observe_fn,
            label="combo action broadcast",
        )

    def output_tracker(action_type: str, code: int, value: int) -> bool:
        return track_output(manager, action_type, code, value)

    def repeat_path_recorder(slot: str) -> None:
        remember_superkey_path(
            manager.repeat_state,
            action,
            slot,
            source_device=trigger_binding.hardware_id,
            source_button=trigger_name,
        )

    natural_mouse_mover = getattr(manager, "move_cursor_natural", None)
    machine = machine_type(
        config=config,
        event_name=trigger_name,
        keyboard_uinput=cast(adapters.WritableUInput, manager.output_state.keyboard_uinput),
        mouse_uinput=cast(adapters.WritableUInput, manager.output_state.mouse_uinput),
        gamepad_uinput=cast(adapters.WritableUInput, manager.output_state.gamepad_uinput),
        source_device=trigger_binding.hardware_id,
        broadcast_callback=broadcast,
        cursor_position_setter=manager.set_cursor_position,
        natural_mouse_mover=(
            cast(NaturalMouseMover, natural_mouse_mover) if callable(natural_mouse_mover) else None
        ),
        key_event_tracker=output_tracker,
        gamepad_output_resolver=lambda output_id, context: manager.resolve_gamepad_output(
            output_id,
            context=context,
        ),
        macro_player=manager.play_macro,
        emergency_resetter=manager.emergency_reset,
        cancel_macro_playback=cancel_macro_playback,
        action_deps=action_execution_deps(deps),
        await_action_tasks=False,
        repeat_path_recorder=repeat_path_recorder,
    )
    manager.combo_state.superkey_machines[combo_id] = machine
    manager.combo_state.superkey_machine_bindings[combo_id] = machine_bindings
    return machine
