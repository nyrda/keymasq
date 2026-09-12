from pathlib import Path

import pytest

pytest.importorskip("gi")

from keymasq.common.model.core import DeviceType
from keymasq.common.model.hardware import (
    AnalogAxisDefinition,
    AnalogInputDefinition,
    ButtonDefinition,
    EvdevDevice,
    HardwareConfig,
)
from keymasq.common.model.profiles import ProfileConfig
from keymasq.gui.widgets.device_tab.tab import DeviceTab
from keymasq.session.profile.types import ProfileInfo


def test_hardware_route_descriptions_do_not_depend_on_profile():
    hardware = HardwareConfig(
        "1234",
        "5678",
        "Controller",
        [
            EvdevDevice("/dev/input/event0", DeviceType.GAMEPAD, "pad"),
        ],
        [
            ButtonDefinition("south", "South", "btn_south", source="pad"),
            ButtonDefinition("capture", "Capture", "btn_z", source="pad"),
        ],
        analog_inputs=[
            AnalogInputDefinition(
                "stick",
                "Stick",
                "stick",
                "pad",
                [
                    AnalogAxisDefinition("x", "abs_x"),
                    AnalogAxisDefinition("y", "abs_y"),
                ],
            ),
        ],
    )
    tab = DeviceTab(hardware, None, demo_mode=True)
    hardware.default_output = "virtual-gamepad-1"
    from keymasq.common.virtual_device_templates import VirtualDeviceConfig, resolve_virtual_devices

    tab._default_output_templates = {
        d.output_id: d.template for d in resolve_virtual_devices(1, VirtualDeviceConfig())
    }
    for profile in (ProfileConfig("First"), ProfileConfig("Second")):
        tab._selected_profile = ProfileInfo(Path("profile.toml"), profile)
        assert "Default → virtual-gamepad-1" in tab._describe_passthrough_output(
            hardware.buttons[0]
        )
        assert "No matching output" in tab._describe_passthrough_output(hardware.buttons[1])
        assert "ABS_X, ABS_Y" in (tab._default_output_description(hardware.analog_inputs[0]) or "")
        assert not hasattr(tab, "_default_output_row")
        tab._update_button_display("south")
        label = tab._button_widgets["south"]._action_label
        assert label.get_text() == "→ A"
        assert "virtual-gamepad-1" in label.get_tooltip_text()
        assert "BTN_SOUTH" in label.get_tooltip_text()
        tab._update_button_display("stick")
        assert tab._button_widgets["stick"]._action_label.get_text() == "→ X, Y"
    hardware.default_output = "missing-pad"
    assert "unavailable" in (tab._default_output_description(hardware.buttons[0]) or "")


def test_keyboard_does_not_offer_controller_route():
    hardware = HardwareConfig(
        "1234",
        "5678",
        "Keyboard",
        [
            EvdevDevice("/dev/input/event0", DeviceType.KEYBOARD, "keyboard"),
        ],
        [],
    )
    tab = DeviceTab(hardware, None, demo_mode=True)
    assert not hasattr(tab, "_default_output_row")


