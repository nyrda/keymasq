#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pwd
import shutil
from pathlib import Path
from typing import Any

import evdev
import tomli_w

from keymasq.common.models import (
    DEFAULT_NATURAL_MOUSE_MOVE_MAX_DURATION_MS,
    DEFAULT_NATURAL_MOUSE_MOVE_TOLERANCE,
)
from keymasq.keymasqd.macro_store import MacroStore
from keymasq.keymasqd.recording import (
    START_MOUSE_MOVE_CURVE,
    START_MOUSE_MOVE_JITTER,
    START_MOUSE_MOVE_SPEED,
)

Json = dict[str, Any]

CREATED_AT = "2026-01-01T00:00:00"

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


def _load_devices(path: Path) -> Json:
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"docshot device map is not an object: {path}")
    return raw


def _device_path(devices: Json, key: str) -> str:
    raw = devices.get(key)
    if not isinstance(raw, dict):
        raise KeyError(f"missing docshot device {key!r}")
    path = str(raw.get("path", "") or "")
    if not path:
        raise ValueError(f"docshot device {key!r} has no path")
    return path


def _write_toml(path: Path, data: Json) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(data), encoding="utf-8")


def _action(action: str, **fields: Any) -> Json:
    return {"action": action, **{k: v for k, v in fields.items() if v is not None}}


def _hyprland_dispatch(dispatcher: str) -> Json:
    return _action("compositor_dispatch", compositor="hyprland", dispatcher=dispatcher)


def _hyprland_workspace(index: int) -> Json:
    return _hyprland_dispatch(f'hl.dsp.focus({{ workspace = "{index}" }})')


def _keyboard_label_from_evdev(key_name: str) -> str:
    token = key_name[4:] if key_name.startswith("KEY_") else key_name
    token = token.replace("LEFT", "Left ").replace("RIGHT", "Right ")
    token = token.replace("CTRL", "Ctrl").replace("ALT", "Alt")
    token = token.replace("META", "Meta").replace("SHIFT", "Shift")
    token = token.replace("PAGEUP", "Page Up").replace("PAGEDOWN", "Page Down")
    token = token.replace("NUMLOCK", "Num Lock")
    return token.replace("_", " ").strip().title()


def _standard_keyboard_buttons(source_id: str) -> list[Json]:
    buttons: list[Json] = []
    for key_name in STANDARD_KEYBOARD_KEYS:
        if not hasattr(evdev.ecodes, key_name):
            continue
        evdev_name = key_name.lower()
        buttons.append(
            {
                "id": evdev_name,
                "label": _keyboard_label_from_evdev(key_name),
                "evdev": evdev_name,
                "source": source_id,
                "type": "key",
            }
        )
    return buttons


def _combo_event(evdev: str, hardware_id: str, source: str) -> Json:
    return {"evdev": evdev, "hardware_id": hardware_id, "source": source}


def _macro_key_event(t_us: int, key: str, value: int) -> Json:
    return {
        "t_us": t_us,
        "device_type": "keyboard",
        "type": evdev.ecodes.EV_KEY,
        "code": int(getattr(evdev.ecodes, key)),
        "value": value,
    }


def _macro_wait_event(t_us: int, duration_us: int) -> Json:
    return {
        "t_us": t_us,
        "device_type": "macro",
        "type": 0,
        "code": 0,
        "value": 0,
        "macro_action": "wait",
        "duration_us": duration_us,
    }


def _macro_wait_random_event(t_us: int, minimum_us: int, maximum_us: int) -> Json:
    return {
        "t_us": t_us,
        "device_type": "macro",
        "type": 0,
        "code": 0,
        "value": 0,
        "macro_action": "wait_random",
        "min_us": minimum_us,
        "max_us": maximum_us,
    }


def _macro_exec_event(t_us: int, cmd: str, *, wait: bool = True) -> Json:
    return {
        "t_us": t_us,
        "device_type": "macro",
        "type": 0,
        "code": 0,
        "value": 0,
        "macro_action": "exec_sync" if wait else "exec_async",
        "command": cmd,
        "timeout_ms": 3000,
    }


