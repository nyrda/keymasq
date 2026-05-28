import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import cast

from keymasq.common.devices import resolve_evdev_code
from keymasq.common.models import (
    SAME_DEVICE_OUTPUT_ID,
    ActionType,
    AnalogActionThreshold,
    AnalogControlConfig,
    MappingAction,
    analog_control_primary_mode,
    analog_gamepad_output_distance,
)
from keymasq.keymasqd.runtime import grabbed_device_actions as runtime_actions
from keymasq.keymasqd.runtime.action_runner import source_trigger_id
from keymasq.keymasqd.runtime.grabbed_device_outputs import (
    syn_if_passthrough_frame_closed,
    track_abs_state,
)
from keymasq.keymasqd.runtime.grabbed_device_types import (
    ActionExecutionDeps,
    AnalogGamepadOutputState,
    GrabbedDeviceRuntime,
    InputEventLike,
)
from keymasq.keymasqd.runtime.repeat import select_repeated_entry
from keymasq.keymasqd.superkey_state import SuperkeyConfig as RuntimeSuperkeyConfig
from keymasq.keymasqd.superkey_state import superkey_slot_uses_trigger_lifetime_profile

log = logging.getLogger("keymasqd.runtime.analog_controls")
DEFAULT_STICK_MIN = -32768
DEFAULT_STICK_MAX = 32767
DEFAULT_TRIGGER_MIN = 0
DEFAULT_TRIGGER_MAX = 255
STICK_OUTPUT_AXES = {
    "left_stick": ("ABS_X", "ABS_Y"),
    "right_stick": ("ABS_RX", "ABS_RY"),
}
TRIGGER_OUTPUT_AXES = {
    "left_trigger": "ABS_Z",
    "right_trigger": "ABS_RZ",
}


@dataclass
class _SyntheticInputEvent:
    type: int
    code: int
    value: int


def normalize_axis_value(
    raw_value: int,
    minimum: int,
    maximum: int,
    *,
    center: int | None = None,
    invert: bool = False,
) -> float:
    if minimum >= maximum:
        minimum = DEFAULT_STICK_MIN
        maximum = DEFAULT_STICK_MAX

    midpoint = float(center) if center is not None else (float(minimum) + float(maximum)) / 2.0
    raw = float(raw_value)
    if raw < midpoint:
        span = max(1.0, midpoint - float(minimum))
        normalized = (raw - midpoint) / span
    else:
        span = max(1.0, float(maximum) - midpoint)
        normalized = (raw - midpoint) / span
    if invert:
        normalized = -normalized
    return max(-1.0, min(1.0, normalized))


def normalize_control_axis_value(
    raw_value: int,
    minimum: int,
    maximum: int,
    *,
    rest: int | None = None,
) -> float:
    if minimum >= maximum:
        minimum = DEFAULT_TRIGGER_MIN
        maximum = DEFAULT_TRIGGER_MAX
    if rest is None:
        rest = minimum if minimum >= 0 else 0
    positive_span = float(maximum) - float(rest)
    negative_span = float(minimum) - float(rest)
    active_span = positive_span if abs(positive_span) >= abs(negative_span) else negative_span
    if abs(active_span) < 1.0:
        active_span = float(maximum) - float(minimum)
    if abs(active_span) < 1.0:
        return 0.0
    normalized = (float(raw_value) - float(rest)) / active_span
    return max(0.0, min(1.0, normalized))


def denormalize_axis_value(
    value: float,
    minimum: int,
    maximum: int,
    *,
    center: int | None = None,
    invert: bool = False,
) -> int:
    if minimum >= maximum:
        minimum = DEFAULT_STICK_MIN
        maximum = DEFAULT_STICK_MAX
    normalized = max(-1.0, min(1.0, float(value)))
    if invert:
        normalized = -normalized
    midpoint = int(center) if center is not None else int(round((minimum + maximum) / 2.0))
    if normalized >= 0.0:
        return min(maximum, int(round(midpoint + normalized * (maximum - midpoint))))
    return max(minimum, int(round(midpoint + normalized * (midpoint - minimum))))


def denormalize_control_axis_value(
    value: float,
    minimum: int,
    maximum: int,
    *,
    rest: int | None = None,
    invert: bool = False,
) -> int:
    if minimum >= maximum:
        minimum = DEFAULT_TRIGGER_MIN
        maximum = DEFAULT_TRIGGER_MAX
    if rest is None:
        rest = minimum if minimum >= 0 else 0
    normalized = max(0.0, min(1.0, float(value)))
    endpoint = minimum if invert else maximum
    return max(minimum, min(maximum, int(round(float(rest) + normalized * (endpoint - rest)))))


def _action_analog_control_configs(action: MappingAction | None) -> list[AnalogControlConfig]:
    if action is None or action.action_type != ActionType.ANALOG_CONTROL:
        return []
    if action.analog_control_configs:
        return list(action.analog_control_configs)
    if action.analog_control_config is not None:
        return [action.analog_control_config]
    return []


def _control_state_key(source_id: str, index: int, total: int) -> str:
    return source_id if total == 1 else f"{source_id}#analog_control#{index}"


def analog_state_keys_for_action(source_id: str, action: MappingAction | None) -> set[str]:
    configs = _action_analog_control_configs(action)
    return {
        _control_state_key(source_id, index, len(configs))
        for index, _config in enumerate(configs)
    }


def preserved_analog_state_keys(
    old_mapping: dict[str, MappingAction],
    new_mapping: dict[str, MappingAction],
) -> set[str]:
    preserved: set[str] = set()
    for source_id, old_action in old_mapping.items():
        new_action = new_mapping.get(source_id)
        if not _same_analog_control_configs(old_action, new_action):
            continue
        preserved.update(analog_state_keys_for_action(source_id, new_action))
    return preserved


def _same_analog_control_configs(
    old_action: MappingAction | None,
    new_action: MappingAction | None,
) -> bool:
    old_configs = _action_analog_control_configs(old_action)
    if not old_configs:
        return False
    return old_configs == _action_analog_control_configs(new_action)


