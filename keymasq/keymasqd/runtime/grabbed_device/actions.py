import asyncio
import logging
from collections.abc import Callable
from typing import cast

from keymasq.common.ipc import CommandType
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType, SuperkeyMode
from keymasq.common.types import SyntheticInputEvent
from keymasq.keymasqd.output_helpers import resolve_output_code
from keymasq.keymasqd.runtime import action_runner
from keymasq.keymasqd.runtime.action.sequences import (
    ActionSequences,
    SequenceSource,
    runs_natural_move,
)
from keymasq.keymasqd.runtime.action.state import (
    ActionExecutionHandle,
    CancelMacroPlayback,
    ResolveCodeFn,
    mark_action_started,
    register_action_task,
)
from keymasq.keymasqd.runtime.action.triggers import (
    source_trigger_id,
)
from keymasq.keymasqd.runtime.adapters import WritableUInput
from keymasq.keymasqd.runtime.grabbed_device.outputs import (
    track_superkey_abs_output,
    track_superkey_output,
)
from keymasq.keymasqd.runtime.grabbed_device.types import (
    ActionExecutionDeps,
    ActionRuntime,
    GrabbedDeviceRuntime,
    InputEventLike,
)
from keymasq.keymasqd.runtime.repeat import (
    SUPERKEY_SLOT_OVERLOAD,
    RepeatHistoryEntry,
    execute_repeated_superkey_path,
    remember_superkey_path,
    superkey_slot_runs_natural_move,
)
from keymasq.keymasqd.superkey_state import SuperkeyConfig, SuperkeyMachine

log = logging.getLogger("keymasqd.runtime.grabbed_device_actions")


def _build_superkey_machine(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction,
    event_name: str,
    *,
    deps: ActionExecutionDeps,
    execution_handle: ActionExecutionHandle | None = None,
    cancel_macro_playback: CancelMacroPlayback | None = None,
    sequences: ActionSequences | None = None,
    sequence_owner: SequenceSource | None = None,
) -> SuperkeyMachine:
    async def superkey_broadcast(data: dict[str, object]) -> None:
        if device_runtime.broadcast_callback:
            task = deps.fire_and_observe_fn(
                device_runtime.broadcast_callback(
                    CommandType.ACTION_TRIGGER,
                    data,
                ),
                f"superkey action {event_name}",
            )
            register_action_task(execution_handle, task)

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

    def repeat_path_recorder(slot: str) -> None:
        remember_superkey_path(
            getattr(device_runtime, "repeat_state", None),
            action,
            slot,
            source_device=device_runtime.hardware_id,
            source_button=event_name,
        )

    machine = SuperkeyMachine(
        config=cast(SuperkeyConfig, action.superkey_config),
        event_name=event_name,
        keyboard_uinput=cast(WritableUInput, device_runtime.keyboard_uinput),
        mouse_uinput=cast(WritableUInput, device_runtime.mouse_uinput),
        gamepad_uinput=cast(WritableUInput, device_runtime.gamepad_uinput),
        source_device=device_runtime.hardware_id,
        broadcast_callback=superkey_broadcast,
        cursor_position_setter=device_runtime.cursor_position_setter,
        natural_mouse_mover=device_runtime.natural_mouse_mover,
        key_event_tracker=superkey_key_event_tracker,
        axis_event_tracker=superkey_abs_event_tracker,
        gamepad_output_resolver=device_runtime.resolve_gamepad_output,
        macro_player=device_runtime.macro_player,
        emergency_resetter=device_runtime.emergency_resetter,
        cancel_macro_playback=cancel_macro_playback,
        action_deps=deps,
        repeat_path_recorder=repeat_path_recorder,
        keyboard_block_getter=device_runtime.keyboard_block_getter,
        sequences=sequences,
        sequence_owner=sequence_owner,
    )
    machine.source_action = action
    return machine


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
    record_repeat: bool = True,
    detach_cursor_move: bool = False,
) -> None:
    await action_runner.execute_action(
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
        repeat_superkey_executor=_execute_repeated_superkey_path,
        resolve_code_fn=resolve_code_fn,
        record_repeat=record_repeat,
        detach_cursor_move=detach_cursor_move,
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
    record_repeat: bool = True,
    detach_cursor_move: bool = False,
) -> None:
    await execute_action(
        device_runtime,
        action,
        SyntheticInputEvent(int(event.type), int(event.code), 1),
        event_name,
        deps=deps,
        shared_output_tracker=shared_output_tracker,
        shared_abs_output_tracker=shared_abs_output_tracker,
        record_repeat=record_repeat,
        detach_cursor_move=detach_cursor_move,
    )
    await execute_action(
        device_runtime,
        action,
        SyntheticInputEvent(int(event.type), int(event.code), 0),
        event_name,
        deps=deps,
        shared_output_tracker=shared_output_tracker,
        shared_abs_output_tracker=shared_abs_output_tracker,
        record_repeat=False,
    )


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
        mark_action_started(execution_handle)
        return

    if action.superkey_config.mode == SuperkeyMode.OVERLOAD:
        if not _queue_overload_superkey(
            device_runtime,
            action,
            event,
            event_name,
            deps=deps,
        ):
            await _execute_overload_superkey(
                device_runtime,
                action,
                event,
                event_name,
                deps=deps,
            )
        mark_action_started(execution_handle)
        return

    machine = device_runtime.state.superkey_machines.get(event_name)
    if int(event.value) == 1 and not machine:
        machine = _build_superkey_machine(
            device_runtime,
            action,
            event_name,
            deps=deps,
            execution_handle=execution_handle,
            cancel_macro_playback=cancel_macro_playback,
            sequences=device_runtime.state.action_sequences,
            sequence_owner=_sequence_source(event, event_name, action),
        )
        device_runtime.state.superkey_machines[event_name] = machine

    if int(event.value) == 1 and machine is not None:
        await machine.on_down()
    elif int(event.value) == 0 and machine is not None:
        await machine.on_up()
    mark_action_started(execution_handle)


