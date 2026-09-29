# ruff: noqa: E402

from types import SimpleNamespace

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from keymasq.common.model.core import DeviceType
from keymasq.common.model.hardware import ButtonDefinition, EvdevDevice, HardwareConfig
from keymasq.gui.widgets.device_tab.hardware import HardwareSettingsMixin

MOUSE = EvdevDevice("/dev/input/event4", DeviceType.MOUSE, "mouse")
KEYS = EvdevDevice("/dev/input/event5", DeviceType.KEYBOARD, "keys")


def _removed_ids(buttons: list[ButtonDefinition]) -> list[str]:
    hardware = HardwareConfig("cafe", "0004", "Mouse", [KEYS], buttons)
    return HardwareSettingsMixin._remove_controls_for_evdev_device(
        SimpleNamespace(device=hardware), MOUSE
    )


def test_removing_the_last_pointer_interface_removes_pointer_mappings():
    assert _removed_ids([]) == ["pointer"]


def test_removing_a_mouse_interface_keeps_a_surviving_control_named_pointer():
    button = ButtonDefinition("pointer", "Side", "key_a", source="keys")

    assert _removed_ids([button]) == []
