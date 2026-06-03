from __future__ import annotations

import logging
from typing import cast

import evdev

from keymasq.common.devices import resolve_evdev_event_type
from keymasq.common.gamepad_axes import gamepad_axis_range, normalize_gamepad_axis_target
from keymasq.keymasqd.runtime.adapters import WritableUInput

log = logging.getLogger("keymasqd.output_helpers")


def _ecode_value(name: str) -> int | None:
    if not hasattr(evdev.ecodes, name):
        return None
    code = getattr(evdev.ecodes, name)
    if isinstance(code, tuple):
        tuple_code = cast(tuple[object, ...], code)
        first = tuple_code[0] if tuple_code else None
        return first if isinstance(first, int) else None
    return code if isinstance(code, int) else None


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


def resolve_gamepad_axis_code(target: str | None) -> int | None:
    if not target:
        return None

    target_lower = target.strip().lower()
    axis_range = gamepad_axis_range(normalize_gamepad_axis_target(target_lower))
    if axis_range is not None:
        return _ecode_value(axis_range.evdev_name)
    # Advanced: resolve any custom ABS axis code outside the standard template.
    if target_lower.startswith("abs_"):
        return _ecode_value(target_lower.upper())
    return None


def emit_mouse_move(
    uinput_dev: WritableUInput | None,
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
        log.debug("Failed to emit mouse movement", exc_info=True)
