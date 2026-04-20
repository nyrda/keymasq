import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

type JsonObject = dict[str, object]
type IntValueFn = Callable[[object, int], int]
type StrValueFn = Callable[[object, str], str]
type _MacroManager = Any


@dataclass(frozen=True)
class MacroRuntimeDeps:
    asyncio_mod: Any
    contextlib_mod: Any
    evdev_mod: Any
    uinput_writer: Any
    random_mod: Any
    uuid_mod: Any
    command_type: Any
    log: logging.Logger
    int_value_fn: IntValueFn
    str_value_fn: StrValueFn


async def play_macro(
    manager: _MacroManager,
    macro_events: list[dict[str, object]],
    macro_name: str,
    replay_mouse_movement: bool,
    replay_mouse_clicks: bool,
    speed: float,
    loop_mode: str,
    loop_count: int,
    move_to_start: bool,
    start_x: int,
    start_y: int,
    block_mouse_movement: bool,
    source_device: str,
    source_button: str,
    trigger_value: int,
    *,
    deps: MacroRuntimeDeps,
) -> dict[str, object]:
    asyncio_mod = deps.asyncio_mod
    if not (
        manager.output_state.keyboard_uinput
        or manager.output_state.mouse_uinput
        or manager.output_state.gamepad_uinput
    ):
        return {"status": "error", "message": "No output uinput devices available"}

    normalized_loop = str(loop_mode or "none").lower()
    if normalized_loop not in {"none", "count", "hold", "toggle"}:
        normalized_loop = "none"
    count = max(1, int(loop_count or 1))
    source_key = (str(source_device), str(source_button))

    if int(trigger_value) == 0:
        hold_instances = find_matching_macro_instances(
            manager,
            loop_mode="hold",
            source_key=source_key,
        )
        if hold_instances:
            cancelled = await cancel_macro_instances(
                manager,
                hold_instances,
                deps=deps,
            )
            return {"status": "ok", "cancelled": cancelled > 0}
        return {"status": "ok", "cancelled": False}

    if int(trigger_value) != 1:
        return {"status": "ok"}

    if normalized_loop == "toggle":
        toggle_instances = find_matching_macro_instances(
            manager,
            loop_mode="toggle",
            source_key=source_key,
        )
        if toggle_instances:
            cancelled = await cancel_macro_instances(
                manager,
                toggle_instances,
                deps=deps,
            )
            return {"status": "ok", "cancelled": cancelled > 0}

    if normalized_loop == "hold":
        hold_instances = find_matching_macro_instances(
            manager,
            loop_mode="hold",
            source_key=source_key,
        )
        if hold_instances:
            return {"status": "ok", "already_running": True}

    manager.macro_state.instance_seq += 1
    instance_id = manager.macro_state.instance_seq
    manager.macro_state.instance_held[instance_id] = set()
    manager.macro_state.cancel_instance_ids.discard(instance_id)
    manager.macro_state.instance_meta[instance_id] = {
        "loop_mode": normalized_loop,
        "source_device": source_key[0],
        "source_button": source_key[1],
        "macro_name": str(macro_name or ""),
    }

    task = asyncio_mod.create_task(
        play_macro_task(
            manager,
            instance_id=instance_id,
            macro_events=macro_events,
            macro_name=macro_name,
            replay_mouse_movement=replay_mouse_movement,
            replay_mouse_clicks=replay_mouse_clicks,
            speed=max(0.01, speed),
            loop_mode=normalized_loop,
            loop_count=count,
            move_to_start=move_to_start,
            start_x=int(start_x),
            start_y=int(start_y),
            block_mouse_movement=block_mouse_movement,
            deps=deps,
        )
    )
    manager.macro_state.tasks[instance_id] = task
    return {"status": "ok"}


async def cancel_macro_playback(
    manager: _MacroManager,
    *,
    deps: MacroRuntimeDeps,
) -> dict[str, object]:
    running_ids = running_macro_instance_ids(manager)
    cancelled = await cancel_macro_instances(
        manager,
        running_ids,
        deps=deps,
    )
    for devices in manager.grabbed_devices.values():
        for device in devices:
            device.release_tracked_outputs()
    complete_all_macro_exec_waiters(manager, -1)
    manager.macro_state.mouse_inhibit_count = 0
    end_mouse_rel_suppression(manager)
    return {"status": "ok", "cancelled": cancelled > 0}


