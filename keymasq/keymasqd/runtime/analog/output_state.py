"""Tracked virtual-gamepad output writes and reset cleanup."""

from keymasq.common.model.analog import AnalogControlConfig
from keymasq.keymasqd.runtime.analog.metadata import (
    resolve_gamepad_output_target,
    resolved_gamepad_output_id,
)
from keymasq.keymasqd.runtime.grabbed_device.outputs import (
    syn_if_passthrough_frame_closed,
    track_abs_state,
)
from keymasq.keymasqd.runtime.grabbed_device.types import (
    ActionExecutionDeps,
    AnalogGamepadOutputState,
    GrabbedDeviceRuntime,
)


def write_gamepad_axes(
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
    target = target or resolve_gamepad_output_target(device_runtime, source_id, config)
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
    syn_if_passthrough_frame_closed(
        target_uinput,
        writer,
        device_runtime=device_runtime,
    )
    device_runtime.state.analog_gamepad_outputs[state_key] = AnalogGamepadOutputState(
        output_id=resolved_gamepad_output_id(device_runtime, config),
        reset_axes=(
            tuple((int(axis_code), int(value)) for axis_code, value in reset_axes)
            if reset_axes is not None
            else tuple((int(axis_code), 0) for axis_code, _value in axes)
        ),
    )


def reset_recorded_gamepad_outputs(
    device_runtime: GrabbedDeviceRuntime,
    *,
    deps: ActionExecutionDeps,
    preserved: set[str] | None = None,
    state_key_prefix: str | None = None,
) -> None:
    preserved = preserved or set()
    for source_id, output in list(device_runtime.state.analog_gamepad_outputs.items()):
        if source_id in preserved or (
            state_key_prefix is not None and not source_id.startswith(state_key_prefix)
        ):
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
    syn_if_passthrough_frame_closed(
        target_uinput,
        writer,
        device_runtime=device_runtime,
    )


def _clear_tracked_abs_state(
    device_runtime: GrabbedDeviceRuntime,
    bucket: str,
    axis_code: int,
) -> None:
    held = device_runtime.state.held_output_abs.get(bucket)
    if held is not None:
        held.discard(int(axis_code))