def test_setup_and_hardware_settings_save_stable_output(monkeypatch, tmp_path):
    from unittest.mock import Mock

    from keymasq.common.virtual_device_templates import VirtualDeviceConfig
    from keymasq.gui.session_client import GuiTaskResult
    from keymasq.gui.widgets import controller_output
    from keymasq.gui.widgets.device_tab import hardware_settings_dialog
    from keymasq.gui.wizards.hardware_setup.dialog import HardwareSetupDialog
    from keymasq.session.hardware import HardwareManager

    def run(worker, callback):
        callback(GuiTaskResult(value=worker()))

    monkeypatch.setattr(controller_output, "run_gui_task", run)
    monkeypatch.setattr(controller_output, "virtual_gamepad_count", lambda: 1)
    monkeypatch.setattr(controller_output, "virtual_device_config", VirtualDeviceConfig)
    monkeypatch.setattr(hardware_settings_dialog, "run_gui_task", run)
    monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)
    monkeypatch.setattr("keymasq.common.paths.HARDWARE_DIR", tmp_path)
    manager = HardwareManager()
    dialog = HardwareSetupDialog(None, manager)
    dialog._template_state.current = "gamepad"
    dialog._discovery_state.selected_device = {
        "vendor_id": "1234",
        "product_id": "5678",
        "name": "Pad",
    }
    dialog._discovery_state.discovered_interfaces = {
        "pad": {
            "id": "pad",
            "stable_path": "/dev/input/event0",
            "device_types": ["gamepad"],
        }
    }
    dialog._update_describe_mode_ui()
    assert dialog.controller_output.get_visible()
    assert dialog.controller_output.output_id == "passthrough"
    dialog.controller_output.row.set_selected(1)
    dialog._save_gamepad_config()
    hardware = manager.get_hardware("1234:5678")
    assert hardware.default_output == "virtual-gamepad-1"
    changed = Mock()
    settings = hardware_settings_dialog.HardwareSettingsDialog(
        None,
        hardware,
        manager,
        Mock(),
        Mock(),
        Mock(),
        Mock(),
        Mock(return_value=(True, "")),
        Mock(),
        can_delete_profile_mappings=False,
        on_output_changed=changed,
    )
    assert settings._output_group.get_visible()
    assert settings._output_group.row.get_selected() == 1
    settings._output_group.row.set_selected(0)
    assert hardware.default_output == "passthrough"
    assert HardwareManager().get_hardware(hardware.hardware_id).default_output == "passthrough"
    changed.assert_called_once()
    monkeypatch.setattr(
        hardware_settings_dialog,
        "run_gui_task",
        lambda worker, cb: cb(GuiTaskResult(error=OSError("disk full"))),
    )
    settings._output_group.row.set_selected(1)
    assert settings._output_group.output_id == "passthrough"
    assert settings._output_group.row.get_selected() == 0
    assert hardware.default_output == "passthrough"
    assert "disk full" in settings._status_label.get_label()
    changed.assert_called_once()

    pending = []
    monkeypatch.setattr(
        hardware_settings_dialog, "run_gui_task", lambda worker, cb: pending.append((worker, cb))
    )
    settings._output_group.row.set_selected(1)
    assert not settings.get_sensitive()
    assert not settings.get_can_close()
    worker, callback = pending.pop()
    callback(GuiTaskResult(value=worker()))
    assert settings.get_sensitive()
    assert settings.get_can_close()
    assert hardware.default_output == "virtual-gamepad-1"

    devices = list(hardware.evdev_devices)
    hardware.evdev_devices.clear()
    settings._refresh_interface_rows()
    assert not settings._output_group.get_visible()
    hardware.evdev_devices.extend(devices)
    settings._refresh_interface_rows()
    assert settings._output_group.get_visible()


def test_unavailable_hardware_output_is_preserved(monkeypatch):
    from keymasq.gui.session_client import GuiTaskResult
    from keymasq.gui.widgets import controller_output

    monkeypatch.setattr(
        controller_output, "run_gui_task", lambda worker, cb: cb(GuiTaskResult(value=[]))
    )
    group = controller_output.ControllerOutputGroup("missing")
    assert group.output_id == "missing"
    assert group.row.get_selected_item().get_string() == "missing (unavailable)"


@pytest.mark.parametrize(
    ("route", "action_type", "action_output", "expected_tab", "expected_output"),
    [
        ("virtual-gamepad-2", None, None, "gamepad", "virtual-gamepad-2"),
        ("missing-pad", None, None, "gamepad", "missing-pad"),
        ("virtual-gamepad-2", "passthrough", None, "gamepad", "virtual-gamepad-2"),
        ("virtual-gamepad-2", "gamepad", "virtual-gamepad-1", "gamepad", "virtual-gamepad-1"),
        ("virtual-gamepad-2", "gamepad", None, "gamepad", None),
        ("virtual-gamepad-2", "keyboard", None, "keyboard", "virtual-gamepad-2"),
        ("passthrough", None, None, "special", None),
    ],
)
def test_device_selector_defaults_to_hardware_output(
    monkeypatch, route, action_type, action_output, expected_tab, expected_output
):
    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.core import ActionType
    from keymasq.common.virtual_device_templates import VirtualDeviceConfig
    from keymasq.gui.widgets.key_selector import tabs

    monkeypatch.setattr(tabs, "virtual_gamepad_count", lambda: 2)
    monkeypatch.setattr(tabs, "virtual_device_config", VirtualDeviceConfig)
    monkeypatch.setattr(tabs, "load_gamepad_output_hardware_configs", lambda _manager: [])
    hardware = HardwareConfig("1234", "5678", "Controller", [], [], default_output=route)
    tab = DeviceTab(hardware, None, demo_mode=True)
    action = (
        MappingAction(
            action_type=ActionType(action_type),
            target="key_a" if action_type == "keyboard" else "btn_south",
            output_id=action_output,
        )
        if action_type
        else None
    )
    dialog = tab._create_key_selector_dialog(tab, "Capture", action)
    assert dialog.stack.get_visible_child_name() == expected_tab
    assert dialog._selected_gamepad_output_id == expected_output
    assert dialog._gamepad_output_dropdown is not None
    selected = dialog._gamepad_output_dropdown.get_selected()
    assert (dialog._gamepad_output_ids[selected] or "virtual-gamepad-1") == (
        expected_output or "virtual-gamepad-1"
    )