def running_macro_instance_ids(manager: _MacroManager) -> list[int]:
    return [
        instance_id for instance_id, task in manager.macro_state.tasks.items() if not task.done()
    ]


def find_matching_macro_instances(
    manager: _MacroManager,
    *,
    loop_mode: str | None,
    source_key: tuple[str, str] | None,
) -> list[int]:
    ids: list[int] = []
    for instance_id, task in manager.macro_state.tasks.items():
        if task.done():
            continue
        meta = manager.macro_state.instance_meta.get(instance_id, {})
        if loop_mode is not None and meta.get("loop_mode") != loop_mode:
            continue
        if source_key is not None and (
            meta.get("source_device") != source_key[0] or meta.get("source_button") != source_key[1]
        ):
            continue
        ids.append(instance_id)
    return ids


async def cancel_macro_instances(
    manager: _MacroManager,
    instance_ids: list[int],
    *,
    deps: MacroRuntimeDeps,
) -> int:
    asyncio_mod = deps.asyncio_mod
    unique_ids = list(dict.fromkeys(int(i) for i in instance_ids))
    if not unique_ids:
        return 0

    for instance_id in unique_ids:
        manager.macro_state.cancel_instance_ids.add(instance_id)

    for instance_id in unique_ids:
        release_macro_held_for_instance(
            manager,
            instance_id,
            deps=deps,
        )

    tasks = [
        task
        for instance_id, task in manager.macro_state.tasks.items()
        if instance_id in unique_ids and not task.done()
    ]
    for task in tasks:
        task.cancel()

    if tasks:
        with deps.contextlib_mod.suppress(Exception):
            await asyncio_mod.wait_for(
                asyncio_mod.gather(*tasks, return_exceptions=True),
                timeout=1.0,
            )

    for instance_id in unique_ids:
        manager.macro_state.cancel_instance_ids.discard(instance_id)
        manager.macro_state.tasks.pop(instance_id, None)
        manager.macro_state.instance_meta.pop(instance_id, None)

    return len(tasks)


def complete_all_macro_exec_waiters(manager: _MacroManager, returncode: int) -> None:
    for wait_id, waiter in list(manager.macro_state.exec_waiters.items()):
        if waiter.done():
            manager.macro_state.exec_waiters.pop(wait_id, None)
            continue
        waiter.set_result(int(returncode))


