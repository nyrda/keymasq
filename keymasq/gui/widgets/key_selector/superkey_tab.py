# pyright: reportAttributeAccessIssue=false
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.models import ActionType, MappingAction, SuperkeyConfig
from keymasq.session.superkeys import SuperkeyManager

from .compat import notify_session_reload_async


class SuperkeyTabMixin:
    def _build_superkey_tab(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        toolbar_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar_row.set_margin_top(8)
        toolbar_row.set_margin_bottom(4)
        toolbar_row.set_margin_start(12)
        toolbar_row.set_margin_end(12)
        toolbar_row.set_halign(Gtk.Align.START)

        manage_btn = Gtk.Button(label="Open Super Keys…")
        manage_btn.add_css_class("flat")
        manage_btn.set_tooltip_text("Create or edit super keys")
        manage_btn.connect("clicked", self._on_open_superkey_manager_clicked)
        toolbar_row.append(manage_btn)

        selection_hint = Gtk.Label(label="Select one · right-click to edit")
        selection_hint.add_css_class("dim-label")
        selection_hint.add_css_class("caption")
        selection_hint.set_halign(Gtk.Align.START)
        toolbar_row.append(selection_hint)
        outer.append(toolbar_row)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        self._superkey_listbox = Gtk.ListBox()
        self._superkey_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._superkey_listbox.set_valign(Gtk.Align.START)
        self._superkey_listbox.add_css_class("boxed-list")
        self._superkey_listbox.set_margin_start(12)
        self._superkey_listbox.set_margin_end(12)
        self._superkey_listbox.connect("row-selected", self._on_superkey_row_selected)
        scrolled.set_child(self._superkey_listbox)
        outer.append(scrolled)

        self._load_superkey_list()
        return outer

    def _on_open_superkey_manager_clicked(self, _button: Gtk.Button) -> None:
        self._open_superkey_manager()

    def _open_superkey_manager(self, select_name: str | None = None) -> None:
        from keymasq.gui.widgets.superkey_dialog import SuperkeyDialog

        root = self.get_root()
        profile_manager = self._profile_manager_for_child_dialog()
        dialog = SuperkeyDialog(root, profile_manager)
        dialog.connect("superkey-saved", self._on_superkey_manager_changed)
        dialog.connect("superkey-deleted", self._on_superkey_manager_changed)
        dialog.present(root)
        if select_name:
            dialog.select_superkey_by_name(select_name)

    def _on_superkey_manager_changed(self, _dialog, _name: str) -> None:
        notify_session_reload_async()
        self._load_superkey_list()

    def _load_superkey_list(self) -> None:
        manager = SuperkeyManager()
        configs = manager.get_all_superkeys()
        self._superkey_names = manager.list_superkeys()
        self._superkey_list = [
            config for name in self._superkey_names if (config := configs.get(name)) is not None
        ]
        self._populate_superkey_listbox()

    def _populate_superkey_listbox(self) -> None:
        while self._superkey_listbox.get_first_child():
            self._superkey_listbox.remove(self._superkey_listbox.get_first_child())

        if not self._superkey_list:
            self._selected_superkey = None
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            lbl = Gtk.Label(label="No super keys saved yet")
            lbl.add_css_class("dim-label")
            lbl.set_margin_top(12)
            lbl.set_margin_bottom(12)
            row.set_child(lbl)
            self._superkey_listbox.append(row)
            return

        selected_row: Gtk.ListBoxRow | None = None
        for config in self._superkey_list:
            row = Gtk.ListBoxRow()
            row._superkey_name = config.name
            right_click = Gtk.GestureClick()
            right_click.set_button(3)
            right_click.connect("pressed", self._on_superkey_row_right_pressed, config.name)
            row.add_controller(right_click)

            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row_box.set_margin_top(8)
            row_box.set_margin_bottom(8)
            row_box.set_margin_start(12)
            row_box.set_margin_end(12)

            name_label = Gtk.Label(label=config.name)
            name_label.set_halign(Gtk.Align.START)
            name_label.set_hexpand(True)
            row_box.append(name_label)

            info_label = Gtk.Label(label=self._describe_superkey_row(config))
            info_label.add_css_class("dim-label")
            info_label.add_css_class("caption")
            row_box.append(info_label)

            row.set_child(row_box)
            self._superkey_listbox.append(row)

            if self._selected_superkey and config.name == self._selected_superkey:
                selected_row = row

        if selected_row is not None:
            self._superkey_listbox.select_row(selected_row)
        elif self._selected_superkey:
            self._selected_superkey = None

    def _describe_superkey_row(self, config: SuperkeyConfig) -> str:
        if config.mode.value == "overload":
            count = (
                len(config.overload_actions)
                + len(config.overload_down_actions)
                + len(config.overload_up_actions)
            )
            noun = "action" if count == 1 else "actions"
            suffix = (
                " · down/up"
                if config.overload_down_actions or config.overload_up_actions
                else ""
            )
            return f"Overload{suffix} · {count} {noun}"

        slots = sum(
            1
            for actions in (
                config.tap_actions,
                config.double_tap_actions,
                config.hold_actions,
                config.tap_hold_actions,
            )
            if actions
        )
        noun = "slot" if slots == 1 else "slots"
        return f"Pattern · {slots} {noun}"

    def _on_superkey_row_right_pressed(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        _x: float,
        _y: float,
        name: str,
    ) -> None:
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._open_superkey_manager(name)

    def _on_superkey_row_selected(self, listbox, row) -> None:
        if row and hasattr(row, "_superkey_name"):
            self._selected_superkey = row._superkey_name
        else:
            self._selected_superkey = None
        if self.stack.get_visible_child_name() == "superkey":
            self.map_btn.set_sensitive(self._selected_superkey is not None)

    def _on_superkey_map_clicked(self, btn) -> None:
        if not self._selected_superkey:
            return
        self._warn_and_clear_unsupported_rapidfire(ActionType.SUPERKEY)
        action = MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_name=self._selected_superkey,
        )
        self._emit_selected_action(action)
