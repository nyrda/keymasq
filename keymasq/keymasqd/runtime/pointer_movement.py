"""Relative pointer movement factors and controller-axis output."""

import logging
import math
import time
from collections.abc import Mapping

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.analog import AnalogControlConfig, AnalogGamepadOutputConfig
from keymasq.common.model.core import ActionType
from keymasq.common.model.pointer import POINTER_SOURCE_ID, PointerMovementConfig
from keymasq.common.types import SyntheticInputEvent
from keymasq.keymasqd.runtime.analog.gamepad import emit_gamepad_output
from keymasq.keymasqd.runtime.analog.metadata import resolve_gamepad_output_target
from keymasq.keymasqd.runtime.analog.output_state import reset_recorded_gamepad_outputs
from keymasq.keymasqd.runtime.grabbed_device.event.passthrough import emit_passthrough_event
from keymasq.keymasqd.runtime.grabbed_device.types import (
    ActionExecutionDeps,
    EvdevModule,
    GrabbedDeviceRuntime,
    InputEventLike,
)
from keymasq.keymasqd.runtime.pointer_state import PointerMovementState

log = logging.getLogger("keymasqd.runtime.pointer_movement")

POINTER_STATE_PREFIX = "pointer:"
POINTER_STATE_KEYS = (f"{POINTER_STATE_PREFIX}x", f"{POINTER_STATE_PREFIX}y")
_TIMESTAMP_TOLERANCE_NS = 1_000_000_000


def pointer_action(mapping: Mapping[str, MappingAction]) -> MappingAction | None:
    action = mapping.get(POINTER_SOURCE_ID)
    if action is None or action.action_type not in {
        ActionType.POINTER_MOVEMENT,
        ActionType.SUPPRESS,
    }:
        return None
    return action


def consume_pointer_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    mapping: Mapping[str, MappingAction],
    *,
    deps: ActionExecutionDeps,
) -> bool:
    """Take REL_X/REL_Y away from passthrough when the profile maps the pointer."""
    ecodes = deps.evdev_mod.ecodes
    if int(event.type) != int(ecodes.EV_REL):
        return False
    horizontal = int(event.code) == int(ecodes.REL_X)
    if not horizontal and int(event.code) != int(ecodes.REL_Y):
        return False
    action = pointer_action(mapping)
    if action is None:
        return False
    config = action.pointer_movement
    if config is None or device_runtime.state.pointer_resyncing:
        return True
    state = _synced_state(device_runtime, config, deps=deps)
    value = float(event.value)
    x, y = _transformed(config, value if horizontal else 0.0, 0.0 if horizontal else value)
    if config.mode == "mouse":
        _emit_mouse(device_runtime, state, x, y, deps=deps)
    else:
        state.frame_x += x
        state.frame_y += y
        state.frame_pending = True
    return True


def flush_pointer_frame(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    mapping: Mapping[str, MappingAction],
    *,
    deps: ActionExecutionDeps,
) -> None:
    """Turn one complete report of pointer movement into controller axis output."""
    state = device_runtime.state.pointer_movement
    if not state.frame_pending:
        return
    dx, dy = state.frame_x, state.frame_y
    discard_pointer_frame(device_runtime)
    action = pointer_action(mapping)
    # A resent but unchanged mapping keeps the state; its settle task follows state.config.
    config = state.config
    if (
        config is None
        or config.mode != "axes"
        or action is None
        or action.pointer_movement != config
    ):
        return
    now_ns = _event_ns(event)
    state.last_arrival_ns = time.monotonic_ns()
    if config.behavior == "velocity":
        window_ns = config.window_ms * 1_000_000
        # Idle expiry counts from arrival too, so a late report is not settled on arrival.
        expiry_ns = max(now_ns, state.last_arrival_ns) + window_ns
        state.samples.append((now_ns, expiry_ns, dx, dy))
        window_start_ns = now_ns - window_ns
        while state.samples and state.samples[0][0] <= window_start_ns:
            state.samples.popleft()
    else:
        state.position_x = _moved(state.position_x, dx, config, config.x_direction)
        state.position_y = _moved(state.position_y, dy, config, config.y_direction)
        state.last_motion_ns = now_ns
    _emit_axes(device_runtime, state, config, deps=deps)
    _ensure_settle_task(device_runtime, state, config, deps=deps)


