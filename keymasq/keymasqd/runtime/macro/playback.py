from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

from keymasq.common.coercion import coerce_float
from keymasq.common.ipc import CommandType
from keymasq.common.model.actions import normalize_macro_loop_stop_behavior
from keymasq.keymasqd.runtime.macro import cleanup, loops, outputs, scheduler
from keymasq.keymasqd.runtime.macro.events import list_macro_event_source
from keymasq.keymasqd.runtime.macro.exceptions import MacroCallError
from keymasq.keymasqd.runtime.macro.options import MacroPlaybackOptions
from keymasq.keymasqd.runtime.macro.state import MacroEventSource, MacroRuntimeDeps
from keymasq.keymasqd.runtime.macro.timing import MacroPauseState

type MacroManager = Any


def resume_paused_macro(
    manager: MacroManager,
    playback_options: MacroPlaybackOptions,
    *,
    deps: MacroRuntimeDeps,
) -> bool:
    """Resume existing invocations before attempting to load a new macro file."""
    if int(playback_options.trigger_value) != 1:
        return False
    state = manager.macro_state
    source_key = (str(playback_options.source_device), str(playback_options.source_button))
    resumable_roots: set[int] = set()
    now_s = deps.asyncio_mod.get_running_loop().time()
    for instance_id in loops.find_matching_macro_instances(
        state, loop_mode=None, source_key=source_key
    ):
        if instance_id in state.cancel_instance_ids:
            continue
        pause = state.pauses.get(instance_id)
        if pause is not None and pause.paused:
            if pause.expired(now_s):
                pause.expire()
                continue
            root_id = int(state.instance_meta[instance_id]["root_instance_id"])
            if state.instance_meta[root_id].get("macro_name") == playback_options.macro_name:
                resumable_roots.add(root_id)
    resumable_roots.difference_update(state.cancel_instance_ids)
    for root_id in resumable_roots:
        state.instance_meta[root_id]["source_lifecycle_active"] = True
        for instance_id in state.descendant_instance_ids([root_id]):
            if instance_id in state.cancel_instance_ids:
                continue
            pause = state.pauses.get(instance_id)
            if pause is not None:
                pause.resume(now_s)
    return bool(resumable_roots)


async def play_macro(
    manager: MacroManager,
    playback_options: MacroPlaybackOptions,
    *,
    deps: MacroRuntimeDeps,
    macro_event_source: MacroEventSource | None = None,
) -> dict[str, object]:
    """Apply trigger and loop policy, then start one playback task."""

    if not (
        manager.output_state.keyboard_uinput
        or manager.output_state.mouse_uinput
        or manager.output_state.gamepad_uinput
        or manager.output_state.virtual_gamepad_uinputs
    ):
        return {"status": "error", "message": "No output uinput devices available"}

    normalized_loop = loops.normalize_loop_mode(playback_options.loop_mode)
    normalized_loop_stop_behavior = normalize_macro_loop_stop_behavior(
        playback_options.loop_stop_behavior
    )
    count = max(1, int(playback_options.loop_count or 1))
    source_key = (str(playback_options.source_device), str(playback_options.source_button))
    state = manager.macro_state

    if int(playback_options.trigger_value) == 0:
        if source_key[0] or source_key[1]:
            manager.macro_state.mark_source_released(source_key)
        for instance_id in loops.find_matching_macro_instances(
            state, loop_mode=None, source_key=source_key
        ):
            pause = state.pauses.get(instance_id)
            if pause is not None and not pause.paused:
                pause.pause(deps.asyncio_mod.get_running_loop().time())
                outputs.pause_macro_outputs(manager, instance_id, deps=deps)
        hold_instances = loops.find_matching_macro_instances(
            manager.macro_state,
            loop_mode="hold",
            source_key=source_key,
        )
        if hold_instances:
            cancelled = await _stop_loop_instances(manager, hold_instances, deps=deps)
            return {"status": "ok", "cancelled": cancelled > 0}
        return {"status": "ok", "cancelled": False}

    if int(playback_options.trigger_value) != 1:
        return {"status": "ok"}

    if resume_paused_macro(manager, playback_options, deps=deps):
        return {"status": "ok", "resumed": True}

    if normalized_loop == "toggle" and not playback_options.playback_id:
        toggle_instances = loops.find_matching_macro_instances(
            manager.macro_state,
            loop_mode="toggle",
            source_key=source_key,
        )
        if toggle_instances:
            cancelled = await _stop_loop_instances(manager, toggle_instances, deps=deps)
            return {"status": "ok", "cancelled": cancelled > 0}

    if normalized_loop == "hold" and not (source_key[0] or source_key[1]):
        normalized_loop = "none"

    if normalized_loop == "hold" and loops.find_matching_macro_instances(
        manager.macro_state,
        loop_mode="hold",
        source_key=source_key,
    ):
        return {"status": "ok", "already_running": True}

    event_source = macro_event_source or list_macro_event_source(
        playback_options.macro_events,
    )
    if event_source.event_count <= 0:
        if playback_options.playback_id:
            manager._broadcast_runtime_event(
                CommandType.MACRO_PLAYBACK_FINISHED,
                {
                    "playback_id": playback_options.playback_id,
                    "state": "completed",
                },
            )
        return {"status": "ok"}

    _start_macro_instance(
        manager,
        playback_options,
        macro_event_source=event_source,
        normalized_loop=normalized_loop,
        normalized_loop_stop_behavior=normalized_loop_stop_behavior,
        loop_count=count,
        source_key=source_key,
        deps=deps,
    )

    return {"status": "ok"}