async def play_macro_task(
    manager: _MacroManager,
    instance_id: int,
    macro_events: list[dict[str, object]],
    macro_name: str,
    replay_mouse_movement: bool,
    replay_mouse_clicks: bool,
    speed: float,
    loop_mode: str,
    loop_count: int,
    move_to_start: bool,
    start_x: int,
    start_y: int,
    block_mouse_movement: bool,
    *,
    deps: MacroRuntimeDeps,
) -> None:
    asyncio_mod = deps.asyncio_mod
    evdev_mod = deps.evdev_mod
    int_value_fn = deps.int_value_fn
    str_value_fn = deps.str_value_fn
    uinput_writer = deps.uinput_writer
    mouse_btn_codes = frozenset(range(0x110, 0x118))
    pending_abs_moves: dict[str, dict[str, int]] = {}

    if manager.verbosity >= 1:
        deps.log.debug("Macro playback started: %s", macro_name or "<unnamed>")

    speed_factor = max(0.01, speed)
    macro_duration_us = (
        max(int_value_fn(ev.get("t_us"), 0) for ev in macro_events) if macro_events else 0
    )
    suppression_timeout_s = max(2.0, (macro_duration_us / speed_factor) / 1_000_000.0 + 1.0)
    event_loop = asyncio_mod.get_running_loop()
    try:
        if block_mouse_movement:
            acquire_macro_mouse_inhibit(
                manager,
                timeout_s=suppression_timeout_s,
                deps=deps,
            )

        iterations = 0
        while True:
            if instance_id in manager.macro_state.cancel_instance_ids:
                break
            if block_mouse_movement:
                begin_mouse_rel_suppression(
                    manager,
                    timeout_s=max(0.1, suppression_timeout_s),
                    deps=deps,
                )
            iterations += 1
            pending_abs_moves.clear()
            if move_to_start:
                await manager.set_cursor_position(int(start_x), int(start_y))

            # Anchor every replay iteration to a single monotonic reference so
            # each event's wait is computed against its absolute deadline rather
            # than the gap from the previous event. Overshoot from any single
            # asyncio.sleep() is absorbed by the next event's remaining-time
            # calculation instead of accumulating across the macro. Control
            # actions that intentionally block replay extend the timeline via a
            # separate offset so later event deadlines preserve legacy
            # sequential semantics.
            iteration_anchor = event_loop.time()
            timeline_offset_s = 0.0
            for idx, ev in enumerate(macro_events):
                if instance_id in manager.macro_state.cancel_instance_ids:
                    break
                if (idx & 127) == 127:
                    await asyncio_mod.sleep(0)

                t_us = int_value_fn(ev.get("t_us"), 0)
                deadline = (
                    iteration_anchor
                    + timeline_offset_s
                    + (t_us / speed_factor) / 1_000_000.0
                )
                remaining = deadline - event_loop.time()
                # Skip sub-500µs waits: asyncio's timer resolution can't hit
                # them, and the drift they'd introduce is already compensated
                # on the next event by the anchored deadline.
                if remaining >= 0.0005:
                    await asyncio_mod.sleep(remaining)

                action_type = str(ev.get("macro_action", "") or "")
                if action_type:
                    timeline_offset_s += await run_macro_control_action(
                        manager,
                        ev,
                        speed,
                        deps=deps,
                    )
                    continue

                event_type = int_value_fn(ev.get("type"), 0)
                event_code = int_value_fn(ev.get("code"), 0)
                event_value = int_value_fn(ev.get("value"), 0)
                device_type = str_value_fn(ev.get("device_type"), "other")

                if (
                    event_type == evdev_mod.ecodes.EV_REL
                    and ev.get("synthetic_move")
                    and ev.get("move_mode") == "abs"
                ):
                    move_id = str_value_fn(ev.get("move_id"), "")
                    if move_id:
                        slot = pending_abs_moves.setdefault(move_id, {})
                        if ev.get("move_step") == 1:
                            if event_code == evdev_mod.ecodes.REL_X:
                                slot["x"] = event_value
                            elif event_code == evdev_mod.ecodes.REL_Y:
                                slot["y"] = event_value
                            if "x" in slot and "y" in slot:
                                await manager.set_cursor_position(slot["x"], slot["y"])
                                pending_abs_moves.pop(move_id, None)
                    continue

                if event_type == evdev_mod.ecodes.EV_SYN:
                    continue
                if event_type == evdev_mod.ecodes.EV_REL and not replay_mouse_movement:
                    continue
                if (
                    event_type == evdev_mod.ecodes.EV_KEY
                    and event_code in mouse_btn_codes
                    and not replay_mouse_clicks
                ):
                    continue

                if device_type == "keyboard":
                    uinput = manager.output_state.keyboard_uinput
                    output_class = "keyboard"
                elif device_type == "mouse":
                    uinput = manager.output_state.mouse_uinput
                    output_class = "mouse"
                elif device_type == "gamepad":
                    uinput = manager.output_state.gamepad_uinput
                    output_class = "gamepad"
                else:
                    if event_type == evdev_mod.ecodes.EV_KEY:
                        uinput = manager.output_state.keyboard_uinput
                        output_class = "keyboard"
                    elif event_type in (evdev_mod.ecodes.EV_REL, evdev_mod.ecodes.EV_ABS):
                        uinput = manager.output_state.mouse_uinput
                        output_class = "mouse"
                    else:
                        continue

                if not uinput:
                    continue

                output = uinput_writer(uinput)
                if output is None:
                    continue
                output.write(event_type, event_code, event_value)
                output.syn()
                if event_type == evdev_mod.ecodes.EV_KEY:
                    if event_value == 1:
                        track_macro_key_press(manager, instance_id, output_class, event_code)
                    elif event_value == 0:
                        track_macro_key_release(manager, instance_id, output_class, event_code)

            if not macro_events and loop_mode in {"hold", "toggle"}:
                await asyncio_mod.sleep(0.01)
            else:
                await asyncio_mod.sleep(0)

            if loop_mode == "count":
                if iterations >= max(1, loop_count):
                    break
            elif loop_mode == "none":
                break
    except asyncio_mod.CancelledError:
        pass
    except Exception as exc:
        deps.log.warning("Macro playback aborted: %s", exc)
    finally:
        manager.macro_state.cancel_instance_ids.discard(instance_id)
        release_macro_held_for_instance(
            manager,
            instance_id,
            deps=deps,
        )
        manager.macro_state.tasks.pop(instance_id, None)
        manager.macro_state.instance_meta.pop(instance_id, None)
        if block_mouse_movement:
            release_macro_mouse_inhibit(manager)
        if manager.verbosity >= 1:
            deps.log.debug("Macro playback finished: %s", macro_name or "<unnamed>")


