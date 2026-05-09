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
        assert dialog._unlock_status.get_label() == "Recording locked"
        assert "raw original-input capture" in (dialog._unlock_btn.get_tooltip_text() or "")

        dialog._apply_unlock_state(
            {
                "status": "ok",
                "recording_unlocked": True,
                "recording_refresh_owner": False,
            }
        )
        assert dialog._unlock_btn.get_visible() is True
        assert dialog._unlock_status.get_label() == "Unlocked in another session"
        assert "active owner" in (dialog._unlock_btn.get_tooltip_text() or "")

        dialog._apply_unlock_state(
            {
                "status": "ok",
                "recording_unlocked": True,
                "recording_refresh_owner": True,
            }
        )
        assert dialog._unlock_btn.get_visible() is False
        assert dialog._unlock_status.get_label() == "Recording unlocked"

    def test_record_dialog_docs_button_links_to_live_recording_docs(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        import keymasq.gui.widgets.record_macro_dialog as record_macro_dialog_module
        from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

        monkeypatch.setattr(RecordMacroDialog, "_load_initial_state_async", lambda self: None)
        monkeypatch.setattr(record_macro_dialog_module, "__version__", "1.2.3")

        dialog = RecordMacroDialog(Gtk.Window())

        assert dialog.recording_docs_btn.get_label() == "?"
        assert (
            dialog.recording_docs_btn.get_tooltip_text()
            == "Open macro recording documentation"
        )
        assert record_macro_dialog_module._macro_recording_docs_url() == (
            "https://keymasq.tools/docs/v1.2.3/MACROS/#live-recording"
        )

        monkeypatch.setattr(record_macro_dialog_module, "__version__", "1.2.3.dev1")
        assert record_macro_dialog_module._macro_recording_docs_url() == (
            "https://keymasq.tools/docs/master/MACROS/#live-recording"
        )

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
        assert dialog._unlock_status.get_halign() == Gtk.Align.CENTER
        assert dialog._unlock_btn.get_parent() is dialog._save_btn.get_parent()
        assert dialog._unlock_btn.get_next_sibling() is dialog._save_btn

    def test_record_dialog_locked_reason_explains_blocked_recording(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

        monkeypatch.setattr(RecordMacroDialog, "_load_initial_state_async", lambda self: None)

        dialog = RecordMacroDialog(Gtk.Window(), reason="recording_locked")

        assert dialog._title_label.get_label() == "Unlock Macro Recording"
        assert dialog._locked_notice.get_visible() is True
        assert dialog._locked_notice_title.get_label() == "Recording needs unlock"

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

        dialog._on_discard_clicked(Gtk.Button())

        assert captured == {
            "command": "discard_recording",
            "pending_save_token": "pending-1",
        }

    def test_save_macro_dialog_discard_failure_keeps_dialog_open(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        import keymasq.gui.widgets.save_macro_dialog as save_macro_dialog_module
        from keymasq.gui.widgets.save_macro_dialog import SaveMacroDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)

        def fake_session_request_with_hooks(payload, callback, on_start=None, on_done=None):
            if on_start:
                on_start()
            callback({"status": "error", "message": "discard failed"})
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
        force_closed: list[bool] = []
        monkeypatch.setattr(dialog, "force_close", lambda: force_closed.append(True))

        dialog._on_discard_clicked(Gtk.Button())

        assert force_closed == []
        assert dialog._saved is False
        assert dialog._error_label.get_label() == "discard failed"
        assert dialog.get_can_close() is True


class TestDialogConstruction:
    def test_about_dialog_uses_packaged_app_identity(self, monkeypatch):
        from keymasq import __version__
        import keymasq.gui.application as application_module

        captured: dict[str, object] = {}

        class DummyAboutDialog:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                captured["links"] = []

            def add_link(self, title, url):
                captured["links"].append((title, url))

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
        docs_version = "master" if not __version__ or "dev" in __version__ else f"v{__version__}"
        assert captured["links"] == [
            ("Website", "https://keymasq.tools/"),
            ("Documentation", f"https://keymasq.tools/docs/{docs_version}/"),
            ("License", "https://github.com/nyrda/keymasq/blob/main/LICENSE"),
        ]
        assert captured["parent"] is app.window

    def test_superkey_dialog_constructs_without_missing_right_panel(self, temp_config_dir):
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk

        from keymasq.gui.widgets.superkey_dialog import SuperkeyDialog

        dialog = SuperkeyDialog(Gtk.Window())

        assert dialog.get_child() is not None
        assert dialog.right_box.get_parent() is not None
        assert isinstance(dialog.tap_row, Adw.ExpanderRow)

    def test_superkey_dialog_action_expanders_render_empty_and_populated_rows(
        self, temp_config_dir
    ):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.common.models import ActionType, SuperkeyAction
        from keymasq.gui.widgets.superkey_dialog import SuperkeyDialog

        dialog = SuperkeyDialog(Gtk.Window())

        dialog._populate_action_row(dialog.tap_row, [])

        assert dialog.tap_row.get_enable_expansion() is False
        assert "(none)" in dialog.tap_row.get_subtitle()
        assert dialog.tap_row._child_rows == []

        dialog._populate_action_row(
            dialog.tap_row,
            [
                SuperkeyAction(action_type=ActionType.KEYBOARD, target="key_a"),
                SuperkeyAction(action_type=ActionType.PROFILE_TOGGLE, profile_name="Gaming"),
            ],
        )

        assert dialog.tap_row.get_enable_expansion() is True
        assert dialog.tap_row.get_expanded() is True
        assert "2 actions" in dialog.tap_row.get_subtitle()
        assert len(dialog.tap_row._child_rows) == 2
        assert dialog.tap_row._child_rows[0].get_title().startswith("1. ")
        assert dialog.tap_row._child_rows[1].get_title().startswith("2. ")
        assert dialog.tap_row._child_rows[0].get_use_markup() is False

    def test_superkey_dialog_clear_removes_expander_child_rows(self, temp_config_dir):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.common.models import ActionType, SuperkeyAction
        from keymasq.gui.widgets.superkey_dialog import SuperkeyDialog

        dialog = SuperkeyDialog(Gtk.Window())
        dialog._populate_action_row(
            dialog.tap_row,
            [SuperkeyAction(action_type=ActionType.KEYBOARD, target="key_a")],
        )

        assert dialog.tap_row._child_rows

        dialog._on_clear_action_clicked(Gtk.Button(), dialog.tap_row)

        assert dialog.tap_row._action_items == []
        assert dialog.tap_row.get_enable_expansion() is False
        assert dialog.tap_row._child_rows == []

    def test_superkey_dialog_overload_expanders_keep_static_descriptions(
        self, temp_config_dir
    ):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.common.models import ActionType, MappingAction
        from keymasq.gui.widgets.superkey_dialog import SuperkeyDialog

        dialog = SuperkeyDialog(Gtk.Window())

        dialog._populate_action_row(dialog.overload_row, [])

        assert "Held while pressed, released when you let go" in dialog.overload_row.get_subtitle()
        assert "(none)" in dialog.overload_row.get_subtitle()

        dialog._populate_action_row(
            dialog.overload_row,
            [MappingAction(action_type=ActionType.KEYBOARD, target="key_leftctrl")],
        )

        assert "Held while pressed, released when you let go" in dialog.overload_row.get_subtitle()
        assert "1 action" in dialog.overload_row.get_subtitle()

    def test_superkey_dialog_overload_saves_press_and_release_actions(
        self, temp_config_dir, monkeypatch
    ):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.common import paths
        from keymasq.common.models import ActionType, MappingAction, SuperkeyMode
        from keymasq.gui.widgets.superkey_dialog import SuperkeyDialog

        monkeypatch.setattr(paths, "SUPERKEYS_DIR", temp_config_dir / "superkeys")

        dialog = SuperkeyDialog(Gtk.Window())
        dialog.mode_dropdown.set_selected(1)
        dialog.overload_row._action_items = [
            MappingAction(action_type=ActionType.KEYBOARD, target="key_leftctrl")
        ]
        dialog.overload_down_row._action_items = [
            MappingAction(action_type=ActionType.KEYBOARD, target="key_a")
        ]
        dialog.overload_up_row._action_items = [
            MappingAction(action_type=ActionType.KEYBOARD, target="key_b")
        ]
        dialog.name_entry.set_text("Split Overload")
        dialog._on_save_clicked(Gtk.Button())

        saved = dialog.manager.get_superkey("Split Overload")
        assert saved is not None
        assert saved.mode == SuperkeyMode.OVERLOAD
        assert dialog.overload_row.get_title() == "Main Actions"
        assert dialog.overload_down_row.get_visible() is True
        assert dialog.overload_up_row.get_visible() is True
        assert [action.target for action in saved.overload_actions] == ["key_leftctrl"]
        assert [action.target for action in saved.overload_down_actions] == ["key_a"]
        assert [action.target for action in saved.overload_up_actions] == ["key_b"]

    def test_superkey_dialog_empty_state_starts_new_draft_and_keeps_close_available(
        self, temp_config_dir, monkeypatch
    ):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gdk, Gtk

        from keymasq.common import paths
        from keymasq.gui.widgets.superkey_dialog import SuperkeyDialog

        monkeypatch.setattr(paths, "SUPERKEYS_DIR", temp_config_dir / "superkeys")

        dialog = SuperkeyDialog(Gtk.Window())
        closed: list[bool] = []
        monkeypatch.setattr(dialog, "close", lambda: closed.append(True))

        new_row = dialog.new_superkey_row
        assert new_row is not None
        assert new_row is dialog.new_superkey_row
        assert getattr(new_row, "_is_new_superkey", False) is True
        assert new_row.has_css_class("superkey-add-row") is True
        assert new_row.get_tooltip_text() == "Add a new Super Key"
        assert dialog.list_box.get_selected_row() is new_row
        assert dialog.name_entry.get_text() == "New Super Key"
        assert dialog.editor_box.get_sensitive() is True
        assert dialog.delete_btn.get_sensitive() is False
        assert dialog.right_box.get_sensitive() is True
        assert dialog.close_btn.get_sensitive() is True

        dialog.close_btn.emit("clicked")
        assert closed == [True]

        closed.clear()
        assert dialog._on_key_pressed(None, Gdk.KEY_Escape, 0, 0) is True
        assert closed == [True]

    def test_superkey_dialog_docs_button_links_to_superkeys_docs(
        self, temp_config_dir, monkeypatch
    ):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        import keymasq.gui.widgets.superkey_dialog as superkey_dialog_module
        from keymasq.gui.widgets.superkey_dialog import SuperkeyDialog

        monkeypatch.setattr(superkey_dialog_module, "__version__", "1.2.3")

        dialog = SuperkeyDialog(Gtk.Window())

        assert dialog.superkeys_docs_btn.get_label() == "?"
        assert (
            dialog.superkeys_docs_btn.get_tooltip_text()
            == "Open Super Keys documentation"
        )
        assert superkey_dialog_module._superkeys_docs_url() == (
            "https://keymasq.tools/docs/v1.2.3/SUPERKEYS/"
        )

        monkeypatch.setattr(superkey_dialog_module, "__version__", "1.2.3.dev1")
        assert superkey_dialog_module._superkeys_docs_url() == (
            "https://keymasq.tools/docs/master/SUPERKEYS/"
        )

    def test_application_presents_superkey_dialog_on_main_window(self, monkeypatch):
        import keymasq.gui.application as application_module
        import keymasq.gui.widgets.superkey_dialog as superkey_dialog_module

        captured: dict[str, object] = {}
        window = SimpleNamespace(profile_manager=object())

        class DummySuperkeyDialog:
            def __init__(self, parent, profile_manager):
                captured["parent"] = parent
                captured["profile_manager"] = profile_manager

            def connect(self, signal_name, callback):
                captured.setdefault("signals", []).append(signal_name)
                captured["callback"] = callback

            def present(self, parent):
                captured["present_parent"] = parent

        monkeypatch.setattr(superkey_dialog_module, "SuperkeyDialog", DummySuperkeyDialog)

        app = application_module.Application(demo_mode=True)
        app.window = window
        app._open_superkey_dialog()

        assert captured["parent"] is window
        assert captured["profile_manager"] is window.profile_manager
        assert captured["present_parent"] is window
        assert captured["signals"] == ["superkey-saved", "superkey-deleted"]

    def test_superkey_action_editor_presents_on_main_window(self, temp_config_dir, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        import keymasq.gui.widgets.superkey_dialog as superkey_dialog_module
        from keymasq.gui.widgets.superkey_dialog import SuperkeyDialog

        captured: dict[str, object] = {}
        parent = Gtk.Window()

        class DummyActionListDialog:
            def __init__(self, *args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs

            def connect(self, signal_name, callback, row):
                captured["signal_name"] = signal_name
                captured["row"] = row

            def present(self, parent):
                captured["present_parent"] = parent

        monkeypatch.setattr(superkey_dialog_module, "ActionListDialog", DummyActionListDialog)

        dialog = SuperkeyDialog(parent)
        dialog._on_edit_action_clicked(Gtk.Button(), dialog.tap_row)

        assert captured["present_parent"] is parent
        assert captured["signal_name"] == "actions-selected"

    def test_pattern_superkey_actions_use_shared_key_selector_dialog(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.common.models import ActionType, SuperkeyAction
        import keymasq.gui.widgets.key_selector_dialog as key_selector_dialog_module
        from keymasq.gui.widgets.superkey_dialog import ActionListDialog

        captured: dict[str, object] = {}

        parent = Gtk.Window()

        class DummyDialog:
            def __init__(self, _parent, _label, current_action=None, **kwargs):
                captured["parent"] = _parent
                captured["current_action"] = current_action
                captured["kwargs"] = kwargs

            def connect(self, signal_name, callback, index):
                captured["signal_name"] = signal_name
                captured["callback"] = callback
                captured["index"] = index

            def present(self, parent):
                captured["presented"] = True
                captured["present_parent"] = parent

        monkeypatch.setattr(key_selector_dialog_module, "KeySelectorDialog", DummyDialog)

        dialog = ActionListDialog(parent, "Hold Actions", "pattern", action_key="hold")
        dialog._open_child_editor(
            SuperkeyAction(action_type=ActionType.PROFILE_TOGGLE, profile_name="Gaming"),
            2,
        )

        current_action = captured["current_action"]
        assert captured["signal_name"] == "key-selected"
        assert captured["presented"] is True
        assert captured["parent"] is parent
        assert captured["present_parent"] is parent
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
        dialog._recording_unlocked = False
        dialog._sync_record_button_state()
        assert "raw original-input capture" in (dialog._record_btn.get_tooltip_text() or "")
        assert (
            dialog.playback_stop_hint.get_label()
            == "Interrupt macro playback: Ctrl+Alt+Esc"
        )
        assert dialog.playback_stop_hint.get_halign() == Gtk.Align.CENTER

    def test_macro_manager_dialog_docs_button_links_to_macros_docs(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        import keymasq.gui.widgets.macro_manager_dialog as macro_manager_dialog_module
        from keymasq.gui.widgets.macro_manager_dialog import MacroManagerDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)
        monkeypatch.setattr(macro_manager_dialog_module, "__version__", "1.2.3")

        dialog = MacroManagerDialog(Gtk.Window())

        assert dialog.macros_docs_btn is not None
        assert dialog.macros_docs_btn.get_label() == "?"
        assert dialog.macros_docs_btn.get_tooltip_text() == "Open Macros documentation"
        assert macro_manager_dialog_module._macros_docs_url() == (
            "https://keymasq.tools/docs/v1.2.3/MACROS/"
        )

        monkeypatch.setattr(macro_manager_dialog_module, "__version__", "1.2.3.dev1")
        assert macro_manager_dialog_module._macros_docs_url() == (
            "https://keymasq.tools/docs/master/MACROS/"
        )

    def test_type_macro_builder_normalizes_common_pasted_text(self):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
        import evdev

        from keymasq.gui.widgets.macro_manager_dialog import TypeMacroDialog

        dialog = TypeMacroDialog(Gtk.Window())

        events = dialog._build_type_events("A\u00a0\u201cHi\u201d\u2026\r\nx\u2014y", 10, 0)

        press_codes = [
            event["code"]
            for event in events
            if event["type"] == evdev.ecodes.EV_KEY and event["value"] == 1
        ]
        assert evdev.ecodes.KEY_SPACE in press_codes
        assert evdev.ecodes.KEY_APOSTROPHE in press_codes
        assert press_codes.count(evdev.ecodes.KEY_DOT) == 3
        assert press_codes.count(evdev.ecodes.KEY_ENTER) == 1
        assert evdev.ecodes.KEY_MINUS in press_codes

    def test_type_macro_builder_reports_unsupported_character_position(self):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.widgets.macro_manager_dialog import TypeMacroDialog

        dialog = TypeMacroDialog(Gtk.Window())

        with pytest.raises(ValueError, match=r"position 2: 'é'"):
            dialog._build_type_events("aé", 10, 0)

    def test_type_macro_dialog_shows_unicode_input_option_enabled_by_default(self):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.widgets.macro_manager_dialog import TypeMacroDialog

        dialog = TypeMacroDialog(Gtk.Window())
        buffer = dialog.text_view.get_buffer()

        buffer.set_text("hello")
        assert dialog.unicode_check.get_visible() is False

        buffer.set_text("hello \u2014")
        assert dialog.unicode_check.get_visible() is True
        assert dialog.unicode_check.get_active() is True
        assert (
            dialog.unicode_check.get_label()
            == "Use Ctrl+Shift+U for detected Unicode characters"
        )

    def test_type_macro_builder_can_emit_unicode_input_sequence(self):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
        import evdev

        from keymasq.gui.widgets.macro_manager_dialog import TypeMacroDialog

        dialog = TypeMacroDialog(Gtk.Window())

        events = dialog._build_type_events("é", 10, 0, use_unicode_input=True)

        press_codes = [
            event["code"]
            for event in events
            if event["type"] == evdev.ecodes.EV_KEY and event["value"] == 1
        ]
        assert press_codes == [
            evdev.ecodes.KEY_LEFTCTRL,
            evdev.ecodes.KEY_LEFTSHIFT,
            evdev.ecodes.KEY_U,
            evdev.ecodes.KEY_E,
            evdev.ecodes.KEY_9,
            evdev.ecodes.KEY_ENTER,
        ]

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

    def test_macro_manager_initial_state_populates_macro_rows(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        from keymasq.gui.session_client import GuiTaskResult
        from keymasq.gui.widgets.macro_manager_dialog import MacroManagerDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)
        dialog = MacroManagerDialog(Gtk.Window())

        result = GuiTaskResult(
            value=(
                {
                    "recording_active": False,
                    "recording_unlock_required": False,
                    "recording_unlocked": False,
                },
                {
                    "macros": [
                        {
                            "name": "short",
                            "duration_us": 250_000,
                            "device_types": ["keyboard", "mouse"],
                            "event_count": 4,
                        },
                        {
                            "name": "long",
                            "duration_us": 1_500_000,
                            "device_types": ["gamepad"],
                            "event_count": 2,
                        },
                    ]
                },
            )
        )

        assert dialog._on_initial_state_loaded(result) is False
        assert dialog._recording_unlocked is True
        assert dialog._empty_label.get_visible() is False
        assert dialog._listbox.get_first_child() is not None
        assert dialog._record_btn.get_tooltip_text() == "Record a new macro"

        dialog._on_macros_loaded({"macros": []})

        assert dialog._empty_label.get_visible() is True

    def test_macro_manager_duplicate_request_and_finish_paths(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        from keymasq.gui.session_client import GuiTaskResult
        import keymasq.gui.widgets.macro_manager_dialog as macro_manager_dialog_module
        from keymasq.gui.widgets.macro_manager_dialog import MacroManagerDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)
        requests: list[dict] = []

        def fake_session_request(payload):
            requests.append(payload)
            if payload["command"] == "get_macro":
                return {
                    "status": "ok",
                    "macro": {
                        "name": payload["name"],
                        "revision": 9,
                        "events": [{"t_us": 1}],
                    },
                }
            if payload["command"] == "create_macro":
                return {"status": "ok"}
            return {"status": "error", "message": "unexpected"}

        monkeypatch.setattr(macro_manager_dialog_module, "session_request", fake_session_request)
        dialog = MacroManagerDialog(Gtk.Window())
        dialog._macros = [{"name": "copy"}, {"name": "copy_1"}]

        assert dialog._duplicate_macro_request("copy") == {"status": "ok"}

        create_payload = requests[-1]
        assert create_payload["command"] == "create_macro"
        assert create_payload["macro"]["name"] == "copy_2"
        assert "revision" not in create_payload["macro"]

        loaded: list[bool] = []
        monkeypatch.setattr(dialog, "_load_macros", lambda: loaded.append(True) or False)

        assert dialog._on_duplicate_finished(GuiTaskResult(value={"status": "ok"})) is False
        assert loaded == [True]

    def test_macro_manager_create_and_record_button_paths(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        import keymasq.gui.widgets.macro_manager_dialog as macro_manager_dialog_module
        from keymasq.gui.widgets.macro_manager_dialog import MacroManagerDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)
        opened: list[str] = []
        hook_requests: list[dict] = []

        class Parent(Gtk.Window):
            def present_unlock_dialog(self, on_success):
                opened.append("unlock")
                on_success()

        def fake_session_request_async(payload, callback):
            if payload["command"] == "get_status":
                callback({"recording_unlock_required": True, "recording_unlocked": True})
            elif payload["command"] == "list_macros":
                callback({"macros": [{"name": "macro"}, {"name": "macro_1"}]})

        def fake_session_request_with_hooks(payload, callback, on_done=None, **_kwargs):
            hook_requests.append(payload)
            callback({"status": "ok"})
            if on_done:
                on_done()

        monkeypatch.setattr(
            macro_manager_dialog_module,
            "session_request_async",
            fake_session_request_async,
        )
        monkeypatch.setattr(
            macro_manager_dialog_module,
            "session_request_with_hooks",
            fake_session_request_with_hooks,
        )

        dialog = MacroManagerDialog(Parent())
        opened_names: list[str] = []
        monkeypatch.setattr(dialog, "_open_empty_macro_editor", opened_names.append)

        dialog._recording_active = False
        dialog._recording_unlocked = False
        dialog._on_record_new(dialog._record_btn)
        dialog._on_empty_macro_names_loaded({"macros": [{"name": "macro"}]})

        assert opened == ["unlock"]
        assert dialog._recording_unlocked is True
        assert opened_names == ["macro_1"]

        dialog._recording_active = False
        dialog._recording_unlocked = True
        dialog._on_record_new(dialog._record_btn)
        dialog._recording_active = True
        dialog._on_record_new(dialog._record_btn)

        assert hook_requests == [
            {"command": "start_recording"},
            {"command": "stop_recording"},
        ]

    def test_type_macro_create_validates_and_submits_macro(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        import keymasq.gui.widgets.macro_manager_dialog as macro_manager_dialog_module
        from keymasq.gui.widgets.macro_manager_dialog import TypeMacroDialog

        requests: list[dict] = []
        created: list[bool] = []

        def fake_session_request_with_hooks(payload, callback, on_start=None, on_done=None):
            requests.append(payload)
            if on_start:
                on_start()
            callback({"status": "ok"})
            if on_done:
                on_done()

        monkeypatch.setattr(
            macro_manager_dialog_module,
            "session_request_with_hooks",
            fake_session_request_with_hooks,
        )
        dialog = TypeMacroDialog(Gtk.Window(), on_created=lambda: created.append(True))

        dialog.name_entry.set_text("")
        dialog._on_create(dialog._create_btn)
        assert dialog.error_label.get_label() == "Macro name is required"

        dialog.name_entry.set_text("bad name")
        dialog._on_create(dialog._create_btn)
        assert dialog.error_label.get_label() == "Only letters, numbers, underscores and hyphens"

        dialog.name_entry.set_text("typed")
        dialog.text_view.get_buffer().set_text("Hi")
        dialog.down_spin.set_value(5)
        dialog.pause_spin.set_value(7)
        dialog._on_create(dialog._create_btn)

        assert requests[0]["command"] == "create_macro"
        assert requests[0]["macro"]["name"] == "typed"
        assert requests[0]["macro"]["device_types"] == ["keyboard"]
        assert requests[0]["macro"]["events"]
        assert created == [True]
