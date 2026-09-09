"""Read without python-evdev's private batch buffer, so recovery can drain input."""

import asyncio
from collections.abc import AsyncIterator

from keymasq.keymasqd.runtime.grabbed_device.types import GrabbedDeviceRuntime, InputEventLike


async def read_events(runtime: GrabbedDeviceRuntime) -> AsyncIterator[InputEventLike]:
    device = runtime.device
    if device is None:
        return
    loop = asyncio.get_running_loop()
    readable = asyncio.Event()
    fd = device.fileno()
    loop.add_reader(fd, readable.set)
    try:
        while runtime.running:
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
        loop.remove_reader(fd)
