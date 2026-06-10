#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import signal
import time
from pathlib import Path
from typing import Any

from evdev import AbsInfo, UInput, ecodes

Json = dict[str, Any]

STANDARD_KEYBOARD_KEYS = (
    "KEY_ESC",
    "KEY_GRAVE",
    "KEY_1",
    "KEY_2",
    "KEY_3",
    "KEY_4",
    "KEY_5",
    "KEY_6",
    "KEY_7",
    "KEY_8",
    "KEY_9",
    "KEY_0",
    "KEY_MINUS",
    "KEY_EQUAL",
    "KEY_BACKSPACE",
    "KEY_TAB",
    "KEY_Q",
    "KEY_W",
    "KEY_E",
    "KEY_R",
    "KEY_T",
    "KEY_Y",
    "KEY_U",
    "KEY_I",
    "KEY_O",
    "KEY_P",
    "KEY_LEFTBRACE",
    "KEY_RIGHTBRACE",
    "KEY_BACKSLASH",
    "KEY_CAPSLOCK",
    "KEY_A",
    "KEY_S",
    "KEY_D",
    "KEY_F",
    "KEY_G",
    "KEY_H",
    "KEY_J",
    "KEY_K",
    "KEY_L",
    "KEY_SEMICOLON",
    "KEY_APOSTROPHE",
    "KEY_ENTER",
    "KEY_LEFTSHIFT",
    "KEY_Z",
    "KEY_X",
    "KEY_C",
    "KEY_V",
    "KEY_B",
    "KEY_N",
    "KEY_M",
    "KEY_COMMA",
    "KEY_DOT",
    "KEY_SLASH",
    "KEY_RIGHTSHIFT",
    "KEY_LEFTCTRL",
    "KEY_LEFTALT",
    "KEY_LEFTMETA",
    "KEY_SPACE",
    "KEY_RIGHTALT",
    "KEY_RIGHTCTRL",
    "KEY_RIGHTMETA",
    "KEY_SYSRQ",
    "KEY_SCROLLLOCK",
    "KEY_PAUSE",
    "KEY_INSERT",
    "KEY_HOME",
    "KEY_PAGEUP",
    "KEY_DELETE",
    "KEY_END",
    "KEY_PAGEDOWN",
    "KEY_UP",
    "KEY_LEFT",
    "KEY_DOWN",
    "KEY_RIGHT",
    "KEY_F1",
    "KEY_F2",
    "KEY_F3",
    "KEY_F4",
    "KEY_F5",
    "KEY_F6",
    "KEY_F7",
    "KEY_F8",
    "KEY_F9",
    "KEY_F10",
    "KEY_F11",
    "KEY_F12",
    "KEY_NUMLOCK",
    "KEY_KPSLASH",
    "KEY_KPASTERISK",
    "KEY_KPMINUS",
    "KEY_KP7",
    "KEY_KP8",
    "KEY_KP9",
    "KEY_KPPLUS",
    "KEY_KP4",
    "KEY_KP5",
    "KEY_KP6",
    "KEY_KP1",
    "KEY_KP2",
    "KEY_KP3",
    "KEY_KPENTER",
    "KEY_KP0",
    "KEY_KPDOT",
)


def _abs(value: int, minimum: int, maximum: int, *, flat: int = 0) -> AbsInfo:
    return AbsInfo(
        value=value,
        min=minimum,
        max=maximum,
        fuzz=0,
        flat=flat,
        resolution=0,
    )


def _path(device: UInput) -> str:
    input_device = device.device
    return str(getattr(input_device, "path", None) or getattr(input_device, "fn", ""))


def _record(device: UInput, *, key: str, vendor: int, product: int) -> Json:
    path = _path(device)
    if not path:
        raise RuntimeError(f"uinput device {key!r} did not expose an event path")
    return {
        "name": device.name,
        "path": path,
        "vendor_id": f"{vendor:04x}",
        "product_id": f"{product:04x}",
    }


def _keyboard_caps(keys: list[int]) -> dict[int, list[int]]:
    return {
        ecodes.EV_KEY: keys,
        ecodes.EV_MSC: [ecodes.MSC_SCAN],
    }


def _keyboard_codes(key_names: tuple[str, ...]) -> list[int]:
    return [int(getattr(ecodes, key_name)) for key_name in key_names if hasattr(ecodes, key_name)]


