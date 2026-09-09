"""Physical axis coordinates, independent of mappings and stroke references."""

from keymasq.keymasqd.runtime.grabbed_device.types import (
    ActionExecutionDeps,
    GrabbedDeviceRuntime,
    InputEventLike,
)


async def read_missing_source_axes(
    device_runtime: GrabbedDeviceRuntime,
    *,
    deps: ActionExecutionDeps,
) -> bool:
    """Replace coordinates at a drained input boundary, never mix ioctl and history."""
    values = device_runtime.state.analog_source_axis_values
    bindings = dict(device_runtime.analog_axis_bindings)
    missing = bindings.keys() - values.keys()
    if not missing:
        return True
    device = device_runtime.device
    if device is None:
        return False

    retained: list[InputEventLike] = []
    drained_axes = False

    def read_axes() -> dict[tuple[int, int], int] | None:
        nonlocal drained_axes
        try:
            # A snapshot can already include unread reports. Discard their analog
            # history and retry if axes changed during the query. Other events
            # retain their order in the reader's explicitly owned queue.
            for _ in range(4):
                snapshot: dict[tuple[int, int], int] = {}
                for binding in bindings:
                    value = getattr(device.absinfo(binding[1]), "value", None)
                    if not isinstance(value, int):
                        return None
                    snapshot[binding] = value
                changed = False
                for _ in range(4096):
                    try:
                        event = device.read_one()
                    except BlockingIOError:
                        event = None
                    if event is None:
                        break
                    retained.append(event)
                    if (int(event.type), int(event.code)) in bindings:
                        changed = drained_axes = True
                else:
                    return None
                if not changed:
                    return snapshot
        except OSError:
            return None
        return None

    snapshot = await deps.asyncio_mod.to_thread(read_axes)
    device_runtime.state.input_event_buffer.extend(retained)
    if drained_axes:
        state = device_runtime.state
        affected = state.analog_mouse_area_positions.keys() | state.analog_mouse_area_pending.keys()
        state.analog_mouse_area_needs_release.update(affected)
        state.analog_mouse_area_positions.clear()
        state.analog_mouse_area_pending.clear()
        for key in affected:
            state.analog_mouse_accumulators.pop(key, None)
        state.analog_mouse_area_resyncing = True
        state.analog_snapshot_boundary = retained[-1]
    if (
        snapshot is None
        or device_runtime.device is not device
        or device_runtime.analog_axis_bindings != bindings
    ):
        return False
    values.clear()
    values.update(snapshot)
    return not drained_axes
