import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, TypedDict

from keymasq.common.ipc import CommandType
from keymasq.common.models import (
    ActionType,
    MappingAction,
    normalize_macro_loop_stop_behavior,
    normalize_mpris_command,
    profile_deactivation_policy_to_dict,
)
from keymasq.common.types import JsonObject, SyntheticInputEvent
from keymasq.keymasqd.output_helpers import (
    resolve_gamepad_axis_code,
    resolve_output_code,
)
from keymasq.keymasqd.runtime.adapters import AsyncioEvent
from keymasq.keymasqd.runtime.grabbed_device.outputs import (
    bucket_for_uinput,
    passthrough,
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
    CursorPositionSetter,
    EmergencyResetter,
    GrabbedDeviceState,
    InputEventLike,
    MacroPlayer,
    NaturalMouseMover,
)
from keymasq.keymasqd.runtime.mouse_actions import (
    resolve_mouse_output_target,
    write_relative_pulse,
)
from keymasq.keymasqd.runtime.repeat import (
    RepeatHistoryEntry,
    RepeatRuntimeState,
    refresh_repeated_exec_source,
    remember_action,
    select_repeated_entry,
)

type BroadcastCallback = Callable[[CommandType, JsonObject], Awaitable[None]]
type FireAndObserve = Callable[[Awaitable[object], str], asyncio.Task[object]]
type OutputTracker = Callable[[str, int, int], bool]
type ResolveCodeFn = Callable[[str], int | None]
type CancelMacroPlayback = Callable[[], Awaitable[JsonObject]]

_SyntheticInputEvent = SyntheticInputEvent
log = logging.getLogger("keymasqd.runtime.action_runner")


class SuperkeyExecutor(Protocol):
    async def __call__(
        self,
        device_runtime: ActionRuntime,
        action: MappingAction,
        event: InputEventLike,
        event_name: str,
        *,
        deps: ActionExecutionDeps,
        shared_output_tracker: OutputTracker | None = None,
        shared_abs_output_tracker: OutputTracker | None = None,
        execution_handle: "ActionExecutionHandle | None" = None,
        cancel_macro_playback: CancelMacroPlayback | None = None,
        resolve_code_fn: ResolveCodeFn = resolve_output_code,
    ) -> None: ...


class RepeatSuperkeyExecutor(Protocol):
    async def __call__(
        self,
        device_runtime: ActionRuntime,
        repeated_entry: RepeatHistoryEntry,
        event_name: str,
        *,
        deps: ActionExecutionDeps,
        execution_handle: "ActionExecutionHandle | None" = None,
        cancel_macro_playback: CancelMacroPlayback | None = None,
        resolve_code_fn: ResolveCodeFn = resolve_output_code,
    ) -> None: ...


class MacroPlaybackRequest(TypedDict):
    macro_events: list[JsonObject]
    macro_name: str
    replay_mouse_movement: bool
    replay_mouse_clicks: bool
    speed: float
    loop_mode: str
    loop_count: int
    loop_stop_behavior: str
    move_to_start: bool
    start_x: int
    start_y: int
    block_mouse_movement: bool
    source_device: str
    source_button: str
    trigger_value: int


@dataclass
class ActionOutputTarget:
    output_id: str
    uinput: object | None
    bucket: str


@dataclass
class ActionRuntimeContext:
    path: str
    hardware_id: str
    state: GrabbedDeviceState = field(default_factory=GrabbedDeviceState)
    uinput: object | None = None
    keyboard_uinput: object | None = None
    mouse_uinput: object | None = None
    gamepad_uinput: object | None = None
    broadcast_callback: BroadcastCallback | None = None
    cursor_position_setter: CursorPositionSetter | None = None
    natural_mouse_mover: NaturalMouseMover | None = None
    macro_player: MacroPlayer | None = None
    emergency_resetter: EmergencyResetter | None = None
    repeat_state: RepeatRuntimeState | None = None
    suppress_rel_getter: Callable[[], bool] | None = None
    gamepad_output_resolver: Callable[[str | None, str], object | None] | None = None
    running: bool = True

    @property
    def _running(self) -> bool:
        return self.running

    def stop(self) -> None:
        self.running = False

    def resolve_gamepad_output(self, output_id: str | None, context: str) -> object | None:
        if self.gamepad_output_resolver is not None:
            return self.gamepad_output_resolver(output_id, context)
        return ActionOutputTarget(
            output_id=output_id or "virtual-gamepad-1",
            uinput=self.gamepad_uinput,
            bucket="gamepad",
        )


@dataclass
class ActionExecutionHandle:
    started: AsyncioEvent | None = None
    tasks: list[asyncio.Task[object]] = field(default_factory=list)


def mark_action_started(handle: ActionExecutionHandle | None) -> None:
    if handle is not None and handle.started is not None:
        handle.started.set()


def register_action_task(
    handle: ActionExecutionHandle | None,
    task: asyncio.Task[object],
) -> None:
    if handle is not None:
        handle.tasks.append(task)


