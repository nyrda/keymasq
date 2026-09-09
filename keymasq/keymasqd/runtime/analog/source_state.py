"""Physical axis coordinates, independent of mappings and stroke references."""

from keymasq.keymasqd.runtime.grabbed_device.types import (
    ActionExecutionDeps,
    GrabbedDeviceRuntime,
)


async def read_missing_source_axes(
    device_runtime: GrabbedDeviceRuntime,
    *,
    deps: ActionExecutionDeps,
) -> bool:
    """Query unchanged axes at startup, or all axes after a dropped report."""
    values = device_runtime.state.analog_source_axis_values
    bindings = dict(device_runtime.analog_axis_bindings)
    missing = bindings.keys() - values.keys()
    if not missing:
        return True
    device = device_runtime.device
    if device is None:
        return False

    def read_axes() -> dict[tuple[int, int], int] | None:
        snapshot: dict[tuple[int, int], int] = {}
        try:
            for binding in missing:
                value = getattr(device.absinfo(binding[1]), "value", None)
                if not isinstance(value, int):
                    return None
                snapshot[binding] = value
        except OSError:
            return None
        return snapshot

    snapshot = await deps.asyncio_mod.to_thread(read_axes)
    if (
        snapshot is None
        or device_runtime.device is not device
        or device_runtime.analog_axis_bindings != bindings
    ):
        return False
    values.update(snapshot)
    return True
