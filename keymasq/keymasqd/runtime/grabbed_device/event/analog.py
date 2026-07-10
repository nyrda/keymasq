"""Analog-control dispatch for grabbed evdev events."""

from keymasq.common.model.actions import MappingAction
from keymasq.keymasqd.runtime.analog_controls import process_analog_event
from keymasq.keymasqd.runtime.grabbed_device.types import (
    ActionExecutionDeps,
    GrabbedDeviceRuntime,
    InputEventLike,
)


def is_bound_analog_axis_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    *,
    ev_abs: int,
) -> bool:
    return (
        int(event.type) == int(ev_abs)
        and (
            int(event.type),
            int(event.code),
        )
        in device_runtime.analog_axis_bindings
    )


async def dispatch_analog_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    mapping: dict[str, MappingAction],
    *,
    deps: ActionExecutionDeps,
) -> bool:
    """Dispatch a known analog binding and report whether it consumed the event."""

    return await process_analog_event(
        device_runtime,
        event,
        event_name,
        mapping,
        deps=deps,
    )
