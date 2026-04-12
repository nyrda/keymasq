# ruff: noqa: F403, F405, I001
from tests.gui.support import *

class TestDemoDevice:
    def test_demo_device_creation(self):
        from keyforge.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig

        demo_device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Demo Mouse",
            evdev_devices=[
                EvdevDevice(path="/dev/input/event0", device_type=DeviceType.MOUSE),
            ],
            buttons=[
                ButtonDefinition(id="btn_left", label="Left Click", evdev="btn_left", zone="left"),
                ButtonDefinition(
                    id="btn_right", label="Right Click", evdev="btn_right", zone="right"
                ),
            ],
        )

        assert demo_device.name == "Demo Mouse"
        assert demo_device.hardware_id == "1234:5678"
        assert len(demo_device.buttons) == 2

    def test_demo_profile_mapping(self):
        from keyforge.common.models import (
            ActionType,
            DeviceProfileLayer,
            MappingAction,
            ProfileConfig,
        )

        profile = ProfileConfig(
            name="Demo Gaming Profile",
            enabled=True,
            device_layers={
                "1234:5678": DeviceProfileLayer(
                    hardware_id="1234:5678",
                    mappings={
                        "btn_back": MappingAction(action_type=ActionType.KEYBOARD, target="key_1"),
                        "btn_forward": MappingAction(
                            action_type=ActionType.KEYBOARD, target="key_2"
                        ),
                    },
                )
            },
        )

        assert profile.name == "Demo Gaming Profile"
        assert "btn_back" in profile.device_layers["1234:5678"].mappings
        assert profile.device_layers["1234:5678"].mappings["btn_back"].target == "key_1"


class TestRecordMacroDialog:
    def test_record_dialog_uses_unlock_and_owner_state(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keyforge.gui.widgets.record_macro_dialog import RecordMacroDialog

        monkeypatch.setattr(RecordMacroDialog, "_load_initial_state_async", lambda self: None)

        dialog = RecordMacroDialog(Gtk.Window())

        dialog._apply_unlock_state(
            {
                "status": "ok",
                "recording_unlocked": False,
                "recording_refresh_owner": False,
            }
        )
        assert dialog._unlock_btn.get_visible() is True
        assert dialog._unlock_btn.get_label() == "Unlock"
        assert dialog._unlock_status.get_label() == "Unlock required"

        dialog._apply_unlock_state(
            {
                "status": "ok",
                "recording_unlocked": True,
                "recording_refresh_owner": False,
            }
        )
        assert dialog._unlock_btn.get_visible() is True
        assert dialog._unlock_btn.get_label() == "Claim Unlock"
        assert dialog._unlock_status.get_label() == "Unlock active in another session"

        dialog._apply_unlock_state(
            {
                "status": "ok",
                "recording_unlocked": True,
                "recording_refresh_owner": True,
            }
        )
        assert dialog._unlock_btn.get_visible() is False
        assert dialog._unlock_status.get_label() == "Unlock active"


class TestDialogConstruction:
    def test_superkey_dialog_constructs_without_missing_right_panel(self, temp_config_dir):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keyforge.gui.widgets.superkey_dialog import SuperkeyDialog

        dialog = SuperkeyDialog(Gtk.Window())

        assert dialog.get_child() is not None
        assert dialog.right_box.get_parent() is not None

    def test_macro_manager_dialog_constructs_with_close_handler(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        from keyforge.gui.widgets.macro_manager_dialog import MacroManagerDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)

        dialog = MacroManagerDialog(Gtk.Window())

        assert dialog.get_child() is not None
        assert callable(dialog._on_close_clicked)


