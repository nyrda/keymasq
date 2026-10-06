from __future__ import annotations

from keymasq.common.model.actions import MappingAction
from keymasq.keymasqd.output_helpers import resolve_gamepad_axis_code, resolve_output_code
from keymasq.keymasqd.runtime.action.state import (
    ActionExecutionHandle,
    OutputTracker,
    ResolveCodeFn,
    mark_action_started,
    register_action_task,
)
from keymasq.keymasqd.runtime.grabbed_device.outputs import (
    bucket_for_uinput,
    target_axis_release_value,
    write_abs_axis,
    write_key,
)
from keymasq.keymasqd.runtime.grabbed_device.repeat import (
    emit_move_action,
    rapidfire_abs_axis,
    rapidfire_key,
    rapidfire_move,
    rapidfire_relative,
    start_rapidfire_task,
    stop_rapidfire_async,
    tap_abs_axis,
    tap_key,
    tap_move,
    tap_relative,
)
from keymasq.keymasqd.runtime.grabbed_device.types import (
    ActionExecutionDeps,
    ActionRuntime,
    InputEventLike,
)
from keymasq.keymasqd.runtime.mouse_actions import (
    resolve_mouse_output_target,
    write_relative_pulse,
)


async def execute_gamepad_axis_action(
    device_runtime: ActionRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    deps: ActionExecutionDeps,
    shared_abs_output_tracker: OutputTracker | None = None,
    execution_handle: ActionExecutionHandle | None = None,
) -> None:
    if not action.target:
        mark_action_started(execution_handle)
        return
    target = device_runtime.resolve_gamepad_output(
        action.output_id,
        f"{event_name} -> {action.target}",
    )
    if target is None:
        mark_action_started(execution_handle)
        return
    axis_code = resolve_gamepad_axis_code(action.target)
    if axis_code is None:
        mark_action_started(execution_handle)
        return
    target_bucket = str(getattr(target, "bucket", "gamepad"))
    active_value = int(action.axis_value)
    axis_ranges = getattr(target, "axis_ranges", {})
    if axis_code in axis_ranges:
        minimum, maximum = axis_ranges[axis_code]
        active_value = max(minimum, min(maximum, active_value))
    await execute_abs_axis_output(
        device_runtime,
        action,
        event,
        event_name,
        deps=deps,
        axis_code=axis_code,
        active_value=active_value,
        release_value=target_axis_release_value(target, axis_code),
        target_uinput=getattr(target, "uinput", None),
        target_bucket=target_bucket,
        rapidfire_kind="axis",
        tap_label=f"tap axis action {event_name}",
        shared_abs_output_tracker=shared_abs_output_tracker,
        execution_handle=execution_handle,
    )


async def execute_abs_axis_output(
    device_runtime: ActionRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    deps: ActionExecutionDeps,
    axis_code: int,
    active_value: int,
    release_value: int,
    target_uinput: object | None,
    target_bucket: str,
    rapidfire_kind: str,
    tap_label: str,
    shared_abs_output_tracker: OutputTracker | None = None,
    execution_handle: ActionExecutionHandle | None = None,
) -> None:
    if action.rapidfire_enabled:
        if int(event.value) == 1:
            start_rapidfire_task(
                device_runtime,
                event_name,
                rapidfire_kind,
                lambda: deps.asyncio_mod.create_task(
                    rapidfire_abs_axis(
                        device_runtime,
                        axis_code,
                        active_value,
                        release_value,
                        action.rapidfire_hold_ms,
                        action.rapidfire_wait_ms,
                        event_name,
                        target_uinput,
                        asyncio_mod=deps.asyncio_mod,
                        evdev_mod=deps.evdev_mod,
                        uinput_writer=deps.uinput_writer,
                        bucket=target_bucket,
                        started=execution_handle.started if execution_handle else None,
                        output_tracker=shared_abs_output_tracker,
                    )
                ),
                code=None,
                uinput=target_uinput,
                axis_code=axis_code,
                bucket=target_bucket,
                axis_release_value=release_value,
                output_tracker=shared_abs_output_tracker,
            )
        elif int(event.value) == 0:
            await stop_rapidfire_async(device_runtime, event_name)
            mark_action_started(execution_handle)
        return

    if action.tap_enabled:
        if int(event.value) == 1 and not device_runtime.state.tap_active.get(event_name, False):
            device_runtime.state.tap_active[event_name] = True
            task = deps.fire_and_observe_fn(
                tap_abs_axis(
                    device_runtime,
                    axis_code,
                    active_value,
                    release_value,
                    action.tap_hold_ms,
                    event_name,
                    target_uinput,
                    asyncio_mod=deps.asyncio_mod,
                    evdev_mod=deps.evdev_mod,
                    uinput_writer=deps.uinput_writer,
                    bucket=target_bucket,
                    started=execution_handle.started if execution_handle else None,
                ),
                tap_label,
            )
            register_action_task(execution_handle, task)
        elif int(event.value) == 0:
            mark_action_started(execution_handle)
        return

    output_value = active_value if int(event.value) else release_value
    should_emit = True
    if shared_abs_output_tracker is not None:
        should_emit = shared_abs_output_tracker(target_bucket, int(axis_code), int(output_value))
    if should_emit:
        write_abs_axis(
            device_runtime,
            target_uinput,
            axis_code,
            output_value,
            evdev_mod=deps.evdev_mod,
            uinput_writer=deps.uinput_writer,
            bucket=target_bucket,
            release_value=release_value,
        )
    mark_action_started(execution_handle)


