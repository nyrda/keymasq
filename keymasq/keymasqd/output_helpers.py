from __future__ import annotations

from typing import Protocol, cast

import evdev

from keymasq.common.devices import resolve_evdev_event_type


class _WritableUInput(Protocol):
    def write(self, event_type: int, code: int, value: int) -> None: ...

    def syn(self) -> None: ...


def _ecode_value(name: str) -> int | None:
    if not hasattr(evdev.ecodes, name):
        return None
    code = getattr(evdev.ecodes, name)
    if isinstance(code, tuple):
        tuple_code = cast(tuple[object, ...], code)
        first = tuple_code[0] if tuple_code else None
        return first if isinstance(first, int) else None
    return int(code)


def resolve_output_code(target: str | None) -> int | None:
    if not target:
        return None

    key_lower = str(target).split(":", 1)[0].lower()

    upper_code = _ecode_value(key_lower.upper())
    if upper_code is not None:
        return upper_code

    lower_code = _ecode_value(key_lower)
    if lower_code is not None:
        return lower_code

    return None


def parse_mouse_output_target(target: str | None) -> tuple[int | None, int | None, int]:
    if not target:
        return (None, None, 0)

    raw_target = str(target).strip().lower()
    if not raw_target:
        return (None, None, 0)

    base_target = raw_target
    relative_value = 0
    if ":" in raw_target:
        base_target, raw_value = raw_target.split(":", 1)
        try:
            relative_value = int(raw_value)
        except ValueError:
            relative_value = 0

    event_type = resolve_evdev_event_type(base_target)
    code = resolve_output_code(base_target)
    if event_type == evdev.ecodes.EV_REL and relative_value == 0:
        relative_value = 1
    return (event_type, code, relative_value)


def get_trigger_axis(target: str | None) -> tuple[bool, int | None]:
    if not target:
        return (False, None)

    target_lower = target.lower()
    if target_lower in ("btn_tl2", "btn_lt"):
        return (True, evdev.ecodes.ABS_Z)
    if target_lower in ("btn_tr2", "btn_rt"):
        return (True, evdev.ecodes.ABS_RZ)
    return (False, None)


def emit_mouse_move(
    uinput_dev: _WritableUInput | None,
    move_x: int,
    move_y: int,
    *,
    absolute: bool = False,
) -> None:
    if not uinput_dev:
        return

    try:
        if absolute:
            uinput_dev.write(evdev.ecodes.EV_REL, evdev.ecodes.REL_X, -2147483648)
            uinput_dev.write(evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, -2147483648)
            uinput_dev.syn()

        uinput_dev.write(evdev.ecodes.EV_REL, evdev.ecodes.REL_X, int(move_x))
        uinput_dev.write(evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, int(move_y))
        uinput_dev.syn()
    except Exception:
        pass
