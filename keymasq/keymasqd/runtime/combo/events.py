"""Event normalization, capture, and progression orchestration for combos."""

import asyncio
import queue

from keymasq.common.combos import is_combo_pulse_evdev
from keymasq.common.devices import (
    evdev_alias_name,
    high_res_wheel_low_res_code,
    normalize_wheel_value,
    wheel_button_id,
)
from keymasq.keymasqd.combo_engine import ComboDecision, RuntimeComboBinding
from keymasq.keymasqd.runtime import adapters
from keymasq.keymasqd.runtime.combo.actions import apply_combo_action_transition
from keymasq.keymasqd.runtime.combo.lifecycle import refresh_combo_timeout_watchdog
from keymasq.keymasqd.runtime.combo.recall import (
    emit_combo_recalls,
    held_combo_modifier_bindings_for_scope,
)
from keymasq.keymasqd.runtime.combo.state import (
    ComboManager,
    ComboRuntimeDeps,
    GetInterfaceIdFn,
    IntValueFn,
    ResolveStablePathFn,
    StrValueFn,
)


async def on_device_event(
    manager: ComboManager,
    hardware_id: str,
    evdev_path: str,
    event_type: int,
    event_code: int,
    event_value: int,
    stable_path: str | None,
    source: str | None,
    *,
    resolve_stable_path_fn: ResolveStablePathFn,
    get_interface_id_fn: GetInterfaceIdFn,
    int_value_fn: IntValueFn,
    str_value_fn: StrValueFn,
    deps: ComboRuntimeDeps,
) -> ComboDecision | bool | None:
    event_source = str(source or "").lower()
    if not event_source:
        resolved_path = stable_path or resolve_stable_path_fn(evdev_path)
        event_source = str(get_interface_id_fn(resolved_path) or "").lower()
    async with manager.combo_state.runtime_lock:
        suppress_high_res = should_suppress_high_res_combo_wheel_event(
            manager,
            hardware_id,
            event_type,
            event_code,
            event_value,
            event_source,
            evdev_mod=deps.evdev_mod,
        )
    if suppress_high_res:
        return True
    combo_payload = build_combo_event_payload(
        hardware_id,
        evdev_path,
        event_type,
        event_code,
        event_value,
        stable_path=stable_path,
        source=event_source,
        evdev_mod=deps.evdev_mod,
        resolve_stable_path_fn=resolve_stable_path_fn,
        get_interface_id_fn=get_interface_id_fn,
    )
    if queue_combo_capture_event(manager, combo_payload, str_value_fn=str_value_fn):
        return True
    return await process_runtime_combo_event(
        manager,
        combo_payload,
        int_value_fn=int_value_fn,
        str_value_fn=str_value_fn,
        deps=deps,
    )


def build_combo_event_payload(
    hardware_id: str,
    evdev_path: str,
    event_type: int,
    event_code: int,
    event_value: int,
    *,
    stable_path: str | None,
    source: str | None,
    evdev_mod: adapters.ComboEvdevAdapter,
    resolve_stable_path_fn: ResolveStablePathFn,
    get_interface_id_fn: GetInterfaceIdFn,
) -> dict[str, object] | None:
    payload_value = int(event_value)
    if event_type == evdev_mod.ecodes.EV_KEY:
        if payload_value not in {0, 1, 2}:
            return None
        raw_code_name: object = evdev_mod.ecodes.bytype.get(event_type, {}).get(
            event_code,
            str(event_code),
        )
        evdev_name = _evdev_code_name(raw_code_name, event_code)
        if not evdev_name.startswith(("key_", "btn_")):
            return None
    elif event_type == evdev_mod.ecodes.EV_REL:
        evdev_name = combo_wheel_evdev_for_rel_event(
            event_code,
            event_value,
            evdev_mod=evdev_mod,
        )
        if evdev_name is None:
            return None
        payload_value = 1
    else:
        return None

    resolved_stable_path = stable_path or resolve_stable_path_fn(evdev_path)
    return {
        "evdev": evdev_name,
        "code": int(event_code),
        "value": payload_value,
        "source": str(source or get_interface_id_fn(resolved_stable_path) or "").lower(),
        "stable_path": resolved_stable_path,
        "device_path": evdev_path,
        "hardware_id": str(hardware_id).lower(),
    }


def combo_wheel_evdev_for_rel_event(
    event_code: int,
    event_value: int,
    *,
    evdev_mod: adapters.ComboEvdevAdapter,
) -> str | None:
    rel_evdev = low_res_wheel_evdev_name(event_code, evdev_mod=evdev_mod)
    if rel_evdev is None:
        return None
    normalized_value = normalize_wheel_value(int(event_value))
    if normalized_value is None:
        return None
    return wheel_button_id(rel_evdev, normalized_value)