def _macro_mouse_event(t_us: int, code: str, value: int) -> Json:
    return {
        "t_us": t_us,
        "device_type": "mouse",
        "type": evdev.ecodes.EV_KEY,
        "code": int(getattr(evdev.ecodes, code)),
        "value": value,
    }


def _macro_start_mouse_move_event(t_us: int, x: int, y: int) -> Json:
    return {
        "t_us": t_us,
        "device_type": "macro",
        "type": 0,
        "code": 0,
        "value": 0,
        "macro_action": "mouse_move_natural_abs",
        "x": x,
        "y": y,
        "speed": START_MOUSE_MOVE_SPEED,
        "jitter": START_MOUSE_MOVE_JITTER,
        "curve": START_MOUSE_MOVE_CURVE,
        "tolerance": DEFAULT_NATURAL_MOUSE_MOVE_TOLERANCE,
        "max_duration_ms": DEFAULT_NATURAL_MOUSE_MOVE_MAX_DURATION_MS,
        "stop_on_failure": False,
    }


def _seed_hardware(config_dir: Path, devices: Json) -> None:
    hardware_dir = config_dir / "hardware"

    dygma_path = _device_path(devices, "dygma_keyboard")
    razer_mouse_path = _device_path(devices, "razer_mouse")
    razer_keys_path = _device_path(devices, "razer_keys")
    xbox_path = _device_path(devices, "xbox_gamepad")

    _write_toml(
        hardware_dir / "35ef_0021.toml",
        {
            "hardware": {
                "name": "DYGMA RAISE2 Keyboard",
                "vendor_id": "35ef",
                "product_id": "0021",
                "evdev": {
                    "devices": [
                        {
                            "path": dygma_path,
                            "type": "keyboard",
                            "id": "if02_kbd",
                            "capabilities": [
                                key_name.lower() for key_name in STANDARD_KEYBOARD_KEYS
                            ],
                        }
                    ]
                },
                "layout": {
                    "type": "keyboard",
                    "buttons": _standard_keyboard_buttons("if02_kbd"),
                },
            }
        },
    )

    _write_toml(
        hardware_dir / "1532_00b4.toml",
        {
            "hardware": {
                "name": "Razer Naga V2 HyperSpeed",
                "vendor_id": "1532",
                "product_id": "00b4",
                "evdev": {
                    "devices": [
                        {"path": razer_mouse_path, "type": "mouse", "id": "mouse"},
                        {"path": razer_keys_path, "type": "keyboard", "id": "if02"},
                    ]
                },
                "layout": {
                    "type": "mouse",
                    "buttons": [
                        {
                            "id": "btn_left",
                            "label": "Left Click",
                            "evdev": "btn_left",
                            "source": "mouse",
                            "type": "mouse",
                        },
                        {
                            "id": "btn_right",
                            "label": "Right Click",
                            "evdev": "btn_right",
                            "source": "mouse",
                            "type": "mouse",
                        },
                        {
                            "id": "btn_middle",
                            "label": "Middle Click",
                            "evdev": "btn_middle",
                            "source": "mouse",
                            "type": "mouse",
                        },
                        {
                            "id": "btn_side",
                            "label": "Back",
                            "evdev": "btn_side",
                            "source": "mouse",
                            "type": "mouse",
                        },
                        {
                            "id": "btn_extra",
                            "label": "Forward",
                            "evdev": "btn_extra",
                            "source": "mouse",
                            "type": "mouse",
                        },
                        {
                            "id": "wheel_up",
                            "label": "Scroll Up",
                            "evdev": "rel_wheel",
                            "evdev_value": 1,
                            "source": "mouse",
                            "type": "wheel",
                        },
                        {
                            "id": "wheel_down",
                            "label": "Scroll Down",
                            "evdev": "rel_wheel",
                            "evdev_value": -1,
                            "source": "mouse",
                            "type": "wheel",
                        },
                        *[
                            {
                                "id": f"extra_{index}",
                                "label": f"Extra Button {index}",
                                "evdev": evdev,
                                "source": "if02",
                                "type": "button",
                            }
                            for index, evdev in enumerate(
                                [
                                    "key_f5",
                                    "key_f6",
                                    "key_f7",
                                    "key_f8",
                                    "key_f9",
                                    "key_f10",
                                    "key_9",
                                    "key_0",
                                    "key_minus",
                                    "key_equal",
                                    "key_leftbrace",
                                    "key_rightbrace",
                                    "key_7",
                                    "key_8",
                                ],
                                start=1,
                            )
                        ],
                    ],
                },
            }
        },
    )

    gamepad_buttons = [
        ("btn_tl", "LB"),
        ("btn_tr", "RB"),
        ("btn_select", "Select"),
        ("btn_mode", "Guide"),
        ("btn_start", "Start"),
        ("btn_north", "X"),
        ("btn_west", "Y"),
        ("btn_east", "B"),
        ("btn_south", "A"),
        ("btn_thumbl", "LS"),
        ("btn_thumbr", "RS"),
        ("btn_dpad_up", "D-Up"),
        ("btn_dpad_left", "D-Left"),
        ("btn_dpad_right", "D-Right"),
        ("btn_dpad_down", "D-Down"),
    ]
    _write_toml(
        hardware_dir / "045e_02a1.toml",
        {
            "hardware": {
                "name": "Xbox 360 1",
                "vendor_id": "045e",
                "product_id": "02a1",
                "evdev": {
                    "devices": [
                        {
                            "path": xbox_path,
                            "type": "gamepad",
                            "id": "joystick",
                            "capabilities": [
                                *(button for button, _label in gamepad_buttons),
                                "abs_x",
                                "abs_y",
                                "abs_rx",
                                "abs_ry",
                                "abs_z",
                                "abs_rz",
                                "abs_hat0x",
                                "abs_hat0y",
                            ],
                        }
                    ]
                },
                "layout": {
                    "type": "gamepad",
                    "buttons": [
                        {
                            "id": button,
                            "label": label,
                            "evdev": button,
                            "source": "joystick",
                            "type": "gamepad",
                        }
                        for button, label in gamepad_buttons
                    ],
                    "analogs": [
                        {
                            "id": "left_stick",
                            "label": "Left Stick",
                            "type": "stick",
                            "source": "joystick",
                            "axes": [
                                {"role": "x", "evdev": "abs_x"},
                                {"role": "y", "evdev": "abs_y"},
                            ],
                        },
                        {
                            "id": "right_stick",
                            "label": "Right Stick",
                            "type": "stick",
                            "source": "joystick",
                            "axes": [
                                {"role": "x", "evdev": "abs_rx"},
                                {"role": "y", "evdev": "abs_ry"},
                            ],
                        },
                        {
                            "id": "left_trigger",
                            "label": "Left Trigger",
                            "type": "axis",
                            "source": "joystick",
                            "axes": [{"role": "value", "evdev": "abs_z"}],
                        },
                    ],
                },
            }
        },
    )


