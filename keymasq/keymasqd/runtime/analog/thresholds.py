"""Digital-threshold transition handling and child-action execution."""

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.analog import (
    AnalogActionThreshold,
    AnalogControlConfig,
)
from keymasq.common.model.core import ActionType
from keymasq.common.types import SyntheticInputEvent
from keymasq.keymasqd.runtime.action.triggers import source_trigger_id
from keymasq.keymasqd.runtime.analog.threshold_state import (
    DigitalThresholdStateMachine,
    threshold_key,
)
from keymasq.keymasqd.runtime.grabbed_device import actions
from keymasq.keymasqd.runtime.grabbed_device.outputs import track_refcounted_output_bucket
from keymasq.keymasqd.runtime.grabbed_device.types import (
    ActionExecutionDeps,
    GrabbedDeviceRuntime,
    InputEventLike,
)


async def evaluate_thresholds(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    config: AnalogControlConfig,
    event: InputEventLike,
    *,
    source_profile_name: str | None = None,
    deps: ActionExecutionDeps,
) -> None:
    active_keys = device_runtime.state.analog_active_thresholds.setdefault(source_id, set())
    axis_values = device_runtime.state.analog_axis_values.setdefault(source_id, {})
    machine = DigitalThresholdStateMachine(source_id, active_keys)
    for transition in machine.evaluate(
        config.thresholds,
        axis_values,
        input_type=config.input_type,
    ):
        if transition.kind == "activate":
            await activate_threshold_actions(
                device_runtime,
                source_id,
                transition.index,
                transition.threshold,
                event,
                source_profile_name=source_profile_name,
                deps=deps,
            )
            continue
        await release_threshold_actions(
            device_runtime,
            source_id,
            transition.index,
            transition.threshold,
            deps=deps,
            event_type=int(event.type),
            event_code=int(event.code),
        )
        device_runtime.state.analog_active_threshold_actions.pop(
            threshold_key(source_id, transition.index),
            None,
        )


async def activate_threshold_actions(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    index: int,
    threshold: AnalogActionThreshold,
    event: InputEventLike,
    *,
    source_profile_name: str | None,
    deps: ActionExecutionDeps,
) -> None:
    synthetic = SyntheticInputEvent(int(event.type), int(event.code), 1)
    key = threshold_key(source_id, index)
    executable_actions: list[tuple[int, MappingAction]] = []
    recorded_profile_action = False
    for action_index, action in enumerate(threshold.actions):
        if action.action_type in {
            ActionType.PASSTHROUGH,
            ActionType.SUPPRESS,
            ActionType.ANALOG_CONTROL,
        }:
            continue
        executable_actions.append((action_index, action))
        if not recorded_profile_action:
            _record_threshold_profile_action(
                device_runtime,
                source_profile_name,
                source_id,
            )
            recorded_profile_action = True
        child_event_name = _child_event_name(source_id, index, action_index)
        _observe_threshold_profile_trigger(
            device_runtime,
            action,
            child_event_name,
            active=True,
        )
        await actions.execute_action(
            device_runtime,
            action,
            synthetic,
            child_event_name,
            deps=deps,
            shared_output_tracker=lambda bucket, code, value: track_threshold_output(
                device_runtime,
                bucket,
                code,
                value,
            ),
            shared_abs_output_tracker=lambda bucket, axis_code, value: track_threshold_abs_output(
                device_runtime,
                bucket,
                axis_code,
                value,
            ),
        )
    device_runtime.state.analog_active_threshold_actions[key] = tuple(executable_actions)


async def release_threshold_actions(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    index: int,
    threshold: AnalogActionThreshold | None,
    *,
    deps: ActionExecutionDeps,
    event_type: int = 0,
    event_code: int = 0,
    active_actions: tuple[tuple[int, MappingAction], ...] | None = None,
) -> None:
    synthetic = SyntheticInputEvent(event_type, event_code, 0)
    action_entries = (
        active_actions
        if active_actions is not None
        else tuple(enumerate(threshold.actions))
        if threshold is not None
        else ()
    )
    for action_index, action in action_entries:
        if action.action_type in {
            ActionType.PASSTHROUGH,
            ActionType.SUPPRESS,
            ActionType.ANALOG_CONTROL,
        }:
            continue
        child_event_name = _child_event_name(source_id, index, action_index)
        await actions.execute_action(
            device_runtime,
            action,
            synthetic,
            child_event_name,
            deps=deps,
            shared_output_tracker=lambda bucket, code, value: track_threshold_output(
                device_runtime,
                bucket,
                code,
                value,
            ),
            shared_abs_output_tracker=lambda bucket, axis_code, value: track_threshold_abs_output(
                device_runtime,
                bucket,
                axis_code,
                value,
            ),
        )
        _observe_threshold_profile_trigger(
            device_runtime,
            action,
            child_event_name,
            active=False,
        )


def _observe_threshold_profile_trigger(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction,
    child_event_name: str,
    *,
    active: bool,
) -> None:
    policy = action.profile_deactivation
    if (
        action.action_type != ActionType.PROFILE_ENABLE
        or policy is None
        or not policy.on_trigger_end
    ):
        return
    observer_name = (
        "profile_activation_trigger_start_observer"
        if active
        else "profile_activation_trigger_end_observer"
    )
    observer = getattr(device_runtime, observer_name, None)
    if observer is not None:
        observer(source_trigger_id(device_runtime.hardware_id, child_event_name))


def _record_threshold_profile_action(
    device_runtime: GrabbedDeviceRuntime,
    source_profile_name: str | None,
    source_id: str | None = None,
) -> None:
    recorder = getattr(device_runtime, "profile_activation_recorder", None)
    if recorder is not None:
        trigger_id = source_trigger_id(device_runtime.hardware_id, source_id) if source_id else None
        recorder(source_profile_name, trigger_id)


def track_threshold_output(
    device_runtime: GrabbedDeviceRuntime,
    bucket: str,
    code: int,
    value: int,
) -> bool:
    return track_refcounted_output_bucket(
        device_runtime.state.analog_threshold_output_refcounts,
        device_runtime.state.held_output_keys,
        bucket,
        code,
        value,
    )


def track_threshold_abs_output(
    device_runtime: GrabbedDeviceRuntime,
    bucket: str,
    axis_code: int,
    value: int,
) -> bool:
    return track_refcounted_output_bucket(
        device_runtime.state.analog_threshold_abs_refcounts,
        device_runtime.state.held_output_abs,
        bucket,
        axis_code,
        value,
        pressed_value=None,
    )


def _child_event_name(source_id: str, threshold_index: int, action_index: int) -> str:
    return f"{source_id}#analog_threshold#{threshold_index}#{action_index}"
