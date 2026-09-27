"""Adapt native frames to existing runtime/inspection event consumers.

These events stay inside the daemon. No synthetic kernel input device is created.
Driver protocols and shared subscriptions use named channels, not evdev codes.
Motion channels become ABS samples; button channels become EV_KEY transitions
on private codes above KEY_MAX.
"""

from collections.abc import AsyncGenerator, Mapping

import evdev

from keymasq.common.native_sources import native_button_code
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


def button_codes(binding: Binding) -> dict[str, int]:
    return {
        channel.name: code
        for channel in binding.driver.channels
        if channel.kind == "button" and (code := native_button_code(channel.role)) is not None
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


def native_buttons(binding: Binding) -> list[JsonObject]:
    codes = button_codes(binding)
    return [
        {"evdev": channel.role, "evdev_code": codes[channel.name]}
        for channel in binding.driver.channels
        if channel.name in codes
    ]


def frame_events(
    binding: Binding,
    frame: InputFrame,
    pressed: Mapping[str, int] | None = None,
) -> list[evdev.InputEvent]:
    sec, ns = divmod(frame.sample_ns, 1_000_000_000)
    usec = ns // 1000
    codes = channel_codes(binding)
    buttons = button_codes(binding)
    previous = pressed or {}
    events: list[evdev.InputEvent] = []
    if frame.discontinuity and codes:
        events.extend((evdev.InputEvent(sec, usec, 0, 3, 0), evdev.InputEvent(sec, usec, 0, 0, 0)))
    events.extend(
        evdev.InputEvent(sec, usec, 3, codes[name], value)
        for name, value in frame.values.items()
        if name in codes
    )
    events.extend(
        evdev.InputEvent(sec, usec, evdev.ecodes.EV_KEY, buttons[name], value)
        for name, value in frame.values.items()
        if name in buttons and previous.get(name, 0) != value
    )
    if not events:
        return []
    events.append(evdev.InputEvent(sec, usec, 0, 0, 0))
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
        self._buttons = button_codes(self.binding)
        self._pressed: dict[str, int] = {}
        self._stream: AsyncGenerator[evdev.InputEvent] | None = None

    def capabilities(self, *, absinfo: bool = False) -> dict[int, list[object]]:
        capabilities: dict[int, list[object]] = {}
        if self._codes:
            codes = self._codes.values()
            capabilities[3] = (
                [(code, self.absinfo(code)) for code in codes] if absinfo else list(codes)
            )
        if self._buttons:
            capabilities[evdev.ecodes.EV_KEY] = list(self._buttons.values())
        return capabilities

    def input_props(self) -> list[int]:
        return [evdev.ecodes.INPUT_PROP_ACCELEROMETER] if self._codes else []

    def absinfo(self, code: int) -> evdev.AbsInfo:
        return evdev.AbsInfo(self._values.get(code, 0), -32768, 32767, 0, 0, 0)

    def active_keys(self) -> list[int]:
        return [self._buttons[name] for name, value in self._pressed.items() if value]

    def grab(self) -> None:
        # Nothing else receives these inputs, so there is nothing to take over.
        pass

    def ungrab(self) -> None:
        pass

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
        try:
            async with source_manager().subscribe(self.binding) as subscriber:
                while True:
                    frame = await subscriber.read()
                    self._values = {
                        self._codes[name]: value
                        for name, value in frame.values.items()
                        if name in self._codes
                    }
                    events = frame_events(self.binding, frame, self._pressed)
                    self._pressed.update(
                        {
                            name: value
                            for name, value in frame.values.items()
                            if name in self._buttons
                        }
                    )
                    for event in events:
                        yield event
        finally:
            self._pressed.clear()