async def execute_key_action(
    device_runtime: ActionRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    deps: ActionExecutionDeps,
    uinput_dev: object | None,
    trigger_kind: str,
    shared_output_tracker: OutputTracker | None = None,
    explicit_bucket: str | None = None,
    execution_handle: ActionExecutionHandle | None = None,
    resolve_code_fn: ResolveCodeFn = resolve_output_code,
) -> None:
    if not action.target:
        mark_action_started(execution_handle)
        return
    code = resolve_code_fn(action.target)
    if code is None:
        mark_action_started(execution_handle)
        return
    if action.rapidfire_enabled:
        if int(event.value) == 1:
            tracker_bucket = explicit_bucket or bucket_for_uinput(device_runtime, uinput_dev)
            output_tracker = shared_output_tracker if tracker_bucket is not None else None
            start_rapidfire_task(
                device_runtime,
                event_name,
                trigger_kind,
                lambda: deps.asyncio_mod.create_task(
                    rapidfire_key(
                        device_runtime,
                        code,
                        action.rapidfire_hold_ms,
                        action.rapidfire_wait_ms,
                        event_name,
                        uinput_dev,
                        asyncio_mod=deps.asyncio_mod,
                        bucket=tracker_bucket,
                        started=execution_handle.started if execution_handle else None,
                        output_tracker=output_tracker,
                    )
                ),
                code=code,
                uinput=uinput_dev,
                axis_code=None,
                bucket=tracker_bucket,
                output_tracker=output_tracker,
            )
        elif int(event.value) == 0:
            await stop_rapidfire_async(device_runtime, event_name)
            mark_action_started(execution_handle)
        return

    if action.tap_enabled:
        if int(event.value) == 1 and not device_runtime.state.tap_active.get(event_name, False):
            device_runtime.state.tap_active[event_name] = True
            task = deps.fire_and_observe_fn(
                tap_key(
                    device_runtime,
                    code,
                    action.tap_hold_ms,
                    event_name,
                    uinput_dev,
                    asyncio_mod=deps.asyncio_mod,
                    bucket=explicit_bucket,
                    started=execution_handle.started if execution_handle else None,
                ),
                f"tap action {event_name}",
            )
            register_action_task(execution_handle, task)
        elif int(event.value) == 0:
            mark_action_started(execution_handle)
        return

    should_emit = True
    if shared_output_tracker is not None:
        bucket = explicit_bucket or bucket_for_uinput(device_runtime, uinput_dev)
        if bucket is not None:
            should_emit = shared_output_tracker(bucket, int(code), int(event.value))
    if should_emit:
        write_key(
            device_runtime,
            uinput_dev,
            code,
            int(event.value),
            evdev_mod=deps.evdev_mod,
            uinput_writer=deps.uinput_writer,
            bucket=explicit_bucket,
        )
    mark_action_started(execution_handle)


