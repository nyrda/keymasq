import contextlib
from typing import Any, cast


def _evdev_code_name(raw_name: object, fallback: int) -> str:
    if isinstance(raw_name, tuple):
        names = cast(tuple[object, ...], raw_name)
        first: object = names[0] if names else str(fallback)
        return str(first).lower()
    return str(raw_name).lower()


async def on_device_event(
    manager: Any,
    hardware_id: str,
    evdev_path: str,
    event_type: int,
    event_code: int,
    event_value: int,
    stable_path: str | None,
    source: str | None,
    *,
    evdev_mod: Any,
    resolve_stable_path_fn: Any,
    get_interface_id_fn: Any,
    combo_binding_cls: Any,
    combo_input_event_cls: Any,
    int_value_fn: Any,
    str_value_fn: Any,
    time_mod: Any,
    action_type_enum: Any,
    mapping_action_cls: Any,
    emit_mouse_move_fn: Any,
    get_trigger_axis_fn: Any,
    resolve_code_fn: Any,
    fire_and_observe_fn: Any,
    command_type: Any,
    asyncio_mod: Any,
    contextlib_mod: Any,
    uinput_writer: Any,
) -> Any:
    combo_payload = build_combo_event_payload(
        hardware_id,
        evdev_path,
        event_type,
        event_code,
        event_value,
        stable_path=stable_path,
        source=source,
        evdev_mod=evdev_mod,
        resolve_stable_path_fn=resolve_stable_path_fn,
        get_interface_id_fn=get_interface_id_fn,
    )
    capture_active = queue_combo_capture_event(manager, combo_payload, str_value_fn=str_value_fn)
    if capture_active:
        return True
    return await process_runtime_combo_event(
        manager,
        combo_payload,
        combo_binding_cls=combo_binding_cls,
        combo_input_event_cls=combo_input_event_cls,
        int_value_fn=int_value_fn,
        str_value_fn=str_value_fn,
        time_mod=time_mod,
        action_type_enum=action_type_enum,
        mapping_action_cls=mapping_action_cls,
        emit_mouse_move_fn=emit_mouse_move_fn,
        get_trigger_axis_fn=get_trigger_axis_fn,
        resolve_code_fn=resolve_code_fn,
        fire_and_observe_fn=fire_and_observe_fn,
        command_type=command_type,
        asyncio_mod=asyncio_mod,
        contextlib_mod=contextlib_mod,
        evdev_mod=evdev_mod,
        uinput_writer=uinput_writer,
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
    evdev_mod: Any,
    resolve_stable_path_fn: Any,
    get_interface_id_fn: Any,
) -> dict[str, object] | None:
    if event_type != evdev_mod.ecodes.EV_KEY or int(event_value) not in {0, 1, 2}:
        return None

    raw_code_name: object = evdev_mod.ecodes.bytype.get(event_type, {}).get(
        event_code, str(event_code)
    )
    evdev_name = _evdev_code_name(raw_code_name, event_code)
    if not evdev_name.startswith(("key_", "btn_")):
        return None

    resolved_stable_path = stable_path or resolve_stable_path_fn(evdev_path)
    return {
        "evdev": evdev_name,
        "code": int(event_code),
        "value": int(event_value),
        "source": str(source or get_interface_id_fn(resolved_stable_path) or "").lower(),
        "stable_path": resolved_stable_path,
        "device_path": evdev_path,
        "hardware_id": str(hardware_id).lower(),
    }


def queue_combo_capture_event(
    manager: Any,
    payload: dict[str, object] | None,
    *,
    str_value_fn: Any,
) -> bool:
    if payload is None or not manager._combo_capture_queues:
        return False
    hardware_id = str_value_fn(payload.get("hardware_id"), "")
    for capture_queue, hardware_ids, notify_event in manager._combo_capture_queues.values():
        if hardware_ids and hardware_id not in hardware_ids:
            continue
        capture_queue.put(dict(payload))
        if notify_event is not None:
            notify_event.set()
    return True


