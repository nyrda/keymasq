"""Macro recording state-machine controller."""

# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.actions import MAX_MACRO_RECORDING_SLOTS
from keymasq.common.recording_policy import (
    MACRO_RECORDING_DISABLED_MESSAGE,
    is_macro_recording_disabled_error,
)
from keymasq.gui.session_client import JsonDict

log = logging.getLogger("keymasq.gui.widgets.macro_manager_dialog")


class RecordingControllerMixin:
    """Coordinate recording transitions, session requests, and GTK controls."""

    def _parent_macro_recording_allowed(self) -> bool:
        allowed = getattr(self._parent, "macro_recording_allowed", None)
        return bool(allowed()) if callable(allowed) else True

    def _apply_recording_status(self, status: JsonDict) -> None:
        self._recording_state.active = bool(status.get("recording_active", False))
        self._recording_state.allowed = bool(
            status.get("macro_recording_allowed", self._recording_state.allowed)
        )
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
            self._sync_record_button_state()

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
            self._sync_record_button_state()
            return False

        if command == "start_recording" and is_macro_recording_disabled_error(result):
            self._recording_state.active_slot = 0
            self._recording_state.allowed = False
            self._sync_record_button_state()
            self._show_recording_error("Macro Recording Disabled", MACRO_RECORDING_DISABLED_MESSAGE)
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
        self._show_recording_error("Recording Error", message)
        if command == "start_recording":
            self._recording_state.active_slot = 0
        return False

    def _show_recording_error(self, heading: str, message: str) -> None:
        dialog = Adw.AlertDialog()
        dialog.set_heading(heading)
        dialog.set_body(message)
        dialog.add_response("ok", "OK")
        dialog.present(self._parent)

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

    def _on_macro_recording_disabled(self, _data: dict) -> None:
        self._recording_state.allowed = False
        self._sync_record_button_state()

    def _on_macro_recording_policy_changed(self, data: dict) -> None:
        self._recording_state.allowed = bool(
            data.get("macro_recording_allowed", self._recording_state.allowed)
        )
        self._sync_record_button_state()

    def _sync_record_button_state(self) -> None:
        if not self._record_btn:
            return

        can_record = self._recording_state.active or self._recording_state.allowed
        self._record_btn.set_sensitive(can_record)
        if self._slot_dropdown is not None:
            self._slot_dropdown.set_sensitive(can_record and not self._recording_state.active)

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
        self._record_btn.set_tooltip_text(
            "Record a new macro" if can_record else MACRO_RECORDING_DISABLED_MESSAGE
        )

    def _on_record_settings(self, _btn: Gtk.Button) -> None:
        self._open_recording_settings_dialog()

    def _open_recording_settings_dialog(self) -> None:
        present_settings = getattr(self._parent, "present_recording_settings_dialog", None)
        if callable(present_settings):
            present_settings()
            return
        from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

        record_dialog = RecordMacroDialog(self._parent)
        record_dialog.present(self._parent)
