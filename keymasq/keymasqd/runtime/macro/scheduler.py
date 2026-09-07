from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

from keymasq.common.coercion import coerce_bool, coerce_float
from keymasq.common.model.actions import (
    DEFAULT_NATURAL_MOUSE_MOVE_CURVE,
    DEFAULT_NATURAL_MOUSE_MOVE_SPEED,
)
from keymasq.keymasqd.macro_file import MacroFileChangedError
from keymasq.keymasqd.runtime.macro import controls, events, mouse, outputs
from keymasq.keymasqd.runtime.macro.cache import (
    MAX_CACHEABLE_MACRO_RUNTIME_US,
    MacroCacheCandidate,
)
from keymasq.keymasqd.runtime.macro.exceptions import (
    MacroCallError,
    MacroChildPlaybackError,
)
from keymasq.keymasqd.runtime.macro.loops import (
    MacroLoopStateMachine,
    is_loop_instance_active,
)
from keymasq.keymasqd.runtime.macro.state import (
    MacroEventSource,
    MacroRuntimeDeps,
    NaturalMacroMover,
)
from keymasq.keymasqd.runtime.macro.timing import MacroPlaybackTimeline

type MacroManager = Any
type ControlActionRunner = Callable[..., Awaitable[float]]
type RuntimeAction = Callable[..., None]

_MOUSE_BUTTON_CODES = frozenset(range(0x110, 0x118))
_SEMANTIC_MOUSE_ACTIONS = frozenset({"mouse_move_abs", "mouse_move_rel", "mouse_move_natural_abs"})
_MACRO_CALL_ACTIONS = frozenset({"macro_sync", "macro_parallel"})
_PARALLEL_CONTROL_ACTIONS = frozenset({"exec_parallel"})


