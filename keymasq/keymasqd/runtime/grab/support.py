"""Shared runtime adapters used by grab acquisition and release transactions."""

import asyncio
from collections.abc import Awaitable, Sequence
from typing import Protocol

from keymasq.keymasqd.output_helpers import resolve_output_code
from keymasq.keymasqd.runtime import adapters
from keymasq.keymasqd.runtime.combo.state import ComboRuntimeDeps, FireAndObserve, ResolveCodeFn


def _fire_and_forget(
    coro: Awaitable[object],
    _label: str,
) -> asyncio.Task[object]:
    return asyncio.ensure_future(coro)


def combo_runtime_deps(
    *,
    resolve_code_fn: ResolveCodeFn = resolve_output_code,
    fire_and_observe_fn: FireAndObserve = _fire_and_forget,
) -> ComboRuntimeDeps:
    return ComboRuntimeDeps(
        asyncio_mod=adapters.ASYNCIO_RUNTIME,
        evdev_mod=adapters.COMBO_EVDEV_RUNTIME,
        uinput_writer=adapters.identity_uinput_writer,
        resolve_code_fn=resolve_code_fn,
        fire_and_observe_fn=fire_and_observe_fn,
    )


class EventLoopDevice(Protocol):
    async def stop_event_loop(self) -> None: ...


async def stop_device_event_loop(device: EventLoopDevice) -> None:
    await device.stop_event_loop()


async def stop_device_event_loops(devices: Sequence[EventLoopDevice]) -> None:
    for device in devices:
        await stop_device_event_loop(device)
