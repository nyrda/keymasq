import contextlib
import logging
import random
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from typing import Any

from keymasq.common.ipc import CommandType
from keymasq.common.models import (
    DEFAULT_MACRO_LOOP_STOP_BEHAVIOR,
    normalize_macro_loop_stop_behavior,
)
from keymasq.keymasqd.runtime.grabbed_device_outputs import syn_if_passthrough_frame_closed

type IntValueFn = Callable[[object, int], int]
type StrValueFn = Callable[[object, str], str]
type _MacroManager = Any
type MacroEventIteratorFactory = Callable[[], Iterator[dict[str, object]]]


@dataclass(frozen=True)
class MacroRuntimeDeps:
    asyncio_mod: Any
    evdev_mod: Any
    uinput_writer: Any
    log: logging.Logger
    int_value_fn: IntValueFn
    str_value_fn: StrValueFn


@dataclass(frozen=True)
class MacroEventSource:
    event_count: int
    duration_us: int
    iter_events: MacroEventIteratorFactory


def gamepad_abs_cleanup_codes(evdev_mod: Any) -> frozenset[int]:
    return frozenset(
        int(code)
        for code in (
            evdev_mod.ecodes.ABS_X,
            evdev_mod.ecodes.ABS_Y,
            evdev_mod.ecodes.ABS_RX,
            evdev_mod.ecodes.ABS_RY,
            evdev_mod.ecodes.ABS_Z,
            evdev_mod.ecodes.ABS_RZ,
            evdev_mod.ecodes.ABS_HAT0X,
            evdev_mod.ecodes.ABS_HAT0Y,
        )
    )


def list_macro_event_source(
    macro_events: list[dict[str, object]],
    *,
    int_value_fn: IntValueFn,
) -> MacroEventSource:
    return MacroEventSource(
        event_count=len(macro_events),
        duration_us=max((int_value_fn(ev.get("t_us"), 0) for ev in macro_events), default=0),
        iter_events=lambda: iter(macro_events),
    )