def start_child_macro(
    manager: MacroManager,
    playback_options: MacroPlaybackOptions,
    *,
    parent_instance_id: int,
    macro_event_source: MacroEventSource,
    deps: MacroRuntimeDeps,
) -> asyncio.Task[None] | None:
    """Start a structured child invocation and return its join handle."""

    source_available, source_active = manager.macro_state.source_lifecycle(parent_instance_id)
    normalized_loop = loops.normalize_loop_mode(playback_options.loop_mode)
    if normalized_loop == "toggle":
        normalized_loop = "none"
    if normalized_loop == "hold":
        if not source_available:
            normalized_loop = "none"
        elif not source_active and playback_options.loop_stop_behavior != "pause_run":
            return None

    macro_name = str(playback_options.macro_name or "")
    if manager.macro_state.call_chain_contains(parent_instance_id, macro_name):
        raise MacroCallError("recursive macro call blocked")

    parent_meta = manager.macro_state.instance_meta.get(parent_instance_id, {})
    source_key = (
        str(parent_meta.get("source_device", "")),
        str(parent_meta.get("source_button", "")),
    )
    return _start_macro_instance(
        manager,
        playback_options,
        macro_event_source=macro_event_source,
        normalized_loop=normalized_loop,
        normalized_loop_stop_behavior=normalize_macro_loop_stop_behavior(
            playback_options.loop_stop_behavior
        ),
        loop_count=max(1, int(playback_options.loop_count or 1)),
        source_key=source_key,
        deps=deps,
        parent_instance_id=parent_instance_id,
    )


def _start_macro_instance(
    manager: MacroManager,
    playback_options: MacroPlaybackOptions,
    *,
    macro_event_source: MacroEventSource,
    normalized_loop: str,
    normalized_loop_stop_behavior: str,
    loop_count: int,
    source_key: tuple[str, str],
    deps: MacroRuntimeDeps,
    parent_instance_id: int | None = None,
) -> asyncio.Task[None]:
    instance_id = manager.macro_state.allocate_instance(
        loop_mode=normalized_loop,
        source_key=source_key,
        macro_name=str(playback_options.macro_name or ""),
        loop_stop_behavior=normalized_loop_stop_behavior,
        parent_instance_id=parent_instance_id,
    )
    manager.macro_state.instance_meta[instance_id]["playback_id"] = playback_options.playback_id
    source_available, source_active = manager.macro_state.source_lifecycle(instance_id)
    if (
        normalized_loop_stop_behavior == "pause_run"
        and normalized_loop != "toggle"
        and source_available
    ):
        root_id = int(manager.macro_state.instance_meta[instance_id]["root_instance_id"])
        pause = MacroPauseState(
            timeout_s=max(0.0, coerce_float(playback_options.pause_timeout_s, 0.0)),
            on_expire=partial(cleanup.expire_paused_instance, manager, instance_id, deps=deps),
        )
        manager.macro_state.pauses[instance_id] = pause
        if not source_active:
            pause.pause(
                coerce_float(
                    manager.macro_state.instance_meta[root_id].get("source_released_at_s"),
                    float(deps.asyncio_mod.get_running_loop().time()),
                )
            )
    task = deps.asyncio_mod.create_task(
        scheduler.play_macro_task(
            manager,
            instance_id=instance_id,
            macro_events=playback_options.macro_events,
            macro_event_source=macro_event_source,
            macro_name=playback_options.macro_name,
            replay_mouse_movement=playback_options.replay_mouse_movement,
            replay_mouse_clicks=playback_options.replay_mouse_clicks,
            speed=max(0.01, playback_options.speed),
            loop_mode=normalized_loop,
            loop_count=loop_count,
            move_to_start=playback_options.move_to_start,
            start_x=int(playback_options.start_x),
            start_y=int(playback_options.start_y),
            block_mouse_movement=playback_options.block_mouse_movement,
            deps=deps,
        )
    )
    manager.macro_state.tasks[instance_id] = task
    if playback_options.playback_id:

        def finished(done: asyncio.Task[None]) -> None:
            result: dict[str, object] = {"playback_id": playback_options.playback_id}
            if done.cancelled():
                result["state"] = "cancelled"
            elif (error := done.exception()) is not None:
                result.update(state="failed", message=str(error))
            else:
                result["state"] = "completed"
            manager._broadcast_runtime_event(CommandType.MACRO_PLAYBACK_FINISHED, result)

        task.add_done_callback(finished)
    return task


async def _stop_loop_instances(
    manager: MacroManager,
    instance_ids: list[int],
    *,
    deps: MacroRuntimeDeps,
) -> int:
    plan = loops.plan_loop_stop(
        manager.macro_state,
        [
            instance_id
            for instance_id in instance_ids
            if instance_id not in manager.macro_state.pauses
        ],
    )
    loops.mark_loop_instances_stopping(manager.macro_state, plan.finish_instance_ids)
    return await cleanup.cancel_macro_instances(
        manager,
        list(plan.cancel_instance_ids),
        deps=deps,
    )
