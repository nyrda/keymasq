"""Native input source backends and the private codes of their synthetic buttons."""

import evdev

HIDRAW_BACKEND = "hidraw"
HID_BPF_BACKEND = "hid-bpf"
NATIVE_BACKENDS = frozenset({HIDRAW_BACKEND, HID_BPF_BACKEND})

# Above KEY_MAX, so they never collide with kernel evdev codes or reach an output.
NATIVE_BUTTON_CODES: dict[str, int] = {
    "btn_touch_ls": evdev.ecodes.KEY_MAX + 1,
    "btn_touch_rs": evdev.ecodes.KEY_MAX + 2,
    "btn_touch_lp": evdev.ecodes.KEY_MAX + 3,
    "btn_touch_rp": evdev.ecodes.KEY_MAX + 4,
}
NATIVE_BUTTON_NAMES: dict[int, str] = {code: name for name, code in NATIVE_BUTTON_CODES.items()}
NATIVE_BUTTON_LABELS: dict[str, str] = {
    "btn_touch_ls": "LS Touch",
    "btn_touch_rs": "RS Touch",
    "btn_touch_lp": "LP Touch",
    "btn_touch_rp": "RP Touch",
}


def is_native_backend(value: object) -> bool:
    return isinstance(value, str) and value in NATIVE_BACKENDS


def native_button_name(code: object) -> str | None:
    return NATIVE_BUTTON_NAMES.get(code) if isinstance(code, int) else None


def native_button_code(name: object) -> int | None:
    return NATIVE_BUTTON_CODES.get(str(name).strip().lower()) if name else None


def is_native_button(name: object) -> bool:
    return native_button_code(name) is not None