def _seed_profiles(config_dir: Path) -> None:
    profiles_dir = config_dir / "profiles"

    desktop = {
        "profile": {
            "name": "Desktop",
            "enabled": True,
            "is_permanent": True,
            "priority": 3,
            "notify_on_activation": False,
            "created_at": CREATED_AT,
        },
        "devices": {
            "35ef:0021": {
                "always_grab_all": True,
                "mapping": {
                    "key_capslock": _action("keyboard", target="key_esc"),
                    "key_leftalt": _action("superkey", superkey_name="navlayer"),
                },
            },
            "1532:00b4": {
                "always_grab_all": True,
                "mapping": {
                    "extra_1": _hyprland_workspace(1),
                    "extra_2": _hyprland_workspace(2),
                    "extra_3": _hyprland_workspace(3),
                    "extra_4": _hyprland_workspace(4),
                    "extra_5": _hyprland_workspace(5),
                    "extra_6": _hyprland_workspace(6),
                    "extra_7": _hyprland_dispatch(
                        'hl.dsp.window.float({ action = "toggle" })'
                    ),
                    "extra_8": _hyprland_dispatch(
                        'hl.dsp.window.pin({ action = "toggle" })'
                    ),
                    "extra_9": _action("start_macro_recording"),
                    "extra_10": _action("exec", cmd="grimblast --freeze copy area"),
                    "extra_11": _action("superkey", superkey_name="wpctl_volume_rocker"),
                    "extra_12": _hyprland_dispatch("hl.dsp.window.center()"),
                    "extra_13": _action("superkey", superkey_name="paste"),
                    "extra_14": _action("superkey", superkey_name="copy"),
                },
            },
            "045e:02a1": {
                "always_grab_all": False,
                "mapping": {
                    "btn_south": _action("keyboard", target="key_space"),
                    "left_stick": _action("analog_control", analog_control_name="Mouse Area"),
                    "right_stick": _action("analog_control", analog_control_name="Scroll Wheel"),
                },
            },
        },
        "combos": [
            {
                "id": "aa72ab0b",
                "name": "Move mouse button workspace",
                "steps": [
                    {
                        "events": [
                            _combo_event("key_f5", "1532:00b4", "if02"),
                            _combo_event("meta", "35ef:0021", "if02_kbd"),
                        ]
                    }
                ],
                "action": _action(
                    "compositor_dispatch",
                    compositor="hyprland",
                    dispatcher='hl.dsp.window.move({ workspace = "1", follow = true })',
                ),
            },
            {
                "id": "d6cd2116",
                "name": "Alt+C copy for terminals",
                "recall_trigger_keys": True,
                "steps": [
                    {
                        "events": [
                            _combo_event("alt", "35ef:0021", "if02_kbd"),
                            _combo_event("key_c", "35ef:0021", "if02_kbd"),
                        ]
                    }
                ],
                "action": _action("superkey", superkey_name="copy_ctrl_shift_c"),
            },
            {
                "id": "beaf3549",
                "name": "Alt+V paste for terminals",
                "recall_trigger_keys": True,
                "steps": [
                    {
                        "events": [
                            _combo_event("alt", "35ef:0021", "if02_kbd"),
                            _combo_event("key_v", "35ef:0021", "if02_kbd"),
                        ]
                    }
                ],
                "action": _action("superkey", superkey_name="paste_ctrl_shift_v"),
            },
        ],
    }
    _write_toml(profiles_dir / "Desktop.toml", desktop)

    macro_demo = {
        "profile": {
            "name": "Macro Demo",
            "enabled": True,
            "is_permanent": True,
            "priority": 2,
            "notify_on_activation": False,
            "created_at": CREATED_AT,
        },
        "devices": {
            "1532:00b4": {
                "always_grab_all": True,
                "mapping": {
                    "extra_9": _action("macro", macro_name="volume_up"),
                },
            },
        },
    }
    _write_toml(profiles_dir / "Macro_Demo.toml", macro_demo)

    navigation = {
        "profile": {
            "name": "navigation",
            "enabled": False,
            "is_permanent": True,
            "priority": 102,
            "notify_on_activation": False,
            "created_at": CREATED_AT,
        },
        "devices": {
            "35ef:0021": {
                "always_grab_all": False,
                "mapping": {
                    "key_w": _action("keyboard", target="key_up"),
                    "key_s": _action("keyboard", target="key_down"),
                    "key_d": _action("keyboard", target="key_right"),
                    "key_a": _action("keyboard", target="key_left"),
                    "key_q": _action("keyboard", target="key_home"),
                    "key_e": _action("keyboard", target="key_end"),
                    "key_z": _action("mouse", target="btn_side"),
                    "key_c": _action("mouse", target="btn_extra"),
                    "key_r": _action(
                        "mouse",
                        target="rel_wheel:1",
                        rapidfire_enabled=True,
                        rapidfire_hold_ms=20,
                        rapidfire_wait_ms=20,
                    ),
                    "key_f": _action(
                        "mouse",
                        target="rel_wheel:-1",
                        rapidfire_enabled=True,
                        rapidfire_hold_ms=20,
                        rapidfire_wait_ms=20,
                    ),
                },
            }
        },
    }
    _write_toml(profiles_dir / "navigation.toml", navigation)

    _write_toml(
        profiles_dir / "Default.toml",
        {
            "profile": {
                "name": "Default",
                "enabled": True,
                "is_permanent": True,
                "priority": 1,
                "notify_on_activation": False,
                "created_at": CREATED_AT,
            },
            "devices": {
                "35ef:0021": {"always_grab_all": False, "mapping": {}},
                "1532:00b4": {
                    "always_grab_all": False,
                    "mapping": {
                        "extra_10": _action("exec", cmd="grimblast --freeze copy area"),
                    },
                },
                "045e:02a1": {
                    "always_grab_all": False,
                    "mapping": {
                        "btn_south": _action("keyboard", target="key_space"),
                    },
                },
            },
        },
    )