def track_macro_key_press(
    manager: _MacroManager, instance_id: int, device_class: str, code: int
) -> None:
    key = (device_class, int(code))
    held = manager.macro_state.instance_held.setdefault(instance_id, set())
    if key in held:
        return
    held.add(key)
    held_refcount = manager.macro_state.held_refcount
    held_refcount[key] = held_refcount.get(key, 0) + 1


def track_macro_key_release(
    manager: _MacroManager, instance_id: int, device_class: str, code: int
) -> None:
    key = (device_class, int(code))
    held = manager.macro_state.instance_held.get(instance_id)
    if not held or key not in held:
        return
    held.remove(key)
    held_refcount = manager.macro_state.held_refcount
    count = held_refcount.get(key, 0)
    if count <= 1:
        held_refcount.pop(key, None)
    else:
        held_refcount[key] = count - 1


def release_macro_held_for_instance(
    manager: _MacroManager,
    instance_id: int,
    *,
    deps: MacroRuntimeDeps,
) -> None:
    held = manager.macro_state.instance_held.pop(instance_id, set())
    if not held:
        return

    uinputs = {
        "keyboard": deps.uinput_writer(manager.output_state.keyboard_uinput),
        "mouse": deps.uinput_writer(manager.output_state.mouse_uinput),
        "gamepad": deps.uinput_writer(manager.output_state.gamepad_uinput),
    }
    synced: set[str] = set()
    held_refcount = manager.macro_state.held_refcount

    for key in held:
        count = held_refcount.get(key, 0)
        if count <= 1:
            held_refcount.pop(key, None)
            device_class, code = key
            uinput = uinputs.get(device_class)
            if not uinput:
                continue
            try:
                uinput.write(deps.evdev_mod.ecodes.EV_KEY, int(code), 0)
                synced.add(device_class)
            except Exception:
                continue
        else:
            held_refcount[key] = count - 1

    for device_class in synced:
        uinput = uinputs.get(device_class)
        if not uinput:
            continue
        try:
            uinput.syn()
        except Exception:
            pass


def acquire_macro_mouse_inhibit(
    manager: _MacroManager, timeout_s: float, *, deps: MacroRuntimeDeps
) -> None:
    manager.macro_state.mouse_inhibit_count += 1
    begin_mouse_rel_suppression(manager, timeout_s=max(0.1, timeout_s), deps=deps)


def release_macro_mouse_inhibit(manager: _MacroManager) -> None:
    if manager.macro_state.mouse_inhibit_count > 0:
        manager.macro_state.mouse_inhibit_count -= 1
    if manager.macro_state.mouse_inhibit_count == 0:
        end_mouse_rel_suppression(manager)


