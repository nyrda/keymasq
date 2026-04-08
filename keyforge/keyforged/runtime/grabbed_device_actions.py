import contextlib
import logging
from collections.abc import Callable
from typing import cast

import evdev

from keyforge.common.ipc import CommandType
from keyforge.common.models import ActionType, MappingAction, SuperkeyMode
from keyforge.keyforged.output_helpers import get_trigger_axis, resolve_output_code
from keyforge.keyforged.runtime.action_runner import (
    build_action_trigger_payload,
    build_macro_playback_request,
    dispatch_action_trigger,
)
from keyforge.keyforged.runtime.grabbed_device_outputs import (
    emit_configured_mouse_move,
    passthrough,
    track_superkey_output,
    write_key,
)
from keyforge.keyforged.runtime.grabbed_device_repeat import (
    rapidfire_key,
    rapidfire_move,
    rapidfire_trigger,
    start_rapidfire_task,
    stop_rapidfire_async,
    tap_key,
    tap_move,
    tap_trigger,
)
from keyforge.keyforged.runtime.grabbed_device_types import (
    AsyncioModule,
    EvdevModule,
    FireAndObserve,
    GrabbedDeviceRuntime,
    InputEventLike,
    UInputWriter,
    WritableUInput,
)
from keyforge.keyforged.superkey_state import SuperkeyConfig as RuntimeSuperkeyConfig
from keyforge.keyforged.superkey_state import SuperkeyMachine

log = logging.getLogger("keyforged.runtime.grabbed_device_actions")


