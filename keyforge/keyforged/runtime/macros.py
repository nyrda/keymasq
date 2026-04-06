import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any, ClassVar, Protocol

from keyforge.common.ipc import CommandType

type JsonObject = dict[str, object]
type IntValueFn = Callable[[object, int], int]
type StrValueFn = Callable[[object, str], str]


class _WritableUInput(Protocol):
    def write(self, event_type: int, code: int, value: int) -> None: ...

    def syn(self) -> None: ...


type UInputWriter = Callable[[object | None], _WritableUInput | None]
type BroadcastCallback = Callable[[CommandType, JsonObject], Awaitable[None]]


class _OutputState(Protocol):
    @property
    def keyboard_uinput(self) -> object | None: ...

    @property
    def mouse_uinput(self) -> object | None: ...

    @property
    def gamepad_uinput(self) -> object | None: ...


class _MacroState(Protocol):
    tasks: dict[int, asyncio.Task[None]]
    instance_meta: dict[int, dict[str, str]]
    instance_seq: int
    instance_held: dict[int, set[tuple[str, int]]]
    held_refcount: dict[tuple[str, int], int]
    cancel_instance_ids: set[int]
    mouse_inhibit_count: int
    exec_waiters: dict[str, asyncio.Future[int]]
    mouse_rel_suppressed: bool
    mouse_rel_suppression_watchdog_task: asyncio.Task[None] | None


class _TrackedOutputDevice(Protocol):
    def release_tracked_outputs(self) -> None: ...


class _UUIDLike(Protocol):
    @property
    def hex(self) -> str: ...


class _UUIDModule(Protocol):
    def uuid4(self) -> _UUIDLike: ...


class _RandomModule(Protocol):
    def randint(self, a: int, b: int) -> int: ...


class _CommandTypeEnum(Protocol):
    ACTION_TRIGGER: ClassVar[CommandType]


class _AsyncioLoop(Protocol):
    def create_future(self) -> asyncio.Future[int]: ...


class _AsyncioModule(Protocol):
    CancelledError: ClassVar[type[BaseException]]
    TimeoutError: ClassVar[type[BaseException]]

    def create_task(self, coro: Awaitable[None], /) -> asyncio.Task[None]: ...

    async def sleep(self, delay: float, /) -> None: ...

    def get_running_loop(self) -> _AsyncioLoop: ...

    def gather(self, *aws: object, return_exceptions: bool = False) -> Awaitable[object]: ...

    def wait_for(self, aw: Awaitable[object], timeout: float) -> Awaitable[object]: ...


class _ContextlibModule(Protocol):
    def suppress(self, *exceptions: type[BaseException]) -> AbstractContextManager[None]: ...


class _MacroManager(Protocol):
    @property
    def output_state(self) -> _OutputState: ...

    @property
    def macro_state(self) -> _MacroState: ...

    @property
    def grabbed_devices(self) -> Mapping[str, Sequence[_TrackedOutputDevice]]: ...

    @property
    def broadcast_callback(self) -> BroadcastCallback | None: ...

    @property
    def verbosity(self) -> int: ...


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
    asyncio_mod: _AsyncioModule,
    contextlib_mod: _ContextlibModule,
    evdev_mod: Any,
    log: logging.Logger,
    int_value_fn: IntValueFn,
    str_value_fn: StrValueFn,
    uinput_writer: UInputWriter,
    random_mod: _RandomModule,
    uuid_mod: _UUIDModule,
    command_type: _CommandTypeEnum,
) -> dict[str, object]:
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
                asyncio_mod=asyncio_mod,
                contextlib_mod=contextlib_mod,
                evdev_mod=evdev_mod,
                uinput_writer=uinput_writer,
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
                asyncio_mod=asyncio_mod,
                contextlib_mod=contextlib_mod,
                evdev_mod=evdev_mod,
                uinput_writer=uinput_writer,
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
            asyncio_mod=asyncio_mod,
            evdev_mod=evdev_mod,
            log=log,
            int_value_fn=int_value_fn,
            str_value_fn=str_value_fn,
            uinput_writer=uinput_writer,
            contextlib_mod=contextlib_mod,
            random_mod=random_mod,
            uuid_mod=uuid_mod,
            command_type=command_type,
        )
    )
    manager.macro_state.tasks[instance_id] = task
    return {"status": "ok"}