async def process_runtime_combo_event(
    manager: Any,
    payload: dict[str, object] | None,
    *,
    combo_binding_cls: Any,
    combo_input_event_cls: Any,
    int_value_fn: Any,
    str_value_fn: Any,
    time_mod: Any,
    action_type_enum: Any,
    mapping_action_cls: Any,
    emit_mouse_move_fn: Any,
    get_trigger_axis_fn: Any,
    resolve_code_fn: Any,
    fire_and_observe_fn: Any,
    command_type: Any,
    asyncio_mod: Any,
    contextlib_mod: Any,
    evdev_mod: Any,
    uinput_writer: Any,
) -> Any:
    if payload is None or not manager.active_combos:
        return None

    raw_value = payload.get("value")
    value = int_value_fn(raw_value, -1) if raw_value is not None else -1
    if value not in {0, 1, 2}:
        return None

    binding = combo_binding_cls(
        hardware_id=str_value_fn(payload.get("hardware_id"), ""),
        evdev=str_value_fn(payload.get("evdev"), ""),
        source=str_value_fn(payload.get("source"), ""),
    )
    if value == 1:
        held_modifiers = held_combo_modifier_bindings_for_scope(
            manager,
            binding.hardware_id,
            binding.source,
            combo_binding_cls=combo_binding_cls,
        )
        if binding in held_modifiers:
            held_modifiers.discard(binding)
        manager._combo_engine.prime_held_bindings(held_modifiers)
    decision = manager._combo_engine.handle_event(
        combo_input_event_cls(binding=binding, value=value),
        time_mod.monotonic(),
    )
    if decision.recall_events:
        emit_combo_recalls(manager, decision.recall_events)
    if decision.action_transition is not None:
        await apply_combo_action_transition(
            manager,
            decision.action_transition,
            action_type_enum=action_type_enum,
            mapping_action_cls=mapping_action_cls,
            emit_mouse_move_fn=emit_mouse_move_fn,
            get_trigger_axis_fn=get_trigger_axis_fn,
            resolve_code_fn=resolve_code_fn,
            fire_and_observe_fn=fire_and_observe_fn,
            command_type=command_type,
            asyncio_mod=asyncio_mod,
            contextlib_mod=contextlib_mod,
            evdev_mod=evdev_mod,
            uinput_writer=uinput_writer,
        )
    for transition in decision.extra_action_transitions:
        await apply_combo_action_transition(
            manager,
            transition,
            action_type_enum=action_type_enum,
            mapping_action_cls=mapping_action_cls,
            emit_mouse_move_fn=emit_mouse_move_fn,
            get_trigger_axis_fn=get_trigger_axis_fn,
            resolve_code_fn=resolve_code_fn,
            fire_and_observe_fn=fire_and_observe_fn,
            command_type=command_type,
            asyncio_mod=asyncio_mod,
            contextlib_mod=contextlib_mod,
            evdev_mod=evdev_mod,
            uinput_writer=uinput_writer,
        )
    refresh_combo_timeout_watchdog(
        manager,
        asyncio_mod=asyncio_mod,
        time_mod=time_mod,
        action_type_enum=action_type_enum,
        mapping_action_cls=mapping_action_cls,
        emit_mouse_move_fn=emit_mouse_move_fn,
        get_trigger_axis_fn=get_trigger_axis_fn,
        resolve_code_fn=resolve_code_fn,
        fire_and_observe_fn=fire_and_observe_fn,
        command_type=command_type,
        contextlib_mod=contextlib_mod,
        evdev_mod=evdev_mod,
        uinput_writer=uinput_writer,
    )
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


def emit_combo_recalls(manager: Any, recall_events: list[Any]) -> None:
    for event in recall_events:
        device = find_grabbed_device_for_binding(manager, event.binding)
        if device is not None:
            device.emit_combo_release(event.binding.evdev)


def find_grabbed_device_for_binding(manager: Any, binding: Any) -> Any:
    for device in manager.grabbed_devices.get(binding.hardware_id, []):
        if binding.source and device.interface_id != binding.source:
            continue
        return device
    return None


def held_combo_modifier_bindings_for_scope(
    manager: Any,
    hardware_id: str,
    source: str,
    *,
    combo_binding_cls: Any,
) -> set[Any]:
    held: set[Any] = set()
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
        modifier_names_str = [name for name in modifier_name_values if isinstance(name, str)]
        for evdev_name in modifier_names_str:
            held.add(
                combo_binding_cls(
                    hardware_id=hardware_id,
                    evdev=evdev_name,
                    source=device.interface_id,
                )
            )
    return held