async def execute_action(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    asyncio_mod: AsyncioModule,
    command_type: type[CommandType],
    fire_and_observe_fn: FireAndObserve,
    action_type_enum: type[ActionType],
    superkey_machine_cls: type[SuperkeyMachine],
    evdev_mod: EvdevModule,
    uinput_writer: UInputWriter,
    shared_output_tracker: Callable[[str, int, int], bool] | None = None,
) -> None:
    if action.action_type == action_type_enum.PASSTHROUGH:
        passthrough(device_runtime, event, evdev_mod=evdev_mod, uinput_writer=uinput_writer)

    elif action.action_type == action_type_enum.SUPPRESS:
        pass

    elif action.action_type == action_type_enum.KEYBOARD:
        await _execute_key_action(
            device_runtime,
            action,
            event,
            event_name,
            asyncio_mod=asyncio_mod,
            fire_and_observe_fn=fire_and_observe_fn,
            uinput_dev=device_runtime.keyboard_uinput,
            target_kind="key",
            trigger_kind="key",
            shared_output_tracker=shared_output_tracker,
        )

    elif action.action_type == action_type_enum.MOUSE:
        await _execute_key_action(
            device_runtime,
            action,
            event,
            event_name,
            asyncio_mod=asyncio_mod,
            fire_and_observe_fn=fire_and_observe_fn,
            uinput_dev=device_runtime.mouse_uinput,
            target_kind="key",
            trigger_kind="key",
            shared_output_tracker=shared_output_tracker,
        )

    elif action.action_type == action_type_enum.GAMEPAD:
        if action.target:
            is_trigger, axis_code = get_trigger_axis(action.target)
            if is_trigger:
                if axis_code is None:
                    return
                if action.rapidfire_enabled:
                    if event.value == 1:
                        start_rapidfire_task(
                            device_runtime,
                            event_name,
                            "trigger",
                            lambda: asyncio_mod.create_task(
                                rapidfire_trigger(
                                    device_runtime,
                                    axis_code,
                                    action.rapidfire_hold_ms,
                                    action.rapidfire_wait_ms,
                                    event_name,
                                    asyncio_mod=asyncio_mod,
                                    evdev_mod=evdev_mod,
                                    uinput_writer=uinput_writer,
                                )
                            ),
                            axis_code=axis_code,
                            code=None,
                            uinput=None,
                        )
                    elif event.value == 0:
                        await stop_rapidfire_async(
                            device_runtime,
                            event_name,
                            asyncio_mod=asyncio_mod,
                            contextlib_mod=contextlib,
                        )
                elif action.tap_enabled:
                    if event.value == 1 and not device_runtime.state.tap_active.get(
                        event_name, False
                    ):
                        device_runtime.state.tap_active[event_name] = True
                        fire_and_observe_fn(
                            tap_trigger(
                                device_runtime,
                                axis_code,
                                action.tap_hold_ms,
                                event_name,
                                asyncio_mod=asyncio_mod,
                                evdev_mod=evdev_mod,
                                uinput_writer=uinput_writer,
                            ),
                            f"tap action {event_name}",
                        )
                else:
                    gamepad_uinput = uinput_writer(device_runtime.gamepad_uinput)
                    if gamepad_uinput is None:
                        return
                    should_emit = True
                    if shared_output_tracker is not None:
                        should_emit = shared_output_tracker(
                            action_type_enum.GAMEPAD.value,
                            axis_code,
                            int(event.value),
                        )
                    if not should_emit:
                        return
                    gamepad_uinput.write(
                        evdev_mod.ecodes.EV_ABS,
                        axis_code,
                        255 if event.value else 0,
                    )
                    gamepad_uinput.syn()
            else:
                await _execute_key_action(
                    device_runtime,
                    action,
                    event,
                    event_name,
                    asyncio_mod=asyncio_mod,
                    fire_and_observe_fn=fire_and_observe_fn,
                    uinput_dev=device_runtime.gamepad_uinput,
                    target_kind="key",
                    trigger_kind="key",
                    shared_output_tracker=shared_output_tracker,
                )

    elif action.action_type == action_type_enum.EXEC:
        if event.value == 1:
            dispatch_action_trigger(
                device_runtime.broadcast_callback,
                build_action_trigger_payload(
                    action,
                    source_device=device_runtime.hardware_id,
                    source_button=event_name,
                ),
                fire_and_observe_fn=fire_and_observe_fn,
                command_type=command_type,
                label=f"exec action {event_name}",
            )

    elif action.action_type == action_type_enum.COMPOSITOR_DISPATCH:
        if event.value == 1:
            dispatch_action_trigger(
                device_runtime.broadcast_callback,
                build_action_trigger_payload(
                    action,
                    source_device=device_runtime.hardware_id,
                    source_button=event_name,
                ),
                fire_and_observe_fn=fire_and_observe_fn,
                command_type=command_type,
                label=f"compositor action {event_name}",
            )

    elif action.action_type == action_type_enum.START_MACRO_RECORDING:
        if event.value == 1:
            dispatch_action_trigger(
                device_runtime.broadcast_callback,
                build_action_trigger_payload(
                    action,
                    source_device=device_runtime.hardware_id,
                    source_button=event_name,
                ),
                fire_and_observe_fn=fire_and_observe_fn,
                command_type=command_type,
                label=f"start recording action {event_name}",
            )

    elif action.action_type == action_type_enum.STOP_MACRO_RECORDING:
        if event.value == 1:
            dispatch_action_trigger(
                device_runtime.broadcast_callback,
                build_action_trigger_payload(
                    action,
                    source_device=device_runtime.hardware_id,
                    source_button=event_name,
                ),
                fire_and_observe_fn=fire_and_observe_fn,
                command_type=command_type,
                label=f"stop recording action {event_name}",
            )

    elif action.action_type == action_type_enum.CANCEL_MACRO_PLAYBACK:
        if event.value == 1:
            dispatch_action_trigger(
                device_runtime.broadcast_callback,
                build_action_trigger_payload(
                    action,
                    source_device=device_runtime.hardware_id,
                    source_button=event_name,
                ),
                fire_and_observe_fn=fire_and_observe_fn,
                command_type=command_type,
                label=f"cancel macro action {event_name}",
            )

    elif action.action_type in (
        action_type_enum.PROFILE_ENABLE,
        action_type_enum.PROFILE_DISABLE,
        action_type_enum.PROFILE_TOGGLE,
    ):
        if event.value == 1:
            dispatch_action_trigger(
                device_runtime.broadcast_callback,
                build_action_trigger_payload(
                    action,
                    source_device=device_runtime.hardware_id,
                    source_button=event_name,
                ),
                fire_and_observe_fn=fire_and_observe_fn,
                command_type=command_type,
                label=f"profile action {event_name}",
            )

    elif action.action_type == action_type_enum.MACRO:
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

    elif action.action_type in (action_type_enum.MOUSE_MOVE_REL, action_type_enum.MOUSE_MOVE_ABS):
        await _execute_move_action(
            device_runtime,
            action,
            event,
            event_name,
            asyncio_mod=asyncio_mod,
            fire_and_observe_fn=fire_and_observe_fn,
        )

    elif action.action_type == action_type_enum.SUPERKEY:
        if action.superkey_config:
            if action.superkey_config.mode == SuperkeyMode.OVERLOAD:
                await _execute_overload_superkey(
                    device_runtime,
                    action,
                    event,
                    event_name,
                    asyncio_mod=asyncio_mod,
                    command_type=command_type,
                    fire_and_observe_fn=fire_and_observe_fn,
                    action_type_enum=action_type_enum,
                    superkey_machine_cls=superkey_machine_cls,
                    evdev_mod=evdev_mod,
                    uinput_writer=uinput_writer,
                )
                return

            machine = device_runtime.state.superkey_machines.get(event_name)
            if event.value == 1 and not machine:

                async def superkey_broadcast(data: dict[str, object]) -> None:
                    if device_runtime.broadcast_callback:
                        fire_and_observe_fn(
                            device_runtime.broadcast_callback(command_type.ACTION_TRIGGER, data),
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

                machine = superkey_machine_cls(
                    config=cast(RuntimeSuperkeyConfig, action.superkey_config),
                    event_name=event_name,
                    keyboard_uinput=cast(WritableUInput, device_runtime.keyboard_uinput),
                    mouse_uinput=cast(WritableUInput, device_runtime.mouse_uinput),
                    gamepad_uinput=cast(WritableUInput, device_runtime.gamepad_uinput),
                    broadcast_callback=superkey_broadcast,
                    key_event_tracker=superkey_key_event_tracker,
                )
                device_runtime.state.superkey_machines[event_name] = machine

            if event.value == 1 and machine is not None:
                await machine.on_down()
            elif event.value == 0 and machine is not None:
                await machine.on_up()


async def _execute_overload_superkey(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    asyncio_mod: AsyncioModule,
    command_type: type[CommandType],
    fire_and_observe_fn: FireAndObserve,
    action_type_enum: type[ActionType],
    superkey_machine_cls: type[SuperkeyMachine],
    evdev_mod: EvdevModule,
    uinput_writer: UInputWriter,
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

    for index, child_action in enumerate(config.overload_actions):
        if child_action.action_type == action_type_enum.SUPERKEY:
            log.warning(
                "Skipping unexpected nested superkey in overload fanout for '%s' at child %d",
                config.name,
                index,
            )
            continue
        child_event_name = f"{event_name}#overload#{index}"
        await execute_action(
            device_runtime,
            child_action,
            event,
            child_event_name,
            asyncio_mod=asyncio_mod,
            command_type=command_type,
            fire_and_observe_fn=fire_and_observe_fn,
            action_type_enum=action_type_enum,
            superkey_machine_cls=superkey_machine_cls,
            evdev_mod=evdev_mod,
            uinput_writer=uinput_writer,
            shared_output_tracker=overload_output_tracker,
        )


async def _execute_key_action(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    asyncio_mod: AsyncioModule,
    fire_and_observe_fn: FireAndObserve,
    uinput_dev: object | None,
    target_kind: str,
    trigger_kind: str,
    shared_output_tracker: Callable[[str, int, int], bool] | None = None,
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
                lambda: asyncio_mod.create_task(
                    rapidfire_key(
                        device_runtime,
                        code,
                        action.rapidfire_hold_ms,
                        action.rapidfire_wait_ms,
                        event_name,
                        uinput_dev,
                        asyncio_mod=asyncio_mod,
                    )
                ),
                code=code,
                uinput=uinput_dev,
                axis_code=None,
            )
        elif event.value == 0:
            await stop_rapidfire_async(
                device_runtime,
                event_name,
                asyncio_mod=asyncio_mod,
                contextlib_mod=contextlib,
            )
    elif action.tap_enabled:
        if event.value == 1 and not device_runtime.state.tap_active.get(event_name, False):
            device_runtime.state.tap_active[event_name] = True
            fire_and_observe_fn(
                tap_key(
                    device_runtime,
                    code,
                    action.tap_hold_ms,
                    event_name,
                    uinput_dev,
                    asyncio_mod=asyncio_mod,
                ),
                f"tap action {event_name}",
            )
    else:
        should_emit = True
        if shared_output_tracker is not None:
            bucket = "keyboard" if uinput_dev is device_runtime.keyboard_uinput else None
            if uinput_dev is device_runtime.mouse_uinput:
                bucket = "mouse"
            elif uinput_dev is device_runtime.gamepad_uinput:
                bucket = "gamepad"
            if bucket is not None:
                should_emit = shared_output_tracker(bucket, int(code), int(event.value))
        if not should_emit:
            return
        write_key(
            device_runtime,
            uinput_dev,
            code,
            int(event.value),
            evdev_mod=evdev,
            uinput_writer=_uinput_writer,
        )


async def _execute_move_action(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    asyncio_mod: AsyncioModule,
    fire_and_observe_fn: FireAndObserve,
) -> None:
    if action.rapidfire_enabled:
        if event.value == 1:
            start_rapidfire_task(
                device_runtime,
                event_name,
                "move",
                lambda: asyncio_mod.create_task(
                    rapidfire_move(
                        device_runtime,
                        action,
                        event_name,
                        action.rapidfire_hold_ms,
                        action.rapidfire_wait_ms,
                        asyncio_mod=asyncio_mod,
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
                asyncio_mod=asyncio_mod,
                contextlib_mod=contextlib,
            )
    elif action.tap_enabled:
        if event.value == 1 and not device_runtime.state.tap_active.get(event_name, False):
            device_runtime.state.tap_active[event_name] = True
            fire_and_observe_fn(
                tap_move(
                    device_runtime,
                    action,
                    event_name,
                    action.tap_hold_ms,
                    asyncio_mod=asyncio_mod,
                ),
                f"tap move {event_name}",
            )
    elif event.value == 1:
        emit_configured_mouse_move(device_runtime, action)


def _uinput_writer(device: object | None) -> WritableUInput | None:
    return cast(WritableUInput | None, device)