async def run_macro_control_action(
    manager: _MacroManager,
    ev: dict[str, object],
    speed: float,
    *,
    deps: MacroRuntimeDeps,
) -> float:
    asyncio_mod = deps.asyncio_mod
    str_value_fn = deps.str_value_fn
    int_value_fn = deps.int_value_fn
    action_type = str_value_fn(ev.get("macro_action"), "")
    if action_type == "wait_fixed":
        duration_ms = max(0, int_value_fn(ev.get("duration_ms"), 0))
        scaled = duration_ms / max(speed, 0.01)
        if scaled > 0:
            loop = asyncio_mod.get_running_loop()
            started_at = loop.time()
            await asyncio_mod.sleep(scaled / 1000.0)
            return max(0.0, loop.time() - started_at)
        return 0.0

    if action_type == "wait_random":
        min_ms = max(0, int_value_fn(ev.get("min_ms"), 0))
        max_ms = max(min_ms, int_value_fn(ev.get("max_ms"), min_ms))
        sampled_ms = deps.random_mod.randint(min_ms, max_ms)
        scaled = sampled_ms / max(speed, 0.01)
        if scaled > 0:
            loop = asyncio_mod.get_running_loop()
            started_at = loop.time()
            await asyncio_mod.sleep(scaled / 1000.0)
            return max(0.0, loop.time() - started_at)
        return 0.0

    if action_type == "exec_async":
        command = str_value_fn(ev.get("command"), "").strip()
        if not command:
            return 0.0
        if manager.broadcast_callback:
            await manager.broadcast_callback(
                deps.command_type.ACTION_TRIGGER,
                {
                    "action_type": "exec",
                    "cmd": command,
                    "macro_exec_async": True,
                },
            )
        return 0.0

    if action_type == "exec_sync":
        command = str_value_fn(ev.get("command"), "").strip()
        if not command:
            return 0.0

        timeout_ms = max(1, int_value_fn(ev.get("timeout_ms"), 30000))
        inhibit_mouse = bool(ev.get("inhibit_mouse", False))
        loop = asyncio_mod.get_running_loop()
        started_at = loop.time()
        if inhibit_mouse:
            acquire_macro_mouse_inhibit(
                manager,
                timeout_s=max(1.0, timeout_ms / 1000.0 + 1.0),
                deps=deps,
            )

        wait_id = deps.uuid_mod.uuid4().hex
        try:
            waiter = loop.create_future()
            manager.macro_state.exec_waiters[wait_id] = waiter

            if manager.broadcast_callback:
                await manager.broadcast_callback(
                    deps.command_type.ACTION_TRIGGER,
                    {
                        "action_type": "exec",
                        "cmd": command,
                        "macro_exec_wait_id": wait_id,
                    },
                )
                with deps.contextlib_mod.suppress(asyncio_mod.TimeoutError):
                    await asyncio_mod.wait_for(waiter, timeout=max(0.1, timeout_ms / 1000.0))
        finally:
            manager.macro_state.exec_waiters.pop(wait_id, None)
            if inhibit_mouse:
                release_macro_mouse_inhibit(manager)
        return max(0.0, loop.time() - started_at)

    return 0.0


def complete_macro_exec_wait(
    manager: _MacroManager, wait_id: str, returncode: int
) -> dict[str, object]:
    wait_key = str(wait_id or "").strip()
    if not wait_key:
        return {"status": "error", "message": "missing wait_id"}

    waiter = manager.macro_state.exec_waiters.get(wait_key)
    if waiter and not waiter.done():
        waiter.set_result(int(returncode))
        return {"status": "ok", "matched": True}
    return {"status": "ok", "matched": False}


def begin_mouse_rel_suppression(
    manager: _MacroManager, timeout_s: float, *, deps: MacroRuntimeDeps
) -> None:
    asyncio_mod = deps.asyncio_mod
    manager.macro_state.mouse_rel_suppressed = True
    if (
        manager.macro_state.mouse_rel_suppression_watchdog_task
        and not manager.macro_state.mouse_rel_suppression_watchdog_task.done()
    ):
        manager.macro_state.mouse_rel_suppression_watchdog_task.cancel()
    manager.macro_state.mouse_rel_suppression_watchdog_task = asyncio_mod.create_task(
        mouse_rel_suppression_watchdog(manager, timeout_s, deps=deps)
    )


def end_mouse_rel_suppression(manager: _MacroManager) -> None:
    manager.macro_state.mouse_rel_suppressed = False
    if (
        manager.macro_state.mouse_rel_suppression_watchdog_task
        and not manager.macro_state.mouse_rel_suppression_watchdog_task.done()
    ):
        manager.macro_state.mouse_rel_suppression_watchdog_task.cancel()
    manager.macro_state.mouse_rel_suppression_watchdog_task = None


async def mouse_rel_suppression_watchdog(
    manager: _MacroManager, timeout_s: float, *, deps: MacroRuntimeDeps
) -> None:
    asyncio_mod = deps.asyncio_mod
    try:
        await asyncio_mod.sleep(timeout_s)
        manager.macro_state.mouse_rel_suppressed = False
    except asyncio_mod.CancelledError:
        pass