async def apply_combo_action_transition(
    manager: Any,
    transition: Any,
    *,
    action_type_enum: Any,
    mapping_action_cls: Any,
    emit_mouse_move_fn: Any,
    get_trigger_axis_fn: Any,
    resolve_code_fn: Any,
    fire_and_observe_fn: Any,
    command_type: Any,
    asyncio_mod: Any,
    contextlib_mod: Any,
    evdev_mod: Any,
    uinput_writer: Any,
) -> None:
    if transition.kind == "press":
        await start_combo_action(
            manager,
            transition.combo_id,
            transition.action,
            transition.trigger_binding,
            action_type_enum=action_type_enum,
            asyncio_mod=asyncio_mod,
            emit_mouse_move_fn=emit_mouse_move_fn,
            get_trigger_axis_fn=get_trigger_axis_fn,
            resolve_code_fn=resolve_code_fn,
            fire_and_observe_fn=fire_and_observe_fn,
            command_type=command_type,
            evdev_mod=evdev_mod,
            uinput_writer=uinput_writer,
        )
    elif transition.kind == "release":
        await stop_combo_action(
            manager,
            transition.combo_id,
            asyncio_mod=asyncio_mod,
            contextlib_mod=contextlib_mod,
            mapping_action_cls=mapping_action_cls,
            evdev_mod=evdev_mod,
            uinput_writer=uinput_writer,
            emit_mouse_move_fn=emit_mouse_move_fn,
            get_trigger_axis_fn=get_trigger_axis_fn,
            resolve_code_fn=resolve_code_fn,
            fire_and_observe_fn=fire_and_observe_fn,
            command_type=command_type,
            action_type_enum=action_type_enum,
        )


async def broadcast_combo_action(
    manager: Any, data: dict[str, object], *, fire_and_observe_fn: Any, command_type: Any
) -> None:
    if manager.broadcast_callback is None:
        return
    fire_and_observe_fn(
        manager.broadcast_callback(command_type.ACTION_TRIGGER, data),
        "combo action broadcast",
    )


def prune_combo_action_task(manager: Any, combo_id: str, task: Any) -> None:
    if task is None:
        return
    state = manager._active_combo_actions.get(combo_id)
    if state is not None and state.get("task") is task:
        manager._active_combo_actions.pop(combo_id, None)


async def combo_tap_key(
    manager: Any,
    combo_id: str,
    uinput_dev: Any,
    code: int,
    hold_ms: int,
    *,
    asyncio_mod: Any,
    evdev_mod: Any,
    uinput_writer: Any,
) -> None:
    task = asyncio_mod.current_task()
    pressed = False

    try:
        write_combo_key(uinput_dev, code, 1, evdev_mod=evdev_mod, uinput_writer=uinput_writer)
        pressed = True
        await asyncio_mod.sleep(max(0.001, float(hold_ms) / 1000.0))
    except asyncio_mod.CancelledError:
        raise
    finally:
        if pressed:
            write_combo_key(uinput_dev, code, 0, evdev_mod=evdev_mod, uinput_writer=uinput_writer)
        prune_combo_action_task(manager, combo_id, task)


async def combo_tap_trigger(
    manager: Any,
    combo_id: str,
    axis_code: int,
    hold_ms: int,
    *,
    asyncio_mod: Any,
    evdev_mod: Any,
    uinput_writer: Any,
) -> None:
    task = asyncio_mod.current_task()
    pressed = False

    try:
        write_combo_trigger(
            manager,
            axis_code,
            255,
            evdev_mod=evdev_mod,
            uinput_writer=uinput_writer,
        )
        pressed = True
        await asyncio_mod.sleep(max(0.001, float(hold_ms) / 1000.0))
    except asyncio_mod.CancelledError:
        raise
    finally:
        if pressed:
            write_combo_trigger(
                manager,
                axis_code,
                0,
                evdev_mod=evdev_mod,
                uinput_writer=uinput_writer,
            )
        prune_combo_action_task(manager, combo_id, task)


