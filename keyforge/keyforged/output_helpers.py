from __future__ import annotations

import evdev


def resolve_output_code(target: str | None) -> int | None:
    if not target:
        return None

    key_lower = target.lower()

    if hasattr(evdev.ecodes, key_lower.upper()):
        code = getattr(evdev.ecodes, key_lower.upper())
        if isinstance(code, tuple):
            return code[0] if code else None
        return int(code)

    if hasattr(evdev.ecodes, key_lower):
        code = getattr(evdev.ecodes, key_lower)
        if isinstance(code, tuple):
            return code[0] if code else None
        return int(code)

    return None


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
    uinput_dev: evdev.UInput | None,
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