def _record_axis_value(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    analog_id: str,
    axis_role: str,
    raw_value: int,
    config: AnalogControlConfig,
) -> None:
    fallback_range = (
        (DEFAULT_TRIGGER_MIN, DEFAULT_TRIGGER_MAX)
        if config.input_type == "axis"
        else (DEFAULT_STICK_MIN, DEFAULT_STICK_MAX)
    )
    minimum, maximum = device_runtime.analog_axis_ranges.get((analog_id, axis_role), fallback_range)
    calibration = device_runtime.analog_axis_calibrations.get((analog_id, axis_role), {})
    rest_value = calibration.get("rest")
    center_value = calibration.get("center")
    if config.input_type == "axis":
        rest = rest_value if isinstance(rest_value, int) else (minimum if minimum >= 0 else 0)
        normalized = normalize_control_axis_value(raw_value, minimum, maximum, rest=rest)
        signed_normalized = normalize_axis_value(raw_value, minimum, maximum, center=rest)
    else:
        normalized = normalize_axis_value(
            raw_value,
            minimum,
            maximum,
            center=center_value if isinstance(center_value, int) else None,
            invert=bool(calibration.get("invert", False)),
        )
        signed_normalized = normalized
    axis_values = device_runtime.state.analog_axis_values.setdefault(state_key, {})
    axis_values[axis_role] = normalized
    axis_values[f"{axis_role}_signed"] = signed_normalized


async def process_analog_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    mapping: dict[str, MappingAction],
    *,
    deps: ActionExecutionDeps,
) -> bool:
    binding = device_runtime.analog_axis_bindings.get((int(event.type), int(event.code)))
    if binding is None:
        return False

    analog_id, axis_role = binding
    action = mapping.get(analog_id)
    if action is None or action.action_type == ActionType.PASSTHROUGH:
        return False
    if action.action_type == ActionType.SUPPRESS:
        return True
    if action.action_type != ActionType.ANALOG_CONTROL:
        return True

    configs = _action_analog_control_configs(action)
    if not configs:
        return False

    for index, config in enumerate(configs):
        state_key = _control_state_key(analog_id, index, len(configs))
        mode = analog_control_primary_mode(config)
        _record_axis_value(
            device_runtime,
            state_key,
            analog_id,
            axis_role,
            int(event.value),
            config,
        )
        if mode == "digital":
            await _evaluate_thresholds(
                device_runtime,
                state_key,
                config,
                event,
                source_profile_name=action.source_profile_name,
                deps=deps,
            )
        elif mode == "gamepad":
            _emit_gamepad_output(device_runtime, state_key, analog_id, config, deps=deps)
        elif mode == "mouse":
            if not await _emit_mouse_area_motion(
                device_runtime,
                state_key,
                config,
                deps=deps,
            ):
                _ensure_mouse_task(device_runtime, state_key, config, deps=deps)
    return True


async def reset_analog_controls(
    device_runtime: GrabbedDeviceRuntime,
    *,
    deps: ActionExecutionDeps,
    preserve_state_keys: set[str] | None = None,
) -> None:
    preserved = set(preserve_state_keys or ())
    mapping = device_runtime.mapping_getter()
    state_configs: dict[str, tuple[str, AnalogControlConfig]] = {}
    for source_id, action in mapping.items():
        configs = _action_analog_control_configs(action)
        for index, config in enumerate(configs):
            state_configs[_control_state_key(source_id, index, len(configs))] = (
                source_id,
                config,
            )

    _reset_recorded_gamepad_outputs(device_runtime, deps=deps, preserved=preserved)
    for state_key, active in list(device_runtime.state.analog_active_thresholds.items()):
        if state_key in preserved:
            continue
        state_config = state_configs.get(state_key)
        config = state_config[1] if state_config is not None else None
        for threshold_key in list(active):
            index = _threshold_index(threshold_key)
            actions = device_runtime.state.analog_active_threshold_actions.get(threshold_key)
            threshold = (
                config.thresholds[index]
                if config is not None and index is not None and index < len(config.thresholds)
                else None
            )
            if index is None or (threshold is None and actions is None):
                continue
            await _release_threshold_actions(
                device_runtime,
                state_key,
                index,
                threshold,
                deps=deps,
                actions=actions,
            )

    for state_key, (source_id, config) in state_configs.items():
        if state_key in preserved:
            continue
        if analog_control_primary_mode(config) == "gamepad":
            _reset_gamepad_output(device_runtime, state_key, source_id, config, deps=deps)

    tasks_to_cancel = [
        task
        for state_key, task in list(device_runtime.state.analog_mouse_tasks.items())
        if state_key not in preserved
    ]
    for task in tasks_to_cancel:
        if not task.done():
            task.cancel()
    if tasks_to_cancel:
        await asyncio.gather(
            *tasks_to_cancel,
            return_exceptions=True,
        )

    _discard_unpreserved_keys(device_runtime.state.analog_axis_values, preserved)
    _discard_unpreserved_keys(device_runtime.state.analog_active_thresholds, preserved)
    for threshold_key in list(device_runtime.state.analog_active_threshold_actions):
        if _threshold_source_key(threshold_key) not in preserved:
            device_runtime.state.analog_active_threshold_actions.pop(threshold_key, None)
    device_runtime.state.analog_repeat_superkey_profile_triggers = {
        child_event_name
        for child_event_name in device_runtime.state.analog_repeat_superkey_profile_triggers
        if _threshold_source_key(child_event_name) in preserved
    }
    _discard_unpreserved_keys(device_runtime.state.analog_mouse_tasks, preserved)
    _discard_unpreserved_keys(device_runtime.state.analog_mouse_accumulators, preserved)
    _discard_unpreserved_keys(device_runtime.state.analog_mouse_area_offsets, preserved)
    device_runtime.state.analog_mouse_area_active.intersection_update(preserved)
    _discard_unpreserved_keys(device_runtime.state.analog_gamepad_outputs, preserved)


