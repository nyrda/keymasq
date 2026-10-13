import evdev

HIGH_RES_WHEEL_CODES = frozenset(
    int(code)
    for code in (
        getattr(evdev.ecodes, "REL_WHEEL_HI_RES", None),
        getattr(evdev.ecodes, "REL_HWHEEL_HI_RES", None),
    )
    if code is not None
)


def is_wheel_rel_code(code: int, *, rel_wheel: int, rel_hwheel: int) -> bool:
    return code == rel_wheel or code == rel_hwheel or code in HIGH_RES_WHEEL_CODES