async def start_combo_action(
    manager: Any,
    combo_id: str,
    action: Any,
    trigger_binding: Any,
    *,
    action_type_enum: Any,
    asyncio_mod: Any,
    emit_mouse_move_fn: Any,
    get_trigger_axis_fn: Any,
    resolve_code_fn: Any,
    fire_and_observe_fn: Any,
    command_type: Any,
    evdev_mod: Any,
    uinput_writer: Any,
) -> None:
    if action is None:
        return

    await stop_combo_action(
        manager,
        combo_id,
        asyncio_mod=asyncio_mod,
        contextlib_mod=contextlib,
        mapping_action_cls=action.__class__,
        evdev_mod=evdev_mod,
        uinput_writer=uinput_writer,
        emit_mouse_move_fn=emit_mouse_move_fn,
        get_trigger_axis_fn=get_trigger_axis_fn,
        resolve_code_fn=resolve_code_fn,
        fire_and_observe_fn=fire_and_observe_fn,
        command_type=command_type,
        action_type_enum=action_type_enum,
    )
    trigger_name = f"combo:{combo_id}"

    if action.action_type == action_type_enum.SUPERKEY:
        return

    if action.action_type == action_type_enum.KEYBOARD and action.target:
        await start_combo_key_action(
            manager,
            combo_id,
            action,
            manager._keyboard_uinput,
            asyncio_mod=asyncio_mod,
            resolve_code_fn=resolve_code_fn,
            evdev_mod=evdev_mod,
            uinput_writer=uinput_writer,
        )
        return

    if action.action_type == action_type_enum.MOUSE and action.target:
        await start_combo_key_action(
            manager,
            combo_id,
            action,
            manager._mouse_uinput,
            asyncio_mod=asyncio_mod,
            resolve_code_fn=resolve_code_fn,
            evdev_mod=evdev_mod,
            uinput_writer=uinput_writer,
        )
        return

    if action.action_type == action_type_enum.GAMEPAD and action.target:
        is_trigger, axis_code = get_trigger_axis_fn(action.target)
        if is_trigger and axis_code is not None:
            if action.tap_enabled:
                task = asyncio_mod.create_task(
                    combo_tap_trigger(
                        manager,
                        combo_id,
                        axis_code,
                        action.tap_hold_ms,
                        asyncio_mod=asyncio_mod,
                        evdev_mod=evdev_mod,
                        uinput_writer=uinput_writer,
                    )
                )
                manager._active_combo_actions[combo_id] = {
                    "kind": "tap_trigger",
                    "axis_code": axis_code,
                    "task": task,
                }
                return
            if action.rapidfire_enabled:
                task = asyncio_mod.create_task(
                    combo_rapidfire_trigger(
                        manager,
                        combo_id,
                        axis_code,
                        action.rapidfire_hold_ms,
                        action.rapidfire_wait_ms,
                        asyncio_mod=asyncio_mod,
                        evdev_mod=evdev_mod,
                        uinput_writer=uinput_writer,
                    )
                )
                manager._active_combo_actions[combo_id] = {
                    "kind": "rapidfire_trigger",
                    "axis_code": axis_code,
                    "active": True,
                    "task": task,
                }
                return
            write_combo_trigger(
                manager,
                axis_code,
                255,
                evdev_mod=evdev_mod,
                uinput_writer=uinput_writer,
            )
            manager._active_combo_actions[combo_id] = {
                "kind": "trigger",
                "axis_code": axis_code,
            }
            return
        await start_combo_key_action(
            manager,
            combo_id,
            action,
            manager._gamepad_uinput,
            asyncio_mod=asyncio_mod,
            resolve_code_fn=resolve_code_fn,
            evdev_mod=evdev_mod,
            uinput_writer=uinput_writer,
        )
        return

    if action.action_type in (action_type_enum.MOUSE_MOVE_REL, action_type_enum.MOUSE_MOVE_ABS):
        emit_combo_mouse_move(manager, action, emit_mouse_move_fn=emit_mouse_move_fn)
        return

    if action.action_type == action_type_enum.MACRO:
        if action.macro_events or action.macro_name:
            await manager.play_macro(
                macro_events=action.macro_events or [],
                macro_name=action.macro_name or "",
                replay_mouse_movement=action.macro_replay_mouse_movement,
                replay_mouse_clicks=action.macro_replay_mouse_clicks,
                speed=action.macro_speed,
                loop_mode=action.macro_loop_mode,
                loop_count=action.macro_loop_count,
                move_to_start=action.macro_move_to_start,
                start_x=action.macro_start_x,
                start_y=action.macro_start_y,
                block_mouse_movement=action.macro_block_mouse_movement,
                source_device="combo",
                source_button=trigger_name,
                trigger_value=1,
            )
            if str(action.macro_loop_mode or "none").lower() == "hold":
                manager._active_combo_actions[combo_id] = {
                    "kind": "macro_hold",
                    "action": action,
                    "source_device": "combo",
                    "source_button": trigger_name,
                }
        return

    if action.action_type == action_type_enum.EXEC:
        await broadcast_combo_action(
            manager,
            {
                "action_type": "exec",
                "exec_ref": action.exec_ref,
                "source_device": trigger_binding.hardware_id,
                "source_button": trigger_name,
            },
            fire_and_observe_fn=fire_and_observe_fn,
            command_type=command_type,
        )
        return

    if action.action_type == action_type_enum.COMPOSITOR_DISPATCH:
        await broadcast_combo_action(
            manager,
            {
                "action_type": "compositor_dispatch",
                "compositor": action.compositor_id or "",
                "dispatcher": action.compositor_dispatcher or "",
                "args": action.compositor_args or "",
                "source_device": trigger_binding.hardware_id,
                "source_button": trigger_name,
            },
            fire_and_observe_fn=fire_and_observe_fn,
            command_type=command_type,
        )
        return

    if action.action_type in (
        action_type_enum.START_MACRO_RECORDING,
        action_type_enum.STOP_MACRO_RECORDING,
        action_type_enum.CANCEL_MACRO_PLAYBACK,
    ):
        await broadcast_combo_action(
            manager,
            {
                "action_type": action.action_type.value,
                "source_device": trigger_binding.hardware_id,
                "source_button": trigger_name,
            },
            fire_and_observe_fn=fire_and_observe_fn,
            command_type=command_type,
        )
        return

    if action.action_type in (
        action_type_enum.PROFILE_ENABLE,
        action_type_enum.PROFILE_DISABLE,
        action_type_enum.PROFILE_TOGGLE,
    ):
        await broadcast_combo_action(
            manager,
            {
                "action_type": action.action_type.value,
                "profile_name": action.profile_name or action.target or "",
                "source_device": trigger_binding.hardware_id,
                "source_button": trigger_name,
            },
            fire_and_observe_fn=fire_and_observe_fn,
            command_type=command_type,
        )


