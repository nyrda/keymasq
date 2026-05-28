import logging
from collections.abc import Callable
from typing import cast

from keymasq.common.ipc import CommandType
from keymasq.common.models import ActionType, MappingAction, SuperkeyMode
from keymasq.keymasqd.output_helpers import resolve_output_code
from keymasq.keymasqd.runtime import action_runner as shared_action_runner
from keymasq.keymasqd.runtime.action_runner import (
    ActionExecutionHandle,
    CancelMacroPlayback,
    ResolveCodeFn,
    source_trigger_id,
)
from keymasq.keymasqd.runtime.grabbed_device_outputs import (
    track_superkey_abs_output,
    track_superkey_output,
)
from keymasq.keymasqd.runtime.grabbed_device_types import (
    ActionExecutionDeps,
    ActionRuntime,
    GrabbedDeviceRuntime,
    InputEventLike,
    WritableUInput,
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
    execution_handle: ActionExecutionHandle | None = None,
    cancel_macro_playback: CancelMacroPlayback | None = None,
    resolve_code_fn: ResolveCodeFn = resolve_output_code,
) -> None:
    await shared_action_runner.execute_action(
        device_runtime,
        action,
        event,
        event_name,
        deps=deps,
        shared_output_tracker=shared_output_tracker,
        shared_abs_output_tracker=shared_abs_output_tracker,
        execution_handle=execution_handle,
        cancel_macro_playback=cancel_macro_playback,
        superkey_executor=_execute_superkey_action,
        resolve_code_fn=resolve_code_fn,
    )


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


async def _execute_superkey_action(
    device_runtime: ActionRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    deps: ActionExecutionDeps,
    shared_output_tracker: Callable[[str, int, int], bool] | None = None,
    shared_abs_output_tracker: Callable[[str, int, int], bool] | None = None,
    execution_handle: ActionExecutionHandle | None = None,
    cancel_macro_playback: CancelMacroPlayback | None = None,
    resolve_code_fn: ResolveCodeFn = resolve_output_code,
) -> None:
    device_runtime = cast(GrabbedDeviceRuntime, device_runtime)
    del shared_output_tracker, shared_abs_output_tracker, resolve_code_fn
    if action.superkey_config is None:
        shared_action_runner.mark_action_started(execution_handle)
        return

    if action.superkey_config.mode == SuperkeyMode.OVERLOAD:
        await _execute_overload_superkey(
            device_runtime,
            action,
            event,
            event_name,
            deps=deps,
        )
        shared_action_runner.mark_action_started(execution_handle)
        return

    machine = device_runtime.state.superkey_machines.get(event_name)
    if int(event.value) == 1 and not machine:

        async def superkey_broadcast(data: dict[str, object]) -> None:
            if device_runtime.broadcast_callback:
                task = deps.fire_and_observe_fn(
                    device_runtime.broadcast_callback(
                        CommandType.ACTION_TRIGGER,
                        data,
                    ),
                    f"superkey action {event_name}",
                )
                shared_action_runner.register_action_task(execution_handle, task)

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
            macro_player=device_runtime.macro_player,
            emergency_resetter=device_runtime.emergency_resetter,
            cancel_macro_playback=cancel_macro_playback,
            action_deps=deps,
        )
        device_runtime.state.superkey_machines[event_name] = machine

    if int(event.value) == 1 and machine is not None:
        await machine.on_down()
    elif int(event.value) == 0 and machine is not None:
        await machine.on_up()
    shared_action_runner.mark_action_started(execution_handle)


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
            device_runtime.state.held_profile_trigger_events.add(child_event_name)
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
            device_runtime.state.held_profile_trigger_events.discard(child_event_name)
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
