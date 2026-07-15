"""Recall, restoration, and binding-scope helpers for combo input state."""

from collections.abc import Sequence
from typing import cast

from keymasq.common.combos import normalize_combo_evdev
from keymasq.keymasqd.combo_engine import (
    ComboSyntheticEvent,
    RuntimeCombo,
    RuntimeComboBinding,
)
from keymasq.keymasqd.runtime.combo.state import ComboManager, GrabbedComboDevice


def emit_combo_recalls(
    manager: ComboManager,
    recall_events: list[ComboSyntheticEvent],
) -> None:
    for event in recall_events:
        device = find_grabbed_device_for_binding(manager, event.binding)
        if device is None:
            continue
        is_active = getattr(device, "combo_passthrough_binding_active", None)
        if callable(is_active) and not bool(is_active(event.binding.evdev)):
            continue
        device.emit_combo_release(event.binding.evdev)
        mark_recalled = getattr(device, "mark_combo_recalled_binding", None)
        if callable(mark_recalled):
            mark_recalled(event.binding.evdev)


def find_grabbed_device_for_binding(
    manager: ComboManager,
    binding: RuntimeComboBinding,
) -> GrabbedComboDevice | None:
    for device in manager.grabbed_devices.get(binding.hardware_id, []):
        if binding.source and device.interface_id != binding.source:
            continue
        return device
    return None


def held_combo_modifier_bindings_for_scope(
    manager: ComboManager,
    hardware_id: str,
    source: str,
) -> set[RuntimeComboBinding]:
    held: set[RuntimeComboBinding] = set()
    for device in manager.grabbed_devices.get(hardware_id, []):
        if source and device.interface_id != source:
            continue
        modifier_getter = getattr(device, "combo_passthrough_held_modifiers", None)
        if not callable(modifier_getter):
            continue
        modifier_names = modifier_getter()
        if not isinstance(modifier_names, (list, tuple, set, frozenset)):
            continue
        modifier_name_values = cast(
            list[object] | tuple[object, ...] | set[object] | frozenset[object],
            modifier_names,
        )
        for evdev_name in (name for name in modifier_name_values if isinstance(name, str)):
            held.add(
                RuntimeComboBinding(
                    hardware_id=hardware_id,
                    evdev=evdev_name,
                    source=device.interface_id,
                )
            )
    return held


def prime_combo_engine_with_held_bindings(manager: ComboManager) -> None:
    held: set[RuntimeComboBinding] = set()
    for devices in manager.grabbed_devices.values():
        for device in devices:
            held_getter = getattr(device, "combo_held_source_bindings", None)
            if not callable(held_getter):
                continue
            held_names = held_getter()
            if not isinstance(held_names, (list, tuple, set, frozenset)):
                continue
            held_name_values = cast(
                list[object] | tuple[object, ...] | set[object] | frozenset[object],
                held_names,
            )
            for evdev_name in (name for name in held_name_values if isinstance(name, str)):
                held.add(
                    RuntimeComboBinding(
                        hardware_id=str(device.hardware_id or "").lower(),
                        evdev=str(evdev_name or "").lower(),
                        source=str(device.interface_id or "").lower(),
                    )
                )
    manager.combo_state.progression.engine.prime_held_bindings(held)


def runtime_combo(manager: ComboManager, combo_id: str) -> RuntimeCombo | None:
    for combo in manager.combo_state.active_combos:
        if combo.id == combo_id:
            return combo
    return None


def combo_step_count(manager: ComboManager, combo_id: str) -> int:
    combo = runtime_combo(manager, combo_id)
    return len(combo.steps) if combo is not None else 1


def ordered_unique_bindings(
    bindings: Sequence[RuntimeComboBinding],
) -> list[RuntimeComboBinding]:
    ordered: list[RuntimeComboBinding] = []
    seen: set[RuntimeComboBinding] = set()
    for binding in bindings:
        if binding in seen:
            continue
        seen.add(binding)
        ordered.append(binding)
    return ordered


def binding_matches_scope(
    binding: RuntimeComboBinding | None,
    hardware_id: str,
    source: str | None,
) -> bool:
    """Return whether a concrete runtime binding belongs to a normalized scope."""
    if binding is None:
        return False
    normalized_hardware_id = str(hardware_id or "")
    normalized_source = None if source is None else str(source or "")
    if binding.hardware_id != normalized_hardware_id:
        return False
    return normalized_source is None or binding.source == normalized_source


def combo_trigger_recall_state(
    manager: ComboManager,
    combo_id: str,
    trigger_bindings: Sequence[RuntimeComboBinding],
) -> tuple[list[RuntimeComboBinding], list[RuntimeComboBinding]]:
    combo = runtime_combo(manager, combo_id)
    if combo is None or not combo.recall_trigger_keys:
        return ([], [])

    ordered_bindings = ordered_unique_bindings(trigger_bindings)
    recalled_bindings: list[RuntimeComboBinding] = []
    for binding in reversed(ordered_bindings):
        device = find_grabbed_device_for_binding(manager, binding)
        is_recalled = getattr(device, "combo_binding_recalled", None)
        if callable(is_recalled) and bool(is_recalled(binding.evdev)):
            recalled_bindings.append(binding)
            continue
        is_active = getattr(device, "combo_passthrough_binding_active", None)
        if callable(is_active) and not bool(is_active(binding.evdev)):
            continue
        if device is not None:
            device.emit_combo_release(binding.evdev)
            mark_recalled = getattr(device, "mark_combo_recalled_binding", None)
            if callable(mark_recalled):
                mark_recalled(binding.evdev)
            recalled_bindings.append(binding)

    restore_names = set(combo.restore_trigger_keys)
    recalled_set = set(recalled_bindings)
    restore_bindings = [
        binding
        for binding in ordered_bindings
        if binding in recalled_set and normalize_combo_evdev(binding.evdev) in restore_names
    ]
    return (recalled_bindings, restore_bindings)


def restore_combo_trigger_bindings(
    manager: ComboManager,
    restore_bindings: Sequence[RuntimeComboBinding],
) -> None:
    for binding in restore_bindings:
        device = find_grabbed_device_for_binding(manager, binding)
        if device is None:
            continue
        clear_recalled = getattr(device, "clear_combo_recalled_binding", None)
        is_held = getattr(device, "combo_source_binding_held", None)
        is_active = getattr(device, "combo_passthrough_binding_active", None)
        if callable(is_held) and not bool(is_held(binding.evdev)):
            if callable(clear_recalled):
                clear_recalled(binding.evdev)
            continue
        if callable(is_active) and bool(is_active(binding.evdev)):
            if callable(clear_recalled):
                clear_recalled(binding.evdev)
            continue
        emit_press = getattr(device, "emit_combo_press", None)
        if callable(emit_press):
            emit_press(binding.evdev)
        if callable(clear_recalled):
            clear_recalled(binding.evdev)


def attach_combo_trigger_recall_state(
    manager: ComboManager,
    combo_id: str,
    recalled_bindings: Sequence[RuntimeComboBinding],
    restore_bindings: Sequence[RuntimeComboBinding],
) -> bool:
    state = manager.combo_state.active_actions.get(combo_id)
    if state is None:
        return False
    state.recalled_bindings = list(recalled_bindings)
    state.restore_bindings = list(restore_bindings)
    return True