async def start_combo_key_action(
    manager: Any,
    combo_id: str,
    action: Any,
    uinput_dev: Any,
    *,
    asyncio_mod: Any,
    resolve_code_fn: Any,
    evdev_mod: Any,
    uinput_writer: Any,
) -> None:
    target = str(action.target or "")
    if not target:
        return
    code = resolve_code_fn(target)
    if code is None:
        return
    if action.tap_enabled:
        task = asyncio_mod.create_task(
            combo_tap_key(
                manager,
                combo_id,
                uinput_dev,
                code,
                action.tap_hold_ms,
                asyncio_mod=asyncio_mod,
                evdev_mod=evdev_mod,
                uinput_writer=uinput_writer,
            )
        )
        manager._active_combo_actions[combo_id] = {
            "kind": "tap_key",
            "uinput": uinput_dev,
            "code": code,
            "task": task,
        }
        return
    if action.rapidfire_enabled:
        task = asyncio_mod.create_task(
            combo_rapidfire_key(
                manager,
                combo_id,
                uinput_dev,
                code,
                action.rapidfire_hold_ms,
                action.rapidfire_wait_ms,
                asyncio_mod=asyncio_mod,
                evdev_mod=evdev_mod,
                uinput_writer=uinput_writer,
            )
        )
        manager._active_combo_actions[combo_id] = {
            "kind": "rapidfire_key",
            "uinput": uinput_dev,
            "code": code,
            "active": True,
            "task": task,
        }
        return
    write_combo_key(uinput_dev, code, 1, evdev_mod=evdev_mod, uinput_writer=uinput_writer)
    manager._active_combo_actions[combo_id] = {
        "kind": "key",
        "uinput": uinput_dev,
        "code": code,
    }


