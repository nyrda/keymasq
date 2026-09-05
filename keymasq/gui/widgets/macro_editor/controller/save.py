"""Macro editor persistence and revision flow."""

# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false

import copy

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.session_client import GuiTaskResult, JsonDict
from keymasq.gui.widgets.macro_editor.document import (
    MacroDocument,
    SaveMode,
    is_valid_macro_name,
    resolve_save_target,
)
from keymasq.gui.widgets.macro_editor.panel.controls import _set_entry_text_if_needed
from keymasq.gui.widgets.macro_editor.panel.settings import (
    _LOOP_MODE_OPTIONS,
    _get_dropdown_selected_id,
)


class SaveControllerMixin:
    """Validate, serialize, and persist an edited macro."""

    def _on_save(self, btn: Gtk.Button) -> None:
        self._save_current_macro(btn, close_after_save=True)

    def _on_apply(self, btn: Gtk.Button) -> None:
        self._save_current_macro(btn, close_after_save=False)

    def _save_current_macro(self, btn: Gtk.Button | None, *, close_after_save: bool) -> None:
        if self._save_in_flight:
            return

        new_name = self._name_entry.get_text().strip()
        if not self._validate_name_for_save(new_name):
            return

        macro_payload = self._build_macro_payload(new_name)
        revision = int(self._macro_data.get("revision", 1))

        def save_request() -> JsonDict | None:
            return self._save_macro_request(new_name, macro_payload, revision)

        def on_save_finished(result: GuiTaskResult[JsonDict | None]) -> bool:
            return self._on_save_finished(
                result,
                new_name,
                macro_payload,
                close_after_save=close_after_save,
            )

        def on_save_start() -> None:
            self._set_save_controls_sensitive(False, extra_button=btn)
            self._set_editor_busy(True, "Saving macro…")

        def on_save_done() -> None:
            self._finish_save_request(extra_button=btn)

        self._save_in_flight = True
        self._sync_close_guard()
        self._run_gui_task(
            save_request,
            on_save_finished,
            on_start=on_save_start,
            on_done=on_save_done,
        )

    def _set_save_controls_sensitive(
        self,
        sensitive: bool,
        *,
        extra_button: Gtk.Button | None = None,
    ) -> None:
        for button in self._footer_action_buttons:
            button.set_sensitive(sensitive)
        if extra_button is not None and not any(
            button is extra_button for button in self._footer_action_buttons
        ):
            extra_button.set_sensitive(sensitive)

    def _finish_save_request(self, *, extra_button: Gtk.Button | None = None) -> None:
        self._save_in_flight = False
        if self._dialog_closed:
            return
        self._set_editor_busy(False)
        self._set_save_controls_sensitive(True, extra_button=extra_button)
        self._sync_close_guard()

    def _save_macro_request(
        self,
        new_name: str,
        macro_payload: dict,
        revision: int,
    ) -> JsonDict | None:
        target = resolve_save_target(
            macro_exists=self._macro_exists,
            current_name=self._macro_name,
            requested_name=new_name,
            revision=revision,
        )
        if target.mode is SaveMode.CREATE:
            return self._session_request({"command": "create_macro", "macro": macro_payload}) or {}

        if target.mode is SaveMode.RENAME:
            create_result = (
                self._session_request({"command": "create_macro", "macro": macro_payload}) or {}
            )
            if create_result.get("status") != "ok":
                return create_result
            delete_result = (
                self._session_request(
                    {
                        "command": "delete_macro",
                        "name": target.current_name,
                        "expected_revision": target.revision,
                    }
                )
                or {}
            )
            if delete_result.get("status") != "ok":
                detail = str(
                    delete_result.get("message", "Failed to remove the old macro")
                    or "Failed to remove the old macro"
                )
                create_result["warning"] = (
                    f"Macro saved as '{target.requested_name}', but "
                    f"'{target.current_name}' could not be removed: {detail}. "
                    "Both macros remain."
                )
            return create_result

        return (
            self._session_request(
                {
                    "command": "update_macro",
                    "name": target.current_name,
                    "macro": macro_payload,
                    "expected_revision": target.revision,
                }
            )
            or {}
        )

    def _on_save_finished(
        self,
        result: GuiTaskResult[JsonDict | None],
        requested_name: str,
        requested_payload: dict,
        *,
        close_after_save: bool,
    ) -> bool:
        payload = result.value if result.ok and isinstance(result.value, dict) else {}
        if payload.get("status") != "ok":
            if self._is_name_conflict_response(payload, requested_name):
                self._show_name_conflict(requested_name)
            else:
                self._show_save_error(self._save_error_message(result, payload))
            return False

        self._apply_saved_macro_state(payload, requested_name, requested_payload)
        self._notify_session_reload()
        warning = payload.get("warning")
        if isinstance(warning, str) and warning.strip():
            self._show_save_warning(warning)
        if close_after_save:
            self._force_close_without_warning()
        return False

    def _is_name_conflict_response(self, payload: JsonDict, requested_name: str) -> bool:
        status = str(payload.get("status", "") or "")
        if status in {"name-conflict", "name_conflict"}:
            return True
        message = str(payload.get("message", "") or "")
        return status == "error" and message == f"Macro '{requested_name}' already exists"

    def _save_error_message(
        self,
        result: GuiTaskResult[JsonDict | None],
        payload: JsonDict,
    ) -> str:
        if isinstance(payload.get("message"), str) and payload["message"].strip():
            return str(payload["message"])
        if result.error is not None:
            return str(result.error).strip() or result.error.__class__.__name__
        return "Failed to save macro"

    def _apply_saved_macro_state(
        self,
        save_response: JsonDict,
        requested_name: str,
        requested_payload: dict,
    ) -> None:
        saved_macro = save_response.get("macro")
        if not isinstance(saved_macro, dict):
            saved_macro = dict(requested_payload)

        self._macro_data = copy.deepcopy(saved_macro)
        saved_name = str(saved_macro.get("name", requested_name) or requested_name)
        self._macro_name = saved_name
        self._macro_exists = True
        _set_entry_text_if_needed(self._name_entry, saved_name)
        self.set_title(f"Edit macro ({saved_name})")
        self._initial_state_loaded = True
        self._initial_macro_data = copy.deepcopy(saved_macro)
        self._sync_close_guard()

    def _on_save_as_copy(self, _btn) -> None:
        dialog = Adw.Dialog(title="Save as Copy", content_width=360)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        lbl = Gtk.Label(label="Name for the copy:")
        lbl.set_halign(Gtk.Align.START)
        box.append(lbl)

        entry = Gtk.Entry()
        entry.set_text(f"{self._macro_name}_copy")
        entry.select_region(0, -1)
        box.append(entry)

        error_lbl = Gtk.Label()
        error_lbl.add_css_class("error")
        error_lbl.add_css_class("caption")
        error_lbl.set_halign(Gtk.Align.START)
        error_lbl.set_visible(False)
        box.append(error_lbl)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.END)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", self._on_close_dialog_clicked, dialog)
        btn_row.append(cancel)

        save = Gtk.Button(label="Save Copy")
        save.add_css_class("suggested-action")

        def on_save_copy(_button) -> None:
            if not save.get_sensitive():
                return
            name = entry.get_text().strip()
            if not name:
                error_lbl.set_label("Name cannot be empty")
                error_lbl.set_visible(True)
                return
            if not is_valid_macro_name(name):
                error_lbl.set_label("Only letters, numbers, underscores and hyphens")
                error_lbl.set_visible(True)
                return
            copy_payload = self._build_macro_payload(name)

            def create_copy_request() -> JsonDict | None:
                return (
                    self._session_request({"command": "create_macro", "macro": copy_payload}) or {}
                )

            def on_copy_finished(result: GuiTaskResult[JsonDict | None]) -> bool:
                return self._on_save_copy_finished(
                    result,
                    name,
                    error_lbl,
                    dialog,
                )

            def on_copy_start() -> None:
                save.set_sensitive(False)

            def on_copy_done() -> None:
                save.set_sensitive(True)

            self._run_gui_task(
                create_copy_request,
                on_copy_finished,
                on_start=on_copy_start,
                on_done=on_copy_done,
            )

        save.connect("clicked", on_save_copy)
        entry.connect("activate", on_save_copy)
        btn_row.append(save)
        box.append(btn_row)

        dialog.set_child(box)
        dialog.present(self._parent)

    def _on_save_copy_finished(
        self,
        result: GuiTaskResult[JsonDict | None],
        requested_name: str,
        error_label: Gtk.Label,
        dialog: Adw.Dialog,
    ) -> bool:
        payload = result.value if result.ok and isinstance(result.value, dict) else {}
        if payload.get("status") != "ok":
            error_label.set_label(payload.get("message", f"'{requested_name}' already exists"))
            error_label.set_visible(True)
            return False
        self._notify_session_reload()
        dialog.close()
        return False

    def _validate_name_for_save(self, name: str) -> bool:
        return is_valid_macro_name(name)

    def _show_name_conflict(self, name: str) -> None:
        dialog = Adw.AlertDialog()
        dialog.set_heading("Name Conflict")
        dialog.set_body(f"A macro named '{name}' already exists. Choose a different name.")
        dialog.add_response("ok", "OK")
        dialog.present(self._parent)

    def _show_save_error(self, message: str) -> None:
        dialog = Adw.AlertDialog()
        dialog.set_heading("Unable To Save Macro")
        dialog.set_body(message)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self._parent)

    def _show_save_warning(self, message: str) -> None:
        dialog = Adw.AlertDialog()
        dialog.set_heading("Macro Saved With Warning")
        dialog.set_body(message)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self._parent)

    def _build_macro_payload(self, name: str) -> dict:
        document = MacroDocument(
            source=self._macro_data,
            events=self._events,
            relative_events=self._rel_events,
            passthrough_events=self._passthrough_events,
            moves=self._synthetic_moves,
            controls=self._control_events,
            duration_us=self._duration_us,
            has_move_to_start_setting=self._macro_has_move_to_start_setting,
            move_to_start=self._macro_move_to_start,
            start_x=self._macro_start_x,
            start_y=self._macro_start_y,
            block_mouse_movement=self._macro_block_mouse_movement,
            loop_mode=self._macro_loop_mode,
            loop_count=self._macro_loop_count,
            loop_stop_behavior=self._macro_loop_stop_behavior,
        )
        return document.to_payload(
            name,
            loop_mode=_get_dropdown_selected_id(
                self._macro_loop_mode_combo,
                _LOOP_MODE_OPTIONS,
                "none",
            ),
            loop_count=int(self._macro_loop_count_spin.get_value()),
            loop_stop_behavior=(
                "finish_run" if self._macro_loop_finish_check.get_active() else "cancel_run"
            ),
            move_to_start=self._macro_move_to_start_check.get_active(),
            start_x=int(self._macro_start_x_spin.get_value()),
            start_y=int(self._macro_start_y_spin.get_value()),
            block_mouse_movement=self._macro_block_mouse_check.get_active(),
        )