async def cancel_macro_playback(
    manager: _MacroManager,
    *,
    asyncio_mod: _AsyncioModule,
    contextlib_mod: _ContextlibModule,
    evdev_mod: Any,
    uinput_writer: UInputWriter,
) -> dict[str, object]:
    running_ids = running_macro_instance_ids(manager)
    cancelled = await cancel_macro_instances(
        manager,
        running_ids,
        asyncio_mod=asyncio_mod,
        contextlib_mod=contextlib_mod,
        evdev_mod=evdev_mod,
        uinput_writer=uinput_writer,
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
    asyncio_mod: _AsyncioModule,
    contextlib_mod: _ContextlibModule,
    evdev_mod: Any,
    uinput_writer: UInputWriter,
) -> int:
    unique_ids = list(dict.fromkeys(int(i) for i in instance_ids))
    if not unique_ids:
        return 0

    for instance_id in unique_ids:
        manager.macro_state.cancel_instance_ids.add(instance_id)

    for instance_id in unique_ids:
        release_macro_held_for_instance(
            manager,
            instance_id,
            evdev_mod=evdev_mod,
            uinput_writer=uinput_writer,
        )

    tasks = [
        task
        for instance_id, task in manager.macro_state.tasks.items()
        if instance_id in unique_ids and not task.done()
    ]
    for task in tasks:
        task.cancel()

    if tasks:
        with contextlib_mod.suppress(Exception):
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
    asyncio_mod: _AsyncioModule,
    evdev_mod: Any,
    log: logging.Logger,
    int_value_fn: IntValueFn,
    str_value_fn: StrValueFn,
    uinput_writer: UInputWriter,
    contextlib_mod: _ContextlibModule,
    random_mod: _RandomModule,
    uuid_mod: _UUIDModule,
    command_type: _CommandTypeEnum,
) -> None:
    mouse_btn_codes = frozenset(range(0x110, 0x118))
    pending_abs_moves: dict[str, dict[str, int]] = {}

    if manager.verbosity >= 1:
        log.debug("Macro playback started: %s", macro_name or "<unnamed>")

    macro_duration_us = (
        max(int_value_fn(ev.get("t_us"), 0) for ev in macro_events) if macro_events else 0
    )
    suppression_timeout_s = max(2.0, (macro_duration_us / max(speed, 0.01)) / 1_000_000.0 + 1.0)
    try:
        if block_mouse_movement:
            acquire_macro_mouse_inhibit(
                manager,
                timeout_s=suppression_timeout_s,
                asyncio_mod=asyncio_mod,
            )

        iterations = 0
        while True:
            if instance_id in manager.macro_state.cancel_instance_ids:
                break
            iterations += 1
            pending_abs_moves.clear()
            if move_to_start:
                emit_absolute_mouse_move(
                    manager,
                    int(start_x),
                    int(start_y),
                    evdev_mod=evdev_mod,
                    uinput_writer=uinput_writer,
                )

            prev_t_us = 0
            for idx, ev in enumerate(macro_events):
                if instance_id in manager.macro_state.cancel_instance_ids:
                    break
                if (idx & 127) == 127:
                    await asyncio_mod.sleep(0)

                t_us = int_value_fn(ev.get("t_us"), 0)
                delay_us = max(0, t_us - prev_t_us)
                prev_t_us = t_us
                scaled_delay_us = int(delay_us / speed)
                if scaled_delay_us >= 500:
                    await asyncio_mod.sleep(scaled_delay_us / 1_000_000)

                action_type = str(ev.get("macro_action", "") or "")
                if action_type:
                    await run_macro_control_action(
                        manager,
                        ev,
                        speed,
                        asyncio_mod=asyncio_mod,
                        contextlib_mod=contextlib_mod,
                        random_mod=random_mod,
                        uuid_mod=uuid_mod,
                        command_type=command_type,
                        str_value_fn=str_value_fn,
                        int_value_fn=int_value_fn,
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
                                emit_absolute_mouse_move(
                                    manager,
                                    slot["x"],
                                    slot["y"],
                                    evdev_mod=evdev_mod,
                                    uinput_writer=uinput_writer,
                                )
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
        log.warning("Macro playback aborted: %s", exc)
    finally:
        manager.macro_state.cancel_instance_ids.discard(instance_id)
        release_macro_held_for_instance(
            manager,
            instance_id,
            evdev_mod=evdev_mod,
            uinput_writer=uinput_writer,
        )
        manager.macro_state.tasks.pop(instance_id, None)
        manager.macro_state.instance_meta.pop(instance_id, None)
        if block_mouse_movement:
            release_macro_mouse_inhibit(manager)
        if manager.verbosity >= 1:
            log.debug("Macro playback finished: %s", macro_name or "<unnamed>")


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
    evdev_mod: Any,
    uinput_writer: UInputWriter,
) -> None:
    held = manager.macro_state.instance_held.pop(instance_id, set())
    if not held:
        return

    uinputs = {
        "keyboard": uinput_writer(manager.output_state.keyboard_uinput),
        "mouse": uinput_writer(manager.output_state.mouse_uinput),
        "gamepad": uinput_writer(manager.output_state.gamepad_uinput),
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
                uinput.write(evdev_mod.ecodes.EV_KEY, int(code), 0)
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
    manager: _MacroManager, timeout_s: float, *, asyncio_mod: _AsyncioModule
) -> None:
    manager.macro_state.mouse_inhibit_count += 1
    begin_mouse_rel_suppression(manager, timeout_s=max(0.1, timeout_s), asyncio_mod=asyncio_mod)