async def play_macro_task(
    manager: MacroManager,
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
    control_action_fn: ControlActionRunner | None = None,
    acquire_mouse_inhibit_fn: RuntimeAction | None = None,
    release_mouse_inhibit_fn: RuntimeAction | None = None,
    begin_mouse_suppression_fn: RuntimeAction | None = None,
    renew_mouse_suppression_fn: RuntimeAction | None = None,
    release_held_fn: RuntimeAction | None = None,
) -> None:
    """Schedule and execute one macro instance until its loop policy finishes."""

    asyncio_mod = deps.asyncio_mod
    control_action = control_action_fn or controls.run_macro_control_action
    acquire_mouse_inhibit = acquire_mouse_inhibit_fn or mouse.acquire_macro_mouse_inhibit
    release_mouse_inhibit = release_mouse_inhibit_fn or mouse.release_macro_mouse_inhibit
    begin_mouse_suppression = begin_mouse_suppression_fn or mouse.begin_mouse_rel_suppression
    renew_mouse_suppression = renew_mouse_suppression_fn or mouse.renew_macro_mouse_suppression
    release_held = release_held_fn or outputs.release_macro_held_for_instance
    if macro_event_source is None:
        macro_event_source = events.list_macro_event_source(
            macro_events,
            int_value_fn=deps.int_value_fn,
        )

    if manager.verbosity >= 1:
        deps.log.debug("Macro playback started: %s", macro_name or "<unnamed>")

    speed_factor = max(0.01, speed)
    macro_duration_us = int(macro_event_source.duration_us)
    suppression_timeout_s = max(
        2.0,
        (macro_duration_us / speed_factor) / 1_000_000.0 + 1.0,
    )
    event_loop = asyncio_mod.get_running_loop()
    loop_state = MacroLoopStateMachine(loop_mode, loop_count)
    diagnostic_initial_load_us = macro_event_source.diagnostic_initial_load_us
    cached_events = macro_event_source.cached_events
    verify_cached_revision = cached_events is not None
    cache_candidate = (
        macro_event_source.begin_cache_candidate()
        if cached_events is None and macro_event_source.begin_cache_candidate is not None
        else None
    )
    try:
        if block_mouse_movement:
            acquire_mouse_inhibit(
                manager,
                timeout_s=suppression_timeout_s,
                deps=deps,
            )

        while True:
            if instance_id in manager.macro_state.cancel_instance_ids:
                break
            iteration_started_ns = (
                time.perf_counter_ns()
                if cache_candidate is not None
                or (
                    diagnostic_initial_load_us is not None and deps.diagnostics_recorder is not None
                )
                else None
            )
            if block_mouse_movement:
                begin_mouse_suppression(
                    manager,
                    timeout_s=max(0.1, suppression_timeout_s),
                    deps=deps,
                )
            loop_state.begin_iteration()
            if move_to_start:
                await manager.set_cursor_position(int(start_x), int(start_y))

            timeline = MacroPlaybackTimeline(event_loop.time(), speed_factor)
            parallel_tasks: set[asyncio.Task[Any]] = set()
            try:
                stop_current_run = await _play_iteration(
                    manager,
                    instance_id=instance_id,
                    macro_event_source=macro_event_source,
                    macro_name=macro_name,
                    replay_mouse_movement=replay_mouse_movement,
                    replay_mouse_clicks=replay_mouse_clicks,
                    block_mouse_movement=block_mouse_movement,
                    timeline=timeline,
                    event_loop=event_loop,
                    deps=deps,
                    control_action_fn=control_action,
                    renew_mouse_suppression_fn=renew_mouse_suppression,
                    diagnostic_initial_load_us=diagnostic_initial_load_us,
                    cached_events=cached_events,
                    verify_cached_revision=verify_cached_revision,
                    cache_candidate=cache_candidate,
                    parallel_tasks=parallel_tasks,
                )
                if diagnostic_initial_load_us is not None:
                    diagnostic_initial_load_us = 0.0
                if cached_events is not None:
                    verify_cached_revision = True
                if stop_current_run:
                    break

                if instance_id not in manager.macro_state.cancel_instance_ids:
                    remaining = timeline.nominal_end_delay(
                        macro_duration_us,
                        now_s=event_loop.time(),
                    )
                    if remaining >= 0.0005:
                        if block_mouse_movement:
                            renew_mouse_suppression(
                                manager,
                                timeout_s=remaining + 1.0,
                                deps=deps,
                            )
                        await asyncio_mod.sleep(remaining)

                await _join_parallel_tasks(parallel_tasks, asyncio_mod=asyncio_mod)
            finally:
                await _cancel_parallel_tasks(parallel_tasks, asyncio_mod=asyncio_mod)

            iteration_elapsed_us = (
                max(0, time.perf_counter_ns() - iteration_started_ns) / 1000.0
                if iteration_started_ns is not None
                else None
            )
            if instance_id not in manager.macro_state.cancel_instance_ids:
                if iteration_elapsed_us is not None and deps.diagnostics_recorder is not None:
                    deps.diagnostics_recorder("macro_iteration", iteration_elapsed_us)
                if cache_candidate is not None:
                    entry = (
                        cache_candidate.commit()
                        if iteration_elapsed_us is not None
                        and iteration_elapsed_us < MAX_CACHEABLE_MACRO_RUNTIME_US
                        else None
                    )
                    if entry is None and cache_candidate.active:
                        cache_candidate.reject()
                    cache_candidate = None
                    if entry is not None:
                        cached_events = entry.events
                        verify_cached_revision = True

            if macro_event_source.event_count <= 0 and loop_state.mode in {"hold", "toggle"}:
                await asyncio_mod.sleep(0.01)
            else:
                await asyncio_mod.sleep(0)

            loop_state.active = is_loop_instance_active(manager.macro_state, instance_id)
            if not loop_state.should_continue():
                break
    except asyncio_mod.CancelledError:
        raise
    except MacroCallError as exc:
        if _is_child_instance(manager, instance_id) or manager.macro_state.instance_meta.get(
            instance_id, {}
        ).get("playback_id"):
            raise
        deps.log.error("Macro playback aborted: %s", exc)
    except MacroFileChangedError:
        if _is_child_instance(manager, instance_id) or manager.macro_state.instance_meta.get(
            instance_id, {}
        ).get("playback_id"):
            raise MacroCallError(
                f"{manager.macro_state.call_chain(instance_id)}: macro was modified or removed"
            ) from None
        deps.log.debug(
            "Macro playback ended because %s was modified or removed",
            macro_name or "<unnamed>",
        )
    except Exception as exc:
        if _is_child_instance(manager, instance_id) or manager.macro_state.instance_meta.get(
            instance_id, {}
        ).get("playback_id"):
            if isinstance(exc, MacroChildPlaybackError):
                raise
            raise MacroChildPlaybackError(
                f"{manager.macro_state.call_chain(instance_id)}: {exc}"
            ) from exc
        deps.log.exception("Macro playback aborted: %s", exc)
    finally:
        if cache_candidate is not None:
            cache_candidate.discard()
        manager.macro_state.cancel_instance_ids.discard(instance_id)
        release_held(manager, instance_id, deps=deps)
        manager.macro_state.forget_instance(instance_id)
        if block_mouse_movement:
            release_mouse_inhibit(manager)
        if manager.verbosity >= 1:
            deps.log.debug("Macro playback finished: %s", macro_name or "<unnamed>")


