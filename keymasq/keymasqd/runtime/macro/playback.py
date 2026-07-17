from __future__ import annotations

from dataclasses import replace
from typing import Any

from keymasq.common.model.actions import normalize_macro_loop_stop_behavior
from keymasq.keymasqd.runtime.macro import cleanup, loops, scheduler
from keymasq.keymasqd.runtime.macro.events import list_macro_event_source
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
        _close_event_source(macro_event_source)
        return {"status": "error", "message": "No output uinput devices available"}

    normalized_loop = loops.normalize_loop_mode(playback_options.loop_mode)
    normalized_loop_stop_behavior = normalize_macro_loop_stop_behavior(
        playback_options.loop_stop_behavior
    )
    count = max(1, int(playback_options.loop_count or 1))
    source_key = (str(playback_options.source_device), str(playback_options.source_button))

    if int(playback_options.trigger_value) == 0:
        hold_instances = loops.find_matching_macro_instances(
            manager.macro_state,
            loop_mode="hold",
            source_key=source_key,
        )
        if hold_instances:
            cancelled = await _stop_loop_instances(manager, hold_instances, deps=deps)
            _close_event_source(macro_event_source)
            return {"status": "ok", "cancelled": cancelled > 0}
        _close_event_source(macro_event_source)
        return {"status": "ok", "cancelled": False}

    if int(playback_options.trigger_value) != 1:
        _close_event_source(macro_event_source)
        return {"status": "ok"}

    if normalized_loop == "toggle":
        toggle_instances = loops.find_matching_macro_instances(
            manager.macro_state,
            loop_mode="toggle",
            source_key=source_key,
        )
        if toggle_instances:
            cancelled = await _stop_loop_instances(manager, toggle_instances, deps=deps)
            _close_event_source(macro_event_source)
            return {"status": "ok", "cancelled": cancelled > 0}

    if normalized_loop == "hold" and loops.find_matching_macro_instances(
        manager.macro_state,
        loop_mode="hold",
        source_key=source_key,
    ):
        _close_event_source(macro_event_source)
        return {"status": "ok", "already_running": True}

    event_source = macro_event_source or list_macro_event_source(
        playback_options.macro_events,
        int_value_fn=deps.int_value_fn,
    )
    if event_source.event_count <= 0:
        _close_event_source(event_source)
        return {"status": "ok"}

    source_closed = False
    original_close = event_source.close

    def close_event_source_once() -> None:
        nonlocal source_closed
        if source_closed:
            return
        source_closed = True
        if original_close is None:
            return
        try:
            original_close()
        except Exception:
            deps.log.exception("Failed to close macro playback snapshot")

    event_source = replace(event_source, close=close_event_source_once)

    instance_id = manager.macro_state.allocate_instance(
        loop_mode=normalized_loop,
        source_key=source_key,
        macro_name=str(playback_options.macro_name or ""),
        loop_stop_behavior=normalized_loop_stop_behavior,
    )
    task = deps.asyncio_mod.create_task(
        scheduler.play_macro_task(
            manager,
            instance_id=instance_id,
            macro_events=playback_options.macro_events,
            macro_event_source=event_source,
            macro_name=playback_options.macro_name,
            replay_mouse_movement=playback_options.replay_mouse_movement,
            replay_mouse_clicks=playback_options.replay_mouse_clicks,
            speed=max(0.01, playback_options.speed),
            loop_mode=normalized_loop,
            loop_count=count,
            move_to_start=playback_options.move_to_start,
            start_x=int(playback_options.start_x),
            start_y=int(playback_options.start_y),
            block_mouse_movement=playback_options.block_mouse_movement,
            deps=deps,
        )
    )
    manager.macro_state.tasks[instance_id] = task

    def close_event_source(_task: object) -> None:
        _close_event_source(event_source)

    task.add_done_callback(close_event_source)
    return {"status": "ok"}


def _close_event_source(source: MacroEventSource | None) -> None:
    if source is not None and source.close is not None:
        source.close()


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
