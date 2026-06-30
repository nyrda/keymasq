# ruff: noqa: I001
import threading
import time
from types import SimpleNamespace

import pytest

from tests.gui.support import collect_widgets

gi = pytest.importorskip("gi")


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
        assert dialog._unlock_status.get_label() == "Save access locked"
        assert "saving temporary recording slots" in (
            dialog._unlock_btn.get_tooltip_text() or ""
        )

        dialog._apply_unlock_state(
            {
                "status": "ok",
                "recording_unlocked": True,
                "recording_refresh_owner": False,
            }
        )
        assert dialog._unlock_btn.get_visible() is True
        assert dialog._unlock_status.get_label() == "Unlocked in another session"
        assert "saving temporary recording slots" in (
            dialog._unlock_btn.get_tooltip_text() or ""
        )

        dialog._apply_unlock_state(
            {
                "status": "ok",
                "recording_unlocked": True,
                "recording_refresh_owner": True,
            }
        )
        assert dialog._unlock_btn.get_visible() is False
        assert dialog._unlock_status.get_label() == "Save access unlocked"

    def test_record_dialog_updates_macro_recording_opt_in_state(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

        monkeypatch.setattr(RecordMacroDialog, "_load_initial_state_async", lambda self: None)
        monkeypatch.setattr(RecordMacroDialog, "_sync_settings_async", lambda self: None)

        enabled: list[bool] = []
        disabled: list[bool] = []

        class Parent(Gtk.Window):
            def present_macro_recording_enable_dialog(self, on_success) -> None:
                enabled.append(True)
                on_success()

            def present_macro_recording_disable_dialog(self, on_success) -> None:
                disabled.append(True)
                on_success()

        def button_label(button: Gtk.Button) -> str:
            child = button.get_child()
            return " ".join(
                label.get_text()
                for label in collect_widgets(child, Gtk.Label, include_self=True)
            )

        dialog = RecordMacroDialog(Parent())
        monkeypatch.setattr(dialog, "_refresh_unlock_state_async", lambda: None)

        dialog._apply_unlock_state(
            {
                "status": "ok",
                "macro_recording_enabled": False,
                "macro_recording_source": "none",
                "macro_recording_expires_at": 0,
            }
        )
        assert dialog._macro_recording_row.get_subtitle() == "Disabled"
        assert button_label(dialog._macro_recording_toggle_btn) == "Enable"
        dialog._on_macro_recording_toggle_clicked(dialog._macro_recording_toggle_btn)
        assert enabled == [True]

        dialog._apply_unlock_state(
            {
                "status": "ok",
                "macro_recording_enabled": True,
                "macro_recording_source": "persistent",
                "macro_recording_expires_at": 0,
            }
        )
        assert dialog._macro_recording_row.get_subtitle() == "Enabled"
        assert button_label(dialog._macro_recording_toggle_btn) == "Disable"
        dialog._on_macro_recording_toggle_clicked(dialog._macro_recording_toggle_btn)
        assert disabled == [True]

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

        labels = [
            button.get_label()
            for button in collect_widgets(dialog.get_child(), Gtk.Button, include_self=True)
            if button.get_label()
        ]

        assert "Done" in labels
        assert "Cancel" not in labels
        assert "Save Settings" not in labels
        assert dialog._unlock_status.get_halign() == Gtk.Align.CENTER
        assert dialog._unlock_btn.get_parent() is dialog._save_btn.get_parent()
        assert dialog._unlock_btn.get_next_sibling() is dialog._save_btn

    def test_record_dialog_done_is_disabled_until_settings_load(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        import keymasq.gui.widgets.record_macro_dialog as record_macro_dialog_module
        from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

        requests: list[dict[str, object]] = []

        def fake_session_request(payload, timeout=5.0):
            requests.append(dict(payload))
            return {"status": "ok"}

        monkeypatch.setattr(RecordMacroDialog, "_load_initial_state_async", lambda self: None)
        monkeypatch.setattr(record_macro_dialog_module, "session_request", fake_session_request)

        dialog = RecordMacroDialog(Gtk.Window())

        assert dialog._save_btn.get_sensitive() is False
        dialog._on_save_settings(dialog._save_btn)

        assert requests == []

    def test_record_dialog_ignores_initial_state_after_close(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

        monkeypatch.setattr(RecordMacroDialog, "_load_initial_state_async", lambda self: None)
        dialog = RecordMacroDialog(Gtk.Window())
        monkeypatch.setattr(
            dialog,
            "_populate_device_list",
            lambda: pytest.fail("closed dialog should not populate devices"),
        )

        dialog._on_dialog_closed(dialog)

        assert (
            dialog._apply_initial_state(
                {"status": "ok", "devices": [{"name": "Keyboard"}]},
                {"status": "ok"},
            )
            is False
        )
        assert dialog._devices == []
        assert dialog._settings_loaded is False

    def test_record_dialog_locked_reason_explains_blocked_recording(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

        monkeypatch.setattr(RecordMacroDialog, "_load_initial_state_async", lambda self: None)

        dialog = RecordMacroDialog(Gtk.Window(), reason="recording_locked")

        assert dialog._title_label.get_label() == "Macro Recording Settings"
        assert dialog._locked_notice.get_visible() is True
        assert dialog._locked_notice_title.get_label() == "Saving needs unlock"

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
        dialog._settings_loaded = True
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
                "recording_id": "physical:/dev/input/by-id/usb-Test_Mouse-event-mouse",
                "recording_kind": "physical",
                "device_type": "mouse",
                "device_types": ["mouse"],
                "name": "Raw Mouse",
            },
        ]

        dialog._populate_device_list()

        assert dialog._device_checks["keymasq:output:keyboard"].get_active() is True
        assert (
            dialog._device_checks[
                "physical:/dev/input/by-id/usb-Test_Mouse-event-mouse"
            ].get_active()
            is False
        )
        assert dialog._selection_summary.get_label() == "1 selected (1kb)"

    def test_record_dialog_source_row_activation_toggles_checkbox(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

        monkeypatch.setattr(RecordMacroDialog, "_load_initial_state_async", lambda self: None)
        monkeypatch.setattr(RecordMacroDialog, "_sync_settings_async", lambda self: None)

        dialog = RecordMacroDialog(Gtk.Window())
        dialog._apply_recording_settings({"status": "ok", "device_overrides": {}})
        dialog._settings_loaded = True
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
                "recording_id": "physical:/dev/input/by-id/usb-Test_Mouse-event-mouse",
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
            if (
                getattr(row, "_recording_id", "")
                == "physical:/dev/input/by-id/usb-Test_Mouse-event-mouse"
            ):
                raw_row = row
                break
            row = row.get_next_sibling()

        assert raw_row is not None
        dialog._on_device_row_activated(dialog._device_listbox, raw_row)
        assert (
            dialog._device_checks[
                "physical:/dev/input/by-id/usb-Test_Mouse-event-mouse"
            ].get_active()
            is True
        )
        assert dialog._device_overrides == {
            "physical:/dev/input/by-id/usb-Test_Mouse-event-mouse": True
        }
        assert dialog._selection_summary.get_label() == "2 selected (1kb, 1m)"

        dialog._on_device_row_activated(dialog._device_listbox, raw_row)
        assert (
            dialog._device_checks[
                "physical:/dev/input/by-id/usb-Test_Mouse-event-mouse"
            ].get_active()
            is False
        )
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
        dialog._settings_loaded = True
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
                "recording_id": "physical:/dev/input/by-id/usb-Test_Mouse-event-mouse",
                "recording_kind": "physical",
                "device_type": "mouse",
                "device_types": ["mouse"],
                "name": "Raw Mouse",
            },
        ]

        dialog._populate_device_list()
        assert dialog._selection_warning.get_visible() is True

        dialog._on_select_type_clicked(Gtk.Button(), "mouse", True)
        assert (
            dialog._device_checks[
                "physical:/dev/input/by-id/usb-Test_Mouse-event-mouse"
            ].get_active()
            is True
        )
        assert dialog._selection_summary.get_label() == "2 selected (1kb, 1m)"
        assert dialog._selection_warning.get_visible() is False

        dialog._on_reset_to_recommended_clicked(Gtk.Button())
        assert (
            dialog._device_checks[
                "physical:/dev/input/by-id/usb-Test_Mouse-event-mouse"
            ].get_active()
            is False
        )
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
        dialog._settings_loaded = True
        dialog._device_overrides = {
            "physical:/dev/input/by-id/usb-Test_Mouse-event-mouse": True
        }

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
            {"physical:/dev/input/by-id/usb-Test_Mouse-event-mouse": True},
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
        assert overlay._timer_id != 0

        assert callbacks[0][1]({"status": "error", "message": "Daemon unavailable"}) is False
        assert overlay._stop_btn.get_sensitive() is True
        assert overlay._stop_btn.get_label() == "Stop Recording"
        assert overlay._timer_id != 0
        assert overlay._status_label is not None
        assert overlay._status_label.get_visible() is True
        assert overlay._status_label.get_label() == "Daemon unavailable"

        overlay._on_stop_clicked(overlay._stop_btn)
        assert overlay._status_label.get_visible() is False

        assert callbacks[1][1]({"status": "ok"}) is False
        assert overlay._stop_btn.get_sensitive() is True
        assert overlay._stop_btn.get_label() == "Stop Recording"
        assert overlay._timer_id == 0

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

        def fake_session_request_async(payload, callback, on_start=None, on_done=None):
            captured.update(payload)
            if on_start:
                on_start()
            callback({"status": "ok"})
            if on_done:
                on_done()

        monkeypatch.setattr(
            save_macro_dialog_module,
            "session_request_async",
            fake_session_request_async,
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
        assert "move_to_start" not in captured
        assert "start_x" not in captured
        assert "start_y" not in captured

    def test_save_macro_dialog_requires_explicit_unlock_before_saving(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        import keymasq.gui.widgets.save_macro_dialog as save_macro_dialog_module
        from keymasq.gui.widgets.save_macro_dialog import SaveMacroDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)
        requests: list[dict[str, object]] = []
        unlock_callbacks = []

        class Parent(Gtk.Window):
            _recording_unlock_required = True
            _recording_unlocked = False
            _recording_refresh_owner = False

            def present_unlock_dialog(self, on_success=None):
                unlock_callbacks.append(on_success)

        def fake_session_request_async(payload, callback, on_start=None, on_done=None):
            requests.append(payload)
            if on_start:
                on_start()
            callback({"status": "ok"})
            if on_done:
                on_done()

        monkeypatch.setattr(
            save_macro_dialog_module,
            "session_request_async",
            fake_session_request_async,
        )

        parent = Parent()
        dialog = SaveMacroDialog(
            parent,
            {
                "duration_ms": 100,
                "event_count": 2,
                "pending_save_token": "pending-1",
            },
        )
        dialog._name_entry.set_text("macro_1")
        dialog._on_save_clicked(dialog._save_btn)

        assert requests == []
        assert unlock_callbacks == []
        assert dialog._locked_notice.get_visible() is True
        assert dialog._unlock_btn.get_visible() is True
        assert dialog._save_btn.get_sensitive() is False

        dialog._unlock_btn.emit("clicked")

        assert requests == []
        assert len(unlock_callbacks) == 1

        parent._recording_unlocked = True
        parent._recording_refresh_owner = True
        unlock_callbacks[0]()

        assert requests == []
        assert dialog._locked_notice.get_visible() is False
        assert dialog._unlock_btn.get_visible() is False
        assert dialog._save_btn.get_sensitive() is True

        dialog._on_save_clicked(dialog._save_btn)

        assert requests[0]["command"] == "save_recording"
        assert requests[0]["pending_save_token"] == "pending-1"

    def test_save_macro_dialog_shows_recorded_start_position_without_save_fields(
        self, monkeypatch
    ):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        from keymasq.gui.widgets.save_macro_dialog import SaveMacroDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)

        dialog = SaveMacroDialog(
            Gtk.Window(),
            {
                "duration_ms": 100,
                "event_count": 2,
                "device_types": ["keyboard"],
                "start_position_recorded": True,
            },
        )

        payload = dialog._save_payload("macro_1")

        assert dialog._start_position_recorded is True
        assert "move_to_start" not in payload
        assert "start_x" not in payload
        assert "start_y" not in payload

    def test_save_macro_dialog_save_edit_opens_editor_with_first_event_selected(
        self, monkeypatch
    ):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        import keymasq.gui.widgets.macro_editor_dialog as macro_editor_dialog_module
        import keymasq.gui.widgets.save_macro_dialog as save_macro_dialog_module
        from keymasq.gui.widgets.save_macro_dialog import SaveMacroDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: callback(*args) or 0)
        captured: dict[str, object] = {}

        def fake_session_request_async(payload, callback, on_start=None, on_done=None):
            if payload["command"] == "list_macros":
                callback({"status": "ok", "macros": []})
                return
            captured["save_payload"] = payload
            if on_start:
                on_start()
            callback({"status": "ok", "name": "macro_1"})
            if on_done:
                on_done()

        class DummyEditorDialog:
            def __init__(self, parent, name, *, select_initial_event=True):
                captured["editor_parent"] = parent
                captured["editor_name"] = name
                captured["select_initial_event"] = select_initial_event

            def present(self, parent):
                captured["present_parent"] = parent

        monkeypatch.setattr(
            save_macro_dialog_module,
            "session_request_async",
            fake_session_request_async,
        )
        monkeypatch.setattr(macro_editor_dialog_module, "MacroEditorDialog", DummyEditorDialog)

        parent = Gtk.Window()
        dialog = SaveMacroDialog(
            parent,
            {
                "duration_ms": 100,
                "event_count": 2,
                "device_types": ["keyboard", "mouse"],
                "pending_save_token": "pending-1",
                "start_position_recorded": True,
            },
        )
        dialog._name_entry.set_text("macro_1")

        dialog._on_save_edit_clicked(dialog._save_edit_btn)

        assert captured["save_payload"] == {
            "command": "save_recording",
            "name": "macro_1",
            "block_mouse_movement": False,
            "pending_save_token": "pending-1",
        }
        assert captured["editor_parent"] is parent
        assert captured["editor_name"] == "macro_1"
        assert captured["select_initial_event"] is True
        assert captured["present_parent"] is parent

    def test_save_macro_dialog_keeps_footer_anchored_after_unlock(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        from keymasq.gui.widgets.save_macro_dialog import SaveMacroDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)

        class Parent(Gtk.Window):
            _recording_unlock_required = True
            _recording_unlocked = False
            _recording_refresh_owner = False

        parent = Parent()
        dialog = SaveMacroDialog(
            parent,
            {
                "duration_ms": 100,
                "event_count": 2,
                "pending_save_token": "pending-1",
            },
        )

        assert dialog._locked_notice.get_visible() is True
        assert dialog._layout_frame.get_vexpand() is True
        assert dialog._content_box.get_vexpand() is True

        parent._recording_unlocked = True
        parent._recording_refresh_owner = True
        dialog._on_unlock_success()

        assert dialog._locked_notice.get_visible() is False
        assert dialog._layout_frame.get_vexpand() is True
        assert dialog._content_box.get_vexpand() is True

    def test_save_macro_dialog_failed_save_allows_retry(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        import keymasq.gui.widgets.save_macro_dialog as save_macro_dialog_module
        from keymasq.gui.widgets.save_macro_dialog import SaveMacroDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)

        def fake_session_request_async(payload, callback, on_start=None, on_done=None):
            if on_start:
                on_start()
            callback({"status": "error", "message": "session unavailable"})
            if on_done:
                on_done()

        monkeypatch.setattr(
            save_macro_dialog_module,
            "session_request_async",
            fake_session_request_async,
        )

        dialog = SaveMacroDialog(
            Gtk.Window(),
            {
                "duration_ms": 100,
                "event_count": 2,
                "device_types": ["keyboard"],
            },
        )
        dialog._name_entry.set_text("macro_1")

        dialog._on_save_clicked(dialog._save_btn)

        assert dialog._saved is False
        assert dialog._save_btn.get_sensitive() is True
        assert dialog._error_label.get_label() == "session unavailable"
        assert dialog.get_can_close() is True

    def test_save_macro_dialog_locked_save_result_requires_explicit_unlock(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        import keymasq.gui.widgets.save_macro_dialog as save_macro_dialog_module
        from keymasq.gui.widgets.save_macro_dialog import SaveMacroDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)
        unlock_callbacks = []

        class Parent(Gtk.Window):
            _recording_unlock_required = True
            _recording_unlocked = True
            _recording_refresh_owner = True

            def present_unlock_dialog(self, on_success=None):
                unlock_callbacks.append(on_success)

        def fake_session_request_async(payload, callback, on_start=None, on_done=None):
            if on_start:
                on_start()
            callback(
                {
                    "status": "error",
                    "error_code": "recording_locked",
                    "message": "locked",
                }
            )
            if on_done:
                on_done()

        monkeypatch.setattr(
            save_macro_dialog_module,
            "session_request_async",
            fake_session_request_async,
        )

        dialog = SaveMacroDialog(
            Parent(),
            {
                "duration_ms": 100,
                "event_count": 2,
                "device_types": ["keyboard"],
            },
        )
        dialog._name_entry.set_text("macro_1")

        dialog._on_save_clicked(dialog._save_btn)

        assert unlock_callbacks == []
        assert dialog._error_label.get_label() == "Unlock before saving this slot."
        assert dialog._locked_notice.get_visible() is True
        assert dialog._unlock_btn.get_visible() is True
        assert dialog._save_btn.get_sensitive() is False

        dialog._unlock_btn.emit("clicked")

        assert len(unlock_callbacks) == 1

    def test_save_macro_dialog_keeps_user_name_when_macro_list_loads(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        from keymasq.gui.widgets.save_macro_dialog import SaveMacroDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)

        dialog = SaveMacroDialog(
            Gtk.Window(),
            {
                "duration_ms": 100,
                "event_count": 2,
                "device_types": ["keyboard"],
            },
        )
        dialog._name_entry.set_text("custom_macro")

        dialog._on_existing_macro_names_loaded({"macros": [{"name": "macro"}]})

        assert dialog._name_entry.get_text() == "custom_macro"
        assert dialog._save_btn.get_sensitive() is True

    def test_save_macro_dialog_name_validation_matches_backend_normalization(
        self, monkeypatch
    ):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        from keymasq.gui.widgets.save_macro_dialog import SaveMacroDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)

        dialog = SaveMacroDialog(
            Gtk.Window(),
            {
                "duration_ms": 100,
                "event_count": 2,
                "device_types": ["keyboard"],
            },
        )
        dialog._on_existing_macro_names_loaded({"macros": [{"name": "macro.v1"}]})

        dialog._name_entry.set_text("macro.v2")
        assert dialog._save_btn.get_sensitive() is True

        dialog._name_entry.set_text("_")
        assert dialog._save_btn.get_sensitive() is False
        assert dialog._error_label.get_label() == "Name cannot be empty"

        dialog._name_entry.set_text(".macro.v1_")
        assert dialog._save_btn.get_sensitive() is False
        assert dialog._error_label.get_label() == "A macro named 'macro.v1' already exists"

    def test_save_macro_dialog_later_keeps_pending_slot(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        import keymasq.gui.widgets.save_macro_dialog as save_macro_dialog_module
        from keymasq.gui.widgets.save_macro_dialog import SaveMacroDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)
        requests: list[dict[str, object]] = []

        def fake_session_request_async(payload, callback, on_start=None, on_done=None):
            requests.append(dict(payload))

        monkeypatch.setattr(
            save_macro_dialog_module,
            "session_request_async",
            fake_session_request_async,
        )

        dialog = SaveMacroDialog(
            Gtk.Window(),
            {
                "duration_ms": 100,
                "event_count": 2,
                "device_types": ["keyboard"],
                "pending_save_token": "pending-1",
                "recording_slot": 1,
            },
        )
        force_closed: list[bool] = []
        monkeypatch.setattr(dialog, "force_close", lambda: force_closed.append(True))

        dialog._on_later_clicked(Gtk.Button())

        assert force_closed == [True]
        assert requests == []
        assert dialog._closing_after_resolution is True
        assert dialog._saved is False

    def test_save_macro_dialog_hides_later_for_unslotted_recording(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        from keymasq.gui.widgets.save_macro_dialog import SaveMacroDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)

        dialog = SaveMacroDialog(
            Gtk.Window(),
            {
                "duration_ms": 100,
                "event_count": 2,
                "device_types": ["keyboard"],
                "pending_save_token": "pending-1",
            },
        )

        assert dialog._later_btn is None

    def test_save_macro_dialog_close_keeps_pending_slot(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        import keymasq.gui.widgets.save_macro_dialog as save_macro_dialog_module
        from keymasq.gui.widgets.save_macro_dialog import SaveMacroDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)
        requests: list[dict[str, object]] = []

        def fake_session_request_async(payload, callback, on_start=None, on_done=None):
            requests.append(dict(payload))

        monkeypatch.setattr(
            save_macro_dialog_module,
            "session_request_async",
            fake_session_request_async,
        )

        dialog = SaveMacroDialog(
            Gtk.Window(),
            {
                "duration_ms": 100,
                "event_count": 2,
                "device_types": ["keyboard"],
                "pending_save_token": "pending-1",
                "recording_slot": 1,
            },
        )
        force_closed: list[bool] = []
        monkeypatch.setattr(dialog, "force_close", lambda: force_closed.append(True))

        dialog.do_close_attempt()

        assert force_closed == [True]
        assert requests == []
        assert dialog._closing_after_resolution is True
        assert dialog._saved is False

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

        assert dialog.overload_row.get_tooltip_text() == (
            "Main Actions start before On Press and stay held until after On Release, "
            "so they can provide a held modifier or context for both press/release lists."
        )
        assert "Held while pressed, released when you let go" in dialog.overload_row.get_subtitle()
        assert "(none)" in dialog.overload_row.get_subtitle()

        dialog._populate_action_row(
            dialog.overload_row,
            [MappingAction(action_type=ActionType.KEYBOARD, target="key_leftctrl")],
        )

        assert "Held while pressed, released when you let go" in dialog.overload_row.get_subtitle()
        assert "1 action" in dialog.overload_row.get_subtitle()
        assert dialog.overload_down_row.get_subtitle() == "(none)"
        assert dialog.overload_up_row.get_subtitle() == "(none)"

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

    def test_superkey_dialog_shows_storage_collision_error(self, temp_config_dir, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.common import paths
        from keymasq.common.models import ActionType, SuperkeyAction, SuperkeyConfig, SuperkeyMode
        import keymasq.gui.widgets.superkey_dialog as superkey_dialog_module
        from keymasq.gui.widgets.superkey_dialog import SuperkeyDialog

        monkeypatch.setattr(paths, "SUPERKEYS_DIR", temp_config_dir / "superkeys")

        dialog = SuperkeyDialog(Gtk.Window())
        alerts: list[tuple[object, object]] = []
        monkeypatch.setattr(
            superkey_dialog_module.Adw.AlertDialog,
            "present",
            lambda alert, parent: alerts.append((alert, parent)),
        )
        dialog.manager.save_superkey(
            SuperkeyConfig(
                name="A B",
                mode=SuperkeyMode.PATTERN,
                tap_actions=[SuperkeyAction(action_type=ActionType.KEYBOARD, target="key_a")],
            )
        )

        dialog.start_new_superkey()
        dialog.name_entry.set_text("A_B")

        assert dialog._save_current_superkey() is False
        assert len(alerts) == 1
        alert = alerts[0][0]
        assert alert.get_heading() == "Unable To Save Super Key"
        assert "conflicts with existing superkey 'A B'" in alert.get_body()
        assert dialog.manager.get_superkey("A_B") is None

    def test_superkey_dialog_empty_state_starts_new_draft_and_keeps_close_available(
        self, temp_config_dir, monkeypatch
    ):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gdk, Gtk

        from keymasq.common import paths
        import keymasq.gui.widgets.superkey_dialog as superkey_dialog_module
        from keymasq.gui.widgets.superkey_dialog import SuperkeyDialog

        monkeypatch.setattr(paths, "SUPERKEYS_DIR", temp_config_dir / "superkeys")

        dialog = SuperkeyDialog(Gtk.Window())
        closed: list[bool] = []
        alerts: list[tuple[object, object]] = []
        monkeypatch.setattr(dialog, "force_close", lambda: closed.append(True))
        monkeypatch.setattr(
            superkey_dialog_module.Adw.AlertDialog,
            "present",
            lambda alert, parent: alerts.append((alert, parent)),
        )

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
        assert dialog.get_can_close() is False

        dialog.close_btn.emit("clicked")
        assert closed == []
        assert len(alerts) == 1
        assert alerts[0][1] is dialog

        dialog._on_unsaved_close_response(alerts[0][0], "cancel")
        assert closed == []

        assert dialog._on_key_pressed(None, Gdk.KEY_Escape, 0, 0) is True
        assert closed == []
        assert len(alerts) == 2

        dialog._on_unsaved_close_response(alerts[1][0], "discard")
        assert closed == [True]
        assert dialog.get_can_close() is True

    def test_superkey_dialog_unsaved_close_save_response_saves_and_closes(
        self, temp_config_dir, monkeypatch
    ):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.common import paths
        import keymasq.gui.widgets.superkey_dialog as superkey_dialog_module
        from keymasq.gui.widgets.superkey_dialog import SuperkeyDialog

        monkeypatch.setattr(paths, "SUPERKEYS_DIR", temp_config_dir / "superkeys")

        dialog = SuperkeyDialog(Gtk.Window())
        closed: list[bool] = []
        alerts: list[tuple[object, object]] = []
        saved: list[str] = []
        monkeypatch.setattr(dialog, "force_close", lambda: closed.append(True))
        monkeypatch.setattr(
            superkey_dialog_module.Adw.AlertDialog,
            "present",
            lambda alert, parent: alerts.append((alert, parent)),
        )
        dialog.connect("superkey-saved", lambda _dialog, name: saved.append(name))

        dialog.name_entry.set_text("close_saved")
        dialog._request_close()

        assert closed == []
        assert len(alerts) == 1

        dialog._on_unsaved_close_response(alerts[0][0], "save")

        assert closed == [True]
        assert saved == ["close_saved"]
        assert dialog.manager.get_superkey("close_saved") is not None
        assert dialog.get_can_close() is True

    def test_superkey_dialog_unsaved_selection_warns_and_can_discard(
        self, temp_config_dir, monkeypatch
    ):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        import keymasq.gui.widgets.superkey_dialog as superkey_dialog_module
        from keymasq.common.models import SuperkeyConfig
        from keymasq.gui.widgets.superkey_dialog import SuperkeyDialog
        from keymasq.session.superkeys import SuperkeyManager

        manager = SuperkeyManager()
        manager.save_superkey(SuperkeyConfig(name="Alpha"))
        manager.save_superkey(SuperkeyConfig(name="Beta"))

        alerts: list[tuple[object, object]] = []
        monkeypatch.setattr(
            superkey_dialog_module.Adw.AlertDialog,
            "present",
            lambda alert, parent: alerts.append((alert, parent)),
        )

        dialog = SuperkeyDialog(Gtk.Window())

        def row_for(name: str):
            idx = 0
            while row := dialog.list_box.get_row_at_index(idx):
                if getattr(row, "_superkey_name", None) == name:
                    return row
                idx += 1
            raise AssertionError(f"missing row {name}")

        alpha_row = row_for("Alpha")
        beta_row = row_for("Beta")
        assert dialog.list_box.get_selected_row() is alpha_row

        dialog.desc_entry.set_text("dirty")
        dialog.list_box.select_row(beta_row)

        assert len(alerts) == 1
        assert alerts[0][1] is dialog
        assert dialog.list_box.get_selected_row() is alpha_row
        assert dialog._current_config is not None
        assert dialog._current_config.name == "Alpha"
        assert dialog.desc_entry.get_text() == "dirty"

        dialog._on_unsaved_selection_response(alerts[0][0], "discard")

        assert dialog.list_box.get_selected_row() is beta_row
        assert dialog._current_config is not None
        assert dialog._current_config.name == "Beta"
        assert dialog._modified is False

    def test_superkey_dialog_select_superkey_by_name_selects_saved_superkey(
        self, temp_config_dir
    ):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.common.models import SuperkeyConfig
        from keymasq.gui.widgets.superkey_dialog import SuperkeyDialog
        from keymasq.session.superkeys import SuperkeyManager

        manager = SuperkeyManager()
        manager.save_superkey(SuperkeyConfig(name="Alpha"))
        manager.save_superkey(SuperkeyConfig(name="Beta"))

        dialog = SuperkeyDialog(Gtk.Window())

        def row_for(name: str):
            idx = 0
            while row := dialog.list_box.get_row_at_index(idx):
                if getattr(row, "_superkey_name", None) == name:
                    return row
                idx += 1
            raise AssertionError(f"missing row {name}")

        dialog.select_superkey_by_name("Beta")

        beta_row = row_for("Beta")
        assert dialog.list_box.get_selected_row() is beta_row
        assert dialog._current_config is not None
        assert dialog._current_config.name == "Beta"
        assert dialog.name_entry.get_text() == "Beta"

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
            "allow_repeat": False,
            "allow_rapidfire": True,
            "allow_tap": False,
            "allow_macro_options": True,
        }

    def test_overload_superkey_actions_disable_repeat_selector(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.common.models import ActionType, MappingAction
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

        dialog = ActionListDialog(parent, "Overload Actions", "overload")
        dialog._open_child_editor(
            MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
            1,
        )

        assert captured["signal_name"] == "key-selected"
        assert captured["presented"] is True
        assert captured["parent"] is parent
        assert captured["present_parent"] is parent
        current_action = captured["current_action"]
        assert isinstance(current_action, MappingAction)
        assert current_action.action_type == ActionType.KEYBOARD
        assert captured["kwargs"] == {
            "allow_passthrough": False,
            "allow_clear_mapping": False,
            "allow_suppress": False,
            "allow_superkey": False,
            "allow_repeat": False,
        }

    def test_overload_pulse_action_editors_describe_down_and_up_timing(self, temp_config_dir):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keymasq.gui.widgets.superkey_dialog import ActionListDialog

        parent = Gtk.Window()
        down_dialog = ActionListDialog(
            parent,
            "Edit On Press",
            "overload",
            action_key="overload_down",
        )
        up_dialog = ActionListDialog(
            parent,
            "Edit On Release",
            "overload",
            action_key="overload_up",
        )

        down_label = down_dialog.get_child().get_first_child()
        up_label = up_dialog.get_child().get_first_child()

        assert down_label.get_label() == (
            "Actions go through their press/release cycle when the key goes down."
        )
        assert up_label.get_label() == (
            "Actions go through their press/release cycle when the key comes up."
        )

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
        dialog._macro_recording_enabled = False
        dialog._sync_record_button_state()
        assert dialog._record_btn.get_visible() is False
        assert dialog._slot_dropdown is not None
        assert dialog._slot_dropdown.get_visible() is False
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
                    "macro_recording_enabled": True,
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
        assert dialog._macro_recording_enabled is True
        assert dialog._empty_label.get_visible() is False
        assert dialog._listbox.get_first_child() is not None
        assert dialog._record_btn.get_visible() is True
        assert dialog._slot_dropdown is not None
        assert dialog._slot_dropdown.get_visible() is True
        assert dialog._record_btn.get_tooltip_text() == "Record a new macro"

        dialog._on_macros_loaded({"macros": []})

        assert dialog._empty_label.get_visible() is True

    def test_macro_manager_list_load_errors_keep_existing_rows(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        from keymasq.gui.session_client import GuiTaskResult
        import keymasq.gui.widgets.macro_manager_dialog as macro_manager_dialog_module
        from keymasq.gui.widgets.macro_manager_dialog import MacroManagerDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)
        alerts: list[tuple[object, object]] = []
        monkeypatch.setattr(
            macro_manager_dialog_module.Adw.AlertDialog,
            "present",
            lambda alert, parent: alerts.append((alert, parent)),
        )

        parent = Gtk.Window()
        dialog = MacroManagerDialog(parent)
        dialog._on_macros_loaded(
            {
                "status": "ok",
                "macros": [
                    {
                        "name": "stored",
                        "duration_us": 250_000,
                        "device_types": ["keyboard"],
                        "event_count": 2,
                    }
                ],
            }
        )

        assert dialog._listbox.get_row_at_index(0) is not None
        assert dialog._empty_label.get_visible() is False

        assert dialog._on_macros_loaded({"status": "error", "message": "boom"}) is False

        assert dialog._macros[0]["name"] == "stored"
        assert dialog._listbox.get_row_at_index(0) is not None
        assert dialog._empty_label.get_visible() is False
        assert len(alerts) == 1
        alert, alert_parent = alerts[0]
        assert alert_parent is parent
        assert alert.get_heading() == "Load Macros"
        assert alert.get_body() == "boom"

        result = GuiTaskResult(
            value=(
                {"macro_recording_enabled": True},
                {"status": "error", "message": "initial boom"},
            )
        )

        assert dialog._on_initial_state_loaded(result) is False
        assert dialog._macros[0]["name"] == "stored"
        assert dialog._listbox.get_row_at_index(0) is not None
        assert dialog._empty_label.get_visible() is False
        assert len(alerts) == 2
        assert alerts[1][0].get_body() == "initial boom"

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

    def test_macro_manager_delete_failure_shows_error(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        import keymasq.gui.widgets.macro_manager_dialog as macro_manager_dialog_module
        from keymasq.gui.widgets.macro_manager_dialog import MacroManagerDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)
        requests: list[dict] = []
        alerts: list[tuple[object, object]] = []

        def fake_session_request_async(payload, callback, **_kwargs):
            requests.append(payload)
            callback({"status": "error", "message": "boom"})

        monkeypatch.setattr(
            macro_manager_dialog_module,
            "session_request_async",
            fake_session_request_async,
        )
        monkeypatch.setattr(
            macro_manager_dialog_module.Adw.AlertDialog,
            "present",
            lambda alert, parent: alerts.append((alert, parent)),
        )

        parent = Gtk.Window()
        dialog = MacroManagerDialog(parent)
        loaded: list[bool] = []
        monkeypatch.setattr(dialog, "_load_macros", lambda: loaded.append(True) or False)

        dialog._on_delete_response(None, "delete", "stored")

        assert requests == [{"command": "delete_macro", "name": "stored"}]
        assert loaded == []
        assert len(alerts) == 1
        alert, alert_parent = alerts[0]
        assert alert_parent is parent
        assert alert.get_heading() == "Delete Macro"
        assert alert.get_body() == "boom"

    def test_macro_manager_delete_slot_sends_slot_command(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        import keymasq.gui.widgets.macro_manager_dialog as macro_manager_dialog_module
        from keymasq.gui.widgets.macro_manager_dialog import MacroManagerDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)
        requests: list[dict] = []

        def fake_session_request_async(payload, callback, **_kwargs):
            requests.append(payload)
            callback({"status": "ok"})

        monkeypatch.setattr(
            macro_manager_dialog_module,
            "session_request_async",
            fake_session_request_async,
        )

        dialog = MacroManagerDialog(Gtk.Window())
        loaded: list[bool] = []
        monkeypatch.setattr(dialog, "_load_macros", lambda: loaded.append(True) or False)

        dialog._on_delete_slot_clicked(
            Gtk.Button(),
            {
                "kind": "recording_slot",
                "recording_slot": 2,
                "pending_save_token": "pending-2",
            },
        )

        assert requests == [
            {
                "command": "delete_recording_slot",
                "recording_slot": 2,
                "pending_save_token": "pending-2",
            }
        ]
        assert loaded == [True]

    def test_macro_manager_create_and_record_button_paths(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        import keymasq.gui.widgets.macro_manager_dialog as macro_manager_dialog_module
        from keymasq.gui.widgets.macro_manager_dialog import MacroManagerDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)
        async_requests: list[dict] = []

        def fake_session_request_async(payload, callback, on_done=None, **_kwargs):
            if payload["command"] == "get_status":
                callback(
                    {
                        "recording_unlock_required": True,
                        "recording_unlocked": True,
                        "macro_recording_enabled": True,
                    }
                )
            elif payload["command"] == "list_macros":
                callback({"macros": [{"name": "macro"}, {"name": "macro_1"}]})
            else:
                async_requests.append(payload)
                callback({"status": "ok"})
                if on_done:
                    on_done()

        monkeypatch.setattr(
            macro_manager_dialog_module,
            "session_request_async",
            fake_session_request_async,
        )

        dialog = MacroManagerDialog(Gtk.Window())
        opened_names: list[str] = []
        monkeypatch.setattr(dialog, "_open_empty_macro_editor", opened_names.append)

        dialog._recording_active = False
        dialog._macro_recording_enabled = False
        dialog._sync_record_button_state()
        dialog._on_empty_macro_names_loaded({"macros": [{"name": "macro"}]})

        assert dialog._record_btn.get_visible() is False
        assert dialog._slot_dropdown is not None
        assert dialog._slot_dropdown.get_visible() is False
        assert opened_names == ["macro_1"]

        dialog._recording_active = False
        dialog._macro_recording_enabled = True
        dialog._sync_record_button_state()
        assert dialog._record_btn.get_visible() is True
        assert dialog._slot_dropdown is not None
        assert dialog._slot_dropdown.get_visible() is True
        dialog._on_record_new(dialog._record_btn)
        dialog._recording_active = True
        dialog._sync_record_button_state()
        assert dialog._slot_dropdown.get_sensitive() is False
        dialog._on_record_new(dialog._record_btn)

        assert async_requests == [
            {"command": "start_recording", "recording_slot": 1},
            {"command": "stop_recording", "recording_slot": 1},
        ]

    def test_macro_manager_stop_uses_active_slot_when_dropdown_changes(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        import keymasq.gui.widgets.macro_manager_dialog as macro_manager_dialog_module
        from keymasq.gui.widgets.macro_manager_dialog import MacroManagerDialog

        monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)
        requests: list[dict] = []

        def fake_session_request_async(payload, callback, on_done=None, **_kwargs):
            requests.append(payload)
            callback({"status": "ok"})
            if on_done:
                on_done()

        monkeypatch.setattr(
            macro_manager_dialog_module,
            "session_request_async",
            fake_session_request_async,
        )

        dialog = MacroManagerDialog(Gtk.Window())
        dialog._macro_recording_enabled = True
        assert dialog._slot_dropdown is not None
        dialog._slot_dropdown.set_selected(2)
        dialog._sync_record_button_state()
        dialog._on_record_new(dialog._record_btn)

        dialog._recording_active = True
        dialog._sync_record_button_state()
        dialog._slot_dropdown.set_selected(1)
        dialog._on_record_new(dialog._record_btn)

        assert dialog._recording_slot == 3
        assert requests == [
            {"command": "start_recording", "recording_slot": 3},
            {"command": "stop_recording", "recording_slot": 3},
        ]

    def test_type_macro_create_validates_and_submits_macro(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        import keymasq.gui.widgets.macro_manager_dialog as macro_manager_dialog_module
        from keymasq.gui.widgets.macro_manager_dialog import TypeMacroDialog

        requests: list[dict] = []
        created: list[bool] = []

        def fake_session_request_async(payload, callback, on_start=None, on_done=None):
            requests.append(payload)
            if on_start:
                on_start()
            callback({"status": "ok"})
            if on_done:
                on_done()

        monkeypatch.setattr(
            macro_manager_dialog_module,
            "session_request_async",
            fake_session_request_async,
        )
        dialog = TypeMacroDialog(Gtk.Window(), on_created=lambda: created.append(True))

        dialog.name_entry.set_text("")
        dialog._on_create(dialog._create_btn)
        assert dialog.error_label.get_label() == "Macro name is required"

        dialog.name_entry.set_text("bad name")
        dialog._on_create(dialog._create_btn)
        assert dialog.error_label.get_label() == "Only letters, numbers, underscores and hyphens"

        dialog.name_entry.set_text("typed")
        dialog.text_view.get_buffer().set_text("Hi<click>")
        dialog.down_spin.set_value(5)
        dialog.pause_spin.set_value(7)
        dialog._on_create(dialog._create_btn)

        assert requests[0]["command"] == "create_macro"
        assert requests[0]["macro"]["name"] == "typed"
        assert requests[0]["macro"]["device_types"] == ["keyboard", "mouse"]
        assert requests[0]["macro"]["type_binding"] is True
        assert requests[0]["macro"]["type_text"] == "Hi<click>"
        assert requests[0]["macro"]["type_down_ms"] == 5
        assert requests[0]["macro"]["type_pause_ms"] == 7
        assert requests[0]["macro"]["type_use_unicode_input"] is False
        assert requests[0]["macro"]["events"]
        assert created == [True]