async def _play_iteration(
    manager: MacroManager,
    *,
    instance_id: int,
    macro_event_source: MacroEventSource,
    macro_name: str,
    replay_mouse_movement: bool,
    replay_mouse_clicks: bool,
    block_mouse_movement: bool,
    timeline: MacroPlaybackTimeline,
    event_loop: Any,
    deps: MacroRuntimeDeps,
    control_action_fn: ControlActionRunner,
    renew_mouse_suppression_fn: RuntimeAction,
    diagnostic_initial_load_us: float | None,
    cached_events: tuple[dict[str, object], ...] | None,
    verify_cached_revision: bool,
    cache_candidate: MacroCacheCandidate | None,
    parallel_tasks: set[asyncio.Task[Any]],
) -> bool:
    """Replay one source iteration; return true when a semantic move aborts it."""

    asyncio_mod = deps.asyncio_mod
    index = 0
    async for event in events.iter_macro_source_events(
        macro_event_source,
        deps=deps,
        diagnostic_initial_load_us=diagnostic_initial_load_us,
        cached_events=cached_events,
        verify_cached_revision=verify_cached_revision,
    ):
        _raise_finished_parallel_errors(parallel_tasks)
        if instance_id in manager.macro_state.cancel_instance_ids:
            break
        if cache_candidate is not None:
            cache_candidate.observe(event)
        if (index & 127) == 127:
            await asyncio_mod.sleep(0)
        index += 1

        timestamp_us = deps.int_value_fn(event.get("t_us"), 0)
        remaining = timeline.event_delay(timestamp_us, now_s=event_loop.time())
        if remaining >= 0.0005:
            await asyncio_mod.sleep(remaining)
            _raise_finished_parallel_errors(parallel_tasks)

        action_type = str(event.get("macro_action", "") or "")
        if action_type in _MACRO_CALL_ACTIONS:
            await asyncio_mod.sleep(0)
            child_name = deps.str_value_fn(event.get("macro_name"), "").strip()
            try:
                child_task = await manager.start_macro_child(
                    instance_id,
                    event,
                    deps=deps,
                )
            except MacroCallError as exc:
                chain = manager.macro_state.call_chain(instance_id, child_name or "<missing>")
                raise MacroCallError(f"{chain}: {exc}") from None
            except Exception as exc:
                chain = manager.macro_state.call_chain(instance_id, child_name or "<missing>")
                raise MacroChildPlaybackError(f"{chain}: {exc}") from exc
            if child_task is None:
                continue
            if action_type == "macro_sync":
                started_at = event_loop.time()
                await _await_child(child_task, asyncio_mod=asyncio_mod)
                timeline.extend_for_blocking_action(event_loop.time() - started_at)
            else:
                parallel_tasks.add(child_task)
            continue
        if action_type in _PARALLEL_CONTROL_ACTIONS:
            task = asyncio_mod.create_task(
                control_action_fn(
                    manager,
                    event,
                    renew_mouse_suppression=block_mouse_movement,
                    deps=deps,
                )
            )
            parallel_tasks.add(task)
            await asyncio_mod.sleep(0)
            _raise_finished_parallel_errors(parallel_tasks)
            continue
        if action_type in _SEMANTIC_MOUSE_ACTIONS:
            should_stop = await _run_semantic_mouse_action(
                manager,
                event,
                action_type=action_type,
                block_mouse_movement=block_mouse_movement,
                timeline=timeline,
                event_loop=event_loop,
                deps=deps,
                renew_mouse_suppression_fn=renew_mouse_suppression_fn,
            )
            if should_stop:
                return True
            continue
        if action_type:
            timeline.extend_for_blocking_action(
                await control_action_fn(
                    manager,
                    event,
                    renew_mouse_suppression=block_mouse_movement,
                    deps=deps,
                )
            )
            continue

        event_type = deps.int_value_fn(event.get("type"), 0)
        event_code = deps.int_value_fn(event.get("code"), 0)
        event_value = deps.int_value_fn(event.get("value"), 0)
        device_type = deps.str_value_fn(event.get("device_type"), "other")
        if event_type == deps.evdev_mod.ecodes.EV_SYN:
            continue
        if (
            event_type == deps.evdev_mod.ecodes.EV_REL
            and not replay_mouse_movement
            and not events.is_wheel_event(event_type, event_code, evdev_mod=deps.evdev_mod)
        ):
            continue
        if (
            event_type == deps.evdev_mod.ecodes.EV_KEY
            and event_code in _MOUSE_BUTTON_CODES
            and not replay_mouse_clicks
        ):
            continue

        raw_output_id = deps.str_value_fn(event.get("output_id"), "").strip()
        outputs.emit_macro_event(
            manager,
            instance_id=instance_id,
            event_type=event_type,
            event_code=event_code,
            event_value=event_value,
            device_type=device_type,
            output_id=raw_output_id or None,
            macro_name=macro_name,
            deps=deps,
        )
    return False


