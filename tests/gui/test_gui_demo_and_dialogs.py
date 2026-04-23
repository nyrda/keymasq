# ruff: noqa: F403, F405, I001
import threading
import time

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
        monkeypatch.setattr(RecordMacroDialog, "_sync_settings_async", lambda self: None)

        dialog = RecordMacroDialog(Gtk.Window())

        dialog._apply_unlock_state(
            {
                "status": "ok",
                "recording_unlocked": False,
                "recording_refresh_owner": False,
            }
        )
        assert dialog._unlock_btn.get_visible() is True
        assert dialog._unlock_status.get_label() == "Unlock required"

        dialog._apply_unlock_state(
            {
                "status": "ok",
                "recording_unlocked": True,
                "recording_refresh_owner": False,
            }
        )
        assert dialog._unlock_btn.get_visible() is True
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

    def test_record_dialog_live_settings_footer_uses_done_without_cancel(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

        monkeypatch.setattr(RecordMacroDialog, "_load_initial_state_async", lambda self: None)
        monkeypatch.setattr(RecordMacroDialog, "_sync_settings_async", lambda self: None)

        dialog = RecordMacroDialog(Gtk.Window())

        labels: list[str] = []

        def collect_button_labels(widget) -> None:
            if isinstance(widget, Gtk.Button):
                label = widget.get_label()
                if label:
                    labels.append(label)

            child = widget.get_first_child()
            while child is not None:
                collect_button_labels(child)
                child = child.get_next_sibling()

        collect_button_labels(dialog.get_child())

        assert "Done" in labels
        assert "Cancel" not in labels
        assert "Save Settings" not in labels

    def test_record_dialog_locked_reason_explains_blocked_recording(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

        monkeypatch.setattr(RecordMacroDialog, "_load_initial_state_async", lambda self: None)

        dialog = RecordMacroDialog(Gtk.Window(), reason="recording_locked")

        assert dialog._title_label.get_label() == "Unlock Macro Recording"
        assert dialog._locked_notice.get_visible() is True

        dialog.set_presentation_reason("settings")

        assert dialog._title_label.get_label() == "Macro Recording Settings"
        assert dialog._locked_notice.get_visible() is False

    def test_record_dialog_closes_when_recording_starts(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

        monkeypatch.setattr(RecordMacroDialog, "_load_initial_state_async", lambda self: None)
        registered: dict[str, object] = {}
        unregistered: list[tuple[str, object]] = []

        class Parent(Gtk.Window):
            def register_event_handler(self, event_type: str, callback) -> None:
                registered[event_type] = callback

            def unregister_event_handler(self, event_type: str, callback) -> None:
                unregistered.append((event_type, callback))

        dialog = RecordMacroDialog(Parent())
        closed: list[bool] = []
        monkeypatch.setattr(dialog, "close", lambda: closed.append(True))

        callback = dialog._on_recording_started
        callback({"event": "recording_started"})

        assert closed == [True]

        dialog._on_dialog_closed(dialog)

        assert unregistered == [("recording_started", callback)]

    def test_record_dialog_defaults_to_recommended_sources(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

        monkeypatch.setattr(RecordMacroDialog, "_load_initial_state_async", lambda self: None)
        monkeypatch.setattr(RecordMacroDialog, "_sync_settings_async", lambda self: None)

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
        assert dialog._selection_summary.get_label() == "1 selected (1kb)"

    def test_record_dialog_source_row_activation_toggles_checkbox(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

        monkeypatch.setattr(RecordMacroDialog, "_load_initial_state_async", lambda self: None)
        monkeypatch.setattr(RecordMacroDialog, "_sync_settings_async", lambda self: None)

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

        raw_row = None
        row = dialog._device_listbox.get_first_child()
        while row is not None:
            if getattr(row, "_recording_id", "") == "physical:/dev/input/by-id/raw-mouse":
                raw_row = row
                break
            row = row.get_next_sibling()

        assert raw_row is not None
        dialog._on_device_row_activated(dialog._device_listbox, raw_row)
        assert dialog._device_checks["physical:/dev/input/by-id/raw-mouse"].get_active() is True
        assert dialog._device_overrides == {"physical:/dev/input/by-id/raw-mouse": True}
        assert dialog._selection_summary.get_label() == "2 selected (1kb, 1m)"

        dialog._on_device_row_activated(dialog._device_listbox, raw_row)
        assert dialog._device_checks["physical:/dev/input/by-id/raw-mouse"].get_active() is False
        assert dialog._device_overrides == {}
        assert dialog._selection_summary.get_label() == "1 selected (1kb)"

    def test_record_dialog_bulk_selection_is_helper_only(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

        monkeypatch.setattr(RecordMacroDialog, "_load_initial_state_async", lambda self: None)
        monkeypatch.setattr(RecordMacroDialog, "_sync_settings_async", lambda self: None)

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
        assert dialog._selection_summary.get_label() == "2 selected (1kb, 1m)"
        assert dialog._selection_warning.get_visible() is False

        dialog._on_reset_to_recommended_clicked(Gtk.Button())
        assert dialog._device_checks["physical:/dev/input/by-id/raw-mouse"].get_active() is False
        assert dialog._selection_warning.get_visible() is True

    def test_record_dialog_reset_sync_wins_over_inflight_selection(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        import keymasq.gui.widgets.record_macro_dialog as record_macro_dialog_module
        from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

        monkeypatch.setattr(RecordMacroDialog, "_load_initial_state_async", lambda self: None)

        first_started = threading.Event()
        release_first = threading.Event()
        captured_payloads: list[dict[str, object]] = []

        def fake_session_request(payload, timeout=5.0):
            snapshot = dict(payload)
            snapshot["device_overrides"] = dict(payload.get("device_overrides", {}))
            captured_payloads.append(snapshot)
            if len(captured_payloads) == 1:
                first_started.set()
                assert release_first.wait(2.0)
            return {"status": "ok"}

        monkeypatch.setattr(record_macro_dialog_module, "session_request", fake_session_request)

        dialog = RecordMacroDialog(Gtk.Window())
        dialog._device_overrides = {"physical:/dev/input/by-id/raw-mouse": True}

        dialog._sync_settings_async()
        assert first_started.wait(2.0)

        dialog._device_overrides.clear()
        dialog._sync_settings_async()
        release_first.set()

        for _ in range(100):
            with dialog._settings_sync_lock:
                worker_running = dialog._settings_sync_worker_running
            if not worker_running:
                break
            time.sleep(0.01)
        else:
            pytest.fail("settings sync worker did not finish")

        assert [payload["device_overrides"] for payload in captured_payloads] == [
            {"physical:/dev/input/by-id/raw-mouse": True},
            {},
        ]


class TestRecordingOverlay:
    def test_recording_overlay_uses_prominent_centered_panel(self):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.widgets.recording_overlay import RecordingOverlay

        overlay = RecordingOverlay(Gtk.Window())

        assert overlay.has_css_class("recording-overlay")
        assert overlay.get_hexpand() is True
        assert overlay.get_vexpand() is True

        panel = overlay.get_first_child()
        assert panel is not None
        assert panel.has_css_class("recording-overlay-panel")
        assert panel.has_css_class("card") is False
        assert panel.get_halign() == Gtk.Align.CENTER
        assert panel.get_valign() == Gtk.Align.CENTER
        width_request, _height_request = panel.get_size_request()
        assert width_request >= 420

        assert overlay._stop_btn is not None
        assert overlay._stop_btn.get_label() == "Stop Recording"

    def test_recording_overlay_updates_status_and_stop_feedback(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        import keymasq.gui.widgets.recording_overlay as recording_overlay_module
        from keymasq.gui.widgets.recording_overlay import RecordingOverlay

        callbacks = []

        def fake_session_request_async(payload, callback, timeout=5.0):
            callbacks.append((payload, callback))

        monkeypatch.setattr(
            recording_overlay_module, "session_request_async", fake_session_request_async
        )

        overlay = RecordingOverlay(Gtk.Window())
        overlay.on_started({"event": "recording_started"})
        overlay.on_progress({"duration_ms": 62025, "event_count": 444})

        assert overlay._duration_label.get_label() == "01:02.025"
        assert overlay._events_label.get_label() == "444"

        assert overlay._stop_btn is not None
        overlay._on_stop_clicked(overlay._stop_btn)

        assert callbacks[0][0] == {"command": "stop_recording"}
        assert overlay._stop_btn.get_sensitive() is False
        assert overlay._stop_btn.get_label() == "Stopping..."

        assert callbacks[0][1]({"status": "ok"}) is False
        assert overlay._stop_btn.get_sensitive() is True
        assert overlay._stop_btn.get_label() == "Stop Recording"

        overlay.on_started({"event": "recording_started"})
        assert overlay._timer_id != 0
        overlay.on_stopped()
        assert overlay._timer_id == 0
        assert overlay._stop_btn.get_sensitive() is True


class TestSaveMacroDialog:
    def test_save_macro_dialog_sends_pending_save_token(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        import keymasq.gui.widgets.save_macro_dialog as save_macro_dialog_module
        from keymasq.gui.widgets.save_macro_dialog import SaveMacroDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)
        captured: dict[str, object] = {}

        def fake_session_request_with_hooks(payload, callback, on_start=None, on_done=None):
            captured.update(payload)
            if on_start:
                on_start()
            callback({"status": "ok"})
            if on_done:
                on_done()

        monkeypatch.setattr(
            save_macro_dialog_module,
            "session_request_with_hooks",
            fake_session_request_with_hooks,
        )

        dialog = SaveMacroDialog(
            Gtk.Window(),
            {
                "duration_ms": 100,
                "event_count": 2,
                "device_types": ["keyboard"],
                "pending_save_token": "pending-1",
            },
        )
        dialog._name_entry.set_text("macro_1")
        dialog._on_save_clicked(dialog._save_btn)

        assert captured["command"] == "save_recording"
        assert captured["pending_save_token"] == "pending-1"

    def test_save_macro_dialog_discard_sends_pending_save_token(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        import keymasq.gui.widgets.save_macro_dialog as save_macro_dialog_module
        from keymasq.gui.widgets.save_macro_dialog import SaveMacroDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            save_macro_dialog_module,
            "session_request_async",
            lambda payload, callback: captured.update(payload),
        )

        dialog = SaveMacroDialog(
            Gtk.Window(),
            {
                "duration_ms": 100,
                "event_count": 2,
                "device_types": ["keyboard"],
                "pending_save_token": "pending-1",
            },
        )

        dialog._on_discard_clicked(Gtk.Button())

        assert captured == {
            "command": "discard_recording",
            "pending_save_token": "pending-1",
        }


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

    def test_macro_manager_closes_when_recording_starts(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        from keymasq.gui.widgets.macro_manager_dialog import MacroManagerDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)

        dialog = MacroManagerDialog(Gtk.Window())
        closed: list[bool] = []
        monkeypatch.setattr(dialog, "close", lambda: closed.append(True))

        dialog._on_recording_started({"event": "recording_started"})

        assert dialog._recording_active is True
        assert closed == [True]

    def test_macro_manager_opens_locked_recording_mode_after_recording_locked(
        self,
        monkeypatch,
    ):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        from keymasq.gui.widgets.macro_manager_dialog import MacroManagerDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)
        captured: dict[str, object] = {}

        class Parent(Gtk.Window):
            def present_recording_settings_dialog(self, reason: str = "settings") -> None:
                captured["reason"] = reason

        dialog = MacroManagerDialog(Parent())

        result = dialog._on_record_request_finished(
            {
                "status": "error",
                "error_code": "recording_locked",
                "message": "recording_locked",
            },
            "start_recording",
        )

        assert result is False
        assert captured["reason"] == "recording_locked"

    def test_macro_manager_presents_pending_save_dialog_after_macro_save_pending(
        self,
        monkeypatch,
    ):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        from keymasq.gui.widgets.macro_manager_dialog import MacroManagerDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)
        presented: list[bool] = []

        class Parent(Gtk.Window):
            def present_pending_macro_save_dialog(self) -> bool:
                presented.append(True)
                return True

        dialog = MacroManagerDialog(Parent())

        result = dialog._on_record_request_finished(
            {
                "status": "error",
                "error_code": "macro_save_pending",
                "message": (
                    "Save or discard the current recording before starting another recording."
                ),
            },
            "start_recording",
        )

        assert result is False
        assert presented == [True]

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
