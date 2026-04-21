# ruff: noqa: F403, F405, I001
from tests.gui.support import *

class TestDemoDevice:
    def test_demo_device_creation(self):
        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig

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
        from keymasq.common.models import (
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

        from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

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

    def test_record_dialog_defaults_to_recommended_sources(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

        monkeypatch.setattr(RecordMacroDialog, "_load_initial_state_async", lambda self: None)

        dialog = RecordMacroDialog(Gtk.Window())
        dialog._apply_recording_settings({"status": "ok", "device_overrides": {}})
        dialog._devices = [
            {
                "path": "/dev/input/event20",
                "recording_id": "keymasq:output:keyboard",
                "recording_kind": "keymasq_output",
                "device_type": "keyboard",
                "device_types": ["keyboard"],
                "name": "keymasq-keyboard",
            },
            {
                "path": "/dev/input/event0",
                "recording_id": "physical:/dev/input/by-id/raw-mouse",
                "recording_kind": "physical",
                "device_type": "mouse",
                "device_types": ["mouse"],
                "name": "Raw Mouse",
            },
        ]

        dialog._populate_device_list()

        assert dialog._device_checks["keymasq:output:keyboard"].get_active() is True
        assert dialog._device_checks["physical:/dev/input/by-id/raw-mouse"].get_active() is False
        assert dialog._selection_summary.get_label() == "Selected sources: 1 (Keyboard 1)"

    def test_record_dialog_bulk_selection_is_helper_only(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

        monkeypatch.setattr(RecordMacroDialog, "_load_initial_state_async", lambda self: None)

        dialog = RecordMacroDialog(Gtk.Window())
        dialog._apply_recording_settings(
            {
                "status": "ok",
                "include_mouse_movement": True,
                "device_overrides": {},
            }
        )
        dialog._devices = [
            {
                "path": "/dev/input/event20",
                "recording_id": "keymasq:output:keyboard",
                "recording_kind": "keymasq_output",
                "device_type": "keyboard",
                "device_types": ["keyboard"],
                "name": "keymasq-keyboard",
            },
            {
                "path": "/dev/input/event0",
                "recording_id": "physical:/dev/input/by-id/raw-mouse",
                "recording_kind": "physical",
                "device_type": "mouse",
                "device_types": ["mouse"],
                "name": "Raw Mouse",
            },
        ]

        dialog._populate_device_list()
        assert dialog._selection_warning.get_visible() is True

        dialog._on_select_type_clicked(Gtk.Button(), "mouse", True)
        assert dialog._device_checks["physical:/dev/input/by-id/raw-mouse"].get_active() is True
        assert dialog._selection_summary.get_label() == "Selected sources: 2 (Keyboard 1, Mouse 1)"
        assert dialog._selection_warning.get_visible() is False

        dialog._on_reset_to_recommended_clicked(Gtk.Button())
        assert dialog._device_checks["physical:/dev/input/by-id/raw-mouse"].get_active() is False
        assert dialog._selection_warning.get_visible() is True


class TestDialogConstruction:
    def test_about_dialog_uses_packaged_app_identity(self, monkeypatch):
        from keymasq import __version__
        import keymasq.gui.application as application_module

        captured: dict[str, object] = {}

        class DummyAboutDialog:
            def set_application_name(self, value):
                captured["application_name"] = value

            def set_application_icon(self, value):
                captured["application_icon"] = value

            def set_version(self, value):
                captured["version"] = value

            def set_comments(self, value):
                captured["comments"] = value

            def set_developer_name(self, value):
                captured["developer_name"] = value

            def set_license_type(self, value):
                captured["license_type"] = value

            def present(self, parent):
                captured["parent"] = parent

        monkeypatch.setattr(application_module.Adw, "AboutDialog", DummyAboutDialog)

        app = application_module.Application(demo_mode=True)
        app.window = object()

        assert app.get_application_id() == application_module.APP_ID
        assert application_module.APP_VERSION == __version__

        app._on_about(None, None)

        assert captured["application_name"] == "Keymasq"
        assert captured["application_icon"] == application_module.APP_ICON_NAME
        assert captured["version"] == application_module.APP_VERSION
        assert captured["comments"] == "A key remapping tool for Linux"
        assert captured["developer_name"] == "Keymasq Team"
        assert captured["parent"] is app.window

    def test_superkey_dialog_constructs_without_missing_right_panel(self, temp_config_dir):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.widgets.superkey_dialog import SuperkeyDialog

        dialog = SuperkeyDialog(Gtk.Window())

        assert dialog.get_child() is not None
        assert dialog.right_box.get_parent() is not None

    def test_pattern_superkey_actions_use_shared_key_selector_dialog(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.common.models import ActionType, SuperkeyAction
        import keymasq.gui.widgets.key_selector_dialog as key_selector_dialog_module
        from keymasq.gui.widgets.superkey_dialog import ActionListDialog

        captured: dict[str, object] = {}

        class DummyDialog:
            def __init__(self, _parent, _label, current_action=None, **kwargs):
                captured["current_action"] = current_action
                captured["kwargs"] = kwargs

            def connect(self, signal_name, callback, index):
                captured["signal_name"] = signal_name
                captured["callback"] = callback
                captured["index"] = index

            def present(self):
                captured["presented"] = True

        monkeypatch.setattr(key_selector_dialog_module, "KeySelectorDialog", DummyDialog)

        dialog = ActionListDialog(Gtk.Window(), "Hold Actions", "pattern", action_key="hold")
        dialog._open_child_editor(
            SuperkeyAction(action_type=ActionType.PROFILE_TOGGLE, profile_name="Gaming"),
            2,
        )

        current_action = captured["current_action"]
        assert captured["signal_name"] == "key-selected"
        assert captured["presented"] is True
        assert current_action.action_type == ActionType.PROFILE_TOGGLE
        assert current_action.profile_name == "Gaming"
        assert captured["kwargs"] == {
            "allow_passthrough": False,
            "allow_clear_mapping": False,
            "allow_suppress": False,
            "allow_superkey": False,
            "allow_rapidfire": True,
            "allow_tap": False,
            "allow_macro_options": True,
        }

    def test_pattern_superkey_action_summary_formats_without_label_rewrite(self):
        from keymasq.common.models import ActionType, SuperkeyAction
        from keymasq.gui.widgets.superkey_dialog import _describe_pattern_superkey_action

        label = _describe_pattern_superkey_action(
            SuperkeyAction(action_type=ActionType.PROFILE_TOGGLE, profile_name="Gaming"),
            exec_limit=20,
            exec_prefix="exec ",
            macro_prefix="macro ",
            target_separator=" -> ",
            title_case_target_type=True,
        )
        lower_label = _describe_pattern_superkey_action(
            SuperkeyAction(
                action_type=ActionType.MOUSE_MOVE_REL,
                move_x=12,
                move_y=-4,
            ),
            exec_limit=20,
            exec_prefix="exec ",
            macro_prefix="macro ",
            target_separator=" ",
            title_case_target_type=False,
        )

        assert label == "Toggle Profile -> Gaming"
        assert lower_label == "mouse move (rel) 12, -4"

    def test_macro_manager_dialog_constructs_with_close_handler(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        from keymasq.gui.widgets.macro_manager_dialog import MacroManagerDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)

        dialog = MacroManagerDialog(Gtk.Window())

        assert dialog.get_child() is not None
        assert callable(dialog._on_close_clicked)

    def test_macro_manager_edit_opens_editor_with_closed_handler(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        import keymasq.gui.widgets.macro_editor_dialog as macro_editor_dialog_module
        from keymasq.gui.widgets.macro_manager_dialog import MacroManagerDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)

        captured: dict[str, object] = {}

        class DummyEditorDialog:
            def __init__(self, parent, name):
                captured["parent"] = parent
                captured["name"] = name

            def connect(self, signal_name, callback):
                captured["signal_name"] = signal_name
                captured["callback"] = callback

            def present(self, parent):
                captured["present_parent"] = parent

        monkeypatch.setattr(macro_editor_dialog_module, "MacroEditorDialog", DummyEditorDialog)

        parent = Gtk.Window()
        dialog = MacroManagerDialog(parent)
        dialog._on_edit_clicked(Gtk.Button(), "demo_macro")

        assert captured["parent"] is parent
        assert captured["name"] == "demo_macro"
        assert captured["signal_name"] == "closed"
        assert captured["callback"] == dialog._on_editor_closed
        assert captured["present_parent"] is parent