def release_macro_mouse_inhibit(manager: _MacroManager) -> None:
    if manager.macro_state.mouse_inhibit_count > 0:
        manager.macro_state.mouse_inhibit_count -= 1
    if manager.macro_state.mouse_inhibit_count == 0:
        end_mouse_rel_suppression(manager)


def emit_absolute_mouse_move(
    manager: _MacroManager, x: int, y: int, *, evdev_mod: Any, uinput_writer: UInputWriter
) -> None:
    mouse_uinput = uinput_writer(manager.output_state.mouse_uinput)
    if mouse_uinput is None:
        return
    try:
        mouse_uinput.write(evdev_mod.ecodes.EV_REL, evdev_mod.ecodes.REL_X, -2147483648)
        mouse_uinput.write(evdev_mod.ecodes.EV_REL, evdev_mod.ecodes.REL_Y, -2147483648)
        mouse_uinput.syn()
        mouse_uinput.write(evdev_mod.ecodes.EV_REL, evdev_mod.ecodes.REL_X, int(x))
        mouse_uinput.write(evdev_mod.ecodes.EV_REL, evdev_mod.ecodes.REL_Y, int(y))
        mouse_uinput.syn()
    except Exception:
        pass


async def run_macro_control_action(
    manager: _MacroManager,
    ev: dict[str, object],
    speed: float,
    *,
    asyncio_mod: _AsyncioModule,
    contextlib_mod: _ContextlibModule,
    random_mod: _RandomModule,
    uuid_mod: _UUIDModule,
    command_type: _CommandTypeEnum,
    str_value_fn: StrValueFn,
    int_value_fn: IntValueFn,
) -> None:
    action_type = str_value_fn(ev.get("macro_action"), "")
    if action_type == "wait_fixed":
        duration_ms = max(0, int_value_fn(ev.get("duration_ms"), 0))
        scaled = duration_ms / max(speed, 0.01)
        if scaled > 0:
            await asyncio_mod.sleep(scaled / 1000.0)
        return

    if action_type == "wait_random":
        min_ms = max(0, int_value_fn(ev.get("min_ms"), 0))
        max_ms = max(min_ms, int_value_fn(ev.get("max_ms"), min_ms))
        sampled_ms = random_mod.randint(min_ms, max_ms)
        scaled = sampled_ms / max(speed, 0.01)
        if scaled > 0:
            await asyncio_mod.sleep(scaled / 1000.0)
        return

    if action_type == "exec_async":
        command = str_value_fn(ev.get("command"), "").strip()
        if not command:
            return
        if manager.broadcast_callback:
            await manager.broadcast_callback(
                command_type.ACTION_TRIGGER,
                {
                    "action_type": "exec",
                    "cmd": command,
                    "macro_exec_async": True,
                },
            )
        return

    if action_type == "exec_sync":
        command = str_value_fn(ev.get("command"), "").strip()
        if not command:
            return

        timeout_ms = max(1, int_value_fn(ev.get("timeout_ms"), 30000))
        inhibit_mouse = bool(ev.get("inhibit_mouse", False))
        if inhibit_mouse:
            acquire_macro_mouse_inhibit(
                manager,
                timeout_s=max(1.0, timeout_ms / 1000.0 + 1.0),
                asyncio_mod=asyncio_mod,
            )

        wait_id = uuid_mod.uuid4().hex
        try:
            loop = asyncio_mod.get_running_loop()
            waiter = loop.create_future()
            manager.macro_state.exec_waiters[wait_id] = waiter

            if manager.broadcast_callback:
                await manager.broadcast_callback(
                    command_type.ACTION_TRIGGER,
                    {
                        "action_type": "exec",
                        "cmd": command,
                        "macro_exec_wait_id": wait_id,
                    },
                )
                with contextlib_mod.suppress(asyncio_mod.TimeoutError):
                    await asyncio_mod.wait_for(waiter, timeout=max(0.1, timeout_ms / 1000.0))
        finally:
            manager.macro_state.exec_waiters.pop(wait_id, None)
            if inhibit_mouse:
                release_macro_mouse_inhibit(manager)


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
    manager: _MacroManager, timeout_s: float, *, asyncio_mod: _AsyncioModule
) -> None:
    manager.macro_state.mouse_rel_suppressed = True
    if (
        manager.macro_state.mouse_rel_suppression_watchdog_task
        and not manager.macro_state.mouse_rel_suppression_watchdog_task.done()
    ):
        manager.macro_state.mouse_rel_suppression_watchdog_task.cancel()
    manager.macro_state.mouse_rel_suppression_watchdog_task = asyncio_mod.create_task(
        mouse_rel_suppression_watchdog(manager, timeout_s, asyncio_mod=asyncio_mod)
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
    manager: _MacroManager, timeout_s: float, *, asyncio_mod: _AsyncioModule
) -> None:
    try:
        await asyncio_mod.sleep(timeout_s)
        manager.macro_state.mouse_rel_suppressed = False
    except asyncio_mod.CancelledError:
        pass
