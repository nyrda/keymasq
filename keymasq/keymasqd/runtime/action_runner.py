import logging

from keymasq.common.ipc import CommandType
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.common.types import SyntheticInputEvent
from keymasq.keymasqd.output_helpers import resolve_output_code
from keymasq.keymasqd.runtime.action.outputs import (
    execute_gamepad_axis_action,
    execute_key_action,
    execute_mouse_action,
    execute_move_action,
)
from keymasq.keymasqd.runtime.action.state import (
    ActionExecutionHandle,
    CancelMacroPlayback,
    OutputTracker,
    RepeatSuperkeyExecutor,
    ResolveCodeFn,
    SuperkeyExecutor,
    mark_action_started,
    register_action_task,
)
from keymasq.keymasqd.runtime.action.triggers import (
    build_action_trigger_payload,
    build_macro_playback_request,
    source_trigger_id,
)
from keymasq.keymasqd.runtime.grabbed_device.outputs import passthrough
from keymasq.keymasqd.runtime.grabbed_device.types import (
    ActionExecutionDeps,
    ActionRuntime,
    InputEventLike,
)
from keymasq.keymasqd.runtime.repeat import (
    refresh_repeated_exec_source,
    remember_action,
    select_repeated_entry,
)

log = logging.getLogger("keymasqd.runtime.action_runner")