def _queue_overload_superkey(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    deps: ActionExecutionDeps,
) -> bool:
    config = cast(SuperkeyConfig, action.superkey_config)
    sequences = device_runtime.state.action_sequences
    value = int(event.value)
    if value == 1:
        detach = runs_natural_move(config.overload_actions, config.overload_down_actions)
    elif value == 0:
        detach = runs_natural_move(config.overload_up_actions)
    else:
        return sequences.pending(event_name)
    if not detach and not sequences.pending(event_name):
        return False
    queued_event = SyntheticInputEvent(int(event.type), int(event.code), value)
    return sequences.run(
        event_name,
        lambda: _execute_overload_superkey(
            device_runtime,
            action,
            queued_event,
            event_name,
            deps=deps,
        ),
        detach=detach,
        fire_and_observe_fn=deps.fire_and_observe_fn,
        label=f"overload sequence {event_name}",
        owner=_sequence_source(event, event_name, action),
    )


def _sequence_source(
    event: InputEventLike,
    event_name: str,
    action: MappingAction,
) -> SequenceSource:
    return SequenceSource(int(event.type), int(event.code), int(event.value), event_name, action)


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
    if int(event.value) == 1:
        remember_superkey_path(
            getattr(device_runtime, "repeat_state", None),
            action,
            SUPERKEY_SLOT_OVERLOAD,
            source_device=device_runtime.hardware_id,
            source_button=event_name,
        )

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

    value = int(event.value)
    pressed: list[tuple[str, MappingAction]] = (
        [
            (f"{event_name}#overload#{index}", child_action)
            for index, child_action in enumerate(config.overload_actions)
            if child_action.action_type != ActionType.SUPERKEY
        ]
        if value == 0
        else []
    )
    try:
        if value == 0:
            await _pulse_overload_children(
                device_runtime,
                config.name,
                config.overload_up_actions,
                event,
                f"{event_name}#overload_up",
                deps=deps,
                shared_output_tracker=overload_output_tracker,
                shared_abs_output_tracker=overload_abs_output_tracker,
            )

        for index, child_action in enumerate(config.overload_actions):
            if child_action.action_type == ActionType.SUPERKEY:
                _warn_nested_superkey(config.name, index)
                continue
            child_event_name = f"{event_name}#overload#{index}"
            if value == 0:
                pressed.pop(0)
                await _release_overload_child(
                    device_runtime,
                    child_action,
                    event,
                    child_event_name,
                    deps=deps,
                    shared_output_tracker=overload_output_tracker,
                    shared_abs_output_tracker=overload_abs_output_tracker,
                )
                continue
            if value == 1:
                pressed.append((child_event_name, child_action))
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
                record_repeat=False,
            )

        if value == 1:
            await _pulse_overload_children(
                device_runtime,
                config.name,
                config.overload_down_actions,
                event,
                f"{event_name}#overload_down",
                deps=deps,
                shared_output_tracker=overload_output_tracker,
                shared_abs_output_tracker=overload_abs_output_tracker,
            )
    except asyncio.CancelledError:
        release_event = SyntheticInputEvent(int(event.type), int(event.code), 0)
        for child_event_name, child_action in reversed(pressed):
            await _release_overload_child(
                device_runtime,
                child_action,
                release_event,
                child_event_name,
                deps=deps,
                shared_output_tracker=overload_output_tracker,
                shared_abs_output_tracker=overload_abs_output_tracker,
            )
        raise