def low_res_wheel_evdev_name(
    event_code: int,
    *,
    evdev_mod: adapters.ComboEvdevAdapter,
) -> str | None:
    if int(event_code) == int(evdev_mod.ecodes.REL_WHEEL):
        return "rel_wheel"
    if int(event_code) == int(evdev_mod.ecodes.REL_HWHEEL):
        return "rel_hwheel"
    return None


def should_suppress_high_res_combo_wheel_event(
    manager: ComboManager,
    hardware_id: str,
    event_type: int,
    event_code: int,
    event_value: int,
    source: str | None,
    *,
    evdev_mod: adapters.ComboEvdevAdapter,
) -> bool:
    if int(event_type) != int(evdev_mod.ecodes.EV_REL):
        return False
    low_res_code = high_res_wheel_low_res_code(int(event_code))
    if low_res_code is None:
        return False
    evdev_name = combo_wheel_evdev_for_rel_event(
        low_res_code,
        event_value,
        evdev_mod=evdev_mod,
    )
    if evdev_name is None:
        return False
    binding = RuntimeComboBinding(
        hardware_id=str(hardware_id or "").lower(),
        evdev=evdev_name,
        source=str(source or "").lower(),
    )
    return bool(manager.combo_state.progression.engine.would_consume_pulse(binding))


def queue_combo_capture_event(
    manager: ComboManager,
    payload: dict[str, object] | None,
    *,
    str_value_fn: StrValueFn,
) -> bool:
    if payload is None or not manager.combo_state.capture_queues:
        return False
    hardware_id = str_value_fn(payload.get("hardware_id"), "")
    enqueued = False
    for capture_queue, hardware_ids, notify_event in manager.combo_state.capture_queues.values():
        if hardware_ids and hardware_id not in hardware_ids:
            continue
        capture_queue.put(dict(payload))
        enqueued = True
        if notify_event is not None:
            notify_event.set()
    return enqueued


async def process_runtime_combo_event(
    manager: ComboManager,
    payload: dict[str, object] | None,
    *,
    int_value_fn: IntValueFn,
    str_value_fn: StrValueFn,
    deps: ComboRuntimeDeps,
) -> ComboDecision | None:
    if payload is None or not manager.combo_state.active_combos:
        return None

    raw_value = payload.get("value")
    value = int_value_fn(raw_value, -1) if raw_value is not None else -1
    if value not in {0, 1, 2}:
        return None
    binding = RuntimeComboBinding(
        hardware_id=str_value_fn(payload.get("hardware_id"), ""),
        evdev=str_value_fn(payload.get("evdev"), ""),
        source=str_value_fn(payload.get("source"), ""),
    )
    async with manager.combo_state.runtime_lock:
        held_modifiers = (
            held_combo_modifier_bindings_for_scope(
                manager,
                binding.hardware_id,
                binding.source,
            )
            if value == 1 and not is_combo_pulse_evdev(binding.evdev)
            else ()
        )
        decision = manager.combo_state.progression.handle(
            binding,
            value,
            held_bindings=held_modifiers,
        )
        if decision.recall_events:
            emit_combo_recalls(manager, decision.recall_events)
    if decision.action_transition is not None:
        await apply_combo_action_transition(
            manager,
            decision.action_transition,
            deps=deps,
        )
    for transition in decision.extra_action_transitions:
        await apply_combo_action_transition(manager, transition, deps=deps)
    async with manager.combo_state.runtime_lock:
        refresh_combo_timeout_watchdog(manager, deps=deps)
    if (
        decision.consume_current_event
        or decision.passthrough_current_event
        or decision.recall_events
        or decision.action_transition is not None
        or decision.extra_action_transitions
        or decision.reset_candidates
    ):
        return decision
    return None


def begin_combo_capture(
    manager: ComboManager,
    token: str,
    hardware_ids: set[str],
    notify_event: asyncio.Event | None,
) -> dict[str, object]:
    manager.combo_state.capture_queues[token] = (
        queue.SimpleQueue(),
        set(hardware_ids),
        notify_event,
    )
    return {
        "token": token,
        "grabbed_devices": sum(len(devices) for devices in manager.grabbed_devices.values()),
    }


def read_combo_capture(manager: ComboManager, token: str) -> dict[str, object]:
    capture_state = manager.combo_state.capture_queues.get(token)
    if capture_state is None:
        return {"event": None}
    capture_queue, _hardware_ids, _notify_event = capture_state
    try:
        return {"event": capture_queue.get_nowait()}
    except queue.Empty:
        return {"event": None}


def end_combo_capture(manager: ComboManager, token: str) -> dict[str, object]:
    removed = manager.combo_state.capture_queues.pop(token, None)
    return {"status": "ok", "ended": removed is not None}


def _evdev_code_name(raw_name: object, fallback: int) -> str:
    return evdev_alias_name(raw_name, str(fallback)) or str(fallback)
