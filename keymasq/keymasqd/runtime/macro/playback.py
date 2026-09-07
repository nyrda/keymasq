from __future__ import annotations

import asyncio
from typing import Any

from keymasq.common.ipc import CommandType
from keymasq.common.model.actions import normalize_macro_loop_stop_behavior
from keymasq.keymasqd.runtime.macro import cleanup, loops, scheduler
from keymasq.keymasqd.runtime.macro.events import list_macro_event_source
from keymasq.keymasqd.runtime.macro.exceptions import MacroCallError
from keymasq.keymasqd.runtime.macro.options import MacroPlaybackOptions
from keymasq.keymasqd.runtime.macro.state import MacroEventSource, MacroRuntimeDeps

type MacroManager = Any


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

    if int(playback_options.trigger_value) == 0:
        if source_key[0] or source_key[1]:
            manager.macro_state.mark_source_released(source_key)
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
        int_value_fn=deps.int_value_fn,
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
        elif not source_active:
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
    plan = loops.plan_loop_stop(manager.macro_state, instance_ids)
    loops.mark_loop_instances_stopping(manager.macro_state, plan.finish_instance_ids)
    return await cleanup.cancel_macro_instances(
        manager,
        list(plan.cancel_instance_ids),
        deps=deps,
    )