async def execute_action(
    device_runtime: ActionRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    deps: ActionExecutionDeps,
    shared_output_tracker: OutputTracker | None = None,
    shared_abs_output_tracker: OutputTracker | None = None,
    execution_handle: ActionExecutionHandle | None = None,
    cancel_macro_playback: CancelMacroPlayback | None = None,
    superkey_executor: SuperkeyExecutor | None = None,
    repeat_superkey_executor: RepeatSuperkeyExecutor | None = None,
    resolve_code_fn: ResolveCodeFn = resolve_output_code,
    record_repeat: bool = True,
) -> None:
    if (
        record_repeat
        and int(event.value) == 1
        and action.action_type not in {ActionType.PASSTHROUGH, ActionType.SUPERKEY}
    ):
        remember_action(
            getattr(device_runtime, "repeat_state", None),
            action,
            source_device=device_runtime.hardware_id,
            source_button=event_name,
        )

    if action.action_type == ActionType.PASSTHROUGH:
        passthrough(
            device_runtime,
            event,
            evdev_mod=deps.evdev_mod,
            uinput_writer=deps.uinput_writer,
            sync=False,
        )
        mark_action_started(execution_handle)
        return

    if action.action_type == ActionType.SUPPRESS:
        mark_action_started(execution_handle)
        return

    if action.action_type == ActionType.REPEAT:
        await _execute_repeat_action(
            device_runtime,
            action,
            event,
            event_name,
            deps=deps,
            shared_output_tracker=shared_output_tracker,
            shared_abs_output_tracker=shared_abs_output_tracker,
            execution_handle=execution_handle,
            cancel_macro_playback=cancel_macro_playback,
            superkey_executor=superkey_executor,
            repeat_superkey_executor=repeat_superkey_executor,
            resolve_code_fn=resolve_code_fn,
            record_repeat=record_repeat,
        )
        return

    if action.action_type == ActionType.KEYBOARD:
        await execute_key_action(
            device_runtime,
            action,
            event,
            event_name,
            deps=deps,
            uinput_dev=device_runtime.keyboard_uinput,
            trigger_kind="key",
            shared_output_tracker=shared_output_tracker,
            execution_handle=execution_handle,
            resolve_code_fn=resolve_code_fn,
        )
        return

    if action.action_type == ActionType.MOUSE:
        await execute_mouse_action(
            device_runtime,
            action,
            event,
            event_name,
            deps=deps,
            shared_output_tracker=shared_output_tracker,
            execution_handle=execution_handle,
            resolve_code_fn=resolve_code_fn,
        )
        return

    if action.action_type == ActionType.GAMEPAD_AXIS:
        await execute_gamepad_axis_action(
            device_runtime,
            action,
            event,
            event_name,
            deps=deps,
            shared_abs_output_tracker=shared_abs_output_tracker,
            execution_handle=execution_handle,
        )
        return

    if action.action_type == ActionType.GAMEPAD:
        if action.target:
            target = device_runtime.resolve_gamepad_output(
                action.output_id,
                f"{event_name} -> {action.target}",
            )
            if target is None:
                mark_action_started(execution_handle)
                return
            await execute_key_action(
                device_runtime,
                action,
                event,
                event_name,
                deps=deps,
                uinput_dev=getattr(target, "uinput", None),
                explicit_bucket=str(getattr(target, "bucket", "gamepad")),
                trigger_kind="key",
                shared_output_tracker=shared_output_tracker,
                execution_handle=execution_handle,
                resolve_code_fn=resolve_code_fn,
            )
        else:
            mark_action_started(execution_handle)
        return

    if action.action_type == ActionType.CANCEL_MACRO_PLAYBACK:
        if int(event.value) == 1:
            if cancel_macro_playback is not None:
                task = deps.fire_and_observe_fn(
                    cancel_macro_playback(),
                    f"cancel macro action {event_name}",
                )
                register_action_task(execution_handle, task)
            else:
                _dispatch_trigger_action(
                    device_runtime,
                    action,
                    event_name,
                    deps=deps,
                    execution_handle=execution_handle,
                    label=f"cancel macro action {event_name}",
                )
        mark_action_started(execution_handle)
        return

    if action.action_type == ActionType.EMERGENCY_RESET:
        if int(event.value) == 1:
            if device_runtime.emergency_resetter is not None:
                task = deps.fire_and_observe_fn(
                    device_runtime.emergency_resetter(),
                    f"emergency reset action {event_name}",
                )
                register_action_task(execution_handle, task)
        mark_action_started(execution_handle)
        return

    if action.action_type in {
        ActionType.EXEC,
        ActionType.COMPOSITOR_DISPATCH,
        ActionType.START_MACRO_RECORDING,
        ActionType.STOP_MACRO_RECORDING,
        ActionType.PLAY_MACRO_SLOT,
        ActionType.PROFILE_ENABLE,
        ActionType.PROFILE_DISABLE,
        ActionType.PROFILE_TOGGLE,
        ActionType.MPRIS,
    }:
        if int(event.value) == 1:
            _dispatch_trigger_action(
                device_runtime,
                action,
                event_name,
                deps=deps,
                execution_handle=execution_handle,
                label=f"{action.action_type.value} action {event_name}",
            )
        mark_action_started(execution_handle)
        return

    if action.action_type == ActionType.MACRO:
        macro_request = build_macro_playback_request(
            action,
            source_device=device_runtime.hardware_id,
            source_button=event_name,
            trigger_value=int(event.value),
        )
        if int(event.value) in (0, 1) and macro_request is not None and device_runtime.macro_player:
            task = deps.fire_and_observe_fn(
                device_runtime.macro_player(**macro_request),
                f"macro action {event_name}",
            )
            register_action_task(execution_handle, task)
        elif (
            int(event.value) in (0, 1)
            and macro_request is not None
            and device_runtime.broadcast_callback is not None
        ):
            task = deps.fire_and_observe_fn(
                device_runtime.broadcast_callback(
                    CommandType.ACTION_TRIGGER,
                    {
                        "action_type": "macro",
                        **macro_request,
                    },
                ),
                f"macro action {event_name}",
            )
            register_action_task(execution_handle, task)
        mark_action_started(execution_handle)
        return

    if action.action_type in (
        ActionType.MOUSE_MOVE_REL,
        ActionType.MOUSE_MOVE_ABS,
        ActionType.MOUSE_MOVE_NATURAL_ABS,
    ):
        await execute_move_action(
            device_runtime,
            action,
            event,
            event_name,
            deps=deps,
            execution_handle=execution_handle,
        )
        return

    if action.action_type == ActionType.SUPERKEY and superkey_executor is not None:
        await superkey_executor(
            device_runtime,
            action,
            event,
            event_name,
            deps=deps,
            shared_output_tracker=shared_output_tracker,
            shared_abs_output_tracker=shared_abs_output_tracker,
            execution_handle=execution_handle,
            cancel_macro_playback=cancel_macro_playback,
            resolve_code_fn=resolve_code_fn,
        )
        return

    mark_action_started(execution_handle)


async def execute_action_pulse(
    device_runtime: ActionRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    deps: ActionExecutionDeps,
    shared_output_tracker: OutputTracker | None = None,
    shared_abs_output_tracker: OutputTracker | None = None,
    execution_handle: ActionExecutionHandle | None = None,
    cancel_macro_playback: CancelMacroPlayback | None = None,
    superkey_executor: SuperkeyExecutor | None = None,
    repeat_superkey_executor: RepeatSuperkeyExecutor | None = None,
    resolve_code_fn: ResolveCodeFn = resolve_output_code,
    record_repeat: bool = True,
) -> None:
    await execute_action(
        device_runtime,
        action,
        SyntheticInputEvent(int(event.type), int(event.code), 1),
        event_name,
        deps=deps,
        shared_output_tracker=shared_output_tracker,
        shared_abs_output_tracker=shared_abs_output_tracker,
        execution_handle=execution_handle,
        cancel_macro_playback=cancel_macro_playback,
        superkey_executor=superkey_executor,
        repeat_superkey_executor=repeat_superkey_executor,
        resolve_code_fn=resolve_code_fn,
        record_repeat=record_repeat,
    )
    await execute_action(
        device_runtime,
        action,
        SyntheticInputEvent(int(event.type), int(event.code), 0),
        event_name,
        deps=deps,
        shared_output_tracker=shared_output_tracker,
        shared_abs_output_tracker=shared_abs_output_tracker,
        execution_handle=execution_handle,
        cancel_macro_playback=cancel_macro_playback,
        superkey_executor=superkey_executor,
        repeat_superkey_executor=repeat_superkey_executor,
        resolve_code_fn=resolve_code_fn,
        record_repeat=record_repeat,
    )


