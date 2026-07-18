"""Session loading and row rendering for saved macros and temporary slots."""

# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.session_client import GuiTaskResult, JsonDict
from keymasq.gui.widgets.macro_manager.state import (
    CatalogState,
    CatalogValidationError,
    MacroRowState,
)


class CatalogControllerMixin:
    """Load, validate, and render the macro catalog."""

    def _load_initial_state(self) -> bool:
        self._run_gui_task(self._fetch_initial_state, self._on_initial_state_loaded)
        return False

    def _fetch_initial_state(self) -> tuple[JsonDict | None, JsonDict | None]:
        return (
            self._session_request({"command": "get_status"}) or {},
            self._session_request({"command": "list_macros", "include_slots": True}) or {},
        )

    def _on_initial_state_loaded(
        self,
        result: GuiTaskResult[tuple[JsonDict | None, JsonDict | None]],
    ) -> bool:
        status, macros = result.value if result.ok and result.value is not None else ({}, {})
        self._apply_recording_status(status or {})
        self._apply_macros_response(macros)
        return False

    def _load_macros(self) -> bool:
        self._session_request_async(
            {"command": "list_macros", "include_slots": True},
            self._on_macros_loaded,
        )
        return False

    def _on_macros_loaded(self, result: JsonDict | None) -> bool:
        self._apply_macros_response(result)
        return False

    def _apply_macros_response(self, result: JsonDict | None) -> bool:
        try:
            catalog = CatalogState.from_response(result)
        except CatalogValidationError as error:
            self._show_macro_load_error(str(error))
            return False

        self._catalog.macros = catalog.macros
        self._populate_list()
        return True

    def _show_macro_load_error(self, message: object) -> None:
        dialog = Adw.AlertDialog()
        dialog.set_heading("Load Macros")
        dialog.set_body(str(message or "Failed to load macros"))
        dialog.add_response("ok", "OK")
        dialog.present(self._parent)

    def _populate_list(self) -> None:
        while self._listbox.get_first_child():
            self._listbox.remove(self._listbox.get_first_child())

        for macro in self._catalog.macros:
            self._listbox.append(self._build_macro_row(macro))
        self._listbox.invalidate_filter()
        self._update_empty_state()

    def _update_empty_state(self) -> None:
        if not self._catalog.macros:
            self._empty_label.set_label("No macros recorded yet")
            self._empty_label.set_visible(True)
            return
        if self._catalog.query and not self._catalog.filtered_macros():
            self._empty_label.set_label("No macros match your search")
            self._empty_label.set_visible(True)
            return
        self._empty_label.set_visible(False)

    def _build_macro_row(self, macro: JsonDict) -> Gtk.ListBoxRow:
        state = MacroRowState.from_macro(macro)
        row = Gtk.ListBoxRow()
        row._search_text = state.search_text
        row._macro = macro
        row._row_state = state
        if not state.is_temporary_slot and state.name:
            right_click = Gtk.GestureClick()
            right_click.set_button(3)
            right_click.connect("pressed", self._on_row_right_pressed, row, state.name)
            row.add_controller(right_click)

        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row_box.set_margin_top(6)
        row_box.set_margin_bottom(6)
        row_box.set_margin_start(12)
        row_box.set_margin_end(8)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_box.set_hexpand(True)
        info_box.set_valign(Gtk.Align.CENTER)

        name_label = Gtk.Label(label=state.display_name)
        name_label.set_halign(Gtk.Align.START)
        info_box.append(name_label)

        meta_label = Gtk.Label(label=state.metadata)
        meta_label.add_css_class("caption")
        meta_label.add_css_class("dim-label")
        meta_label.set_halign(Gtk.Align.START)
        info_box.append(meta_label)
        row_box.append(info_box)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        btn_box.set_valign(Gtk.Align.CENTER)
        if state.is_temporary_slot:
            self._append_slot_actions(btn_box, macro)
        else:
            self._append_saved_macro_actions(btn_box, state.name)

        row_box.append(btn_box)
        row.set_child(row_box)
        return row

    def _append_slot_actions(self, box: Gtk.Box, macro: JsonDict) -> None:
        save_btn = Gtk.Button()
        save_btn.set_icon_name("document-save-symbolic")
        save_btn.set_tooltip_text("Save slot as macro")
        save_btn.add_css_class("flat")
        save_btn.connect("clicked", self._on_save_slot_clicked, macro)
        box.append(save_btn)

        delete_btn = Gtk.Button()
        delete_btn.set_icon_name("edit-delete-symbolic")
        delete_btn.set_tooltip_text("Delete slot")
        delete_btn.add_css_class("flat")
        delete_btn.add_css_class("destructive-action")
        delete_btn.connect("clicked", self._on_delete_slot_clicked, macro)
        box.append(delete_btn)

    def _append_saved_macro_actions(self, box: Gtk.Box, name: str) -> None:
        play_btn = Gtk.Button()
        play_btn.set_icon_name("media-playback-start-symbolic")
        play_btn.set_tooltip_text("Play")
        play_btn.add_css_class("flat")
        play_btn.connect("clicked", self._on_play_clicked, name)
        box.append(play_btn)

        edit_btn = Gtk.Button()
        edit_btn.set_icon_name("document-edit-symbolic")
        edit_btn.set_tooltip_text("Edit")
        edit_btn.add_css_class("flat")
        edit_btn.connect("clicked", self._on_edit_clicked, name)
        box.append(edit_btn)

        duplicate_btn = Gtk.Button()
        duplicate_btn.set_icon_name("edit-copy-symbolic")
        duplicate_btn.set_tooltip_text("Duplicate")
        duplicate_btn.add_css_class("flat")
        duplicate_btn.connect(
            "clicked",
            self._on_duplicate_clicked,
            name,
            duplicate_btn,
        )
        box.append(duplicate_btn)

        delete_btn = Gtk.Button()
        delete_btn.set_icon_name("edit-delete-symbolic")
        delete_btn.set_tooltip_text("Delete")
        delete_btn.add_css_class("flat")
        delete_btn.add_css_class("destructive-action")
        delete_btn.connect("clicked", self._on_delete_clicked, name)
        box.append(delete_btn)
