import logging
from collections.abc import Callable
from typing import cast

from keymasq.common.ipc import CommandType
from keymasq.common.models import ActionType, MappingAction, SuperkeyMode
from keymasq.keymasqd.output_helpers import (
    resolve_gamepad_axis_code,
    resolve_output_code,
)
from keymasq.keymasqd.runtime.action_runner import (
    build_action_trigger_payload,
    build_macro_playback_request,
    dispatch_action_trigger,
    source_trigger_id,
)
from keymasq.keymasqd.runtime.grabbed_device_outputs import (
    bucket_for_uinput,
    passthrough,
    track_superkey_abs_output,
    track_superkey_output,
    write_abs_axis,
    write_key,
)
from keymasq.keymasqd.runtime.grabbed_device_repeat import (
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
from keymasq.keymasqd.runtime.grabbed_device_types import (
    ActionExecutionDeps,
    GrabbedDeviceRuntime,
    InputEventLike,
    WritableUInput,
)
from keymasq.keymasqd.runtime.mouse_actions import (
    resolve_mouse_output_target,
    write_relative_pulse,
)
from keymasq.keymasqd.superkey_state import SuperkeyConfig as RuntimeSuperkeyConfig
from keymasq.keymasqd.superkey_state import SuperkeyMachine

log = logging.getLogger("keymasqd.runtime.grabbed_device_actions")


async def execute_action(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    deps: ActionExecutionDeps,
    shared_output_tracker: Callable[[str, int, int], bool] | None = None,
    shared_abs_output_tracker: Callable[[str, int, int], bool] | None = None,
) -> None:
    evdev_mod = deps.evdev_mod
    uinput_writer = deps.uinput_writer
    fire_and_observe_fn = deps.fire_and_observe_fn
    if action.action_type == ActionType.PASSTHROUGH:
        passthrough(
            device_runtime,
            event,
            evdev_mod=evdev_mod,
            uinput_writer=uinput_writer,
            sync=False,
        )

    elif action.action_type == ActionType.SUPPRESS:
        pass

    elif action.action_type == ActionType.KEYBOARD:
        await _execute_key_action(
            device_runtime,
            action,
            event,
            event_name,
            deps=deps,
            uinput_dev=device_runtime.keyboard_uinput,
            target_kind="key",
            trigger_kind="key",
            shared_output_tracker=shared_output_tracker,
        )

    elif action.action_type == ActionType.MOUSE:
        await _execute_mouse_action(
            device_runtime,
            action,
            event,
            event_name,
            deps=deps,
            shared_output_tracker=shared_output_tracker,
        )

    elif action.action_type == ActionType.GAMEPAD_AXIS:
        await _execute_gamepad_axis_action(
            device_runtime,
            action,
            event,
            event_name,
            deps=deps,
            shared_output_tracker=shared_output_tracker,
            shared_abs_output_tracker=shared_abs_output_tracker,
        )

    elif action.action_type == ActionType.GAMEPAD:
        if action.target:
            target = device_runtime.resolve_gamepad_output(
                action.output_id,
                f"{event_name} -> {action.target}",
            )
            if target is None:
                return
            target_uinput = getattr(target, "uinput", None)
            target_bucket = str(getattr(target, "bucket", "gamepad"))
            await _execute_key_action(
                device_runtime,
                action,
                event,
                event_name,
                deps=deps,
                uinput_dev=target_uinput,
                explicit_bucket=target_bucket,
                target_kind="key",
                trigger_kind="key",
                shared_output_tracker=shared_output_tracker,
            )

    elif action.action_type == ActionType.EXEC:
        if event.value == 1:
            dispatch_action_trigger(
                device_runtime.broadcast_callback,
                build_action_trigger_payload(
                    action,
                    source_device=device_runtime.hardware_id,
                    source_button=event_name,
                    trigger_id=source_trigger_id(device_runtime.hardware_id, event_name),
                ),
                fire_and_observe_fn=fire_and_observe_fn,
                label=f"exec action {event_name}",
            )

    elif action.action_type == ActionType.COMPOSITOR_DISPATCH:
        if event.value == 1:
            dispatch_action_trigger(
                device_runtime.broadcast_callback,
                build_action_trigger_payload(
                    action,
                    source_device=device_runtime.hardware_id,
                    source_button=event_name,
                    trigger_id=source_trigger_id(device_runtime.hardware_id, event_name),
                ),
                fire_and_observe_fn=fire_and_observe_fn,
                label=f"compositor action {event_name}",
            )

    elif action.action_type == ActionType.START_MACRO_RECORDING:
        if event.value == 1:
            dispatch_action_trigger(
                device_runtime.broadcast_callback,
                build_action_trigger_payload(
                    action,
                    source_device=device_runtime.hardware_id,
                    source_button=event_name,
                    trigger_id=source_trigger_id(device_runtime.hardware_id, event_name),
                ),
                fire_and_observe_fn=fire_and_observe_fn,
                label=f"start recording action {event_name}",
            )

    elif action.action_type == ActionType.STOP_MACRO_RECORDING:
        if event.value == 1:
            dispatch_action_trigger(
                device_runtime.broadcast_callback,
                build_action_trigger_payload(
                    action,
                    source_device=device_runtime.hardware_id,
                    source_button=event_name,
                    trigger_id=source_trigger_id(device_runtime.hardware_id, event_name),
                ),
                fire_and_observe_fn=fire_and_observe_fn,
                label=f"stop recording action {event_name}",
            )

    elif action.action_type == ActionType.CANCEL_MACRO_PLAYBACK:
        if event.value == 1:
            dispatch_action_trigger(
                device_runtime.broadcast_callback,
                build_action_trigger_payload(
                    action,
                    source_device=device_runtime.hardware_id,
                    source_button=event_name,
                    trigger_id=source_trigger_id(device_runtime.hardware_id, event_name),
                ),
                fire_and_observe_fn=fire_and_observe_fn,
                label=f"cancel macro action {event_name}",
            )

    elif action.action_type == ActionType.EMERGENCY_RESET:
        if event.value == 1 and device_runtime.emergency_resetter is not None:
            fire_and_observe_fn(
                device_runtime.emergency_resetter(),
                f"emergency reset action {event_name}",
            )

    elif action.action_type in (
        ActionType.PROFILE_ENABLE,
        ActionType.PROFILE_DISABLE,
        ActionType.PROFILE_TOGGLE,
    ):
        if event.value == 1:
            dispatch_action_trigger(
                device_runtime.broadcast_callback,
                build_action_trigger_payload(
                    action,
                    source_device=device_runtime.hardware_id,
                    source_button=event_name,
                    trigger_id=source_trigger_id(device_runtime.hardware_id, event_name),
                ),
                fire_and_observe_fn=fire_and_observe_fn,
                label=f"profile action {event_name}",
            )

    elif action.action_type == ActionType.MACRO:
        macro_request = build_macro_playback_request(
            action,
            source_device=device_runtime.hardware_id,
            source_button=event_name,
            trigger_value=int(event.value),
        )
        if event.value in (0, 1) and macro_request is not None and device_runtime.macro_player:
            fire_and_observe_fn(
                device_runtime.macro_player(**macro_request),
                f"macro action {event_name}",
            )

    elif action.action_type in (ActionType.MOUSE_MOVE_REL, ActionType.MOUSE_MOVE_ABS):
        await _execute_move_action(
            device_runtime,
            action,
            event,
            event_name,
            deps=deps,
        )

    elif action.action_type == ActionType.SUPERKEY:
        if action.superkey_config:
            if action.superkey_config.mode == SuperkeyMode.OVERLOAD:
                await _execute_overload_superkey(
                    device_runtime,
                    action,
                    event,
                    event_name,
                    deps=deps,
                )
                return

            machine = device_runtime.state.superkey_machines.get(event_name)
            if event.value == 1 and not machine:

                async def superkey_broadcast(data: dict[str, object]) -> None:
                    if device_runtime.broadcast_callback:
                        fire_and_observe_fn(
                            device_runtime.broadcast_callback(
                                CommandType.ACTION_TRIGGER,
                                data,
                            ),
                            f"superkey action {event_name}",
                        )

                def superkey_key_event_tracker(
                    action_type: str,
                    code: int,
                    value: int,
                ) -> bool:
                    return track_superkey_output(
                        device_runtime,
                        action_type,
                        code,
                        value,
                    )

                def superkey_abs_event_tracker(
                    bucket: str,
                    axis_code: int,
                    value: int,
                ) -> bool:
                    return track_superkey_abs_output(
                        device_runtime,
                        bucket,
                        axis_code,
                        value,
                    )

                machine = SuperkeyMachine(
                    config=cast(RuntimeSuperkeyConfig, action.superkey_config),
                    event_name=event_name,
                    keyboard_uinput=cast(WritableUInput, device_runtime.keyboard_uinput),
                    mouse_uinput=cast(WritableUInput, device_runtime.mouse_uinput),
                    gamepad_uinput=cast(WritableUInput, device_runtime.gamepad_uinput),
                    source_device=device_runtime.hardware_id,
                    broadcast_callback=superkey_broadcast,
                    cursor_position_setter=device_runtime.cursor_position_setter,
                    key_event_tracker=superkey_key_event_tracker,
                    axis_event_tracker=superkey_abs_event_tracker,
                    gamepad_output_resolver=device_runtime.resolve_gamepad_output,
                )
                device_runtime.state.superkey_machines[event_name] = machine

            if event.value == 1 and machine is not None:
                await machine.on_down()
            elif event.value == 0 and machine is not None:
                await machine.on_up()


async def execute_action_pulse(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    deps: ActionExecutionDeps,
    shared_output_tracker: Callable[[str, int, int], bool] | None = None,
    shared_abs_output_tracker: Callable[[str, int, int], bool] | None = None,
    explicit_bucket: str | None = None,
) -> None:
    del explicit_bucket
    await execute_action(
        device_runtime,
        action,
        _SyntheticInputEvent(int(event.type), int(event.code), 1),
        event_name,
        deps=deps,
        shared_output_tracker=shared_output_tracker,
        shared_abs_output_tracker=shared_abs_output_tracker,
    )
    await execute_action(
        device_runtime,
        action,
        _SyntheticInputEvent(int(event.type), int(event.code), 0),
        event_name,
        deps=deps,
        shared_output_tracker=shared_output_tracker,
        shared_abs_output_tracker=shared_abs_output_tracker,
    )


class _SyntheticInputEvent:
    def __init__(self, event_type: int, code: int, value: int) -> None:
        self.type = int(event_type)
        self.code = int(code)
        self.value = int(value)


def _observe_overload_profile_trigger(
    device_runtime: GrabbedDeviceRuntime,
    child_event_name: str,
    *,
    active: bool,
) -> None:
    observer = (
        device_runtime.profile_activation_trigger_start_observer
        if active
        else device_runtime.profile_activation_trigger_end_observer
    )
    if observer is not None:
        observer(source_trigger_id(device_runtime.hardware_id, child_event_name))


async def _execute_overload_superkey(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    deps: ActionExecutionDeps,
) -> None:
    config = action.superkey_config
    if config is None:
        return

    def overload_output_tracker(action_type: str, code: int, value: int) -> bool:
        return track_superkey_output(
            device_runtime,
            action_type,
            code,
            value,
        )

    def overload_abs_output_tracker(bucket: str, axis_code: int, value: int) -> bool:
        return track_superkey_abs_output(
            device_runtime,
            bucket,
            axis_code,
            value,
        )

    if int(event.value) == 0:
        for index, child_action in enumerate(config.overload_up_actions):
            if child_action.action_type == ActionType.SUPERKEY:
                log.warning(
                    "Skipping unexpected nested superkey in overload fanout for '%s' at child %d",
                    config.name,
                    index,
                )
                continue
            child_event_name = f"{event_name}#overload_up#{index}"
            await execute_action_pulse(
                device_runtime,
                child_action,
                event,
                child_event_name,
                deps=deps,
                shared_output_tracker=overload_output_tracker,
                shared_abs_output_tracker=overload_abs_output_tracker,
            )

    for index, child_action in enumerate(config.overload_actions):
        if child_action.action_type == ActionType.SUPERKEY:
            log.warning(
                "Skipping unexpected nested superkey in overload fanout for '%s' at child %d",
                config.name,
                index,
            )
            continue
        child_event_name = f"{event_name}#overload#{index}"
        if int(event.value) == 1:
            _observe_overload_profile_trigger(
                device_runtime,
                child_event_name,
                active=True,
            )
        await execute_action(
            device_runtime,
            child_action,
            event,
            child_event_name,
            deps=deps,
            shared_output_tracker=overload_output_tracker,
            shared_abs_output_tracker=overload_abs_output_tracker,
        )
        if int(event.value) == 0:
            _observe_overload_profile_trigger(
                device_runtime,
                child_event_name,
                active=False,
            )

    if int(event.value) == 1:
        for index, child_action in enumerate(config.overload_down_actions):
            if child_action.action_type == ActionType.SUPERKEY:
                log.warning(
                    "Skipping unexpected nested superkey in overload fanout for '%s' at child %d",
                    config.name,
                    index,
                )
                continue
            child_event_name = f"{event_name}#overload_down#{index}"
            await execute_action_pulse(
                device_runtime,
                child_action,
                event,
                child_event_name,
                deps=deps,
                shared_output_tracker=overload_output_tracker,
                shared_abs_output_tracker=overload_abs_output_tracker,
            )


async def _execute_gamepad_axis_action(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    deps: ActionExecutionDeps,
    shared_output_tracker: Callable[[str, int, int], bool] | None = None,
    shared_abs_output_tracker: Callable[[str, int, int], bool] | None = None,
) -> None:
    del shared_output_tracker
    if not action.target:
        return
    target = device_runtime.resolve_gamepad_output(
        action.output_id,
        f"{event_name} -> {action.target}",
    )
    if target is None:
        return
    axis_code = resolve_gamepad_axis_code(action.target)
    if axis_code is None:
        return
    await _execute_abs_axis_output(
        device_runtime,
        action,
        event,
        event_name,
        deps=deps,
        axis_code=axis_code,
        active_value=int(action.axis_value),
        release_value=0,
        target_uinput=getattr(target, "uinput", None),
        target_bucket=str(getattr(target, "bucket", "gamepad")),
        rapidfire_kind="axis",
        tap_label=f"tap axis action {event_name}",
        shared_abs_output_tracker=shared_abs_output_tracker,
    )


async def _execute_abs_axis_output(
    device_runtime: GrabbedDeviceRuntime,
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
    shared_abs_output_tracker: Callable[[str, int, int], bool] | None = None,
) -> None:
    if action.rapidfire_enabled:
        if event.value == 1:
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
                    )
                ),
                code=None,
                uinput=target_uinput,
                axis_code=axis_code,
                bucket=target_bucket,
                axis_release_value=release_value,
            )
        elif event.value == 0:
            await stop_rapidfire_async(
                device_runtime,
                event_name,
                asyncio_mod=deps.asyncio_mod,
            )
        return

    if action.tap_enabled:
        if event.value == 1 and not device_runtime.state.tap_active.get(event_name, False):
            device_runtime.state.tap_active[event_name] = True
            deps.fire_and_observe_fn(
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
                ),
                tap_label,
            )
        return

    output_value = active_value if int(event.value) else release_value
    should_emit = True
    if shared_abs_output_tracker is not None:
        should_emit = shared_abs_output_tracker(target_bucket, int(axis_code), int(output_value))
    if not should_emit:
        return
    write_abs_axis(
        device_runtime,
        target_uinput,
        axis_code,
        output_value,
        evdev_mod=deps.evdev_mod,
        uinput_writer=deps.uinput_writer,
        bucket=target_bucket,
    )