def _discard_unpreserved_keys[StateValue](
    mapping: dict[str, StateValue],
    preserved: set[str],
) -> None:
    for state_key in list(mapping):
        if state_key not in preserved:
            mapping.pop(state_key, None)


async def _evaluate_thresholds(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    config: AnalogControlConfig,
    event: InputEventLike,
    *,
    source_profile_name: str | None = None,
    deps: ActionExecutionDeps,
) -> None:
    source_active = device_runtime.state.analog_active_thresholds.setdefault(source_id, set())
    axis_values = device_runtime.state.analog_axis_values.setdefault(source_id, {})
    for index, threshold in enumerate(config.thresholds):
        value_key = f"{threshold.axis}_signed" if config.input_type == "axis" else threshold.axis
        value = float(axis_values.get(value_key, 0.0))
        key = _threshold_key(source_id, index)
        is_active = key in source_active
        if not is_active and _inside(value, threshold.trigger_min, threshold.trigger_max):
            source_active.add(key)
            await _activate_threshold_actions(
                device_runtime,
                source_id,
                index,
                threshold,
                event,
                source_profile_name=source_profile_name,
                deps=deps,
            )
        elif is_active and not _inside(value, threshold.release_min, threshold.release_max):
            source_active.discard(key)
            await _release_threshold_actions(
                device_runtime,
                source_id,
                index,
                threshold,
                deps=deps,
                event_type=int(event.type),
                event_code=int(event.code),
            )
            device_runtime.state.analog_active_threshold_actions.pop(key, None)


async def _activate_threshold_actions(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    index: int,
    threshold: AnalogActionThreshold,
    event: InputEventLike,
    *,
    source_profile_name: str | None,
    deps: ActionExecutionDeps,
) -> None:
    synthetic = _SyntheticInputEvent(int(event.type), int(event.code), 1)
    threshold_key = _threshold_key(source_id, index)
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
        lifecycle_action = _threshold_profile_lifecycle_action(
            device_runtime,
            action,
            child_event_name,
        )
        if _threshold_repeat_superkey_uses_trigger_lifetime_profile(
            device_runtime,
            action,
        ):
            _observe_threshold_trigger_name(
                device_runtime,
                child_event_name,
                active=True,
            )
            device_runtime.state.analog_repeat_superkey_profile_triggers.add(
                child_event_name
            )
        _observe_threshold_profile_trigger(
            device_runtime,
            lifecycle_action,
            child_event_name,
            active=True,
        )
        await runtime_actions.execute_action(
            device_runtime,
            action,
            synthetic,
            child_event_name,
            deps=deps,
            shared_output_tracker=lambda bucket, code, value: _track_threshold_output(
                device_runtime,
                bucket,
                code,
                value,
            ),
            shared_abs_output_tracker=lambda bucket, axis_code, value: _track_threshold_abs_output(
                device_runtime,
                bucket,
                axis_code,
                value,
            ),
        )
    device_runtime.state.analog_active_threshold_actions[threshold_key] = tuple(
        executable_actions
    )


async def _release_threshold_actions(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    index: int,
    threshold: AnalogActionThreshold | None,
    *,
    deps: ActionExecutionDeps,
    event_type: int = 0,
    event_code: int = 0,
    actions: tuple[tuple[int, MappingAction], ...] | None = None,
) -> None:
    synthetic = _SyntheticInputEvent(event_type, event_code, 0)
    action_entries = (
        actions
        if actions is not None
        else tuple(enumerate(threshold.actions)) if threshold is not None else ()
    )
    for action_index, action in action_entries:
        if action.action_type in {
            ActionType.PASSTHROUGH,
            ActionType.SUPPRESS,
            ActionType.ANALOG_CONTROL,
        }:
            continue
        child_event_name = _child_event_name(source_id, index, action_index)
        lifecycle_action = _threshold_profile_lifecycle_action(
            device_runtime,
            action,
            child_event_name,
        )
        repeat_superkey_uses_trigger_lifetime = child_event_name in (
            device_runtime.state.analog_repeat_superkey_profile_triggers
        )
        await runtime_actions.execute_action(
            device_runtime,
            action,
            synthetic,
            child_event_name,
            deps=deps,
            shared_output_tracker=lambda bucket, code, value: _track_threshold_output(
                device_runtime,
                bucket,
                code,
                value,
            ),
            shared_abs_output_tracker=lambda bucket, axis_code, value: _track_threshold_abs_output(
                device_runtime,
                bucket,
                axis_code,
                value,
            ),
        )
        _observe_threshold_profile_trigger(
            device_runtime,
            lifecycle_action,
            child_event_name,
            active=False,
        )
        if repeat_superkey_uses_trigger_lifetime:
            _observe_threshold_trigger_name(
                device_runtime,
                child_event_name,
                active=False,
            )
            device_runtime.state.analog_repeat_superkey_profile_triggers.discard(
                child_event_name
            )


def _threshold_profile_lifecycle_action(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction,
    child_event_name: str,
) -> MappingAction:
    if action.action_type != ActionType.REPEAT:
        return action
    active_action = device_runtime.state.repeat_active_actions.get(
        f"{child_event_name}#repeat"
    )
    if active_action is not None:
        return active_action
    repeated_entry = select_repeated_entry(
        getattr(device_runtime, "repeat_state", None),
        action,
    )
    return repeated_entry.action if repeated_entry is not None else action


