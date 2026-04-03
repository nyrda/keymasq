from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib, Gtk, Pango

from keyforge.common.models import ComboConfig
from keyforge.gui.icons import combo_icon_names, image_from_icon_names
from keyforge.gui.widgets.combo_editor_dialog import (
    ComboEditorDialog,
    combo_default_name,
    combo_step_label,
    combo_trigger_label,
    describe_mapping_action,
)
from keyforge.gui.widgets.profile_managed_tab import ProfileManagedTab
from keyforge.session.profiles import ProfileManager

_SORT_NONE = 0
_SORT_NAME = 1
_SORT_TRIGGER = 2
_SORT_ACTION = 3

_ARROW_UP = " \u25B4"
_ARROW_DOWN = " \u25BE"


class ComboTab(ProfileManagedTab):
    def __init__(
        self,
        profile_manager: ProfileManager | None,
        main_window=None,
        demo_mode: bool = False,
        compositor_capabilities: list[str] | None = None,
    ) -> None:
        super().__init__(
            profile_manager=profile_manager,
            main_window=main_window,
            demo_mode=demo_mode,
            compositor_capabilities=compositor_capabilities,
        )

        self._sort_column = _SORT_NONE
        self._sort_ascending = True

        self._setup_header()
        self._setup_profile_selector()
        self._setup_combo_list()
        self.refresh_profiles()

        if not self.demo_mode:
            self._check_active_profiles()
            GLib.timeout_add(500, self._check_active_profiles)

    def _setup_header(self) -> None:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        icon = image_from_icon_names(*combo_icon_names(), pixel_size=32)
        icon.set_valign(Gtk.Align.CENTER)
        header.append(icon)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title = Gtk.Label(label="Combos")
        title.add_css_class("title-2")
        title.set_halign(Gtk.Align.START)
        info_box.append(title)

        subtitle = Gtk.Label(label="Profile combo triggers and actions")
        subtitle.add_css_class("dim-label")
        subtitle.add_css_class("caption")
        subtitle.set_halign(Gtk.Align.START)
        info_box.append(subtitle)
        header.append(info_box)

        self.append(header)

    def _setup_combo_list(self) -> None:
        self.combo_frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.combo_frame.set_vexpand(True)
        self.combo_frame.set_margin_top(12)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.section_label = Gtk.Label(label="Combos")
        self.section_label.add_css_class("heading")
        self.section_label.set_hexpand(True)
        self.section_label.set_halign(Gtk.Align.START)
        toolbar.append(self.section_label)

        self.add_combo_button = Gtk.Button(label="Add Combo")
        self.add_combo_button.add_css_class("suggested-action")
        self.add_combo_button.connect("clicked", self._on_add_combo_clicked)
        toolbar.append(self.add_combo_button)
        self.combo_frame.append(toolbar)

        col_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        col_header.add_css_class("combo-column-header")

        self._name_header_btn = Gtk.Button(label="Name")
        self._name_header_btn.add_css_class("flat")
        self._name_header_btn.add_css_class("combo-col-btn")
        self._name_header_btn.set_hexpand(True)
        self._name_header_btn.set_halign(Gtk.Align.START)
        self._name_header_btn.connect("clicked", self._on_column_header_clicked, _SORT_NAME)
        col_header.append(self._name_header_btn)

        self._trigger_header_btn = Gtk.Button(label="Trigger")
        self._trigger_header_btn.add_css_class("flat")
        self._trigger_header_btn.add_css_class("combo-col-btn")
        self._trigger_header_btn.set_halign(Gtk.Align.START)
        self._trigger_header_btn.connect("clicked", self._on_column_header_clicked, _SORT_TRIGGER)
        col_header.append(self._trigger_header_btn)

        self._action_header_btn = Gtk.Button(label="Action")
        self._action_header_btn.add_css_class("flat")
        self._action_header_btn.add_css_class("combo-col-btn")
        self._action_header_btn.set_size_request(180, -1)
        self._action_header_btn.connect("clicked", self._on_column_header_clicked, _SORT_ACTION)
        col_header.append(self._action_header_btn)

        spacer = Gtk.Box()
        spacer.set_size_request(36, -1)
        col_header.append(spacer)

        self.column_header = col_header
        self.combo_frame.append(col_header)

        self.combo_listbox = Gtk.ListBox()
        self.combo_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.combo_listbox.add_css_class("boxed-list")
        self.combo_listbox.connect("row-activated", self._on_row_activated)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_child(self.combo_listbox)
        self.combo_frame.append(scrolled)

        self.append(self.combo_frame)

    def _selected_combos(self) -> list[ComboConfig]:
        if self._selected_profile is None:
            return []
        return self._selected_profile.config.combos

    def _after_profile_selection_applied(self) -> None:
        selected = self._selected_profile is not None
        self.add_combo_button.set_sensitive(selected)
        self.combo_frame.set_sensitive(selected)
        self._render_combo_list()

    def _sorted_combos(self) -> list[ComboConfig]:
        combos = self._selected_combos()
        if self._sort_column == _SORT_NAME:
            result = sorted(
                combos,
                key=lambda c: (c.name or combo_default_name(c)).casefold(),
            )
        elif self._sort_column == _SORT_TRIGGER:
            result = sorted(
                combos,
                key=lambda c: combo_trigger_label(c.steps).casefold(),
            )
        elif self._sort_column == _SORT_ACTION:
            result = sorted(
                combos,
                key=lambda c: describe_mapping_action(c.action).casefold(),
            )
        else:
            return list(combos)
        if not self._sort_ascending:
            result.reverse()
        return result

    def _update_column_header_labels(self) -> None:
        for col, btn in (
            (_SORT_NAME, self._name_header_btn),
            (_SORT_TRIGGER, self._trigger_header_btn),
            (_SORT_ACTION, self._action_header_btn),
        ):
            base = {_SORT_NAME: "Name", _SORT_TRIGGER: "Trigger", _SORT_ACTION: "Action"}[col]
            if self._sort_column == col:
                arrow = _ARROW_UP if self._sort_ascending else _ARROW_DOWN
                btn.set_label(f"{base}{arrow}")
            else:
                btn.set_label(base)

    def _render_combo_list(self) -> None:
        while row := self.combo_listbox.get_first_child():
            self.combo_listbox.remove(row)

        combos = self._sorted_combos()
        has_combos = bool(combos)
        self.combo_listbox.set_visible(has_combos)
        self.column_header.set_visible(has_combos)

        if self._selected_profile is None:
            self.section_label.set_text("Combos")
            return
        self.section_label.set_text("Combos" if has_combos else "No combos in this profile.")

        for combo in combos:
            self.combo_listbox.append(self._create_combo_row(combo))

    def _create_combo_row(self, combo: ComboConfig) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row._combo_id = combo.id  # type: ignore[attr-defined]

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.add_css_class("combo-row")

        name_label = Gtk.Label(label=combo.name or combo_default_name(combo))
        name_label.set_hexpand(True)
        name_label.set_halign(Gtk.Align.START)
        name_label.set_xalign(0.0)
        name_label.set_ellipsize(Pango.EllipsizeMode.END)
        name_label.add_css_class("heading")
        box.append(name_label)

        trigger_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        trigger_box.set_halign(Gtk.Align.START)
        for index, step in enumerate(combo.steps):
            pill = Gtk.Label(label=combo_step_label(step))
            pill.add_css_class("combo-step-pill")
            trigger_box.append(pill)
            if index < len(combo.steps) - 1:
                arrow = Gtk.Label(label="\u2192")
                arrow.add_css_class("dim-label")
                trigger_box.append(arrow)
        box.append(trigger_box)

        action_label = Gtk.Label(label=describe_mapping_action(combo.action))
        action_label.set_width_chars(22)
        action_label.set_max_width_chars(22)
        action_label.set_ellipsize(Pango.EllipsizeMode.END)
        action_label.set_halign(Gtk.Align.END)
        action_label.set_xalign(1.0)
        action_label.add_css_class("dim-label")
        action_label.add_css_class("caption")
        box.append(action_label)

        delete_button = Gtk.Button(icon_name="user-trash-symbolic")
        delete_button.add_css_class("flat")
        delete_button.add_css_class("destructive-action")
        delete_button.set_tooltip_text("Delete combo")
        delete_button.connect("clicked", self._on_delete_combo_clicked, combo.id)
        box.append(delete_button)

        row.set_child(box)
        return row

    def _on_column_header_clicked(self, _button: Gtk.Button, column: int) -> None:
        if self._sort_column == column:
            self._sort_ascending = not self._sort_ascending
        else:
            self._sort_column = column
            self._sort_ascending = True
        self._update_column_header_labels()
        self._render_combo_list()

    def _on_row_activated(
        self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow
    ) -> None:
        combo_id: str = row._combo_id  # type: ignore[attr-defined]
        combo = next((c for c in self._selected_combos() if c.id == combo_id), None)
        if combo is None:
            return
        self._open_combo_editor(combo)

    def _open_combo_editor(self, combo: ComboConfig | None = None) -> None:
        dialog = ComboEditorDialog(
            self,
            combo,
            profile_name=self.selected_profile_name(),
            sibling_combos=self._selected_combos(),
        )
        dialog.connect("combo-saved", self._on_combo_saved)
        dialog.present(self.get_root())

    def _on_combo_saved(self, _dialog: ComboEditorDialog, combo: ComboConfig) -> None:
        combos = self._selected_combos()
        for index, existing in enumerate(combos):
            if existing.id == combo.id:
                combos[index] = combo
                break
        else:
            combos.append(combo)
        self._save_profile()
        self._render_combo_list()

    def _on_add_combo_clicked(self, _button: Gtk.Button) -> None:
        if self._selected_profile is None:
            return
        self._open_combo_editor()

    def _on_delete_combo_clicked(self, _button: Gtk.Button, combo_id: str) -> None:
        combos = self._selected_combos()
        combos[:] = [combo for combo in combos if combo.id != combo_id]
        self._save_profile()
        self._render_combo_list()