def observe_pointer_sync(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    *,
    evdev_mod: EvdevModule,
) -> None:
    """Ignore pointer movement through the report after SYN_DROPPED, even while paused."""
    if int(event.code) == int(evdev_mod.ecodes.SYN_DROPPED):
        discard_pointer_frame(device_runtime)
        device_runtime.state.pointer_resyncing = True
    elif int(event.code) == 0 and device_runtime.state.pointer_resyncing:  # SYN_REPORT
        discard_pointer_frame(device_runtime)
        device_runtime.state.pointer_resyncing = False


def discard_pointer_frame(device_runtime: GrabbedDeviceRuntime) -> None:
    state = device_runtime.state.pointer_movement
    state.frame_x = 0.0
    state.frame_y = 0.0
    state.frame_pending = False


def same_pointer_movement(
    old_mapping: Mapping[str, MappingAction],
    new_mapping: Mapping[str, MappingAction],
) -> bool:
    old_action = pointer_action(old_mapping)
    new_action = pointer_action(new_mapping)
    return (
        old_action is not None
        and new_action is not None
        and old_action.pointer_movement is not None
        and old_action.pointer_movement == new_action.pointer_movement
    )


def release_pointer_movement(
    device_runtime: GrabbedDeviceRuntime,
    *,
    deps: ActionExecutionDeps,
) -> None:
    """Return driven axes to rest and forget pointer state without yielding to events."""
    task = device_runtime.state.pointer_movement.task
    if task is not None:
        task.cancel()
    device_runtime.state.pointer_movement = PointerMovementState()
    try:
        reset_recorded_gamepad_outputs(
            device_runtime,
            deps=deps,
            state_key_prefix=POINTER_STATE_PREFIX,
        )
    except OSError:
        log.exception("Failed to return pointer axes to rest for %s", device_runtime.path)
    for state_key in POINTER_STATE_KEYS:
        device_runtime.state.analog_gamepad_outputs.pop(state_key, None)
        device_runtime.state.analog_axis_values.pop(state_key, None)


def _synced_state(
    device_runtime: GrabbedDeviceRuntime,
    config: PointerMovementConfig,
    *,
    deps: ActionExecutionDeps,
) -> PointerMovementState:
    state = device_runtime.state.pointer_movement
    if state.config != config:
        # The event loop can see a new mapping before its reset runs.
        release_pointer_movement(device_runtime, deps=deps)
        state = device_runtime.state.pointer_movement
        state.config = config
        state.axis_configs = (
            _axis_output_config(config, config.x_axis, config.x_direction),
            _axis_output_config(config, config.y_axis, config.y_direction),
        )
    return state


def _axis_output_config(
    config: PointerMovementConfig,
    axis: str | None,
    direction: str,
) -> AnalogControlConfig | None:
    if config.mode != "axes" or axis is None:
        return None
    return AnalogControlConfig(
        name="Pointer Movement",
        input_type="axis",
        gamepad_output=AnalogGamepadOutputConfig(
            enabled=True,
            output_id=config.output_id,
            target="axis",
            target_axis=axis,
            output_direction=direction,
        ),
    )


def _transformed(config: PointerMovementConfig, x: float, y: float) -> tuple[float, float]:
    if config.swap_axes:
        x, y = y, x
    if config.invert_x:
        x = -x
    if config.invert_y:
        y = -y
    return x * config.factor_x, y * config.factor_y


def _emit_mouse(
    device_runtime: GrabbedDeviceRuntime,
    state: PointerMovementState,
    x: float,
    y: float,
    *,
    deps: ActionExecutionDeps,
) -> None:
    total_x = state.remainder_x + x
    total_y = state.remainder_y + y
    move_x = math.trunc(total_x)
    move_y = math.trunc(total_y)
    state.remainder_x = total_x - move_x
    state.remainder_y = total_y - move_y
    ecodes = deps.evdev_mod.ecodes
    for code, value in ((ecodes.REL_X, move_x), (ecodes.REL_Y, move_y)):
        if value:
            emit_passthrough_event(
                device_runtime,
                SyntheticInputEvent(ecodes.EV_REL, code, value),
                evdev_mod=deps.evdev_mod,
            )


def _moved(
    position: float,
    delta: float,
    config: PointerMovementConfig,
    direction: str,
) -> float:
    moved = position + delta
    lower = -config.radius if direction == "both" else 0.0
    if config.overshoot == "keep":
        return moved if direction == "both" else max(lower, moved)
    return max(lower, min(config.radius, moved))