def _is_child_instance(manager: MacroManager, instance_id: int) -> bool:
    meta = manager.macro_state.instance_meta.get(instance_id, {})
    return meta.get("parent_instance_id") is not None


def _raise_finished_parallel_errors(tasks: set[asyncio.Task[Any]]) -> None:
    for task in tuple(tasks):
        if not task.done():
            continue
        tasks.discard(task)
        if task.cancelled():
            continue
        error = task.exception()
        if error is not None:
            raise error


async def _await_child(child: asyncio.Task[None], *, asyncio_mod: Any) -> None:
    try:
        await child
    except asyncio_mod.CancelledError:
        current = asyncio_mod.current_task()
        if current is not None and current.cancelling():
            raise


async def _join_parallel_tasks(
    tasks: set[asyncio.Task[Any]],
    *,
    asyncio_mod: Any,
) -> None:
    if not tasks:
        return
    results = await asyncio_mod.gather(*tuple(tasks), return_exceptions=True)
    tasks.clear()
    for result in results:
        if isinstance(result, asyncio_mod.CancelledError):
            continue
        if isinstance(result, BaseException):
            raise result


async def _cancel_parallel_tasks(
    tasks: set[asyncio.Task[Any]],
    *,
    asyncio_mod: Any,
) -> None:
    all_tasks = tuple(tasks)
    pending = [task for task in all_tasks if not task.done()]
    tasks.clear()
    for task in pending:
        task.cancel()
    if all_tasks:
        with contextlib.suppress(Exception):
            await asyncio_mod.gather(*all_tasks, return_exceptions=True)


async def _run_semantic_mouse_action(
    manager: MacroManager,
    event: dict[str, object],
    *,
    action_type: str,
    block_mouse_movement: bool,
    timeline: MacroPlaybackTimeline,
    event_loop: Any,
    deps: MacroRuntimeDeps,
    renew_mouse_suppression_fn: RuntimeAction,
) -> bool:
    x = deps.int_value_fn(event.get("x"), 0)
    y = deps.int_value_fn(event.get("y"), 0)
    if action_type == "mouse_move_abs":
        await manager.set_cursor_position(x, y)
        return False
    if action_type == "mouse_move_rel":
        outputs.emit_relative_mouse_move(manager, x, y, deps=deps)
        return False

    started_at = event_loop.time()
    result: dict[str, object] = {
        "status": "error",
        "message": "Natural mouse movement is unavailable",
    }
    max_duration_ms = deps.int_value_fn(event.get("max_duration_ms"), 3000)
    if block_mouse_movement:
        renew_mouse_suppression_fn(
            manager,
            timeout_s=max(0.1, max_duration_ms / 1000.0 + 1.0),
            deps=deps,
        )
    raw_mover = getattr(manager, "move_cursor_natural", None)
    if callable(raw_mover):
        mover = cast(NaturalMacroMover, raw_mover)
        result = await mover(
            x,
            y,
            coerce_float(event.get("speed"), DEFAULT_NATURAL_MOUSE_MOVE_SPEED),
            coerce_float(event.get("jitter"), 0.3),
            deps.str_value_fn(event.get("curve"), DEFAULT_NATURAL_MOUSE_MOVE_CURVE),
            deps.int_value_fn(event.get("tolerance"), 2),
            max_duration_ms,
        )
    timeline.extend_for_blocking_action(event_loop.time() - started_at)
    return coerce_bool(event.get("stop_on_failure"), False) and result.get("status") != "ok"