async def play_macro(
    manager: _MacroManager,
    macro_events: list[dict[str, object]],
    macro_name: str,
    replay_mouse_movement: bool,
    replay_mouse_clicks: bool,
    speed: float,
    loop_mode: str,
    loop_count: int,
    loop_stop_behavior: str,
    move_to_start: bool,
    start_x: int,
    start_y: int,
    block_mouse_movement: bool,
    source_device: str,
    source_button: str,
    trigger_value: int,
    *,
    deps: MacroRuntimeDeps,
    macro_event_source: MacroEventSource | None = None,
) -> dict[str, object]:
    asyncio_mod = deps.asyncio_mod
    if not (
        manager.output_state.keyboard_uinput
        or manager.output_state.mouse_uinput
        or manager.output_state.gamepad_uinput
        or manager.output_state.virtual_gamepad_uinputs
    ):
        return {"status": "error", "message": "No output uinput devices available"}

    normalized_loop = str(loop_mode or "none").lower()
    if normalized_loop not in {"none", "count", "hold", "toggle"}:
        normalized_loop = "none"
    normalized_loop_stop_behavior = normalize_macro_loop_stop_behavior(
        loop_stop_behavior
    )
    count = max(1, int(loop_count or 1))
    source_key = (str(source_device), str(source_button))

    if int(trigger_value) == 0:
        hold_instances = find_matching_macro_instances(
            manager,
            loop_mode="hold",
            source_key=source_key,
        )
        if hold_instances:
            cancel_instances = loop_stop_cancel_instance_ids(manager, hold_instances)
            finish_instances = [
                instance_id for instance_id in hold_instances if instance_id not in cancel_instances
            ]
            mark_loop_instances_stopping(manager, finish_instances)
            cancelled = await cancel_macro_instances(manager, cancel_instances, deps=deps)
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
            cancel_instances = loop_stop_cancel_instance_ids(manager, toggle_instances)
            finish_instances = [
                instance_id
                for instance_id in toggle_instances
                if instance_id not in cancel_instances
            ]
            mark_loop_instances_stopping(manager, finish_instances)
            cancelled = await cancel_macro_instances(manager, cancel_instances, deps=deps)
            return {"status": "ok", "cancelled": cancelled > 0}

    if normalized_loop == "hold":
        hold_instances = find_matching_macro_instances(
            manager,
            loop_mode="hold",
            source_key=source_key,
        )
        if hold_instances:
            return {"status": "ok", "already_running": True}

    event_source = macro_event_source or list_macro_event_source(
        macro_events,
        int_value_fn=deps.int_value_fn,
    )
    if event_source.event_count <= 0:
        return {"status": "ok"}

    manager.macro_state.instance_seq += 1
    instance_id = manager.macro_state.instance_seq
    manager.macro_state.instance_held[instance_id] = set()
    manager.macro_state.instance_held_abs[instance_id] = set()
    manager.macro_state.cancel_instance_ids.discard(instance_id)
    manager.macro_state.instance_meta[instance_id] = {
        "loop_mode": normalized_loop,
        "source_device": source_key[0],
        "source_button": source_key[1],
        "macro_name": str(macro_name or ""),
        "loop_active": normalized_loop in {"hold", "toggle"},
        "loop_stop_behavior": normalized_loop_stop_behavior,
    }

    task = asyncio_mod.create_task(
        play_macro_task(
            manager,
            instance_id=instance_id,
            macro_events=macro_events,
            macro_event_source=event_source,
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


def loop_stop_cancel_instance_ids(manager: _MacroManager, instance_ids: list[int]) -> list[int]:
    cancel_instances: list[int] = []
    for instance_id in instance_ids:
        meta = manager.macro_state.instance_meta.get(instance_id, {})
        behavior = normalize_macro_loop_stop_behavior(
            meta.get("loop_stop_behavior", DEFAULT_MACRO_LOOP_STOP_BEHAVIOR)
        )
        if behavior == "cancel_run":
            cancel_instances.append(instance_id)
    return cancel_instances


def mark_loop_instances_stopping(manager: _MacroManager, instance_ids: list[int]) -> None:
    for instance_id in instance_ids:
        meta = manager.macro_state.instance_meta.get(instance_id)
        if meta is not None:
            meta["loop_active"] = False


def is_loop_instance_active(manager: _MacroManager, instance_id: int) -> bool:
    meta = manager.macro_state.instance_meta.get(instance_id, {})
    return bool(meta.get("loop_active", True))


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
        with contextlib.suppress(Exception):
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
    macro_event_source: MacroEventSource | None = None,
) -> None:
    asyncio_mod = deps.asyncio_mod
    evdev_mod = deps.evdev_mod
    int_value_fn = deps.int_value_fn
    str_value_fn = deps.str_value_fn
    uinput_writer = deps.uinput_writer
    mouse_btn_codes = frozenset(range(0x110, 0x118))
    if macro_event_source is None:
        macro_event_source = list_macro_event_source(
            macro_events,
            int_value_fn=int_value_fn,
        )

    if manager.verbosity >= 1:
        deps.log.debug("Macro playback started: %s", macro_name or "<unnamed>")

    speed_factor = max(0.01, speed)
    macro_duration_us = int(macro_event_source.duration_us)
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
            if move_to_start:
                await manager.set_cursor_position(int(start_x), int(start_y))

            # Anchor every replay iteration to a single monotonic reference so
            # each event's wait is computed against its absolute deadline rather
            # than the gap from the previous event. Overshoot from any single
            # asyncio.sleep() is absorbed by the next event's remaining-time
            # calculation instead of accumulating across the macro. Control
            # actions that intentionally block replay, such as waits and
            # synchronous exec, extend the timeline via a separate offset so
            # later event deadlines preserve sequential semantics.
            iteration_anchor = event_loop.time()
            timeline_offset_s = 0.0
            idx = 0
            async for ev in iter_macro_source_events(
                macro_event_source,
                deps=deps,
            ):
                if instance_id in manager.macro_state.cancel_instance_ids:
                    break
                if (idx & 127) == 127:
                    await asyncio_mod.sleep(0)
                idx += 1

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
                if action_type in {"mouse_move_abs", "mouse_move_rel"}:
                    if not replay_mouse_movement:
                        continue
                    x = int_value_fn(ev.get("x"), 0)
                    y = int_value_fn(ev.get("y"), 0)
                    if action_type == "mouse_move_abs":
                        await manager.set_cursor_position(x, y)
                    else:
                        uinput = manager.output_state.mouse_uinput
                        output = uinput_writer(uinput) if uinput else None
                        if output is not None:
                            output.write(
                                evdev_mod.ecodes.EV_REL,
                                evdev_mod.ecodes.REL_X,
                                x,
                            )
                            output.write(
                                evdev_mod.ecodes.EV_REL,
                                evdev_mod.ecodes.REL_Y,
                                y,
                            )
                            syn_if_passthrough_frame_closed(uinput, output)
                    continue
                if action_type:
                    timeline_offset_s += await run_macro_control_action(
                        manager,
                        ev,
                        renew_mouse_suppression=block_mouse_movement,
                        deps=deps,
                    )
                    continue

                event_type = int_value_fn(ev.get("type"), 0)
                event_code = int_value_fn(ev.get("code"), 0)
                event_value = int_value_fn(ev.get("value"), 0)
                device_type = str_value_fn(ev.get("device_type"), "other")

                if event_type == evdev_mod.ecodes.EV_SYN:
                    continue
                if (
                    event_type == evdev_mod.ecodes.EV_REL
                    and not replay_mouse_movement
                    and not _is_wheel_event(event_type, event_code, evdev_mod=evdev_mod)
                ):
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
                    output_id = str_value_fn(ev.get("output_id"), "").strip() or None
                    target = manager.resolve_gamepad_output(
                        output_id,
                        context=f"macro {macro_name or '<unnamed>'}",
                    )
                    if target is None:
                        continue
                    uinput = target.uinput
                    output_class = target.bucket
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
                syn_if_passthrough_frame_closed(uinput, output)
                if event_type == evdev_mod.ecodes.EV_KEY:
                    if event_value == 1:
                        track_macro_key_press(manager, instance_id, output_class, event_code)
                    elif event_value == 0:
                        track_macro_key_release(manager, instance_id, output_class, event_code)
                elif event_type == evdev_mod.ecodes.EV_ABS:
                    track_macro_abs_value(
                        manager,
                        instance_id,
                        output_class,
                        event_code,
                        event_value,
                        deps=deps,
                    )

            if instance_id not in manager.macro_state.cancel_instance_ids:
                end_deadline = iteration_anchor + (macro_duration_us / speed_factor) / 1_000_000.0
                remaining = end_deadline - event_loop.time()
                if remaining >= 0.0005:
                    if block_mouse_movement:
                        renew_macro_mouse_suppression(
                            manager,
                            timeout_s=remaining + 1.0,
                            deps=deps,
                        )
                    await asyncio_mod.sleep(remaining)

            if macro_event_source.event_count <= 0 and loop_mode in {"hold", "toggle"}:
                await asyncio_mod.sleep(0.01)
            else:
                await asyncio_mod.sleep(0)

            if loop_mode == "count":
                if iterations >= max(1, loop_count):
                    break
            elif loop_mode in {"hold", "toggle"}:
                if not is_loop_instance_active(manager, instance_id):
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


class _MacroBatchReader:
    def __init__(self, source: MacroEventSource, batch_size: int = 128) -> None:
        self._iterator = source.iter_events()
        self._batch_size = max(1, int(batch_size))

    def next_batch(self) -> list[dict[str, object]]:
        batch: list[dict[str, object]] = []
        for _ in range(self._batch_size):
            try:
                batch.append(next(self._iterator))
            except StopIteration:
                break
        return batch


async def iter_macro_source_events(
    source: MacroEventSource,
    *,
    deps: MacroRuntimeDeps,
) -> AsyncIterator[dict[str, object]]:
    reader = _MacroBatchReader(source)
    while True:
        batch = await deps.asyncio_mod.to_thread(reader.next_batch)
        if not batch:
            break
        for event in batch:
            yield event


def _is_wheel_event(event_type: int, event_code: int, *, evdev_mod: Any) -> bool:
    if int(event_type) != int(evdev_mod.ecodes.EV_REL):
        return False
    return int(event_code) in {
        int(evdev_mod.ecodes.REL_WHEEL),
        int(evdev_mod.ecodes.REL_HWHEEL),
        *(
            int(code)
            for code in (
                getattr(evdev_mod.ecodes, "REL_WHEEL_HI_RES", None),
                getattr(evdev_mod.ecodes, "REL_HWHEEL_HI_RES", None),
            )
            if code is not None
        ),
    }


def gamepad_output_class(device_class: str) -> str | None:
    if device_class == "gamepad":
        return "virtual-gamepad-1"
    if device_class.startswith("gamepad:"):
        output_id = device_class.removeprefix("gamepad:").strip()
        return output_id or None
    return None


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


def track_macro_abs_value(
    manager: _MacroManager,
    instance_id: int,
    device_class: str,
    code: int,
    value: int,
    *,
    deps: MacroRuntimeDeps,
) -> None:
    if not gamepad_output_class(device_class):
        return
    if int(code) not in gamepad_abs_cleanup_codes(deps.evdev_mod):
        return

    key = (device_class, int(code))
    held = manager.macro_state.instance_held_abs.setdefault(instance_id, set())
    held_refcount = manager.macro_state.held_abs_refcount
    if int(value) == 0:
        if key not in held:
            return
        held.remove(key)
        count = held_refcount.get(key, 0)
        if count <= 1:
            held_refcount.pop(key, None)
        else:
            held_refcount[key] = count - 1
        return

    if key in held:
        return
    held.add(key)
    held_refcount[key] = held_refcount.get(key, 0) + 1


def release_macro_held_for_instance(
    manager: _MacroManager,
    instance_id: int,
    *,
    deps: MacroRuntimeDeps,
) -> None:
    held = manager.macro_state.instance_held.pop(instance_id, set())
    held_abs = manager.macro_state.instance_held_abs.pop(instance_id, set())
    if not held and not held_abs:
        return

    uinputs = {
        "keyboard": (
            manager.output_state.keyboard_uinput,
            deps.uinput_writer(manager.output_state.keyboard_uinput),
        ),
        "mouse": (
            manager.output_state.mouse_uinput,
            deps.uinput_writer(manager.output_state.mouse_uinput),
        ),
        "gamepad": (
            manager.output_state.gamepad_uinput,
            deps.uinput_writer(manager.output_state.gamepad_uinput),
        ),
    }
    for output_id, uinput_dev in getattr(
        manager.output_state, "virtual_gamepad_uinputs", {}
    ).items():
        uinputs[f"gamepad:{output_id}"] = (uinput_dev, deps.uinput_writer(uinput_dev))
    for key in [*held, *held_abs]:
        device_class = str(key[0])
        output_id = gamepad_output_class(device_class)
        if output_id is None or device_class in uinputs:
            continue
        target = manager.resolve_gamepad_output(
            output_id,
            context="macro cleanup",
        )
        raw_uinput = target.uinput if target is not None else None
        uinputs[device_class] = (
            raw_uinput,
            deps.uinput_writer(raw_uinput),
        )
    synced: set[str] = set()
    held_refcount = manager.macro_state.held_refcount
    held_abs_refcount = manager.macro_state.held_abs_refcount

    for key in held:
        count = held_refcount.get(key, 0)
        if count <= 1:
            held_refcount.pop(key, None)
            device_class, code = key
            uinput_pair = uinputs.get(device_class)
            if not uinput_pair:
                continue
            _raw_uinput, writer = uinput_pair
            if not writer:
                continue
            try:
                writer.write(deps.evdev_mod.ecodes.EV_KEY, int(code), 0)
                synced.add(device_class)
            except Exception:
                continue
        else:
            held_refcount[key] = count - 1

    for key in held_abs:
        count = held_abs_refcount.get(key, 0)
        if count <= 1:
            held_abs_refcount.pop(key, None)
            device_class, code = key
            uinput_pair = uinputs.get(device_class)
            if not uinput_pair:
                continue
            _raw_uinput, writer = uinput_pair
            if not writer:
                continue
            try:
                writer.write(deps.evdev_mod.ecodes.EV_ABS, int(code), 0)
                synced.add(device_class)
            except Exception:
                continue
        else:
            held_abs_refcount[key] = count - 1

    for device_class in synced:
        uinput_pair = uinputs.get(device_class)
        if not uinput_pair:
            continue
        raw_uinput, writer = uinput_pair
        if not writer:
            continue
        try:
            syn_if_passthrough_frame_closed(raw_uinput, writer)
        except Exception:
            deps.log.debug("Failed to synchronize macro cleanup outputs", exc_info=True)


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
    *,
    renew_mouse_suppression: bool = False,
    deps: MacroRuntimeDeps,
) -> float:
    asyncio_mod = deps.asyncio_mod
    str_value_fn = deps.str_value_fn
    int_value_fn = deps.int_value_fn
    action_type = str_value_fn(ev.get("macro_action"), "")
    if action_type == "wait":
        duration_us = max(0, int_value_fn(ev.get("duration_us"), 0))
        if duration_us > 0:
            duration_s = duration_us / 1_000_000.0
            if renew_mouse_suppression:
                renew_macro_mouse_suppression(
                    manager,
                    timeout_s=duration_s + 1.0,
                    deps=deps,
                )
            loop = asyncio_mod.get_running_loop()
            started_at = loop.time()
            await asyncio_mod.sleep(duration_s)
            return max(0.0, loop.time() - started_at)
        return 0.0

    if action_type == "wait_random":
        min_us = max(0, int_value_fn(ev.get("min_us"), 0))
        max_us = max(min_us, int_value_fn(ev.get("max_us"), min_us))
        sampled_us = random.randint(min_us, max_us)
        if sampled_us > 0:
            sampled_s = sampled_us / 1_000_000.0
            if renew_mouse_suppression:
                renew_macro_mouse_suppression(
                    manager,
                    timeout_s=sampled_s + 1.0,
                    deps=deps,
                )
            loop = asyncio_mod.get_running_loop()
            started_at = loop.time()
            await asyncio_mod.sleep(sampled_s)
            return max(0.0, loop.time() - started_at)
        return 0.0

    if action_type == "exec_async":
        command = str_value_fn(ev.get("command"), "").strip()
        if not command:
            return 0.0
        if manager.broadcast_callback:
            await manager.broadcast_callback(
                CommandType.ACTION_TRIGGER,
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
        timeout_limit = getattr(manager, "macro_exec_timeout_max_ms", None)
        if timeout_limit is not None:
            with contextlib.suppress(TypeError, ValueError):
                timeout_ms = min(timeout_ms, max(1, int(timeout_limit)))
        inhibit_mouse = bool(ev.get("inhibit_mouse", False))
        loop = asyncio_mod.get_running_loop()
        started_at = loop.time()
        if inhibit_mouse:
            acquire_macro_mouse_inhibit(
                manager,
                timeout_s=max(1.0, timeout_ms / 1000.0 + 1.0),
                deps=deps,
            )
        elif renew_mouse_suppression:
            renew_macro_mouse_suppression(
                manager,
                timeout_s=timeout_ms / 1000.0 + 1.0,
                deps=deps,
            )

        wait_id = uuid.uuid4().hex
        try:
            waiter = loop.create_future()
            manager.macro_state.exec_waiters[wait_id] = waiter

            if manager.broadcast_callback:
                await manager.broadcast_callback(
                    CommandType.ACTION_TRIGGER,
                    {
                        "action_type": "exec",
                        "cmd": command,
                        "macro_exec_wait_id": wait_id,
                        "macro_exec_timeout_ms": timeout_ms,
                    },
                )
                with contextlib.suppress(asyncio_mod.TimeoutError):
                    await asyncio_mod.wait_for(waiter, timeout=max(0.1, timeout_ms / 1000.0))
        finally:
            manager.macro_state.exec_waiters.pop(wait_id, None)
            if inhibit_mouse:
                release_macro_mouse_inhibit(manager)
        return max(0.0, loop.time() - started_at)

    if action_type == "compositor_dispatch":
        dispatcher = str_value_fn(ev.get("dispatcher"), "").strip()
        if not dispatcher:
            return 0.0
        if manager.broadcast_callback:
            await manager.broadcast_callback(
                CommandType.ACTION_TRIGGER,
                {
                    "action_type": "compositor_dispatch",
                    "compositor": str_value_fn(ev.get("compositor"), "").strip(),
                    "dispatcher": dispatcher,
                    "args": str_value_fn(ev.get("args"), "").strip(),
                },
            )
        return 0.0

    return 0.0


def renew_macro_mouse_suppression(
    manager: _MacroManager, timeout_s: float, *, deps: MacroRuntimeDeps
) -> None:
    begin_mouse_rel_suppression(manager, timeout_s=max(0.1, timeout_s), deps=deps)


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
        if manager.macro_state.mouse_inhibit_count <= 0:
            manager.macro_state.mouse_rel_suppressed = False
    except asyncio_mod.CancelledError:
        pass