async def _execute_key_action(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    deps: ActionExecutionDeps,
    uinput_dev: object | None,
    target_kind: str,
    trigger_kind: str,
    shared_output_tracker: Callable[[str, int, int], bool] | None = None,
    explicit_bucket: str | None = None,
) -> None:
    del target_kind
    if not action.target:
        return
    code = resolve_output_code(action.target)
    if not code:
        return
    if action.rapidfire_enabled:
        if event.value == 1:
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
                        bucket=explicit_bucket,
                    )
                ),
                code=code,
                uinput=uinput_dev,
                axis_code=None,
                bucket=explicit_bucket,
            )
        elif event.value == 0:
            await stop_rapidfire_async(
                device_runtime,
                event_name,
                asyncio_mod=deps.asyncio_mod,
            )
    elif action.tap_enabled:
        if event.value == 1 and not device_runtime.state.tap_active.get(event_name, False):
            device_runtime.state.tap_active[event_name] = True
            deps.fire_and_observe_fn(
                tap_key(
                    device_runtime,
                    code,
                    action.tap_hold_ms,
                    event_name,
                    uinput_dev,
                    asyncio_mod=deps.asyncio_mod,
                    bucket=explicit_bucket,
                ),
                f"tap action {event_name}",
            )
    else:
        should_emit = True
        if shared_output_tracker is not None:
            bucket = explicit_bucket or bucket_for_uinput(device_runtime, uinput_dev)
            if bucket is not None:
                should_emit = shared_output_tracker(bucket, int(code), int(event.value))
        if not should_emit:
            return
        write_key(
            device_runtime,
            uinput_dev,
            code,
            int(event.value),
            evdev_mod=deps.evdev_mod,
            uinput_writer=deps.uinput_writer,
            bucket=explicit_bucket,
        )