def _seed_superkeys(config_dir: Path) -> None:
    superkeys_dir = config_dir / "superkeys"
    superkeys_dir.mkdir(parents=True, exist_ok=True)
    superkeys = {
        "wpctl_volume_rocker.toml": {
            "name": "wpctl_volume_rocker",
            "mode": "pattern",
            "timing": {
                "tap_timeout_ms": 150,
                "double_tap_window_ms": 250,
                "hold_threshold_ms": 250,
            },
            "actions": {
                "tap": [_action("macro", target="volume_down", macro_name="volume_down")],
                "double_tap": [_action("macro", target="volume_up", macro_name="volume_up")],
                "hold": [_action("macro", target="volume_down", macro_name="volume_down")],
                "tap_hold": [_action("macro", target="volume_up", macro_name="volume_up")],
            },
        },
        "navlayer.toml": {
            "name": "navlayer",
            "mode": "pattern",
            "actions": {
                "hold": [
                    {
                        **_action("profile_enable", target="navigation", profile_name="navigation"),
                        "deactivation": {"on_trigger_end": True},
                    }
                ]
            },
        },
        "copy_ctrl_cc.toml": {
            "name": "copy_ctrl_cc",
            "mode": "overload",
            "actions": {
                "overload": [
                    _action("keyboard", target="key_leftctrl"),
                    _action("keyboard", target="key_c"),
                ]
            },
        },
        "copy_ctrl_shift_c.toml": {
            "name": "copy_ctrl_shift_c",
            "mode": "overload",
            "actions": {
                "overload": [
                    _action("keyboard", target="key_leftctrl"),
                    _action("keyboard", target="key_leftshift"),
                    _action("keyboard", target="key_c"),
                ]
            },
        },
        "copy.toml": {
            "name": "copy",
            "mode": "overload",
            "actions": {
                "overload": [
                    _action("keyboard", target="key_leftctrl"),
                    _action("keyboard", target="key_leftshift"),
                    _action("keyboard", target="key_c"),
                ]
            },
        },
        "paste_ctrl_v.toml": {
            "name": "paste_ctrl_v",
            "mode": "overload",
            "actions": {
                "overload": [
                    _action("keyboard", target="key_leftctrl"),
                    _action("keyboard", target="key_v"),
                ]
            },
        },
        "paste_ctrl_shift_v.toml": {
            "name": "paste_ctrl_shift_v",
            "mode": "overload",
            "actions": {
                "overload": [
                    _action("keyboard", target="key_leftctrl"),
                    _action("keyboard", target="key_leftshift"),
                    _action("keyboard", target="key_v"),
                ]
            },
        },
        "paste.toml": {
            "name": "paste",
            "mode": "overload",
            "actions": {
                "overload": [
                    _action("keyboard", target="key_leftctrl"),
                    _action("keyboard", target="key_leftshift"),
                    _action("keyboard", target="key_v"),
                ]
            },
        },
    }
    for name, data in superkeys.items():
        _write_toml(superkeys_dir / name, data)