async def stop_combo_action(
    manager: Any,
    combo_id: str,
    *,
    asyncio_mod: Any,
    contextlib_mod: Any,
    mapping_action_cls: Any,
    evdev_mod: Any,
    uinput_writer: Any,
    emit_mouse_move_fn: Any,
    get_trigger_axis_fn: Any,
    resolve_code_fn: Any,
    fire_and_observe_fn: Any,
    command_type: Any,
    action_type_enum: Any,
) -> None:
    state = manager._active_combo_actions.pop(combo_id, None)
    if not state:
        return
    kind = str(state.get("kind", "") or "")
    if kind == "key":
        uinput_dev = state.get("uinput")
        code = state.get("code")
        if isinstance(code, int):
            write_combo_key(uinput_dev, code, 0, evdev_mod=evdev_mod, uinput_writer=uinput_writer)
        return
    if kind == "trigger":
        axis_code = state.get("axis_code")
        if isinstance(axis_code, int):
            write_combo_trigger(
                manager,
                axis_code,
                0,
                evdev_mod=evdev_mod,
                uinput_writer=uinput_writer,
            )
        return
    if kind in {"tap_key", "tap_trigger", "rapidfire_key", "rapidfire_trigger"}:
        state["active"] = False
        task = state.get("task")
        if isinstance(task, asyncio_mod.Task) and not task.done():
            task.cancel()
            with contextlib_mod.suppress(asyncio_mod.CancelledError):
                await task
        return
    if kind == "macro_hold":
        action = state.get("action")
        if isinstance(action, mapping_action_cls):
            await manager.play_macro(
                macro_events=[],
                macro_name=action.macro_name or "",
                replay_mouse_movement=action.macro_replay_mouse_movement,
                replay_mouse_clicks=action.macro_replay_mouse_clicks,
                speed=action.macro_speed,
                loop_mode=action.macro_loop_mode,
                loop_count=action.macro_loop_count,
                move_to_start=action.macro_move_to_start,
                start_x=action.macro_start_x,
                start_y=action.macro_start_y,
                block_mouse_movement=action.macro_block_mouse_movement,
                source_device=str(state.get("source_device", "") or ""),
                source_button=str(state.get("source_button", "") or ""),
                trigger_value=0,
            )


async def clear_combo_runtime(
    manager: Any,
    *,
    asyncio_mod: Any,
    contextlib_mod: Any,
    mapping_action_cls: Any,
    evdev_mod: Any,
    uinput_writer: Any,
    emit_mouse_move_fn: Any,
    get_trigger_axis_fn: Any,
    resolve_code_fn: Any,
    fire_and_observe_fn: Any,
    command_type: Any,
    action_type_enum: Any,
    time_mod: Any,
) -> None:
    manager._combo_engine.reset()
    for combo_id in list(manager._active_combo_actions):
        await stop_combo_action(
            manager,
            combo_id,
            asyncio_mod=asyncio_mod,
            contextlib_mod=contextlib_mod,
            mapping_action_cls=mapping_action_cls,
            evdev_mod=evdev_mod,
            uinput_writer=uinput_writer,
            emit_mouse_move_fn=emit_mouse_move_fn,
            get_trigger_axis_fn=get_trigger_axis_fn,
            resolve_code_fn=resolve_code_fn,
            fire_and_observe_fn=fire_and_observe_fn,
            command_type=command_type,
            action_type_enum=action_type_enum,
        )
    if manager._combo_timeout_task and not manager._combo_timeout_task.done():
        manager._combo_timeout_task.cancel()
        with contextlib_mod.suppress(asyncio_mod.CancelledError):
            await manager._combo_timeout_task
    manager._combo_timeout_task = None


