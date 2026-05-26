from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gdk, Gtk, Pango  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.models import ComboConfig
from keymasq.gui.icons import combo_icon_names, image_from_icon_names, resolve_icon_name
from keymasq.gui.widgets.combo_editor_dialog import (
    ComboEditorDialog,
    combo_default_name,
    combo_step_label,
    combo_trigger_label,
    describe_mapping_action,
)
from keymasq.gui.widgets.fuzzy_search import fuzzy_query_matches, install_listbox_fuzzy_filter
from keymasq.gui.widgets.profile_managed_tab import ProfileManagedTab
from keymasq.session.profiles import ProfileManager

_SORT_NONE = 0
_SORT_NAME = 1
_SORT_TRIGGER = 2
_SORT_ACTION = 3

_ARROW_UP = " \u25b4"
_ARROW_DOWN = " \u25be"


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
        self._setup_key_controller()
        self.refresh_profiles()

    def _setup_key_controller(self) -> None:
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)
        self.set_focusable(True)

    def _setup_header(self) -> None:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        icon = image_from_icon_names(*combo_icon_names(), pixel_size=32)
        icon.set_valign(Gtk.Align.CENTER)
        header.append(icon)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label="Combos")
        title.add_css_class("title-2")
        title.set_halign(Gtk.Align.START)
        title_row.append(title)

        if not self.demo_mode:
            inspect_btn = Gtk.Button(
                icon_name=resolve_icon_name(
                    "view-reveal-symbolic",
                    "edit-find-symbolic",
                    "system-search-symbolic",
                    "zoom-in-symbolic",
                    "dialog-information-symbolic",
                )
            )
            inspect_btn.set_tooltip_text("Inspect active combos")
            inspect_btn.add_css_class("flat")
            inspect_btn.set_valign(Gtk.Align.CENTER)
            inspect_btn.connect("clicked", self._on_inspect_combos_clicked)
            title_row.append(inspect_btn)
        info_box.append(title_row)

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

        self.add_combo_button = Gtk.Button(label="Add Combo")
        self.add_combo_button.add_css_class("suggested-action")
        self.add_combo_button.connect("clicked", self._on_add_combo_clicked)
        toolbar.append(self.add_combo_button)

        self.search_button = Gtk.Button()
        self.search_button.set_icon_name("system-search-symbolic")
        self.search_button.set_tooltip_text("Search combos")
        self.search_button.connect("clicked", self._on_search_clicked)
        toolbar.append(self.search_button)

        self.combo_frame.append(toolbar)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search combos")
        self.search_entry.set_tooltip_text(
            "Filter combos by name, trigger, action, profile, device, or source"
        )
        self.search_entry.set_visible(False)
        self.search_entry.connect("stop-search", self._on_search_stop)
        self.combo_frame.append(self.search_entry)

        self.section_label = Gtk.Label(label="")
        self.section_label.add_css_class("heading")
        self.section_label.set_hexpand(True)
        self.section_label.set_halign(Gtk.Align.START)
        self.section_label.set_visible(False)
        self.combo_frame.append(self.section_label)

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
        install_listbox_fuzzy_filter(
            self.combo_listbox,
            self.search_entry,
            after_filter_changed=self._after_search_filter_changed,
        )

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
        self.search_button.set_sensitive(selected)
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

        if self._selected_profile is None:
            self._hide_search()
            self.section_label.set_visible(False)
            self.combo_listbox.set_visible(False)
            self.column_header.set_visible(False)
            return

        for combo in combos:
            self.combo_listbox.append(self._create_combo_row(combo))
        self._update_combo_list_state(has_combos=bool(combos))

    def _create_combo_row(self, combo: ComboConfig) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row._combo_id = combo.id  # type: ignore[attr-defined]
        row._search_text = combo_search_text(  # type: ignore[attr-defined]
            combo,
            profile_name=self.selected_profile_name() or "",
        )

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

    def _iter_combo_rows(self):
        row = self.combo_listbox.get_first_child()
        while row is not None:
            yield row
            row = row.get_next_sibling()

    def _visible_combo_count(self) -> int:
        query = self.search_entry.get_text()
        return sum(
            1
            for row in self._iter_combo_rows()
            if fuzzy_query_matches(query, getattr(row, "_search_text", ""))
        )

    def _update_combo_list_state(self, *, has_combos: bool | None = None) -> None:
        if self._selected_profile is None:
            self._hide_search()
            self.section_label.set_visible(False)
            self.combo_listbox.set_visible(False)
            self.column_header.set_visible(False)
            return

        has_combos = has_combos if has_combos is not None else any(self._iter_combo_rows())
        visible_count = self._visible_combo_count() if has_combos else 0
        has_visible_rows = visible_count > 0
        self.combo_listbox.set_visible(has_visible_rows)
        self.column_header.set_visible(has_visible_rows)
        if has_visible_rows:
            self.section_label.set_visible(False)
        elif has_combos and self.search_entry.get_text().strip():
            self.section_label.set_text("No matching combos.")
            self.section_label.set_visible(True)
        else:
            self.section_label.set_text("No combos in this profile.")
            self.section_label.set_visible(True)

    def _after_search_filter_changed(self) -> None:
        self._update_combo_list_state()

    def _show_search(self) -> None:
        if self._selected_profile is None:
            return
        self.search_entry.set_visible(True)
        self.search_entry.grab_focus()
        self.search_entry.select_region(0, -1)

    def _hide_search(self) -> None:
        if self.search_entry.get_text():
            self.search_entry.set_text("")
        self.search_entry.set_visible(False)

    def _on_search_clicked(self, _button: Gtk.Button) -> None:
        self._show_search()

    def _on_search_stop(self, _entry: Gtk.SearchEntry) -> None:
        self._hide_search()

    def _on_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if keyval in (Gdk.KEY_f, Gdk.KEY_F) and state & Gdk.ModifierType.CONTROL_MASK:
            self._show_search()
            return True
        if keyval == Gdk.KEY_Escape and self.search_entry.get_visible():
            self._hide_search()
            return True
        return False

    def _on_inspect_combos_clicked(self, _button: Gtk.Button) -> None:
        root = self.main_window or self.get_root()
        opener = getattr(root, "open_combo_inspector", None)
        if callable(opener):
            opener()

    def _on_column_header_clicked(self, _button: Gtk.Button, column: int) -> None:
        if self._sort_column == column:
            self._sort_ascending = not self._sort_ascending
        else:
            self._sort_column = column
            self._sort_ascending = True
        self._update_column_header_labels()
        self._render_combo_list()

    def _on_row_activated(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        combo_id: str = row._combo_id  # type: ignore[attr-defined]
        combo = next((c for c in self._selected_combos() if c.id == combo_id), None)
        if combo is None:
            return
        self._open_combo_editor(combo)

    def _open_combo_editor(self, combo: ComboConfig | None = None) -> None:
        emergency_cancel_combo_enabled = True
        policy_getter = getattr(self.main_window, "emergency_cancel_combo_enabled", None)
        if callable(policy_getter):
            emergency_cancel_combo_enabled = bool(policy_getter())
        dialog = ComboEditorDialog(
            self,
            combo,
            profile_name=self.selected_profile_name(),
            sibling_combos=self._selected_combos(),
            emergency_cancel_combo_enabled=emergency_cancel_combo_enabled,
        )
        dialog.connect("combo-saved", self._on_combo_saved)
        dialog.present(self)

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


def combo_search_text(combo: ComboConfig, *, profile_name: str = "") -> str:
    parts = [
        combo.name or combo_default_name(combo),
        combo_trigger_label(combo.steps),
        describe_mapping_action(combo.action),
        profile_name,
    ]
    for step in combo.steps:
        if step.timeout_ms is not None:
            parts.append(f"{int(step.timeout_ms)}ms")
        for event in step.events:
            parts.extend([event.evdev, event.hardware_id, event.source or ""])
    if combo.recall_trigger_keys:
        parts.append("recall trigger keys")
    if combo.restore_trigger_keys:
        parts.extend(combo.restore_trigger_keys)
    if combo.match_across_devices:
        parts.append("any device across devices")
    return " ".join(str(part) for part in parts if str(part or "").strip())
