"""Same-code fallback output for a grabbed controller."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

import evdev

from keymasq.keymasqd.runtime.adapters import identity_uinput_writer
from keymasq.keymasqd.runtime.grabbed_device.types import ActionRuntime, InputEventLike
from keymasq.keymasqd.runtime.virtual_gamepads import GamepadOutputTarget


def source_axis_ranges(capabilities: Mapping[int, Sequence[object]]) -> dict[int, tuple[int, int]]:
    ranges: dict[int, tuple[int, int]] = {}
    for item in capabilities.get(evdev.ecodes.EV_ABS, ()):
        if not isinstance(item, tuple) or len(cast(tuple[object, ...], item)) != 2:
            continue
        code, info = cast(tuple[int, object], item)
        minimum, maximum = getattr(info, "min", None), getattr(info, "max", None)
        if isinstance(minimum, int) and isinstance(maximum, int) and minimum < maximum:
            ranges[int(code)] = (minimum, maximum)
    return ranges


@dataclass
class DefaultControllerRoute:
    output_id: str
    axis_ranges: dict[int, tuple[int, int]] = field(default_factory=dict)
    axes: dict[int, GamepadOutputTarget] = field(default_factory=dict)

    def target(self, runtime: ActionRuntime) -> GamepadOutputTarget | None:
        target = runtime.resolve_gamepad_output(self.output_id, "default controller route")
        # Default routes only address virtual instances, never another source device.
        return (
            target
            if isinstance(target, GamepadOutputTarget)
            and target.is_virtual
            and target.output_id == self.output_id
            else None
        )

    def emit(self, runtime: ActionRuntime, event: InputEventLike, *, sync: bool) -> None:
        target = self.target(runtime)
        if target is None:
            return
        writer = identity_uinput_writer(target.uinput)
        if writer is None:
            return
        code, value = int(event.code), int(event.value)
        if event.type == evdev.ecodes.EV_KEY:
            if code not in target.button_codes:
                return
            held = runtime.state.held_output_keys.setdefault(target.bucket, set())
            if value == 1:
                held.add(code)
            elif value == 0:
                held.discard(code)
            writer.write(evdev.ecodes.EV_KEY, code, value)
        elif event.type == evdev.ecodes.EV_ABS:
            source_range = self.axis_ranges.get(code)
            target_range = target.axis_ranges.get(code)
            if source_range is None or target_range is None:
                return
            minimum, maximum = source_range
            if minimum >= maximum:
                return
            low, high = target_range
            fraction = (max(minimum, min(maximum, value)) - minimum) / (maximum - minimum)
            value = round(low + fraction * (high - low))
            writer.write(
                evdev.ecodes.EV_ABS,
                code,
                target.stick_output.write_base(runtime.hardware_id, code, value),
            )
            self.axes[code] = target
            runtime.state.held_output_abs.setdefault(target.bucket, set()).add(code)
        else:
            return
        if sync:
            writer.syn()
        else:
            runtime.state.passthrough_frame_output = target.uinput

    def release_axes(self, runtime: ActionRuntime, codes: set[int] | None = None) -> None:
        for code, target in list(self.axes.items()):
            if codes is not None and code not in codes:
                continue
            writer = identity_uinput_writer(target.uinput)
            if writer is not None:
                neutral = target.axis_rest_values.get(code, 0)
                writer.write(
                    evdev.ecodes.EV_ABS,
                    code,
                    target.stick_output.write_base(runtime.hardware_id, code, neutral),
                )
                writer.syn()
            runtime.state.held_output_abs.get(target.bucket, set()).discard(code)
            if not runtime.state.held_output_abs.get(target.bucket):
                runtime.state.held_output_abs.pop(target.bucket, None)
            self.axes.pop(code, None)


def controller_route(runtime: object) -> DefaultControllerRoute | None:
    route = getattr(runtime, "default_route", None)
    return route if isinstance(route, DefaultControllerRoute) else None
