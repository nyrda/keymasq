from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gtk, Pango  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.models import ComboConfig
from keymasq.gui.icons import combo_icon_names, image_from_icon_names, resolve_icon_name
from keymasq.gui.widgets.action_labels import describe_mapping_action_compact
from keymasq.gui.widgets.combo_editor_dialog import ComboEditorDialog
from keymasq.gui.widgets.combo_list import SORT_ACTION, SORT_NAME, SORT_TRIGGER, SortableComboList
from keymasq.gui.widgets.combo_presentation import (
    combo_default_name,
    combo_search_text,
    combo_step_label,
    combo_trigger_label,
)
from keymasq.gui.widgets.profile_managed_tab import ProfileManagedTab
from keymasq.session.profiles import ProfileManager


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

        self._combo_list = SortableComboList[ComboConfig](
            search_placeholder="Search combos",
            search_tooltip="Filter combos by name, trigger, action, profile, device, or source",
            empty_text="No combos in this profile.",
            no_match_text="No matching combos.",
            get_items=self._selected_combos,
            sort_keys={
                SORT_NAME: lambda combo: combo.name or combo_default_name(combo),
                SORT_TRIGGER: lambda combo: combo_trigger_label(combo.steps),
                SORT_ACTION: lambda combo: describe_mapping_action_compact(combo.action),
            },
            create_row=self._create_combo_row,
            is_available=lambda: self._selected_profile is not None,
            row_activated=self._on_row_activated,
            trailing_header_width=36,
        )
        self.search_entry = self._combo_list.search_entry
        self.section_label = self._combo_list.section_label
        self.column_header = self._combo_list.column_header
        self.combo_listbox = self._combo_list.listbox
        self._name_header_btn = self._combo_list.name_header_btn
        self._trigger_header_btn = self._combo_list.trigger_header_btn
        self._action_header_btn = self._combo_list.action_header_btn

        self.combo_frame.append(self.search_entry)

        self.combo_frame.append(self.section_label)

        self.combo_frame.append(self.column_header)

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
        self._combo_list.render()

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

        action_label = Gtk.Label(label=describe_mapping_action_compact(combo.action))
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

    def _on_search_clicked(self, _button: Gtk.Button) -> None:
        self._combo_list.show_search()

    def _on_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if keyval in (Gdk.KEY_f, Gdk.KEY_F) and state & Gdk.ModifierType.CONTROL_MASK:
            self._combo_list.show_search()
            return True
        if keyval == Gdk.KEY_Escape and self.search_entry.get_visible():
            self._combo_list.hide_search()
            return True
        return False

    def _on_inspect_combos_clicked(self, _button: Gtk.Button) -> None:
        root = self.main_window or self.get_root()
        opener = getattr(root, "open_combo_inspector", None)
        if callable(opener):
            opener()

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
        self._combo_list.render()

    def _on_add_combo_clicked(self, _button: Gtk.Button) -> None:
        if self._selected_profile is None:
            return
        self._open_combo_editor()

    def _on_delete_combo_clicked(self, _button: Gtk.Button, combo_id: str) -> None:
        combo = next((combo for combo in self._selected_combos() if combo.id == combo_id), None)
        if combo is None:
            return

        dialog = Adw.AlertDialog()
        dialog.set_heading("Delete Combo")
        dialog.set_body(f"Delete '{combo.name or combo_default_name(combo)}'?")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_delete_combo_response, combo_id)
        dialog.present(self.get_root())

    def _on_delete_combo_response(
        self,
        _dialog: Adw.AlertDialog,
        response: str,
        combo_id: str,
    ) -> None:
        if response != "delete":
            return
        self._delete_combo(combo_id)

    def _delete_combo(self, combo_id: str) -> None:
        combos = self._selected_combos()
        combos[:] = [combo for combo in combos if combo.id != combo_id]
        self._save_profile()
        self._combo_list.render()