async def drain_action_tasks(handle: ActionExecutionHandle | None) -> None:
    if handle is None or not handle.tasks:
        return
    tasks = list(dict.fromkeys(handle.tasks))
    handle.tasks.clear()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def cancel_action_tasks(handle: ActionExecutionHandle | None) -> None:
    if handle is None or not handle.tasks:
        return
    tasks = list(dict.fromkeys(handle.tasks))
    handle.tasks.clear()
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def build_action_trigger_payload(
    action: MappingAction,
    *,
    source_device: str,
    source_button: str,
    trigger_id: str | None = None,
) -> JsonObject | None:
    base_payload: JsonObject = {
        "source_device": source_device,
        "source_button": source_button,
    }
    if trigger_id:
        base_payload["trigger_id"] = trigger_id
    if action.source_profile_name:
        base_payload["source_profile_name"] = action.source_profile_name

    if action.action_type == ActionType.EXEC:
        if action.exec_ref is None:
            return None
        return {
            "action_type": "exec",
            "exec_ref": action.exec_ref,
            **base_payload,
        }

    if action.action_type == ActionType.COMPOSITOR_DISPATCH:
        return {
            "action_type": "compositor_dispatch",
            "compositor": action.compositor_id or "",
            "dispatcher": action.compositor_dispatcher or "",
            "args": action.compositor_args or "",
            **base_payload,
        }

    if action.action_type == ActionType.MPRIS:
        return {
            "action_type": "mpris",
            "command": normalize_mpris_command(action.mpris_command),
            **base_payload,
        }

    if action.action_type in {
        ActionType.START_MACRO_RECORDING,
        ActionType.STOP_MACRO_RECORDING,
        ActionType.PLAY_MACRO_SLOT,
        ActionType.CANCEL_MACRO_PLAYBACK,
        ActionType.EMERGENCY_RESET,
    }:
        payload = {
            "action_type": action.action_type.value,
            **base_payload,
        }
        if action.action_type in {
            ActionType.START_MACRO_RECORDING,
            ActionType.STOP_MACRO_RECORDING,
            ActionType.PLAY_MACRO_SLOT,
        }:
            payload["recording_slot"] = int(action.macro_recording_slot)
        return payload

    if action.action_type in {
        ActionType.PROFILE_ENABLE,
        ActionType.PROFILE_DISABLE,
        ActionType.PROFILE_TOGGLE,
    }:
        payload = {
            "action_type": action.action_type.value,
            "profile_name": action.profile_name or action.target or "",
            **base_payload,
        }
        deactivation = profile_deactivation_policy_to_dict(action.profile_deactivation)
        if deactivation is not None and action.action_type != ActionType.PROFILE_DISABLE:
            payload["deactivation"] = deactivation
        return payload

    return None


def source_trigger_id(source_device: str, source_button: str) -> str:
    return f"{source_device}:{source_button}"


def build_macro_playback_request(
    action: MappingAction,
    *,
    source_device: str,
    source_button: str,
    trigger_value: int,
    include_macro_events: bool = True,
) -> MacroPlaybackRequest | None:
    if not (action.macro_events or action.macro_name):
        return None

    return {
        "macro_events": (action.macro_events or []) if include_macro_events else [],
        "macro_name": action.macro_name or "",
        "replay_mouse_movement": action.macro_replay_mouse_movement,
        "replay_mouse_clicks": action.macro_replay_mouse_clicks,
        "speed": action.macro_speed,
        "loop_mode": action.macro_loop_mode,
        "loop_count": action.macro_loop_count,
        "loop_stop_behavior": normalize_macro_loop_stop_behavior(action.macro_loop_stop_behavior),
        "move_to_start": action.macro_move_to_start,
        "start_x": action.macro_start_x,
        "start_y": action.macro_start_y,
        "block_mouse_movement": action.macro_block_mouse_movement,
        "source_device": source_device,
        "source_button": source_button,
        "trigger_value": trigger_value,
    }


def is_hold_macro_action(action: MappingAction) -> bool:
    return str(action.macro_loop_mode or "none").lower() == "hold"


def dispatch_action_trigger(
    broadcast_callback: BroadcastCallback | None,
    data: JsonObject | None,
    *,
    fire_and_observe_fn: FireAndObserve,
    label: str,
) -> bool:
    if broadcast_callback is None or data is None:
        return False
    fire_and_observe_fn(
        broadcast_callback(CommandType.ACTION_TRIGGER, data),
        label,
    )
    return True


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
        await _execute_key_action(
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
        await _execute_mouse_action(
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
        await _execute_gamepad_axis_action(
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
            await _execute_key_action(
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
        await _execute_move_action(
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
        _SyntheticInputEvent(int(event.type), int(event.code), 1),
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
        _SyntheticInputEvent(int(event.type), int(event.code), 0),
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


async def _execute_gamepad_axis_action(
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
        execution_handle=execution_handle,
    )


async def _execute_abs_axis_output(
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
            await stop_rapidfire_async(
                device_runtime,
                event_name,
            )
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
        )
    mark_action_started(execution_handle)


async def _execute_key_action(
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
            await stop_rapidfire_async(
                device_runtime,
                event_name,
            )
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


async def _execute_mouse_action(
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
        await _execute_relative_mouse_action(
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
    await _execute_key_action(
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


async def _execute_relative_mouse_action(
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
            await stop_rapidfire_async(
                device_runtime,
                event_name,
            )
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


async def _execute_move_action(
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
            await stop_rapidfire_async(
                device_runtime,
                event_name,
            )
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
