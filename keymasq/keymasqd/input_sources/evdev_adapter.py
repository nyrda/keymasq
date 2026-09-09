"""Adapt native motion frames to existing runtime/inspection event consumers.

These events stay inside the daemon. No synthetic kernel input device is created.
Driver protocols and shared subscriptions use named channels, not evdev codes.
"""

from collections.abc import AsyncGenerator

import evdev

from keymasq.common.types import JsonObject

from .discovery import binding_for_path
from .manager import source_manager
from .types import Binding, InputFrame


def channel_codes(binding: Binding) -> dict[str, int]:
    codes = {"accelerometer": iter((0, 1, 2)), "gyro": iter((3, 4, 5))}
    return {
        channel.name: next(codes[channel.kind])
        for channel in binding.driver.channels
        if channel.kind in codes
    }


def motion_axes(binding: Binding) -> JsonObject:
    codes = channel_codes(binding)
    result: JsonObject = {"gyro_axes": [], "accelerometer_axes": []}
    for channel in binding.driver.channels:
        if channel.kind not in {"gyro", "accelerometer"}:
            continue
        code = codes[channel.name]
        result[f"{channel.kind}_axes"].append(
            {
                "role": channel.role,
                "evdev": str(evdev.ecodes.ABS[code]).lower(),
                "evdev_code": code,
                "scale": channel.scale,
                "invert": channel.invert,
            }
        )
    return result


def frame_events(binding: Binding, frame: InputFrame) -> list[evdev.InputEvent]:
    sec, ns = divmod(frame.sample_ns, 1_000_000_000)
    codes = channel_codes(binding)
    events: list[evdev.InputEvent] = []
    if frame.discontinuity:
        events.extend(
            (evdev.InputEvent(sec, ns // 1000, 0, 3, 0), evdev.InputEvent(sec, ns // 1000, 0, 0, 0))
        )
    events.extend(
        evdev.InputEvent(sec, ns // 1000, 3, codes[name], value)
        for name, value in frame.values.items()
        if name in codes
    )
    events.append(evdev.InputEvent(sec, ns // 1000, 0, 0, 0))
    return events


class NativeInputDevice:
    def __init__(self, path: str, *, binding: Binding | None = None) -> None:
        self.binding = binding if binding is not None else binding_for_path(path)
        if self.binding.path != path:
            raise ValueError("Native input binding does not match its source address")
        self.path = path
        self.name = self.binding.driver.label
        self.phys = self.binding.endpoint.phys
        self.uniq = ""
        self.info = evdev.DeviceInfo(
            self.binding.endpoint.bus,
            self.binding.endpoint.vendor,
            self.binding.endpoint.product,
            0,
        )
        self._values: dict[int, int] = {}
        self._codes = channel_codes(self.binding)
        self._stream: AsyncGenerator[evdev.InputEvent] | None = None

    def capabilities(self, *, absinfo: bool = False) -> dict[int, list[object]]:
        codes = channel_codes(self.binding).values()
        return {3: [(code, self.absinfo(code)) for code in codes] if absinfo else list(codes)}

    def input_props(self) -> list[int]:
        return [evdev.ecodes.INPUT_PROP_ACCELEROMETER]

    def absinfo(self, code: int) -> evdev.AbsInfo:
        return evdev.AbsInfo(self._values.get(code, 0), -32768, 32767, 0, 0, 0)

    def active_keys(self) -> list[int]:
        return []

    def close(self) -> None:
        # The async reader's finally block owns the shared subscription.
        pass

    def async_read_loop(self) -> AsyncGenerator[evdev.InputEvent]:
        self._stream = self._events()
        return self._stream

    async def aclose(self) -> None:
        if self._stream is not None:
            await self._stream.aclose()
            self._stream = None

    async def _events(self) -> AsyncGenerator[evdev.InputEvent]:
        async with source_manager().subscribe(self.binding) as subscriber:
            while True:
                frame = await subscriber.read()
                self._values = {self._codes[name]: value for name, value in frame.values.items()}
                for event in frame_events(self.binding, frame):
                    yield event