async def clear_combo_runtime_for_binding_scope(
    manager: Any,
    hardware_id: str,
    source: str | None,
    *,
    asyncio_mod: Any,
    contextlib_mod: Any,
    mapping_action_cls: Any,
    evdev_mod: Any,
    uinput_writer: Any,
    emit_mouse_move_fn: Any,
    get_trigger_axis_fn: Any,
    resolve_code_fn: Any,
    fire_and_observe_fn: Any,
    command_type: Any,
    action_type_enum: Any,
    time_mod: Any,
) -> None:
    active_combo_ids = manager._combo_engine.drop_candidates_for_binding_scope(
        str(hardware_id or "").lower(),
        None if source is None else str(source or "").lower(),
    )
    for combo_id in active_combo_ids:
        await stop_combo_action(
            manager,
            combo_id,
            asyncio_mod=asyncio_mod,
            contextlib_mod=contextlib_mod,
            mapping_action_cls=mapping_action_cls,
            evdev_mod=evdev_mod,
            uinput_writer=uinput_writer,
            emit_mouse_move_fn=emit_mouse_move_fn,
            get_trigger_axis_fn=get_trigger_axis_fn,
            resolve_code_fn=resolve_code_fn,
            fire_and_observe_fn=fire_and_observe_fn,
            command_type=command_type,
            action_type_enum=action_type_enum,
        )
    refresh_combo_timeout_watchdog(
        manager,
        asyncio_mod=asyncio_mod,
        time_mod=time_mod,
        action_type_enum=action_type_enum,
        mapping_action_cls=mapping_action_cls,
        emit_mouse_move_fn=emit_mouse_move_fn,
        get_trigger_axis_fn=get_trigger_axis_fn,
        resolve_code_fn=resolve_code_fn,
        fire_and_observe_fn=fire_and_observe_fn,
        command_type=command_type,
        contextlib_mod=contextlib_mod,
        evdev_mod=evdev_mod,
        uinput_writer=uinput_writer,
    )


def refresh_combo_timeout_watchdog(
    manager: Any,
    *,
    asyncio_mod: Any,
    time_mod: Any,
    action_type_enum: Any,
    mapping_action_cls: Any,
    emit_mouse_move_fn: Any,
    get_trigger_axis_fn: Any,
    resolve_code_fn: Any,
    fire_and_observe_fn: Any,
    command_type: Any,
    contextlib_mod: Any,
    evdev_mod: Any,
    uinput_writer: Any,
) -> None:
    deadline = manager._combo_engine.next_deadline()
    if deadline is None:
        if manager._combo_timeout_task and not manager._combo_timeout_task.done():
            manager._combo_timeout_task.cancel()
        manager._combo_timeout_task = None
        return
    if manager._combo_timeout_task and not manager._combo_timeout_task.done():
        manager._combo_timeout_task.cancel()
    manager._combo_timeout_task = asyncio_mod.create_task(
        combo_timeout_watchdog(
            manager,
            deadline,
            asyncio_mod=asyncio_mod,
            time_mod=time_mod,
            action_type_enum=action_type_enum,
            mapping_action_cls=mapping_action_cls,
            emit_mouse_move_fn=emit_mouse_move_fn,
            get_trigger_axis_fn=get_trigger_axis_fn,
            resolve_code_fn=resolve_code_fn,
            fire_and_observe_fn=fire_and_observe_fn,
            command_type=command_type,
            contextlib_mod=contextlib_mod,
            evdev_mod=evdev_mod,
            uinput_writer=uinput_writer,
        )
    )


async def combo_timeout_watchdog(
    manager: Any,
    deadline: float,
    *,
    asyncio_mod: Any,
    time_mod: Any,
    action_type_enum: Any,
    mapping_action_cls: Any,
    emit_mouse_move_fn: Any,
    get_trigger_axis_fn: Any,
    resolve_code_fn: Any,
    fire_and_observe_fn: Any,
    command_type: Any,
    contextlib_mod: Any,
    evdev_mod: Any,
    uinput_writer: Any,
) -> None:
    try:
        await asyncio_mod.sleep(max(0.0, deadline - time_mod.monotonic()))
        manager._combo_engine.expire_timeouts(time_mod.monotonic())
    except asyncio_mod.CancelledError:
        raise
    finally:
        if manager._combo_timeout_task is asyncio_mod.current_task():
            manager._combo_timeout_task = None
        refresh_combo_timeout_watchdog(
            manager,
            asyncio_mod=asyncio_mod,
            time_mod=time_mod,
            action_type_enum=action_type_enum,
            mapping_action_cls=mapping_action_cls,
            emit_mouse_move_fn=emit_mouse_move_fn,
            get_trigger_axis_fn=get_trigger_axis_fn,
            resolve_code_fn=resolve_code_fn,
            fire_and_observe_fn=fire_and_observe_fn,
            command_type=command_type,
            contextlib_mod=contextlib_mod,
            evdev_mod=evdev_mod,
            uinput_writer=uinput_writer,
        )


