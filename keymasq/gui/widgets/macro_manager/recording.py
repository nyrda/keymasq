"""Macro recording state-machine controller."""

# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.actions import MAX_MACRO_RECORDING_SLOTS
from keymasq.gui.session_client import JsonDict

log = logging.getLogger("keymasq.gui.widgets.macro_manager_dialog")


class RecordingControllerMixin:
    """Coordinate recording transitions, session requests, and GTK controls."""

    def _apply_recording_status(self, status: JsonDict) -> None:
        self._recording_state.active = bool(status.get("recording_active", False))
        unlock_required = bool(status.get("recording_unlock_required", True))
        self._recording_state.unlocked = (
            bool(status.get("recording_unlocked", False)) or not unlock_required
        )
        self._recording_state.enabled = bool(status.get("macro_recording_enabled", False))
        active_slot = int(status.get("recording_slot", 0) or 0)
        if 1 <= active_slot <= MAX_MACRO_RECORDING_SLOTS:
            self._recording_state.selected_slot = active_slot
            if self._recording_state.active:
                self._recording_state.active_slot = active_slot
            if self._slot_dropdown is not None:
                self._slot_dropdown.set_selected(active_slot - 1)
        self._sync_record_button_state()

    def _on_recording_slot_changed(self, dropdown: Gtk.DropDown, _pspec) -> None:
        selected = int(dropdown.get_selected())
        resolved = self._recording_state.select_index(
            selected,
            max_slots=MAX_MACRO_RECORDING_SLOTS,
        )
        if resolved != selected:
            dropdown.set_selected(resolved)

    def _on_record_new(self, btn: Gtk.Button) -> None:
        request = self._recording_state.next_request()
        if request is None:
            return

        btn.set_sensitive(False)

        def on_record_request(result: JsonDict | None) -> bool:
            return self._on_record_request_finished(result, request.command)

        def on_record_done() -> None:
            btn.set_sensitive(True)

        self._session_request_async(
            {"command": request.command, "recording_slot": request.slot},
            on_record_request,
            on_done=on_record_done,
        )

    def _on_record_request_finished(self, result: JsonDict | None, command: str) -> bool:
        result = result or {}
        log.debug("macro recording request finished: command=%s result=%r", command, result)
        is_stop_success = command == "stop_recording" and self._is_stop_recording_success(result)
        if result.get("status") == "ok" or is_stop_success:
            if command == "stop_recording":
                self._recording_state.recording_stopped()
            else:
                self._recording_state.unlocked = True
                self._recording_state.enabled = True
            self._sync_record_button_state()
            return False

        if command == "start_recording" and self._is_recording_locked(result):
            self._recording_state.active_slot = 0
            self._recording_state.unlocked = False
            self._sync_record_button_state()
            self._open_recording_settings_dialog(reason="recording_locked")
            return False

        if command == "start_recording" and self._is_macro_recording_disabled(result):
            self._recording_state.active_slot = 0
            self._recording_state.enabled = False
            self._sync_record_button_state()
            return False

        fallback = (
            "Failed to stop recording"
            if command == "stop_recording"
            else "Failed to start recording"
        )
        message = result.get("message", fallback)
        log.warning(
            "showing recording error dialog: command=%s message=%s result=%r",
            command,
            message,
            result,
        )
        dialog = Adw.AlertDialog()
        dialog.set_heading("Recording Error")
        dialog.set_body(message)
        dialog.add_response("ok", "OK")
        dialog.present(self._parent)
        if command == "start_recording":
            self._recording_state.active_slot = 0
        return False

    def _is_stop_recording_success(self, result: dict) -> bool:
        if result.get("status") == "error":
            return False
        return bool(result.get("pending_recording_id")) and "duration_ms" in result

    def _on_recording_started(self, data: dict) -> None:
        slot = int(data.get("recording_slot", 0) or 0)
        self._recording_state.recording_started(
            slot,
            max_slots=MAX_MACRO_RECORDING_SLOTS,
        )
        if 1 <= slot <= MAX_MACRO_RECORDING_SLOTS and self._slot_dropdown is not None:
            self._slot_dropdown.set_selected(slot - 1)
        self._sync_record_button_state()
        self.close()

    def _on_recording_stopped(self, _data: dict) -> None:
        self._recording_state.recording_stopped()
        self._sync_record_button_state()

    def _sync_record_button_state(self) -> None:
        if not self._record_btn:
            return

        show_controls = self._recording_state.active or self._recording_state.enabled
        self._record_btn.set_visible(show_controls)
        if self._slot_dropdown is not None:
            self._slot_dropdown.set_visible(show_controls)
            self._slot_dropdown.set_sensitive(not self._recording_state.active)
        if not show_controls:
            self._record_btn.remove_css_class("destructive-action")
            return

        if self._recording_state.active:
            self._record_btn.set_child(
                self._make_button_content("media-playback-stop-symbolic", "Stop")
            )
            self._record_btn.set_tooltip_text("Stop recording")
            self._record_btn.add_css_class("destructive-action")
            return

        self._record_btn.remove_css_class("destructive-action")
        self._record_btn.set_child(
            self._make_button_content("media-record-symbolic", "Record", "error")
        )
        self._record_btn.set_tooltip_text("Record a new macro")

    def _on_record_settings(self, _btn: Gtk.Button) -> None:
        self._open_recording_settings_dialog()

    def _open_recording_settings_dialog(self, reason: str = "settings") -> None:
        present_settings = getattr(self._parent, "present_recording_settings_dialog", None)
        if callable(present_settings):
            present_settings(reason=reason)
            return
        from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

        record_dialog = RecordMacroDialog(self._parent, reason=reason)
        record_dialog.present(self._parent)

    def _is_recording_locked(self, result: dict) -> bool:
        error_code = str(result.get("error_code", "") or "").strip().lower()
        if error_code == "recording_locked":
            return True
        message = str(result.get("message", "") or "").strip().lower()
        return "recording_locked" in message

    def _is_macro_recording_disabled(self, result: dict) -> bool:
        error_code = str(result.get("error_code", "") or "").strip().lower()
        if error_code == "macro_recording_disabled":
            return True
        message = str(result.get("message", "") or "").strip().lower()
        return "macro_recording_disabled" in message or "macro recording opt-in" in message