async def execute_mouse_action(
    device_runtime: ActionRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    deps: ActionExecutionDeps,
    shared_output_tracker: OutputTracker | None = None,
    execution_handle: ActionExecutionHandle | None = None,
    resolve_code_fn: ResolveCodeFn = resolve_output_code,
) -> None:
    target = resolve_mouse_output_target(action.target)
    if target is None:
        mark_action_started(execution_handle)
        return
    if target.is_relative:
        await execute_relative_mouse_action(
            device_runtime,
            action,
            event,
            event_name,
            code=target.code,
            relative_value=target.relative_value,
            deps=deps,
            execution_handle=execution_handle,
        )
        return
    await execute_key_action(
        device_runtime,
        action,
        event,
        event_name,
        deps=deps,
        uinput_dev=device_runtime.mouse_uinput,
        trigger_kind="key",
        shared_output_tracker=shared_output_tracker,
        execution_handle=execution_handle,
        resolve_code_fn=resolve_code_fn,
    )


async def execute_relative_mouse_action(
    device_runtime: ActionRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    code: int,
    relative_value: int,
    deps: ActionExecutionDeps,
    execution_handle: ActionExecutionHandle | None = None,
) -> None:
    if action.rapidfire_enabled:
        if int(event.value) == 1:
            start_rapidfire_task(
                device_runtime,
                event_name,
                "relative",
                lambda: deps.asyncio_mod.create_task(
                    rapidfire_relative(
                        device_runtime,
                        code,
                        relative_value,
                        action.rapidfire_hold_ms,
                        action.rapidfire_wait_ms,
                        event_name,
                        device_runtime.mouse_uinput,
                        asyncio_mod=deps.asyncio_mod,
                        started=execution_handle.started if execution_handle else None,
                    )
                ),
                code=code,
                uinput=device_runtime.mouse_uinput,
                axis_code=None,
            )
        elif int(event.value) == 0:
            await stop_rapidfire_async(device_runtime, event_name)
            mark_action_started(execution_handle)
        return

    if action.tap_enabled:
        if int(event.value) == 1 and not device_runtime.state.tap_active.get(event_name, False):
            device_runtime.state.tap_active[event_name] = True
            task = deps.fire_and_observe_fn(
                tap_relative(
                    device_runtime,
                    code,
                    relative_value,
                    action.tap_hold_ms,
                    event_name,
                    device_runtime.mouse_uinput,
                    asyncio_mod=deps.asyncio_mod,
                    started=execution_handle.started if execution_handle else None,
                ),
                f"tap action {event_name}",
            )
            register_action_task(execution_handle, task)
        elif int(event.value) == 0:
            mark_action_started(execution_handle)
        return

    if int(event.value) == 1:
        write_relative_pulse(
            device_runtime.mouse_uinput,
            code,
            relative_value,
            ev_rel_code=deps.evdev_mod.ecodes.EV_REL,
            uinput_writer=deps.uinput_writer,
        )
    mark_action_started(execution_handle)


async def execute_move_action(
    device_runtime: ActionRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    deps: ActionExecutionDeps,
    execution_handle: ActionExecutionHandle | None = None,
) -> None:
    if action.rapidfire_enabled:
        if int(event.value) == 1:
            start_rapidfire_task(
                device_runtime,
                event_name,
                "move",
                lambda: deps.asyncio_mod.create_task(
                    rapidfire_move(
                        device_runtime,
                        action,
                        event_name,
                        action.rapidfire_hold_ms,
                        action.rapidfire_wait_ms,
                        asyncio_mod=deps.asyncio_mod,
                        started=execution_handle.started if execution_handle else None,
                    )
                ),
                code=None,
                uinput=None,
                axis_code=None,
            )
        elif int(event.value) == 0:
            await stop_rapidfire_async(device_runtime, event_name)
            mark_action_started(execution_handle)
    elif action.tap_enabled:
        if int(event.value) == 1 and not device_runtime.state.tap_active.get(event_name, False):
            device_runtime.state.tap_active[event_name] = True
            task = deps.fire_and_observe_fn(
                tap_move(
                    device_runtime,
                    action,
                    event_name,
                    action.tap_hold_ms,
                    asyncio_mod=deps.asyncio_mod,
                    started=execution_handle.started if execution_handle else None,
                ),
                f"tap move {event_name}",
            )
            register_action_task(execution_handle, task)
        elif int(event.value) == 0:
            mark_action_started(execution_handle)
    elif int(event.value) == 1:
        await emit_move_action(device_runtime, action)
        mark_action_started(execution_handle)
    else:
        mark_action_started(execution_handle)
