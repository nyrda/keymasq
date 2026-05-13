import os
from pathlib import Path
from types import SimpleNamespace

import evdev
import pytest

from keymasq.common.devices import (
    classify_event_device_type,
    clear_device_path_cache,
    detect_input_classes_from_capabilities,
    find_all_interfaces,
    get_interface_id,
    primary_input_class,
    resolve_stable_path,
)
from keymasq.common.models import DeviceType


def test_get_interface_id_appends_event_suffix_for_if_ids() -> None:
    assert (
        get_interface_id("/dev/input/by-id/usb-DYGMA_RAISE2_C66435BC0A2A8E8F-if02-event-mouse")
        == "if02_mouse"
    )
    assert (
        get_interface_id("/dev/input/by-id/usb-DYGMA_RAISE2_C66435BC0A2A8E8F-if02-event-kbd")
        == "if02_kbd"
    )


def test_get_interface_id_keeps_legacy_non_if_cases() -> None:
    assert get_interface_id("/dev/input/by-id/usb-Example-event-mouse") == "mouse"
    assert get_interface_id("/dev/input/by-id/usb-Example-event-kbd") == "kbd"


def test_get_interface_id_uses_event_path_name_when_no_by_id() -> None:
    assert get_interface_id("/dev/input/event7") == "event7"


def test_get_interface_id_handles_joystick_generic_event_and_default_cases() -> None:
    assert get_interface_id("/dev/input/by-id/usb-Example-event-joystick") == "joystick"
    assert get_interface_id("/dev/input/by-id/usb-Example-event-touchpad") == "event-touchpad"
    assert get_interface_id("/dev/input/by-id/usb-Example") == "default"


def test_resolve_stable_path_caches_by_event_path(monkeypatch: pytest.MonkeyPatch) -> None:
    by_id_dir = Path("/dev/input/by-id")
    symlink = by_id_dir / "usb-Example-event-kbd"
    counts = {"iterdir": 0, "readlink": 0}

    clear_device_path_cache()

    def fake_exists(self: Path) -> bool:
        return self == by_id_dir

    def fake_iterdir(self: Path):
        assert self == by_id_dir
        counts["iterdir"] += 1
        return iter([symlink])

    def fake_is_symlink(self: Path) -> bool:
        return self == symlink

    def fake_readlink(path: os.PathLike[str] | str) -> str:
        assert Path(path) == symlink
        counts["readlink"] += 1
        return "../event4"

    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setattr(Path, "iterdir", fake_iterdir)
    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)
    monkeypatch.setattr(os, "readlink", fake_readlink)

    assert resolve_stable_path("/dev/input/event4") == str(symlink)
    assert resolve_stable_path("/dev/input/event4") == str(symlink)
    assert counts == {"iterdir": 1, "readlink": 1}

    clear_device_path_cache()


def test_resolve_stable_path_returns_original_when_by_id_dir_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_device_path_cache()

    monkeypatch.setattr(Path, "exists", lambda self: False)

    assert resolve_stable_path("/dev/input/event9") == "/dev/input/event9"

    clear_device_path_cache()


def test_resolve_stable_path_skips_symlink_read_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    by_id_dir = Path("/dev/input/by-id")
    symlink = by_id_dir / "usb-Example-event-kbd"

    clear_device_path_cache()

    monkeypatch.setattr(Path, "exists", lambda self: self == by_id_dir)
    monkeypatch.setattr(Path, "iterdir", lambda self: iter([symlink]))
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == symlink)
    monkeypatch.setattr(os, "readlink", lambda _path: (_ for _ in ()).throw(OSError("bad link")))

    assert resolve_stable_path("/dev/input/event9") == "/dev/input/event9"

    clear_device_path_cache()


def test_find_all_interfaces_filters_matching_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    device_paths = ["/dev/input/event1", "/dev/input/event2", "/dev/input/event3"]

    class _FakeInputDevice:
        def __init__(self, path: str) -> None:
            if path == "/dev/input/event3":
                raise OSError("unreadable")
            if path == "/dev/input/event1":
                self.info = SimpleNamespace(vendor=0x1234, product=0x5678)
                self.name = "Main Keyboard"
            else:
                self.info = SimpleNamespace(vendor=0x9999, product=0x5678)
                self.name = "Other Device"

    monkeypatch.setattr("evdev.list_devices", lambda: device_paths)
    monkeypatch.setattr("evdev.InputDevice", _FakeInputDevice)
    monkeypatch.setattr(
        "keymasq.common.devices.resolve_stable_path",
        lambda path: f"/dev/input/by-id/{Path(path).name}",
    )
    monkeypatch.setattr(
        "keymasq.common.devices.get_interface_id",
        lambda stable_path: f"id-{Path(stable_path).name}",
    )

    interfaces = find_all_interfaces("1234", "5678")

    assert interfaces == [
        {
            "path": "/dev/input/event1",
            "stable_path": "/dev/input/by-id/event1",
            "id": "id-event1",
            "name": "Main Keyboard",
            "phys": "",
        }
    ]


def test_detect_input_classes_reports_combo_keyboard_mouse_pointstick() -> None:
    caps = {
        evdev.ecodes.EV_KEY: [
            evdev.ecodes.KEY_A,
            evdev.ecodes.BTN_LEFT,
            evdev.ecodes.BTN_RIGHT,
        ],
        evdev.ecodes.EV_REL: [
            evdev.ecodes.REL_X,
            evdev.ecodes.REL_Y,
            evdev.ecodes.REL_WHEEL,
        ],
    }

    classes = detect_input_classes_from_capabilities(
        caps,
        [evdev.ecodes.INPUT_PROP_POINTING_STICK],
    )

    assert classes == ["mouse", "keyboard", "pointstick"]
    assert primary_input_class(classes) == DeviceType.MOUSE


def test_detect_input_classes_reports_touchpad_without_mouse() -> None:
    caps = {
        evdev.ecodes.EV_KEY: [
            evdev.ecodes.BTN_LEFT,
            evdev.ecodes.BTN_TOUCH,
            evdev.ecodes.BTN_TOOL_FINGER,
        ],
        evdev.ecodes.EV_ABS: [
            evdev.ecodes.ABS_X,
            evdev.ecodes.ABS_Y,
            evdev.ecodes.ABS_MT_POSITION_X,
            evdev.ecodes.ABS_MT_POSITION_Y,
        ],
    }

    classes = detect_input_classes_from_capabilities(
        caps,
        [evdev.ecodes.INPUT_PROP_POINTER, evdev.ecodes.INPUT_PROP_BUTTONPAD],
    )

    assert classes == ["touchpad"]
    assert primary_input_class(classes) == DeviceType.OTHER


def test_classify_event_device_type_uses_event_shape_for_combo_devices() -> None:
    device_types = ["mouse", "keyboard", "pointstick"]

    assert (
        classify_event_device_type(
            evdev.InputEvent(1, 1, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            device_types,
        )
        == "keyboard"
    )
    assert (
        classify_event_device_type(
            evdev.InputEvent(1, 1, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 1),
            device_types,
        )
        == "mouse"
    )
    assert (
        classify_event_device_type(
            evdev.InputEvent(1, 1, evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 1),
            device_types,
        )
        == "mouse"
    )


def test_classify_event_device_type_reports_touchpad() -> None:
    assert (
        classify_event_device_type(
            evdev.InputEvent(1, 1, evdev.ecodes.EV_ABS, evdev.ecodes.ABS_MT_POSITION_X, 100),
            ["touchpad"],
        )
        == "touchpad"
    )
