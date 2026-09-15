import asyncio

import evdev
import pytest

from keymasq.common.types import JsonObject
from keymasq.keymasqd.runtime.device_inspector import (
    INSPECTOR_UPDATE_INTERVAL_S,
    DeviceInspectorState,
)


def event(kind: int, code: int, value: int, *, path: str = "motion") -> JsonObject:
    return {"hardware_id": "controller", "path": path, "type": kind, "code": code, "value": value}


@pytest.mark.asyncio
async def test_motion_burst_keeps_final_frame_without_more_input() -> None:
    state = DeviceInspectorState()
    state.start("controller")
    received: list[JsonObject] = []
    for sample in range(1000):
        for axis in range(6):
            state.queue_event(event(evdev.ecodes.EV_ABS, axis, sample), received.append)
        state.queue_event(event(evdev.ecodes.EV_SYN, 0, 0), received.append)
    assert not received
    await asyncio.sleep(INSPECTOR_UPDATE_INTERVAL_S * 2)
    assert len(received) == 7
    assert [item["value"] for item in received[:6]] == [999] * 6
    assert received[-1]["type"] == evdev.ecodes.EV_SYN
    assert [item["sequence"] for item in received] == list(range(1, 8))


@pytest.mark.asyncio
async def test_button_edges_keep_order_and_other_interface_does_not_flush_motion() -> None:
    state = DeviceInspectorState()
    state.start("controller")
    received: list[JsonObject] = []
    state.queue_event(event(evdev.ecodes.EV_ABS, 0, 42), received.append)
    for value in (1, 0):
        state.queue_event(event(evdev.ecodes.EV_KEY, 304, value, path="buttons"), received.append)
    assert [item["value"] for item in received] == [1, 0]
    # A button on the same interface flushes its preceding axis value first.
    state.queue_event(event(evdev.ecodes.EV_KEY, 305, 1), received.append)
    state.queue_event(event(evdev.ecodes.EV_KEY, 305, 0), received.append)
    assert [item["value"] for item in received] == [1, 0, 42, 1, 0]
    state.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["stop", "reset"])
async def test_stopping_discards_pending_samples_before_restart(operation: str) -> None:
    state = DeviceInspectorState()
    state.start("controller")
    received: list[JsonObject] = []
    state.queue_event(event(evdev.ecodes.EV_ABS, 0, 42), received.append)
    if operation == "stop":
        state.stop("controller")
    else:
        state.reset()
    state.start("controller")
    await asyncio.sleep(INSPECTOR_UPDATE_INTERVAL_S * 2)
    assert not received


@pytest.mark.asyncio
async def test_relative_motion_and_dropped_frames_are_not_coalesced() -> None:
    state = DeviceInspectorState()
    state.start("controller")
    received: list[JsonObject] = []
    for value in (10, -10):
        state.queue_event(event(evdev.ecodes.EV_REL, 0, value), received.append)
    state.queue_event(event(evdev.ecodes.EV_SYN, evdev.ecodes.SYN_DROPPED, 0), received.append)
    assert [item["value"] for item in received] == [10, -10, 0]
    assert received[-1]["code"] == evdev.ecodes.SYN_DROPPED