def _seed_analog_controls(config_dir: Path) -> None:
    analog_dir = config_dir / "analog_controls"
    controls = {
        "mouse_area.toml": {
            "name": "Mouse Area",
            "description": "Map the stick position to a cursor area",
            "input_type": "stick",
            "mouse_motion": {
                "enabled": True,
                "mode": "area",
                "area_radius_x": 420.0,
                "area_radius_y": 280.0,
                "deadzone": 0.12,
                "sensitivity": 1.0,
                "response_curve": 1.0,
            },
            "gamepad_output": {
                "enabled": False,
                "deadzone": 0.0,
                "target": "same",
                "sensitivity": 1.0,
                "response_curve": 1.0,
            },
        },
        "scroll_wheel.toml": {
            "name": "Scroll Wheel",
            "description": "Scroll up/down and side-scroll with the stick",
            "input_type": "stick",
            "mouse_motion": {
                "enabled": False,
                "mode": "velocity",
                "speed": 900.0,
                "speed_x": 900.0,
                "speed_y": 900.0,
                "area_radius_x": 400.0,
                "area_radius_y": 400.0,
                "area_start_enabled": False,
                "area_start_x": 0,
                "area_start_y": 0,
                "deadzone": 0.15,
                "sensitivity": 1.0,
                "response_curve": 1.0,
                "direction": "right",
                "invert_x": False,
                "invert_y": False,
                "tick_ms": 8,
            },
            "gamepad_output": {
                "enabled": False,
                "deadzone": 0.0,
                "target": "same",
                "sensitivity": 1.0,
                "response_curve": 1.0,
            },
            "thresholds": [
                {
                    "axis": "y",
                    "trigger_min": -1.0,
                    "trigger_max": -0.55,
                    "release_min": -1.0,
                    "release_max": -0.45,
                    "actions": [
                        {
                            "action": "mouse",
                            "target": "rel_wheel:1",
                            "rapidfire_enabled": True,
                            "rapidfire_hold_ms": 20,
                            "rapidfire_wait_ms": 60,
                        }
                    ],
                },
                {
                    "axis": "y",
                    "trigger_min": 0.55,
                    "trigger_max": 1.0,
                    "release_min": 0.45,
                    "release_max": 1.0,
                    "actions": [
                        {
                            "action": "mouse",
                            "target": "rel_wheel:-1",
                            "rapidfire_enabled": True,
                            "rapidfire_hold_ms": 20,
                            "rapidfire_wait_ms": 60,
                        }
                    ],
                },
                {
                    "axis": "x",
                    "trigger_min": -1.0,
                    "trigger_max": -0.55,
                    "release_min": -1.0,
                    "release_max": -0.45,
                    "actions": [
                        {
                            "action": "mouse",
                            "target": "rel_hwheel:-1",
                            "rapidfire_enabled": True,
                            "rapidfire_hold_ms": 20,
                            "rapidfire_wait_ms": 60,
                        }
                    ],
                },
                {
                    "axis": "x",
                    "trigger_min": 0.55,
                    "trigger_max": 1.0,
                    "release_min": 0.45,
                    "release_max": 1.0,
                    "actions": [
                        {
                            "action": "mouse",
                            "target": "rel_hwheel:1",
                            "rapidfire_enabled": True,
                            "rapidfire_hold_ms": 20,
                            "rapidfire_wait_ms": 60,
                        }
                    ],
                },
            ],
        },
        "wasd.toml": {
            "name": "WASD",
            "description": "Map the stick to W/A/S/D",
            "input_type": "stick",
            "thresholds": [
                {
                    "axis": "y",
                    "trigger_min": -1.0,
                    "trigger_max": -0.65,
                    "release_min": -1.0,
                    "release_max": -0.55,
                    "actions": [_action("keyboard", target="key_w")],
                },
                {
                    "axis": "y",
                    "trigger_min": 0.65,
                    "trigger_max": 1.0,
                    "release_min": 0.55,
                    "release_max": 1.0,
                    "actions": [_action("keyboard", target="key_s")],
                },
                {
                    "axis": "x",
                    "trigger_min": -1.0,
                    "trigger_max": -0.65,
                    "release_min": -1.0,
                    "release_max": -0.55,
                    "actions": [_action("keyboard", target="key_a")],
                },
                {
                    "axis": "x",
                    "trigger_min": 0.65,
                    "trigger_max": 1.0,
                    "release_min": 0.55,
                    "release_max": 1.0,
                    "actions": [_action("keyboard", target="key_d")],
                },
            ],
        },
    }
    for name, data in controls.items():
        _write_toml(analog_dir / name, data)


