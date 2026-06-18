# pyright: reportAttributeAccessIssue=false
from __future__ import annotations

from typing import cast

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.models import (
    MAX_MACRO_RECORDING_SLOTS,
    ActionType,
    MappingAction,
    SuperkeyAction,
)
from keymasq.gui.widgets.fuzzy_search import (
    fuzzy_query_matches,
    install_listbox_fuzzy_filter,
    macro_search_text,
)
from keymasq.session.profiles import ProfileManager

from .compat import session_request_async


class MacroTabMixin:
    def _create_cancel_macro_playback_button(self) -> Gtk.Button:
        content = Adw.ButtonContent(
            icon_name="media-playback-stop-symbolic",
            label="Cancel Macro Playback",
        )
        content.add_css_class("macro-stop-icon")
        button = Gtk.Button()
        button.set_child(content)
        button.connect(
            "clicked",
            self._on_macro_special_action_clicked,
            "cancel_macro_playback",
        )
        button.set_tooltip_text("Stop currently running macro playback")
        return button

    def _macro_parent_candidates(self) -> list[object]:
        candidates: list[object] = []
        queue: list[object] = [self._parent]
        seen: set[int] = set()
        while queue:
            candidate = queue.pop(0)
            if candidate is None:
                continue
            candidate_id = id(candidate)
            if candidate_id in seen:
                continue
            seen.add(candidate_id)
            candidates.append(candidate)

            main_window = getattr(candidate, "main_window", None)
            if main_window is not None:
                queue.append(main_window)
            parent = getattr(candidate, "_parent", None)
            if parent is not None:
                queue.append(parent)
            get_root = getattr(candidate, "get_root", None)
            if callable(get_root):
                root = get_root()
                if root is not None:
                    queue.append(root)
        return candidates

    def _profile_manager_for_child_dialog(self) -> ProfileManager | None:
        for candidate in self._macro_parent_candidates():
            profile_manager = getattr(candidate, "profile_manager", None)
            if profile_manager is not None:
                return cast(ProfileManager, profile_manager)
        return None

    def _resolve_macro_recording_enabled(self, *, default: bool) -> bool:
        for candidate in self._macro_parent_candidates():
            enabled = getattr(candidate, "macro_recording_enabled", None)
            if callable(enabled):
                return bool(enabled())
            raw_enabled = getattr(candidate, "_macro_recording_enabled", None)
            if isinstance(raw_enabled, bool):
                return raw_enabled
        return default

    def _present_macro_recording_settings(self, _button: Gtk.Button) -> None:
        for candidate in self._macro_parent_candidates():
            present_settings = getattr(candidate, "present_recording_settings_dialog", None)
            if callable(present_settings):
                present_settings(reason="settings")
                self.close()
                return

    def _build_macro_tab(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        outer.append(self._build_macro_slot_console())

        outer.append(Gtk.Separator())

        library_label = Gtk.Label(label="Macro Library")
        library_label.add_css_class("dim-label")
        library_label.set_halign(Gtk.Align.START)
        library_label.set_margin_top(8)
        library_label.set_margin_bottom(6)
        library_label.set_margin_start(12)
        outer.append(library_label)

        self._macro_search_entry = Gtk.SearchEntry()
        self._macro_search_entry.set_placeholder_text("Search macros")
        self._macro_search_entry.set_tooltip_text(
            "Filter macros by name, device type, or event count"
        )
        self._macro_search_entry.set_margin_start(12)
        self._macro_search_entry.set_margin_end(12)
        self._macro_search_entry.set_margin_bottom(8)
        outer.append(self._macro_search_entry)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        self._macro_listbox = Gtk.ListBox()
        self._macro_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._macro_listbox.set_valign(Gtk.Align.START)
        self._macro_listbox.add_css_class("boxed-list")
        self._macro_listbox.set_margin_start(12)
        self._macro_listbox.set_margin_end(12)
        self._macro_listbox.connect("row-selected", self._on_macro_row_selected)
        install_listbox_fuzzy_filter(
            self._macro_listbox,
            self._macro_search_entry,
            after_filter_changed=self._after_macro_search_filter_changed,
        )
        scrolled.set_child(self._macro_listbox)
        outer.append(scrolled)

        self._macro_options_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._macro_options_box.set_margin_top(8)
        self._macro_options_box.set_margin_start(12)
        self._macro_options_box.set_margin_end(12)
        self._macro_options_box.set_visible(False)

        opt_row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        opt_row1.set_halign(Gtk.Align.START)

        self._macro_movement_check = Gtk.CheckButton(label="Replay mouse movement")
        self._macro_movement_check.set_active(self._macro_replay_movement)
        self._macro_movement_check.connect("toggled", self._on_macro_movement_toggled)
        opt_row1.append(self._macro_movement_check)

        self._macro_clicks_check = Gtk.CheckButton(label="Replay mouse clicks")
        self._macro_clicks_check.set_active(self._macro_replay_clicks)
        self._macro_clicks_check.connect("toggled", self._on_macro_clicks_toggled)
        opt_row1.append(self._macro_clicks_check)

        self._macro_options_box.append(opt_row1)

        opt_row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        opt_row2.set_halign(Gtk.Align.START)

        speed_label = Gtk.Label(label="Speed:")
        opt_row2.append(speed_label)

        self._macro_speed_spin = Gtk.SpinButton()
        speed_adj = Gtk.Adjustment(
            value=self._macro_speed, lower=0.1, upper=10.0, step_increment=0.1
        )
        self._macro_speed_spin.set_adjustment(speed_adj)
        self._macro_speed_spin.set_digits(1)
        self._macro_speed_spin.connect("value-changed", self._on_macro_speed_changed)
        opt_row2.append(self._macro_speed_spin)

        speed_suffix = Gtk.Label(label="×")
        opt_row2.append(speed_suffix)

        self._macro_options_box.append(opt_row2)
        outer.append(self._macro_options_box)

        GLib.idle_add(self._load_macro_list)
        return outer

    def _build_macro_slot_console(self) -> Gtk.Widget:
        """Slot-based recording/playback console.

        Renders recording and playback controls for every temporary slot.
        """
        console = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        console.set_margin_top(10)
        console.set_margin_bottom(10)
        console.set_margin_start(12)
        console.set_margin_end(12)
        self._macro_slot_console = console
        self._refresh_macro_slot_console()

        return console

    def _refresh_macro_slot_console(self) -> None:
        console = self._macro_slot_console
        if console is None:
            return
        child = console.get_first_child()
        while child is not None:
            console.remove(child)
            child = console.get_first_child()
        self._macro_recording_enabled = self._resolve_macro_recording_enabled(
            default=False
        )
        if self._macro_recording_enabled:
            console.append(self._build_macro_slot_cards())
        else:
            console.append(self._build_macro_recording_disabled_placeholder())

    def _build_macro_recording_disabled_placeholder(self) -> Gtk.Widget:
        placeholder = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        placeholder.add_css_class("card")
        placeholder.add_css_class("macro-recording-disabled-placeholder")
        placeholder.set_valign(Gtk.Align.CENTER)
        placeholder.set_halign(Gtk.Align.FILL)

        icon = Gtk.Image.new_from_icon_name("channel-insecure-symbolic")
        icon.set_valign(Gtk.Align.START)
        placeholder.append(icon)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True)
        title = Gtk.Label(label="Macro recording is disabled")
        title.add_css_class("caption-heading")
        title.set_halign(Gtk.Align.START)
        text_box.append(title)

        body = Gtk.Label(label="Enable it in Settings > Macro recording to bind slot actions.")
        body.add_css_class("dim-label")
        body.set_wrap(True)
        body.set_halign(Gtk.Align.START)
        text_box.append(body)
        placeholder.append(text_box)

        settings_content = Adw.ButtonContent(
            icon_name="emblem-system-symbolic",
            label="Open Settings",
        )
        settings_btn = Gtk.Button()
        settings_btn.set_child(settings_content)
        settings_btn.set_valign(Gtk.Align.CENTER)
        settings_btn.set_tooltip_text("Open macro recording settings")
        settings_btn.connect("clicked", self._present_macro_recording_settings)
        settings_btn.set_sensitive(
            any(
                callable(getattr(candidate, "present_recording_settings_dialog", None))
                for candidate in self._macro_parent_candidates()
            )
        )
        placeholder.append(settings_btn)
        return placeholder

    def _build_macro_slot_card(self, slot: int) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        card.add_css_class("card")
        card.add_css_class("macro-slot-card")

        card.append(self._build_macro_slot_header(f"Slot {slot}"))

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        buttons.set_halign(Gtk.Align.CENTER)
        buttons.append(self._create_record_slot_button(slot))
        buttons.append(self._create_play_slot_button(slot))
        card.append(buttons)
        return card

    def _build_macro_slot_cards(self) -> Gtk.Widget:
        grid = Gtk.Grid(column_spacing=6, row_spacing=6)
        grid.set_halign(Gtk.Align.CENTER)

        for index in range(MAX_MACRO_RECORDING_SLOTS):
            slot = index + 1
            grid.attach(self._build_macro_slot_card(slot), index, 0, 1, 1)

        return grid

    def _build_macro_slot_header(self, label: str) -> Gtk.Widget:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        header.add_css_class("macro-slot-header")
        header.set_halign(Gtk.Align.CENTER)

        title = Gtk.Label(label=label)
        title.add_css_class("caption-heading")
        title.add_css_class("macro-slot-title")
        title.set_halign(Gtk.Align.CENTER)
        header.append(title)
        return header

    def _create_record_slot_button(self, slot: int) -> Gtk.Button:
        content = Adw.ButtonContent(icon_name="media-record-symbolic", label=str(slot))
        content.add_css_class("macro-record-icon")
        button = Gtk.Button()
        button.add_css_class("macro-slot-button")
        button.set_child(content)
        button.set_size_request(42, 34)
        button.set_tooltip_text(f"Toggle macro recording into slot {slot}")
        button.connect("clicked", self._on_macro_recording_slot_clicked, slot)
        if (
            self._current_action
            and self._current_action.action_type
            in (ActionType.START_MACRO_RECORDING, ActionType.STOP_MACRO_RECORDING)
            and self._current_action.macro_recording_slot == slot
        ):
            button.add_css_class("suggested-action")
        return button

    def _create_play_slot_button(self, slot: int) -> Gtk.Button:
        content = Adw.ButtonContent(
            icon_name="media-playback-start-symbolic", label=str(slot)
        )
        content.add_css_class("macro-play-icon")
        button = Gtk.Button()
        button.add_css_class("macro-slot-button")
        button.set_child(content)
        button.set_size_request(42, 34)
        button.set_tooltip_text(f"Play the macro recorded in slot {slot}")
        button.connect("clicked", self._on_macro_play_slot_clicked, slot)
        if (
            self._current_action
            and self._current_action.action_type == ActionType.PLAY_MACRO_SLOT
            and self._current_action.macro_recording_slot == slot
        ):
            button.add_css_class("suggested-action")
        return button

    def _load_macro_list(self) -> bool:
        session_request_async({"command": "list_macros"}, self._on_macro_list_loaded)
        return False

    def _on_macro_list_loaded(self, result: dict | None) -> bool:
        self._macro_list = (result or {}).get("macros", [])
        self._populate_macro_listbox()
        return False

    def _populate_macro_listbox(self) -> None:
        while self._macro_listbox.get_first_child():
            self._macro_listbox.remove(self._macro_listbox.get_first_child())

        if not self._macro_list:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            lbl = Gtk.Label(label="No macros saved yet")
            lbl.add_css_class("dim-label")
            lbl.set_margin_top(12)
            lbl.set_margin_bottom(12)
            row.set_child(lbl)
            self._macro_listbox.append(row)
            return

        for macro in self._macro_list:
            row = Gtk.ListBoxRow()
            row._macro_name = macro["name"]
            row._search_text = macro_search_text(macro)
            right_click = Gtk.GestureClick()
            right_click.set_button(3)
            right_click.connect("pressed", self._on_macro_row_right_pressed, macro["name"])
            row.add_controller(right_click)

            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row_box.set_margin_top(8)
            row_box.set_margin_bottom(8)
            row_box.set_margin_start(12)
            row_box.set_margin_end(12)

            name_label = Gtk.Label(label=macro["name"])
            name_label.set_halign(Gtk.Align.START)
            name_label.set_hexpand(True)
            row_box.append(name_label)

            duration_s = macro.get("duration_us", 0) / 1_000_000
            device_types = ", ".join(macro.get("device_types", []))
            info_label = Gtk.Label(label=f"{duration_s:.1f}s · {device_types}")
            info_label.add_css_class("dim-label")
            info_label.add_css_class("caption")
            row_box.append(info_label)

            row.set_child(row_box)
            self._macro_listbox.append(row)
        self._macro_listbox.invalidate_filter()

        if self._selected_macro:
            for i, macro in enumerate(self._macro_list):
                if macro["name"] == self._selected_macro:
                    row = self._macro_listbox.get_row_at_index(i)
                    if row and fuzzy_query_matches(
                        self._macro_search_entry.get_text(),
                        getattr(row, "_search_text", ""),
                    ):
                        self._macro_listbox.select_row(row)
                    else:
                        self._clear_macro_selection()
                    break
            else:
                self._clear_macro_selection()
        if not self._selected_macro:
            self._clear_macro_selection()

    def _after_macro_search_filter_changed(self) -> None:
        selected_row = self._macro_listbox.get_selected_row()
        if selected_row is None:
            self._clear_macro_selection()
            return
        if fuzzy_query_matches(
            self._macro_search_entry.get_text(),
            getattr(selected_row, "_search_text", ""),
        ):
            return
        self._macro_listbox.unselect_row(selected_row)
        self._clear_macro_selection()

    def _clear_macro_selection(self) -> None:
        self._selected_macro = None
        self._macro_options_box.set_visible(False)
        if self.stack.get_visible_child_name() == "macro":
            self.map_btn.set_sensitive(False)

    def _on_macro_row_selected(self, listbox, row) -> None:
        if row and hasattr(row, "_macro_name"):
            self._selected_macro = row._macro_name
            self._macro_options_box.set_visible(self._allow_macro_options)
        else:
            self._clear_macro_selection()
        self.map_btn.set_sensitive(self._selected_macro is not None)

    def _on_macro_row_right_pressed(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        _x: float,
        _y: float,
        name: str,
    ) -> None:
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._open_macro_editor(name)

    def _open_macro_editor(self, name: str) -> None:
        from keymasq.gui.widgets.macro_editor_dialog import MacroEditorDialog

        root = self.get_root()
        parent = root if root is not None else self._parent
        dialog = MacroEditorDialog(parent, name)
        dialog.connect("closed", self._on_macro_editor_closed)
        dialog.present(parent)

    def _on_macro_editor_closed(self, _dialog: Adw.Dialog) -> None:
        self._load_macro_list()

    def _on_macro_movement_toggled(self, check) -> None:
        self._macro_replay_movement = check.get_active()

    def _on_macro_clicks_toggled(self, check) -> None:
        self._macro_replay_clicks = check.get_active()

    def _on_macro_speed_changed(self, spin) -> None:
        self._macro_speed = spin.get_value()

    def _on_macro_special_action_clicked(self, _btn, action_name: str) -> None:
        if action_name == "cancel_macro_playback":
            self._warn_and_clear_unsupported_rapidfire(ActionType.CANCEL_MACRO_PLAYBACK)
            action = MappingAction(action_type=ActionType.CANCEL_MACRO_PLAYBACK)
        else:
            return
        self._emit_selected_action(action)

    def _on_macro_recording_slot_clicked(self, _btn, slot: int) -> None:
        self._warn_and_clear_unsupported_rapidfire(ActionType.START_MACRO_RECORDING)
        action = MappingAction(
            action_type=ActionType.START_MACRO_RECORDING,
            macro_recording_slot=slot,
        )
        self._emit_selected_action(action)

    def _on_macro_play_slot_clicked(self, _btn, slot: int) -> None:
        self._warn_and_clear_unsupported_rapidfire(ActionType.PLAY_MACRO_SLOT)
        action = MappingAction(
            action_type=ActionType.PLAY_MACRO_SLOT,
            macro_recording_slot=slot,
        )
        self._emit_selected_action(action)

    def _on_macro_map_clicked(self, btn) -> None:
        if not self._selected_macro:
            return
        self._warn_and_clear_unsupported_rapidfire(ActionType.MACRO)
        action = MappingAction(
            action_type=ActionType.MACRO,
            macro_name=self._selected_macro,
            macro_replay_mouse_movement=self._macro_replay_movement,
            macro_replay_mouse_clicks=self._macro_replay_clicks,
            macro_speed=self._macro_speed,
        )
        self._emit_selected_action(action)


