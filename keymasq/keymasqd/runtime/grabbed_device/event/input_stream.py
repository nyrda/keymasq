"""Read without python-evdev's private batch buffer, so recovery can drain input."""

import asyncio
from collections.abc import AsyncIterator

from keymasq.keymasqd.input_sources.evdev_adapter import NativeInputDevice
from keymasq.keymasqd.runtime.grabbed_device.grab import reconcile_live_key_state
from keymasq.keymasqd.runtime.grabbed_device.types import GrabbedDeviceRuntime, InputEventLike


async def read_events(runtime: GrabbedDeviceRuntime) -> AsyncIterator[InputEventLike]:
    device = runtime.device
    if device is None:
        return
    if isinstance(device, NativeInputDevice):
        # Native sources own an async subscription, not an evdev file descriptor.
        try:
            async for event in device.async_read_loop():
                yield event
        finally:
            await device.aclose()
        return
    loop = asyncio.get_running_loop()
    readable = asyncio.Event()
    fd = device.fileno()
    loop.add_reader(fd, readable.set)
    runtime.state.input_event_ready = readable
    try:
        while runtime.running:
            if (
                runtime.state.key_state_reconcile_requested
                and not runtime.state.input_event_buffer
            ):
                # The previous event is fully processed here, so nothing is in flight.
                runtime.state.key_state_reconcile_requested = False
                reconcile_live_key_state(runtime)
            if runtime.state.input_event_buffer:
                yield runtime.state.input_event_buffer.popleft()
                continue
            readable.clear()
            try:
                event = device.read_one()
            except BlockingIOError:
                event = None
            if event is None:
                await readable.wait()
            else:
                yield event
    finally:
        runtime.state.input_event_ready = None
        loop.remove_reader(fd)