async def combo_rapidfire_key(
    manager: Any,
    combo_id: str,
    uinput_dev: Any,
    code: int,
    hold_ms: int,
    wait_ms: int,
    *,
    asyncio_mod: Any,
    evdev_mod: Any,
    uinput_writer: Any,
) -> None:
    try:
        while manager._active_combo_actions.get(combo_id, {}).get("active") is True:
            write_combo_key(uinput_dev, code, 1, evdev_mod=evdev_mod, uinput_writer=uinput_writer)
            await asyncio_mod.sleep(max(0.001, hold_ms / 1000.0))
            if manager._active_combo_actions.get(combo_id, {}).get("active") is not True:
                break
            write_combo_key(uinput_dev, code, 0, evdev_mod=evdev_mod, uinput_writer=uinput_writer)
            await asyncio_mod.sleep(max(0.001, wait_ms / 1000.0))
    except asyncio_mod.CancelledError:
        raise
    finally:
        write_combo_key(uinput_dev, code, 0, evdev_mod=evdev_mod, uinput_writer=uinput_writer)


async def combo_rapidfire_trigger(
    manager: Any,
    combo_id: str,
    axis_code: int,
    hold_ms: int,
    wait_ms: int,
    *,
    asyncio_mod: Any,
    evdev_mod: Any,
    uinput_writer: Any,
) -> None:
    try:
        while manager._active_combo_actions.get(combo_id, {}).get("active") is True:
            write_combo_trigger(
                manager,
                axis_code,
                255,
                evdev_mod=evdev_mod,
                uinput_writer=uinput_writer,
            )
            await asyncio_mod.sleep(max(0.001, hold_ms / 1000.0))
            if manager._active_combo_actions.get(combo_id, {}).get("active") is not True:
                break
            write_combo_trigger(
                manager,
                axis_code,
                0,
                evdev_mod=evdev_mod,
                uinput_writer=uinput_writer,
            )
            await asyncio_mod.sleep(max(0.001, wait_ms / 1000.0))
    except asyncio_mod.CancelledError:
        raise
    finally:
        write_combo_trigger(manager, axis_code, 0, evdev_mod=evdev_mod, uinput_writer=uinput_writer)


def write_combo_key(
    uinput_dev: Any, code: int, value: int, *, evdev_mod: Any, uinput_writer: Any
) -> None:
    writer = uinput_writer(uinput_dev)
    if writer is None:
        return
    writer.write(evdev_mod.ecodes.EV_KEY, int(code), int(value))
    writer.syn()


def write_combo_trigger(
    manager: Any, axis_code: int, value: int, *, evdev_mod: Any, uinput_writer: Any
) -> None:
    writer = uinput_writer(manager._gamepad_uinput)
    if writer is None:
        return
    writer.write(evdev_mod.ecodes.EV_ABS, int(axis_code), int(value))
    writer.syn()


def emit_combo_mouse_move(manager: Any, action: Any, *, emit_mouse_move_fn: Any) -> None:
    emit_mouse_move_fn(
        manager._mouse_uinput,
        int(action.move_x),
        int(action.move_y),
        absolute=bool(action.action_type.value == "mouse_move_abs"),
    )


def begin_combo_capture(
    manager: Any, token: str, hardware_ids: set[str], notify_event: Any, *, queue_mod: Any
) -> dict[str, object]:
    manager._combo_capture_queues[token] = (
        queue_mod.SimpleQueue[dict[str, object]](),
        set(hardware_ids),
        notify_event,
    )
    return {
        "token": token,
        "grabbed_devices": sum(len(devices) for devices in manager.grabbed_devices.values()),
    }


def read_combo_capture(manager: Any, token: str, *, queue_mod: Any) -> dict[str, object]:
    capture_state = manager._combo_capture_queues.get(token)
    if capture_state is None:
        return {"event": None}
    capture_queue, _hardware_ids, _notify_event = capture_state
    try:
        return {"event": capture_queue.get_nowait()}
    except queue_mod.Empty:
        return {"event": None}


def end_combo_capture(manager: Any, token: str) -> dict[str, object]:
    removed = manager._combo_capture_queues.pop(token, None)
    return {"status": "ok", "ended": removed is not None}