def _axis_values(
    state: PointerMovementState,
    config: PointerMovementConfig,
) -> tuple[float, float]:
    if config.behavior == "position":
        return state.position_x / config.radius, state.position_y / config.radius
    scale = 1000.0 / config.window_ms / config.full_speed
    return (
        sum(sample[2] for sample in state.samples) * scale,
        sum(sample[3] for sample in state.samples) * scale,
    )


def _shaped(value: float, config: PointerMovementConfig, *, one_sided: bool) -> float:
    if one_sided:
        value = max(0.0, value)
    magnitude = min(1.0, abs(value))
    if magnitude <= config.deadzone:
        return 0.0
    normalized = math.pow(
        (magnitude - config.deadzone) / (1.0 - config.deadzone), config.response_curve
    )
    output = config.minimum_output + (1.0 - config.minimum_output) * normalized
    return math.copysign(output, value)


def _emit_axes(
    device_runtime: GrabbedDeviceRuntime,
    state: PointerMovementState,
    config: PointerMovementConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    values = _axis_values(state, config)
    directions = (config.x_direction, config.y_direction)
    outputs = [
        (state_key, axis_config, value, direction)
        for state_key, axis_config, value, direction in zip(
            POINTER_STATE_KEYS, state.axis_configs, values, directions, strict=True
        )
        if axis_config is not None
    ]
    if not outputs:
        return
    target = resolve_gamepad_output_target(device_runtime, POINTER_SOURCE_ID, outputs[0][1])
    uinput = getattr(target, "uinput", None)
    previous_frame = device_runtime.state.passthrough_frame_output
    # One SYN for both axes, so a diagonal move is never observed half applied.
    batched = uinput is not None and previous_frame is not uinput
    if batched:
        device_runtime.state.passthrough_frame_output = uinput
    try:
        for state_key, axis_config, value, direction in outputs:
            shaped = _shaped(value, config, one_sided=direction != "both")
            device_runtime.state.analog_axis_values[state_key] = {
                "x": abs(shaped),
                "x_signed": shaped,
            }
            emit_gamepad_output(
                device_runtime, state_key, POINTER_SOURCE_ID, axis_config, deps=deps
            )
    finally:
        if batched:
            device_runtime.state.passthrough_frame_output = previous_frame
            writer = deps.uinput_writer(uinput)
            if writer is not None:
                writer.syn()


def _ensure_settle_task(
    device_runtime: GrabbedDeviceRuntime,
    state: PointerMovementState,
    config: PointerMovementConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    if config.behavior == "position" and config.recenter_ms <= 0:
        return
    if state.task is not None and not state.task.done():
        return
    state.task = deps.asyncio_mod.create_task(
        _settle_loop(device_runtime, state, config, deps=deps)
    )


async def _settle_loop(
    device_runtime: GrabbedDeviceRuntime,
    state: PointerMovementState,
    config: PointerMovementConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    """Expire idle velocity samples one by one, or recenter position output after idling."""
    try:
        while device_runtime.state.pointer_movement is state and state.config is config:
            now_ns = time.monotonic_ns()
            if config.behavior == "velocity":
                expired = False
                while state.samples and state.samples[0][1] <= now_ns:
                    state.samples.popleft()
                    expired = True
                if expired:
                    _emit_axes(device_runtime, state, config, deps=deps)
                if not state.samples:
                    return
                due_ns = state.samples[0][1]
            else:
                due_ns = (
                    max(state.last_motion_ns, state.last_arrival_ns)
                    + config.recenter_ms * 1_000_000
                )
                if due_ns <= now_ns:
                    state.position_x = 0.0
                    state.position_y = 0.0
                    _emit_axes(device_runtime, state, config, deps=deps)
                    return
            await deps.asyncio_mod.sleep((due_ns - now_ns) / 1_000_000_000)
    except OSError:
        log.exception("Failed to settle pointer axes for %s", device_runtime.path)
        if device_runtime.state.pointer_movement is state:
            state.task = None
            release_pointer_movement(device_runtime, deps=deps)


def _event_ns(event: InputEventLike) -> int:
    now_ns = time.monotonic_ns()
    sec = getattr(event, "sec", None)
    usec = getattr(event, "usec", None)
    if isinstance(sec, int) and isinstance(usec, int):
        timestamp = sec * 1_000_000_000 + usec * 1_000
        # Grabbed devices report CLOCK_MONOTONIC; anything else falls back to arrival time.
        if abs(now_ns - timestamp) <= _TIMESTAMP_TOLERANCE_NS:
            return timestamp
    return now_ns