class SuperkeyMacroTabMixin:
    def _build_macro_tab(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        outer.set_margin_start(12)
        outer.set_margin_end(12)

        label = Gtk.Label(label="Trigger a saved macro")
        label.add_css_class("dim-label")
        label.set_halign(Gtk.Align.START)
        outer.append(label)

        self._superkey_macro_listbox = Gtk.ListBox()
        self._superkey_macro_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._superkey_macro_listbox.connect("row-selected", self._on_superkey_macro_selected)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_child(self._superkey_macro_listbox)
        outer.append(scrolled)

        self._superkey_macro_map_btn = Gtk.Button(label="Map Macro")
        self._superkey_macro_map_btn.add_css_class("suggested-action")
        self._superkey_macro_map_btn.set_sensitive(False)
        self._superkey_macro_map_btn.connect("clicked", self._on_superkey_macro_map_clicked)
        outer.append(self._superkey_macro_map_btn)

        return outer

    def _load_superkey_macro_list(self) -> bool:
        session_request_async({"command": "list_macros"}, self._on_superkey_macro_list_loaded)
        return False

    def _on_superkey_macro_list_loaded(self, result: dict | None) -> bool:
        self._superkey_macro_list = (result or {}).get("macros", [])
        self._populate_superkey_macro_listbox()
        return False

    def _populate_superkey_macro_listbox(self) -> None:
        while self._superkey_macro_listbox.get_first_child():
            self._superkey_macro_listbox.remove(self._superkey_macro_listbox.get_first_child())

        if not self._superkey_macro_list:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            lbl = Gtk.Label(label="No macros saved yet")
            lbl.add_css_class("dim-label")
            lbl.set_margin_top(12)
            lbl.set_margin_bottom(12)
            row.set_child(lbl)
            self._superkey_macro_listbox.append(row)
            self._superkey_macro_map_btn.set_sensitive(False)
            return

        sel_row = None
        for _i, macro in enumerate(self._superkey_macro_list):
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.set_margin_top(8)
            box.set_margin_bottom(8)
            box.set_margin_start(8)
            box.set_margin_end(8)

            name = macro.get("name", "")
            lbl = Gtk.Label(label=name)
            lbl.set_halign(Gtk.Align.START)
            lbl.set_hexpand(True)
            box.append(lbl)

            count = int(macro.get("event_count", 0))
            meta = Gtk.Label(label=f"{count} events")
            meta.add_css_class("caption")
            meta.add_css_class("dim-label")
            box.append(meta)

            row.set_child(box)
            row._macro_name = name
            self._superkey_macro_listbox.append(row)

            if self._superkey_selected_macro and name == self._superkey_selected_macro:
                sel_row = row

        if sel_row is not None:
            self._superkey_macro_listbox.select_row(sel_row)

    def _on_superkey_macro_selected(self, list_box, row) -> None:
        if row and hasattr(row, "_macro_name"):
            self._superkey_selected_macro = row._macro_name
        else:
            self._superkey_selected_macro = None
        self._superkey_macro_map_btn.set_sensitive(bool(self._superkey_selected_macro))

    def _on_superkey_macro_map_clicked(self, btn) -> None:
        if not self._superkey_selected_macro:
            return
        self._warn_and_clear_unsupported_rapidfire(ActionType.MACRO)
        action = SuperkeyAction(
            action_type=ActionType.MACRO,
            macro_name=self._superkey_selected_macro,
            rapidfire_enabled=self._rapidfire_enabled if self.rapidfire_check else False,
            rapidfire_hold_ms=int(self.hold_spin.get_value()) if self.rapidfire_check else 20,
            rapidfire_wait_ms=int(self.wait_spin.get_value()) if self.rapidfire_check else 20,
        )
        self.emit("action-selected", action)
        self.close()