def _threshold_repeat_superkey_uses_trigger_lifetime_profile(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction,
) -> bool:
    if action.action_type != ActionType.REPEAT:
        return False
    repeated_entry = select_repeated_entry(
        getattr(device_runtime, "repeat_state", None),
        action,
    )
    if repeated_entry is None or repeated_entry.superkey_slot is None:
        return False
    config = cast(RuntimeSuperkeyConfig | None, repeated_entry.action.superkey_config)
    return bool(
        config is not None
        and superkey_slot_uses_trigger_lifetime_profile(
            config,
            repeated_entry.superkey_slot,
        )
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


def _observe_threshold_trigger_name(
    device_runtime: GrabbedDeviceRuntime,
    child_event_name: str,
    *,
    active: bool,
) -> None:
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
        trigger_id = (
            source_trigger_id(device_runtime.hardware_id, source_id)
            if source_id
            else None
        )
        recorder(source_profile_name, trigger_id)


def _track_threshold_output(
    device_runtime: GrabbedDeviceRuntime,
    bucket: str,
    code: int,
    value: int,
) -> bool:
    refcounts = device_runtime.state.analog_threshold_output_refcounts.setdefault(bucket, {})
    held = device_runtime.state.held_output_keys.setdefault(bucket, set())
    current = refcounts.get(int(code), 0)
    if int(value) == 1:
        refcounts[int(code)] = current + 1
        held.add(int(code))
        return current == 0
    if int(value) == 0:
        if current <= 1:
            refcounts.pop(int(code), None)
            held.discard(int(code))
            return current == 1
        refcounts[int(code)] = current - 1
        return False
    return True


def _track_threshold_abs_output(
    device_runtime: GrabbedDeviceRuntime,
    bucket: str,
    axis_code: int,
    value: int,
) -> bool:
    refcounts = device_runtime.state.analog_threshold_abs_refcounts.setdefault(bucket, {})
    held = device_runtime.state.held_output_abs.setdefault(bucket, set())
    current = refcounts.get(int(axis_code), 0)
    if int(value) != 0:
        refcounts[int(axis_code)] = current + 1
        held.add(int(axis_code))
        return current == 0
    if current <= 1:
        refcounts.pop(int(axis_code), None)
        held.discard(int(axis_code))
        return current == 1
    refcounts[int(axis_code)] = current - 1
    return False


def _ensure_mouse_task(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    if not config.mouse_motion.enabled:
        return
    if config.mouse_motion.mode == "area":
        return

    task = device_runtime.state.analog_mouse_tasks.get(state_key)
    if task is not None and not task.done():
        return

    device_runtime.state.analog_mouse_tasks[state_key] = deps.asyncio_mod.create_task(
        _mouse_motion_loop(device_runtime, state_key, config, deps=deps)
    )


async def _mouse_motion_loop(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    previous = time.monotonic()
    try:
        while True:
            if not config.mouse_motion.enabled:
                return

            tick_s = max(0.001, float(config.mouse_motion.tick_ms) / 1000.0)
            await deps.asyncio_mod.sleep(tick_s)
            now = time.monotonic()
            dt = max(0.0, now - previous)
            previous = now

            axis_values = device_runtime.state.analog_axis_values.get(state_key, {})
            if config.input_type == "axis":
                dx, dy = _axis_motion_delta(
                    float(axis_values.get("x", 0.0)),
                    signed_value=float(axis_values.get("x_signed", 0.0)),
                    direction=config.mouse_motion.direction,
                    speed=float(config.mouse_motion.speed),
                    deadzone=float(config.mouse_motion.deadzone),
                    sensitivity=float(config.mouse_motion.sensitivity),
                    response_curve=float(config.mouse_motion.response_curve),
                    dt=dt,
                )
            else:
                x = float(axis_values.get("x", 0.0))
                y = float(axis_values.get("y", 0.0))
                if config.mouse_motion.invert_x:
                    x = -x
                if config.mouse_motion.invert_y:
                    y = -y

                dx, dy = _motion_delta(
                    x,
                    y,
                    speed_x=float(
                        config.mouse_motion.speed_x
                        if config.mouse_motion.speed_x is not None
                        else config.mouse_motion.speed
                    ),
                    speed_y=float(
                        config.mouse_motion.speed_y
                        if config.mouse_motion.speed_y is not None
                        else config.mouse_motion.speed
                    ),
                    deadzone=float(config.mouse_motion.deadzone),
                    sensitivity=float(config.mouse_motion.sensitivity),
                    response_curve=float(config.mouse_motion.response_curve),
                    dt=dt,
                )
            await _emit_mouse_delta(device_runtime, state_key, dx, dy, deps=deps)
    except asyncio.CancelledError:
        raise
    finally:
        task = device_runtime.state.analog_mouse_tasks.get(state_key)
        if task is asyncio.current_task():
            device_runtime.state.analog_mouse_tasks.pop(state_key, None)
        device_runtime.state.analog_mouse_accumulators.pop(state_key, None)


def _motion_delta(
    x: float,
    y: float,
    *,
    speed_x: float,
    speed_y: float,
    deadzone: float,
    sensitivity: float,
    response_curve: float,
    dt: float,
) -> tuple[float, float]:
    magnitude = math.sqrt(x * x + y * y)
    scaled = analog_gamepad_output_distance(
        magnitude,
        deadzone=deadzone,
        sensitivity=sensitivity,
        response_curve=response_curve,
    )
    if scaled <= 0.0 or magnitude <= 0.0:
        return 0.0, 0.0
    direction_x = x / magnitude
    direction_y = y / magnitude
    return (
        direction_x * scaled * max(0.0, speed_x) * dt,
        direction_y * scaled * max(0.0, speed_y) * dt,
    )


async def _emit_mouse_area_motion(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> bool:
    if (
        not config.mouse_motion.enabled
        or config.mouse_motion.mode != "area"
        or config.input_type != "stick"
    ):
        return False

    target_x, target_y = _mouse_area_offset(
        device_runtime,
        state_key,
        config,
    )
    active_sources = device_runtime.state.analog_mouse_area_active
    was_active = state_key in active_sources
    is_active = target_x != 0.0 or target_y != 0.0
    if (
        is_active
        and not was_active
        and config.mouse_motion.area_start_enabled
        and device_runtime.cursor_position_setter is not None
    ):
        await device_runtime.cursor_position_setter(
            int(config.mouse_motion.area_start_x),
            int(config.mouse_motion.area_start_y),
        )
        device_runtime.state.analog_mouse_area_offsets[state_key] = (0.0, 0.0)
        device_runtime.state.analog_mouse_accumulators[state_key] = (0.0, 0.0)

    old_x, old_y = device_runtime.state.analog_mouse_area_offsets.get(
        state_key,
        (0.0, 0.0),
    )
    device_runtime.state.analog_mouse_area_offsets[state_key] = (target_x, target_y)
    if is_active:
        active_sources.add(state_key)
    else:
        active_sources.discard(state_key)

    await _emit_mouse_delta(
        device_runtime,
        state_key,
        target_x - old_x,
        target_y - old_y,
        deps=deps,
    )
    return True


def _mouse_area_offset(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    config: AnalogControlConfig,
) -> tuple[float, float]:
    axis_values = device_runtime.state.analog_axis_values.get(state_key, {})
    x = float(axis_values.get("x", 0.0))
    y = float(axis_values.get("y", 0.0))
    if config.mouse_motion.invert_x:
        x = -x
    if config.mouse_motion.invert_y:
        y = -y
    x = _apply_signed_axis_output_curve(
        x,
        deadzone=float(config.mouse_motion.deadzone),
        sensitivity=float(config.mouse_motion.sensitivity),
        response_curve=float(config.mouse_motion.response_curve),
    )
    y = _apply_signed_axis_output_curve(
        y,
        deadzone=float(config.mouse_motion.deadzone),
        sensitivity=float(config.mouse_motion.sensitivity),
        response_curve=float(config.mouse_motion.response_curve),
    )
    return (
        x * max(0.0, float(config.mouse_motion.area_radius_x)),
        y * max(0.0, float(config.mouse_motion.area_radius_y)),
    )


def _axis_motion_delta(
    value: float,
    *,
    signed_value: float,
    direction: str,
    speed: float,
    deadzone: float,
    sensitivity: float,
    response_curve: float,
    dt: float,
) -> tuple[float, float]:
    if direction in {"horizontal", "vertical"}:
        scaled = _apply_signed_axis_output_curve(
            signed_value,
            deadzone=deadzone,
            sensitivity=sensitivity,
            response_curve=response_curve,
        )
    else:
        scaled = _apply_control_axis_output_curve(
            value,
            deadzone=deadzone,
            sensitivity=sensitivity,
            response_curve=response_curve,
        )
    distance = scaled * max(0.0, speed) * dt
    if direction == "horizontal":
        return distance, 0.0
    if direction == "vertical":
        return 0.0, distance
    if direction == "left":
        return -distance, 0.0
    if direction == "up":
        return 0.0, -distance
    if direction == "down":
        return 0.0, distance
    return distance, 0.0


async def _emit_mouse_delta(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    dx: float,
    dy: float,
    *,
    deps: ActionExecutionDeps,
) -> None:
    old_x, old_y = device_runtime.state.analog_mouse_accumulators.get(source_id, (0.0, 0.0))
    accum_x = old_x + dx
    accum_y = old_y + dy
    emit_x = _whole_step(accum_x)
    emit_y = _whole_step(accum_y)
    device_runtime.state.analog_mouse_accumulators[source_id] = (
        accum_x - emit_x,
        accum_y - emit_y,
    )
    if emit_x == 0 and emit_y == 0:
        return

    mouse = deps.uinput_writer(device_runtime.mouse_uinput)
    if mouse is None:
        return
    if emit_x:
        mouse.write(deps.evdev_mod.ecodes.EV_REL, deps.evdev_mod.ecodes.REL_X, emit_x)
    if emit_y:
        mouse.write(deps.evdev_mod.ecodes.EV_REL, deps.evdev_mod.ecodes.REL_Y, emit_y)
    mouse.syn()


def _inside(value: float, minimum: float, maximum: float) -> bool:
    return minimum <= value <= maximum


def _threshold_key(source_id: str, index: int) -> str:
    return f"{source_id}:{index}"


def _threshold_index(threshold_key: str) -> int | None:
    try:
        return int(threshold_key.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


def _threshold_source_key(threshold_key: str) -> str:
    return threshold_key.rsplit(":", 1)[0]


def _child_event_name(source_id: str, threshold_index: int, action_index: int) -> str:
    return f"{source_id}#analog_threshold#{threshold_index}#{action_index}"


def _whole_step(value: float) -> int:
    if value >= 0:
        return int(math.floor(value))
    return int(math.ceil(value))


def _emit_gamepad_output(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    source_id: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    if not config.gamepad_output.enabled:
        return
    if config.input_type == "axis":
        _emit_trigger_gamepad_output(device_runtime, state_key, source_id, config, deps=deps)
        return
    _emit_stick_gamepad_output(device_runtime, state_key, source_id, config, deps=deps)


def _gamepad_output_direction(config: AnalogControlConfig) -> str:
    direction = str(config.gamepad_output.output_direction or "").lower()
    if direction in {"min", "max", "both"}:
        return direction
    return "min" if config.gamepad_output.output_invert else "max"


def _emit_stick_gamepad_output(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    source_id: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    if config.gamepad_output.target == "analog":
        _emit_analog_stick_output(device_runtime, state_key, source_id, config, deps=deps)
        return
    axis_codes = _stick_output_axis_codes(device_runtime, source_id, config, deps=deps)
    if axis_codes is None:
        return
    axis_values = device_runtime.state.analog_axis_values.get(state_key, {})
    x = float(axis_values.get("x", 0.0))
    y = float(axis_values.get("y", 0.0))
    x, y = _apply_stick_output_curve(
        x,
        y,
        deadzone=float(config.gamepad_output.deadzone),
        sensitivity=float(config.gamepad_output.sensitivity),
        response_curve=float(config.gamepad_output.response_curve),
    )
    _write_gamepad_axes(
        device_runtime,
        state_key,
        source_id,
        config,
        (
            (axis_codes[0], _stick_value_to_raw(x)),
            (axis_codes[1], _stick_value_to_raw(y)),
        ),
        deps=deps,
    )


def _emit_trigger_gamepad_output(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    source_id: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    if config.gamepad_output.target == "analog":
        _emit_analog_axis_output(device_runtime, state_key, source_id, config, deps=deps)
        return
    axis_code = _trigger_output_axis_code(device_runtime, source_id, config, deps=deps)
    if axis_code is None:
        return
    axis_values = device_runtime.state.analog_axis_values.get(state_key, {})
    if _gamepad_output_direction(config) == "both":
        value = float(axis_values.get("x_signed", 0.0))
        value = _apply_signed_axis_output_curve(
            value,
            deadzone=float(config.gamepad_output.deadzone),
            sensitivity=float(config.gamepad_output.sensitivity),
            response_curve=float(config.gamepad_output.response_curve),
        )
        raw_value = denormalize_axis_value(
            value,
            DEFAULT_TRIGGER_MIN,
            DEFAULT_TRIGGER_MAX,
            center=(
                config.gamepad_output.output_rest
                if config.gamepad_output.output_rest is not None
                else DEFAULT_TRIGGER_MIN
            ),
        )
    else:
        value = float(axis_values.get("x", 0.0))
        value = _apply_control_axis_output_curve(
            value,
            deadzone=float(config.gamepad_output.deadzone),
            sensitivity=float(config.gamepad_output.sensitivity),
            response_curve=float(config.gamepad_output.response_curve),
        )
        raw_value = denormalize_control_axis_value(
            value,
            DEFAULT_TRIGGER_MIN,
            DEFAULT_TRIGGER_MAX,
            rest=config.gamepad_output.output_rest,
            invert=_gamepad_output_direction(config) == "min",
        )
    _write_gamepad_axes(
        device_runtime,
        state_key,
        source_id,
        config,
        ((axis_code, raw_value),),
        reset_axes=(
            (
                axis_code,
                config.gamepad_output.output_rest
                if config.gamepad_output.output_rest is not None
                else DEFAULT_TRIGGER_MIN,
            ),
        ),
        deps=deps,
    )


def _reset_gamepad_output(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    source_id: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    if config.gamepad_output.target == "analog":
        _reset_analog_gamepad_output(device_runtime, state_key, source_id, config, deps=deps)
        return
    if config.input_type == "axis":
        axis_code = _trigger_output_axis_code(device_runtime, source_id, config, deps=deps)
        if axis_code is None:
            return
        axes = (
            (
                axis_code,
                config.gamepad_output.output_rest
                if config.gamepad_output.output_rest is not None
                else 0,
            ),
        )
    else:
        axis_codes = _stick_output_axis_codes(device_runtime, source_id, config, deps=deps)
        if axis_codes is None:
            return
        axes = (
            (axis_codes[0], 0),
            (axis_codes[1], 0),
        )
    _write_gamepad_axes(
        device_runtime,
        state_key,
        source_id,
        config,
        axes,
        reset_axes=axes,
        releasing=True,
        deps=deps,
    )


def _emit_analog_axis_output(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    source_id: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    target = _resolve_gamepad_output_target(device_runtime, source_id, config)
    if target is None:
        return
    analog = _target_analog_input(target, config, expected_type="axis")
    axis = _target_axis(analog, "x") if analog is not None else None
    if axis is None:
        return
    axis_values = device_runtime.state.analog_axis_values.get(state_key, {})
    axis_code = _axis_code(axis)
    if axis_code is None:
        return
    minimum, maximum = _axis_min_max(axis, DEFAULT_TRIGGER_MIN, DEFAULT_TRIGGER_MAX)
    output_rest = (
        config.gamepad_output.output_rest
        if config.gamepad_output.output_rest is not None
        else _axis_int(axis, "rest")
    )
    reset_value = output_rest if output_rest is not None else (minimum if minimum >= 0 else 0)
    if _gamepad_output_direction(config) == "both":
        value = float(axis_values.get("x_signed", 0.0))
        value = _apply_signed_axis_output_curve(
            value,
            deadzone=float(config.gamepad_output.deadzone),
            sensitivity=float(config.gamepad_output.sensitivity),
            response_curve=float(config.gamepad_output.response_curve),
        )
        raw_value = denormalize_axis_value(
            value,
            minimum,
            maximum,
            center=reset_value,
        )
    else:
        value = float(axis_values.get("x", 0.0))
        value = _apply_control_axis_output_curve(
            value,
            deadzone=float(config.gamepad_output.deadzone),
            sensitivity=float(config.gamepad_output.sensitivity),
            response_curve=float(config.gamepad_output.response_curve),
        )
        raw_value = denormalize_control_axis_value(
            value,
            minimum,
            maximum,
            rest=output_rest,
            invert=_gamepad_output_direction(config) == "min",
        )
    _write_gamepad_axes(
        device_runtime,
        state_key,
        source_id,
        config,
        ((axis_code, raw_value),),
        reset_axes=((axis_code, reset_value),),
        deps=deps,
        target=target,
    )


def _emit_analog_stick_output(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    source_id: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    target = _resolve_gamepad_output_target(device_runtime, source_id, config)
    if target is None:
        return
    analog = _target_analog_input(target, config, expected_type="stick")
    if analog is None:
        return
    axes: list[tuple[int, int]] = []
    reset_axes: list[tuple[int, int]] = []
    axis_values = device_runtime.state.analog_axis_values.get(state_key, {})
    x = float(axis_values.get("x", 0.0))
    y = float(axis_values.get("y", 0.0))
    x, y = _apply_stick_output_curve(
        x,
        y,
        deadzone=float(config.gamepad_output.deadzone),
        sensitivity=float(config.gamepad_output.sensitivity),
        response_curve=float(config.gamepad_output.response_curve),
    )
    for role, normalized in (("x", x), ("y", y)):
        axis = _target_axis(analog, role)
        if axis is None:
            return
        axis_code = _axis_code(axis)
        if axis_code is None:
            return
        minimum, maximum = _axis_min_max(axis, DEFAULT_STICK_MIN, DEFAULT_STICK_MAX)
        reset_value = _stick_axis_center(axis, minimum, maximum)
        axes.append(
            (
                axis_code,
                denormalize_axis_value(
                    normalized,
                    minimum,
                    maximum,
                    center=reset_value,
                    invert=bool(axis.get("invert", False)),
                ),
            )
        )
        reset_axes.append((axis_code, reset_value))
    _write_gamepad_axes(
        device_runtime,
        state_key,
        source_id,
        config,
        tuple(axes),
        reset_axes=tuple(reset_axes),
        deps=deps,
        target=target,
    )


def _reset_analog_gamepad_output(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    source_id: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    target = _resolve_gamepad_output_target(device_runtime, source_id, config)
    if target is None:
        return
    analog = _target_analog_input(target, config, expected_type=config.input_type)
    if analog is None:
        return
    axes: list[tuple[int, int]] = []
    for axis in _target_axes(analog):
        axis_code = _axis_code(axis)
        if axis_code is None:
            continue
        if config.input_type == "axis":
            axes.append(
                (
                    axis_code,
                    config.gamepad_output.output_rest
                    if config.gamepad_output.output_rest is not None
                    else _axis_int(axis, "rest")
                    or DEFAULT_TRIGGER_MIN,
                )
            )
        else:
            minimum, maximum = _axis_min_max(axis, DEFAULT_STICK_MIN, DEFAULT_STICK_MAX)
            axes.append((axis_code, _stick_axis_center(axis, minimum, maximum)))
    _write_gamepad_axes(
        device_runtime,
        state_key,
        source_id,
        config,
        tuple(axes),
        reset_axes=tuple(axes),
        releasing=True,
        deps=deps,
        target=target,
    )


def _write_gamepad_axes(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    source_id: str,
    config: AnalogControlConfig,
    axes: tuple[tuple[int, int], ...],
    *,
    reset_axes: tuple[tuple[int, int], ...] | None = None,
    releasing: bool = False,
    deps: ActionExecutionDeps,
    target: object | None = None,
) -> None:
    if not axes:
        return
    target = target or _resolve_gamepad_output_target(device_runtime, source_id, config)
    if target is None:
        return
    target_uinput = getattr(target, "uinput", None)
    target_bucket = str(getattr(target, "bucket", "gamepad"))
    writer = deps.uinput_writer(target_uinput)
    if writer is None:
        return
    reset_values = (
        {int(axis_code): int(value) for axis_code, value in reset_axes}
        if reset_axes is not None
        else {}
    )
    for axis_code, value in axes:
        axis_code = int(axis_code)
        value = int(value)
        writer.write(deps.evdev_mod.ecodes.EV_ABS, axis_code, value)
        if releasing:
            _clear_tracked_abs_state(device_runtime, target_bucket, axis_code)
        elif value == reset_values.get(axis_code, 0):
            _clear_tracked_abs_state(device_runtime, target_bucket, axis_code)
        else:
            track_abs_state(device_runtime, axis_code, value, bucket=target_bucket)
    syn_if_passthrough_frame_closed(target_uinput, writer)
    device_runtime.state.analog_gamepad_outputs[state_key] = AnalogGamepadOutputState(
        output_id=_resolved_gamepad_output_id(device_runtime, config),
        reset_axes=(
            tuple((int(axis_code), int(value)) for axis_code, value in reset_axes)
            if reset_axes is not None
            else tuple((int(axis_code), 0) for axis_code, _value in axes)
        ),
    )


def _resolve_gamepad_output_target(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    config: AnalogControlConfig,
) -> object | None:
    return device_runtime.resolve_gamepad_output(
        _resolved_gamepad_output_id(device_runtime, config),
        f"{source_id} analog output",
    )


def _resolved_gamepad_output_id(
    device_runtime: GrabbedDeviceRuntime,
    config: AnalogControlConfig,
) -> str | None:
    if config.gamepad_output.output_id == SAME_DEVICE_OUTPUT_ID:
        return device_runtime.hardware_id
    return config.gamepad_output.output_id


def _target_analog_input(
    target: object,
    config: AnalogControlConfig,
    *,
    expected_type: str,
) -> dict[str, object] | None:
    target_analog_id = config.gamepad_output.target_analog_id
    if not target_analog_id:
        return None
    analog_inputs = getattr(target, "analog_inputs", None)
    if not isinstance(analog_inputs, dict):
        return None
    typed_analog_inputs = cast(dict[str, object], analog_inputs)
    raw_analog = typed_analog_inputs.get(target_analog_id)
    if not isinstance(raw_analog, dict):
        return None
    analog = cast(dict[str, object], raw_analog)
    if str(analog.get("type", "") or "") != expected_type:
        return None
    return analog


def _target_axes(analog: dict[str, object]) -> list[dict[str, object]]:
    raw_axes = analog.get("axes")
    if not isinstance(raw_axes, list):
        return []
    axes = cast(list[object], raw_axes)
    return [cast(dict[str, object], axis) for axis in axes if isinstance(axis, dict)]


def _target_axis(analog: dict[str, object], role: str) -> dict[str, object] | None:
    for axis in _target_axes(analog):
        if str(axis.get("role", "") or "") == role:
            return axis
    return None


def _axis_int(axis: dict[str, object], key: str) -> int | None:
    value = axis.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            return None
    return None


def _axis_code(axis: dict[str, object]) -> int | None:
    code = _axis_int(axis, "evdev_code")
    if code is not None:
        return code
    evdev_name = str(axis.get("evdev", "") or "")
    return resolve_evdev_code(evdev_name)


def _axis_min_max(
    axis: dict[str, object],
    fallback_minimum: int,
    fallback_maximum: int,
) -> tuple[int, int]:
    minimum = _axis_int(axis, "minimum")
    maximum = _axis_int(axis, "maximum")
    if minimum is None or maximum is None or minimum >= maximum:
        return fallback_minimum, fallback_maximum
    return minimum, maximum


def _stick_axis_center(axis: dict[str, object], minimum: int, maximum: int) -> int:
    center = _axis_int(axis, "center")
    if center is not None:
        return center
    return int(round((minimum + maximum) / 2.0))


def _reset_recorded_gamepad_outputs(
    device_runtime: GrabbedDeviceRuntime,
    *,
    deps: ActionExecutionDeps,
    preserved: set[str] | None = None,
) -> None:
    preserved = preserved or set()
    for source_id, output in list(device_runtime.state.analog_gamepad_outputs.items()):
        if source_id in preserved:
            continue
        _write_recorded_gamepad_reset(device_runtime, source_id, output, deps=deps)


def _write_recorded_gamepad_reset(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    output: AnalogGamepadOutputState,
    *,
    deps: ActionExecutionDeps,
) -> None:
    target = device_runtime.resolve_gamepad_output(
        output.output_id,
        f"{source_id} analog output reset",
    )
    if target is None:
        return
    target_uinput = getattr(target, "uinput", None)
    target_bucket = str(getattr(target, "bucket", "gamepad"))
    writer = deps.uinput_writer(target_uinput)
    if writer is None:
        return
    for axis_code, value in output.reset_axes:
        writer.write(deps.evdev_mod.ecodes.EV_ABS, int(axis_code), int(value))
        _clear_tracked_abs_state(device_runtime, target_bucket, int(axis_code))
    syn_if_passthrough_frame_closed(target_uinput, writer)


def _clear_tracked_abs_state(
    device_runtime: GrabbedDeviceRuntime,
    bucket: str,
    axis_code: int,
) -> None:
    held = device_runtime.state.held_output_abs.get(bucket)
    if held is not None:
        held.discard(int(axis_code))


def _gamepad_output_stick_id(source_id: str, config: AnalogControlConfig) -> str:
    if config.gamepad_output.target == "left":
        return "left_stick"
    if config.gamepad_output.target == "right":
        return "right_stick"
    return source_id


def _stick_output_axis_codes(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> tuple[int, int] | None:
    if config.gamepad_output.target == "same":
        axis_codes = device_runtime.analog_axis_output_codes
        x_code = axis_codes.get((source_id, "x"))
        y_code = axis_codes.get((source_id, "y"))
        if x_code is not None and y_code is not None:
            return int(x_code), int(y_code)

    axis_names = STICK_OUTPUT_AXES.get(_gamepad_output_stick_id(source_id, config))
    if axis_names is None:
        return None
    return (
        int(getattr(deps.evdev_mod.ecodes, axis_names[0])),
        int(getattr(deps.evdev_mod.ecodes, axis_names[1])),
    )


def _gamepad_output_trigger_id(source_id: str, config: AnalogControlConfig) -> str:
    if config.gamepad_output.target == "left":
        return "left_trigger"
    if config.gamepad_output.target == "right":
        return "right_trigger"
    return source_id


def _trigger_output_axis_code(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> int | None:
    if config.gamepad_output.target == "same":
        trigger_id = _same_trigger_output_id(device_runtime, source_id, deps=deps)
        if trigger_id is not None:
            return int(getattr(deps.evdev_mod.ecodes, TRIGGER_OUTPUT_AXES[trigger_id]))
        axis_code = device_runtime.analog_axis_output_codes.get((source_id, "x"))
        if axis_code is not None:
            return int(axis_code)

    axis_name = TRIGGER_OUTPUT_AXES.get(_gamepad_output_trigger_id(source_id, config))
    if axis_name is None:
        return None
    return int(getattr(deps.evdev_mod.ecodes, axis_name))


def _same_trigger_output_id(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    *,
    deps: ActionExecutionDeps,
) -> str | None:
    if source_id in TRIGGER_OUTPUT_AXES:
        return source_id

    axis_code = device_runtime.analog_axis_output_codes.get((source_id, "x"))
    if axis_code == int(deps.evdev_mod.ecodes.ABS_Z):
        return "left_trigger"
    if axis_code == int(deps.evdev_mod.ecodes.ABS_RZ):
        return "right_trigger"

    input_data = device_runtime.analog_inputs.get(source_id)
    label = ""
    if isinstance(input_data, dict):
        input_metadata = cast(dict[str, object], input_data)
        label_value = input_metadata.get("label")
        label = str(label_value or "")
    text = f"{source_id} {label}".lower().replace("-", "_").replace(" ", "_")
    if (
        "left_trigger" in text
        or text.endswith("_lt")
        or "_lt_" in text
        or "l2" in text
    ):
        return "left_trigger"
    if (
        "right_trigger" in text
        or text.endswith("_rt")
        or "_rt_" in text
        or "r2" in text
    ):
        return "right_trigger"
    return None


def _apply_stick_output_curve(
    x: float,
    y: float,
    *,
    deadzone: float,
    sensitivity: float,
    response_curve: float,
) -> tuple[float, float]:
    magnitude = math.sqrt(x * x + y * y)
    scaled = analog_gamepad_output_distance(
        magnitude,
        deadzone=deadzone,
        sensitivity=sensitivity,
        response_curve=response_curve,
    )
    if scaled <= 0.0 or magnitude <= 0.0:
        return 0.0, 0.0
    direction_x = x / magnitude
    direction_y = y / magnitude
    return direction_x * scaled, direction_y * scaled


def _apply_control_axis_output_curve(
    value: float,
    *,
    deadzone: float,
    sensitivity: float,
    response_curve: float,
) -> float:
    return analog_gamepad_output_distance(
        max(0.0, min(1.0, value)),
        deadzone=deadzone,
        sensitivity=sensitivity,
        response_curve=response_curve,
    )


def _apply_signed_axis_output_curve(
    value: float,
    *,
    deadzone: float,
    sensitivity: float,
    response_curve: float,
) -> float:
    value = max(-1.0, min(1.0, value))
    scaled = _apply_control_axis_output_curve(
        abs(value),
        deadzone=deadzone,
        sensitivity=sensitivity,
        response_curve=response_curve,
    )
    return math.copysign(scaled, value)


def _stick_value_to_raw(value: float) -> int:
    value = max(-1.0, min(1.0, value))
    if value >= 0.0:
        return min(DEFAULT_STICK_MAX, int(round(value * DEFAULT_STICK_MAX)))
    return max(DEFAULT_STICK_MIN, int(round(value * abs(DEFAULT_STICK_MIN))))