async def _execute_mouse_action(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    deps: ActionExecutionDeps,
    shared_output_tracker: Callable[[str, int, int], bool] | None = None,
) -> None:
    target = resolve_mouse_output_target(action.target)
    if target is None:
        return
    if target.is_relative:
        await _execute_relative_mouse_action(
            device_runtime,
            action,
            event,
            event_name,
            code=target.code,
            relative_value=target.relative_value,
            deps=deps,
            shared_output_tracker=shared_output_tracker,
        )
        return
    await _execute_key_action(
        device_runtime,
        action,
        event,
        event_name,
        deps=deps,
        uinput_dev=device_runtime.mouse_uinput,
        target_kind="key",
        trigger_kind="key",
        shared_output_tracker=shared_output_tracker,
    )


async def _execute_relative_mouse_action(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    code: int,
    relative_value: int,
    deps: ActionExecutionDeps,
    shared_output_tracker: Callable[[str, int, int], bool] | None = None,
) -> None:
    del shared_output_tracker

    if action.rapidfire_enabled:
        if event.value == 1:
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
                    )
                ),
                code=code,
                uinput=device_runtime.mouse_uinput,
                axis_code=None,
            )
        elif event.value == 0:
            await stop_rapidfire_async(
                device_runtime,
                event_name,
                asyncio_mod=deps.asyncio_mod,
            )
        return

    if action.tap_enabled:
        if event.value == 1 and not device_runtime.state.tap_active.get(event_name, False):
            device_runtime.state.tap_active[event_name] = True
            deps.fire_and_observe_fn(
                tap_relative(
                    device_runtime,
                    code,
                    relative_value,
                    action.tap_hold_ms,
                    event_name,
                    device_runtime.mouse_uinput,
                    asyncio_mod=deps.asyncio_mod,
                ),
                f"tap action {event_name}",
            )
        return

    if int(event.value) != 1:
        return

    write_relative_pulse(
        device_runtime.mouse_uinput,
        code,
        relative_value,
        ev_rel_code=deps.evdev_mod.ecodes.EV_REL,
        uinput_writer=deps.uinput_writer,
    )


async def _execute_move_action(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    deps: ActionExecutionDeps,
) -> None:
    if action.rapidfire_enabled:
        if event.value == 1:
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
                    )
                ),
                code=None,
                uinput=None,
                axis_code=None,
            )
        elif event.value == 0:
            await stop_rapidfire_async(
                device_runtime,
                event_name,
                asyncio_mod=deps.asyncio_mod,
            )
    elif action.tap_enabled:
        if event.value == 1 and not device_runtime.state.tap_active.get(event_name, False):
            device_runtime.state.tap_active[event_name] = True
            deps.fire_and_observe_fn(
                tap_move(
                    device_runtime,
                    action,
                    event_name,
                    action.tap_hold_ms,
                    asyncio_mod=deps.asyncio_mod,
                ),
                f"tap move {event_name}",
            )
    elif event.value == 1:
        await emit_move_action(device_runtime, action)