def _seed_settings(config_dir: Path) -> None:
    _write_toml(config_dir / "settings.toml", {"gamepads": {"virtual_count": 1}})
    _write_toml(
        config_dir / "gui_settings.toml",
        {
            "appearance": "dark",
            "selected_tab": "device:1532:00b4",
            "tab_order": [
                "device:1532:00b4",
                "device:35ef:0021",
                "device:045e:02a1",
                "combos",
            ],
            "hidden_tabs": [],
        },
    )
    _write_toml(
        config_dir / "recording_settings.toml",
        {
            "include_mouse_movement": True,
            "include_mouse_clicks": True,
            "record_start_position": True,
            "device_overrides": {},
        },
    )


def _seed_macros(state_dir: Path, *, owner: str | None) -> None:
    macro_dir = state_dir / "macros"
    if macro_dir.exists():
        shutil.rmtree(macro_dir)
    store = MacroStore(macro_dir)
    store.ensure()
    macros: list[Json] = [
        {
            "name": "volume_up",
            "created_at": CREATED_AT,
            "block_mouse_movement": False,
            "loop_mode": "none",
            "events": [
                _macro_exec_event(0, "wpctl set-volume @DEFAULT_AUDIO_SINK@ 5%+"),
                _macro_wait_event(20000, 30000),
            ],
        },
        {
            "name": "volume_down",
            "created_at": CREATED_AT,
            "block_mouse_movement": False,
            "loop_mode": "none",
            "events": [
                _macro_exec_event(0, "wpctl set-volume @DEFAULT_AUDIO_SINK@ 5%-"),
                _macro_wait_event(20000, 30000),
            ],
        },
        {
            "name": "open_docs_search",
            "created_at": CREATED_AT,
            "block_mouse_movement": True,
            "loop_mode": "none",
            "events": [
                _macro_start_mouse_move_event(0, 640, 360),
                _macro_key_event(0, "KEY_LEFTCTRL", 1),
                _macro_key_event(10000, "KEY_L", 1),
                _macro_key_event(70000, "KEY_L", 0),
                _macro_key_event(80000, "KEY_LEFTCTRL", 0),
                _macro_wait_random_event(90000, 35000, 120000),
                _macro_key_event(160000, "KEY_ENTER", 1),
                _macro_key_event(210000, "KEY_ENTER", 0),
            ],
        },
        {
            "name": "timing_tools_demo",
            "created_at": CREATED_AT,
            "block_mouse_movement": True,
            "loop_mode": "count",
            "loop_count": 3,
            "events": [
                _macro_start_mouse_move_event(0, 360, 240),
                _macro_key_event(0, "KEY_LEFTCTRL", 1),
                _macro_key_event(15000, "KEY_C", 1),
                _macro_key_event(80000, "KEY_C", 0),
                _macro_key_event(90000, "KEY_LEFTCTRL", 0),
                _macro_wait_event(120000, 220000),
                _macro_wait_random_event(360000, 80000, 180000),
                _macro_mouse_event(600000, "BTN_LEFT", 1),
                _macro_mouse_event(660000, "BTN_LEFT", 0),
            ],
        },
    ]
    for macro in macros:
        events = list(macro["events"])
        duration_us = max(int(event.get("t_us", 0) or 0) for event in events) if events else 0
        payload = dict(macro)
        payload["duration_us"] = duration_us
        store.create(payload)

    if owner:
        pw = pwd.getpwnam(owner)
        for path in [macro_dir, *macro_dir.rglob("*")]:
            os.chown(path, pw.pw_uid, pw.pw_gid)


def seed(
    config_home: Path,
    state_dir: Path,
    devices_json: Path,
    *,
    config_owner: str | None,
    state_owner: str | None,
) -> None:
    config_dir = config_home / "keymasq"
    if config_dir.exists():
        shutil.rmtree(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    devices = _load_devices(devices_json)
    _seed_hardware(config_dir, devices)
    _seed_profiles(config_dir)
    _seed_superkeys(config_dir)
    _seed_analog_controls(config_dir)
    _seed_settings(config_dir)
    _seed_macros(state_dir, owner=state_owner)

    if config_owner:
        pw = pwd.getpwnam(config_owner)
        for path in [config_dir, *config_dir.rglob("*")]:
            os.chown(path, pw.pw_uid, pw.pw_gid)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-home", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--devices-json", required=True, type=Path)
    parser.add_argument("--owner", default="")
    parser.add_argument("--config-owner", default="")
    parser.add_argument("--state-owner", default="")
    args = parser.parse_args()
    config_owner = args.config_owner or args.owner or None
    state_owner = args.state_owner or args.owner or None

    seed(
        config_home=args.config_home,
        state_dir=args.state_dir,
        devices_json=args.devices_json,
        config_owner=config_owner,
        state_owner=state_owner,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