def _dispatch_trigger_action(
    device_runtime: ActionRuntime,
    action: MappingAction,
    event_name: str,
    *,
    deps: ActionExecutionDeps,
    execution_handle: ActionExecutionHandle | None,
    label: str,
) -> bool:
    payload = build_action_trigger_payload(
        action,
        source_device=device_runtime.hardware_id,
        source_button=event_name,
        trigger_id=source_trigger_id(device_runtime.hardware_id, event_name),
    )
    if payload is None:
        log.debug(
            "Cannot dispatch %s action %s: no session payload",
            action.action_type.value,
            event_name,
        )
        return False
    if device_runtime.broadcast_callback is None:
        log.warning(
            "Cannot dispatch %s action %s: no session connection",
            action.action_type.value,
            event_name,
        )
        return False
    log.debug(
        "Dispatching %s action %s to session: %s",
        action.action_type.value,
        event_name,
        payload,
    )
    task = deps.fire_and_observe_fn(
        device_runtime.broadcast_callback(CommandType.ACTION_TRIGGER, payload),
        label,
    )
    register_action_task(execution_handle, task)
    return True


async def _execute_repeat_action(
    device_runtime: ActionRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    deps: ActionExecutionDeps,
    shared_output_tracker: OutputTracker | None = None,
    shared_abs_output_tracker: OutputTracker | None = None,
    execution_handle: ActionExecutionHandle | None = None,
    cancel_macro_playback: CancelMacroPlayback | None = None,
    superkey_executor: SuperkeyExecutor | None = None,
    repeat_superkey_executor: RepeatSuperkeyExecutor | None = None,
    resolve_code_fn: ResolveCodeFn = resolve_output_code,
    record_repeat: bool = True,
) -> None:
    repeat_event_name = f"{event_name}#repeat"
    repeat_state = getattr(device_runtime, "repeat_state", None)
    active_actions = device_runtime.state.repeat_active_actions

    if int(event.value) == 1:
        repeated_entry = select_repeated_entry(repeat_state, action)
        if repeated_entry is None:
            mark_action_started(execution_handle)
            return
        if repeated_entry.superkey_slot is not None:
            if repeat_superkey_executor is not None:
                await repeat_superkey_executor(
                    device_runtime,
                    repeated_entry,
                    event_name,
                    deps=deps,
                    execution_handle=execution_handle,
                    cancel_macro_playback=cancel_macro_playback,
                    resolve_code_fn=resolve_code_fn,
                )
                refresh_repeated_exec_source(repeat_state, repeated_entry)
            mark_action_started(execution_handle)
            return
        repeated_action = repeated_entry.action
        active_actions[repeat_event_name] = repeated_action
        await execute_action(
            device_runtime,
            repeated_action,
            event,
            repeat_event_name,
            deps=deps,
            shared_output_tracker=shared_output_tracker,
            shared_abs_output_tracker=shared_abs_output_tracker,
            execution_handle=execution_handle,
            cancel_macro_playback=cancel_macro_playback,
            superkey_executor=superkey_executor,
            repeat_superkey_executor=repeat_superkey_executor,
            resolve_code_fn=resolve_code_fn,
            record_repeat=record_repeat,
        )
        refresh_repeated_exec_source(repeat_state, repeated_entry)
        return

    if int(event.value) not in {0, 2}:
        mark_action_started(execution_handle)
        return

    repeated_action = (
        active_actions.get(repeat_event_name)
        if int(event.value) == 2
        else active_actions.pop(repeat_event_name, None)
    )
    if repeated_action is None:
        mark_action_started(execution_handle)
        return
    await execute_action(
        device_runtime,
        repeated_action,
        event,
        repeat_event_name,
        deps=deps,
        shared_output_tracker=shared_output_tracker,
        shared_abs_output_tracker=shared_abs_output_tracker,
        execution_handle=execution_handle,
        cancel_macro_playback=cancel_macro_playback,
        superkey_executor=superkey_executor,
        repeat_superkey_executor=repeat_superkey_executor,
        resolve_code_fn=resolve_code_fn,
        record_repeat=record_repeat,
    )