def _warn_nested_superkey(superkey_name: str, index: int) -> None:
    log.warning(
        "Skipping unexpected nested superkey in overload fanout for '%s' at child %d",
        superkey_name,
        index,
    )


async def _pulse_overload_children(
    device_runtime: GrabbedDeviceRuntime,
    superkey_name: str,
    children: list[MappingAction],
    event: InputEventLike,
    event_name_prefix: str,
    *,
    deps: ActionExecutionDeps,
    shared_output_tracker: Callable[[str, int, int], bool],
    shared_abs_output_tracker: Callable[[str, int, int], bool],
) -> None:
    for index, child_action in enumerate(children):
        if child_action.action_type == ActionType.SUPERKEY:
            _warn_nested_superkey(superkey_name, index)
            continue
        await execute_action_pulse(
            device_runtime,
            child_action,
            event,
            f"{event_name_prefix}#{index}",
            deps=deps,
            shared_output_tracker=shared_output_tracker,
            shared_abs_output_tracker=shared_abs_output_tracker,
            record_repeat=False,
        )


async def _release_overload_child(
    device_runtime: GrabbedDeviceRuntime,
    child_action: MappingAction,
    event: InputEventLike,
    child_event_name: str,
    *,
    deps: ActionExecutionDeps,
    shared_output_tracker: Callable[[str, int, int], bool],
    shared_abs_output_tracker: Callable[[str, int, int], bool],
) -> None:
    await execute_action(
        device_runtime,
        child_action,
        event,
        child_event_name,
        deps=deps,
        shared_output_tracker=shared_output_tracker,
        shared_abs_output_tracker=shared_abs_output_tracker,
        record_repeat=False,
    )
    device_runtime.state.held_profile_trigger_events.discard(child_event_name)
    _observe_overload_profile_trigger(
        device_runtime,
        child_event_name,
        active=False,
    )


async def _execute_repeated_superkey_path(
    device_runtime: ActionRuntime,
    repeated_entry: RepeatHistoryEntry,
    event_name: str,
    *,
    deps: ActionExecutionDeps,
    execution_handle: ActionExecutionHandle | None = None,
    cancel_macro_playback: CancelMacroPlayback | None = None,
    resolve_code_fn: ResolveCodeFn = resolve_output_code,
    sequence_source: SequenceSource | None = None,
) -> None:
    del execution_handle, cancel_macro_playback, resolve_code_fn
    device_runtime = cast(GrabbedDeviceRuntime, device_runtime)

    async def execute_overload_once(action: MappingAction, repeat_event_name: str) -> None:
        await _execute_overload_slot_once(
            device_runtime,
            action,
            repeat_event_name,
            deps=deps,
        )

    async def execute_pattern_slot_once(
        action: MappingAction,
        slot: str,
        repeat_event_name: str,
    ) -> None:
        machine = _build_superkey_machine(
            device_runtime,
            action,
            repeat_event_name,
            deps=deps,
        )
        await machine.execute_repeat_slot(slot)
        await machine.stop()

    async def execute_path() -> None:
        await execute_repeated_superkey_path(
            repeated_entry,
            event_name,
            execute_overload_once=execute_overload_once,
            execute_pattern_slot_once=execute_pattern_slot_once,
        )

    if not device_runtime.state.action_sequences.run(
        f"{event_name}#repeat",
        execute_path,
        detach=superkey_slot_runs_natural_move(repeated_entry),
        fire_and_observe_fn=deps.fire_and_observe_fn,
        label=f"repeated superkey sequence {event_name}",
        owner=sequence_source,
    ):
        await execute_path()


async def _execute_overload_slot_once(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction,
    event_name: str,
    *,
    deps: ActionExecutionDeps,
) -> None:
    config = action.superkey_config
    if config is None:
        return
    for value in (1, 0):
        await _execute_overload_superkey(
            device_runtime,
            action,
            SyntheticInputEvent(0, 0, value),
            event_name,
            deps=deps,
        )