def _create_devices() -> tuple[dict[str, Json], list[UInput]]:
    dygma_vendor = 0x35EF
    dygma_product = 0x0021
    razer_vendor = 0x1532
    razer_product = 0x00B4
    xbox_vendor = 0x045E
    xbox_product = 0x02A1

    handles: list[UInput] = []
    devices: dict[str, Json] = {}

    dygma = UInput(
        _keyboard_caps(_keyboard_codes(STANDARD_KEYBOARD_KEYS)),
        name="DYGMA RAISE2 Keyboard",
        vendor=dygma_vendor,
        product=dygma_product,
    )
    handles.append(dygma)
    devices["dygma_keyboard"] = _record(
        dygma,
        key="dygma_keyboard",
        vendor=dygma_vendor,
        product=dygma_product,
    )

    razer_mouse = UInput(
        {
            ecodes.EV_KEY: [
                ecodes.BTN_LEFT,
                ecodes.BTN_RIGHT,
                ecodes.BTN_MIDDLE,
                ecodes.BTN_SIDE,
                ecodes.BTN_EXTRA,
            ],
            ecodes.EV_REL: [
                ecodes.REL_X,
                ecodes.REL_Y,
                ecodes.REL_WHEEL,
                ecodes.REL_HWHEEL,
            ],
        },
        name="Razer Naga V2 HyperSpeed Mouse",
        vendor=razer_vendor,
        product=razer_product,
    )
    handles.append(razer_mouse)
    devices["razer_mouse"] = _record(
        razer_mouse,
        key="razer_mouse",
        vendor=razer_vendor,
        product=razer_product,
    )

    razer_keys = UInput(
        _keyboard_caps(
            [
                ecodes.KEY_F5,
                ecodes.KEY_F6,
                ecodes.KEY_F7,
                ecodes.KEY_F8,
                ecodes.KEY_F9,
                ecodes.KEY_F10,
                ecodes.KEY_7,
                ecodes.KEY_8,
                ecodes.KEY_9,
                ecodes.KEY_0,
                ecodes.KEY_MINUS,
                ecodes.KEY_EQUAL,
                ecodes.KEY_LEFTBRACE,
                ecodes.KEY_RIGHTBRACE,
            ]
        ),
        name="Razer Naga V2 HyperSpeed Buttons",
        vendor=razer_vendor,
        product=razer_product,
    )
    handles.append(razer_keys)
    devices["razer_keys"] = _record(
        razer_keys,
        key="razer_keys",
        vendor=razer_vendor,
        product=razer_product,
    )

    xbox = UInput(
        {
            ecodes.EV_KEY: [
                ecodes.BTN_TL,
                ecodes.BTN_TR,
                ecodes.BTN_SELECT,
                ecodes.BTN_MODE,
                ecodes.BTN_START,
                ecodes.BTN_NORTH,
                ecodes.BTN_WEST,
                ecodes.BTN_EAST,
                ecodes.BTN_SOUTH,
                ecodes.BTN_THUMBL,
                ecodes.BTN_THUMBR,
                ecodes.BTN_DPAD_UP,
                ecodes.BTN_DPAD_LEFT,
                ecodes.BTN_DPAD_RIGHT,
                ecodes.BTN_DPAD_DOWN,
            ],
            ecodes.EV_ABS: [
                (ecodes.ABS_X, _abs(0, -32768, 32767, flat=128)),
                (ecodes.ABS_Y, _abs(0, -32768, 32767, flat=128)),
                (ecodes.ABS_RX, _abs(0, -32768, 32767, flat=128)),
                (ecodes.ABS_RY, _abs(0, -32768, 32767, flat=128)),
                (ecodes.ABS_Z, _abs(0, 0, 255, flat=4)),
                (ecodes.ABS_RZ, _abs(0, 0, 255, flat=4)),
                (ecodes.ABS_HAT0X, _abs(0, -1, 1)),
                (ecodes.ABS_HAT0Y, _abs(0, -1, 1)),
            ],
        },
        name="Xbox 360 1",
        vendor=xbox_vendor,
        product=xbox_product,
    )
    handles.append(xbox)
    devices["xbox_gamepad"] = _record(
        xbox,
        key="xbox_gamepad",
        vendor=xbox_vendor,
        product=xbox_product,
    )

    return devices, handles


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="/run/keymasq-docshots/devices.json", type=Path)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    devices, handles = _create_devices()
    args.output.write_text(json.dumps(devices, indent=2, sort_keys=True) + "\n")

    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    while running:
        time.sleep(1)

    for handle in handles:
        handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
