from __future__ import annotations

from copy import deepcopy

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.models import ComboConfig
from keymasq.gui.icons import combo_icon_names, image_from_icon_names, resolve_icon_name
from keymasq.gui.widgets.action_labels import describe_mapping_action_compact
from keymasq.gui.widgets.combo_editor_dialog import ComboEditorDialog, combo_trigger_signature
from keymasq.gui.widgets.combo_list import SORT_ACTION, SORT_NAME, SORT_TRIGGER, SortableComboList
from keymasq.gui.widgets.combo_presentation import (
    combo_default_name,
    combo_search_text,
    combo_trigger_label,
    create_combo_summary_row,
)
from keymasq.gui.widgets.profile_managed_tab import ProfileManagedTab
from keymasq.session.profiles import ProfileInfo, ProfileManager

_DUPLICATE_COMBO_TRIGGER_MESSAGE = "A combo with the same trigger already exists in this profile."
_STALE_COMBO_EDIT_MESSAGE = (
    "This combo changed before it could be saved. Reopen the combo editor and try again."
)
_DELETED_COMBO_EDIT_MESSAGE = (
    "This combo was deleted before it could be saved. Reopen the combo editor and try again."
)


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

    def _resolve_combo_target_profile(
        self,
        target_profile: ProfileInfo | None,
    ) -> ProfileInfo | None:
        target_profile = target_profile or self._selected_profile
        if target_profile is None:
            return None
        if self.profile_manager is None or self.demo_mode:
            return target_profile
        return self.profile_manager.get_profile(target_profile.config.name)

    def _show_missing_combo_target_profile(self, profile_name: str) -> None:
        self._show_profile_error_dialog(
            f"Profile '{profile_name}' is no longer available. Select a profile and try again."
        )

    def _after_profile_selection_applied(self) -> None:
        selected = self._selected_profile is not None
        self.add_combo_button.set_sensitive(selected)
        self.search_button.set_sensitive(selected)
        self.combo_frame.set_sensitive(selected)
        self._combo_list.render()

    def _create_combo_row(self, combo: ComboConfig) -> Gtk.ListBoxRow:
        delete_button = Gtk.Button(icon_name="user-trash-symbolic")
        delete_button.add_css_class("flat")
        delete_button.add_css_class("destructive-action")
        delete_button.set_tooltip_text("Delete combo")
        delete_button.connect("clicked", self._on_delete_combo_clicked, combo.id)

        row = create_combo_summary_row(
            name=combo.name or combo_default_name(combo),
            steps=combo.steps,
            action=combo.action,
            trailing_widget=delete_button,
        )
        row._combo_id = combo.id  # type: ignore[attr-defined]
        row._search_text = combo_search_text(  # type: ignore[attr-defined]
            combo,
            profile_name=self.selected_profile_name() or "",
        )
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
        target_profile = self._selected_profile
        if target_profile is None:
            return
        emergency_cancel_combo_enabled = True
        policy_getter = getattr(self.main_window, "emergency_cancel_combo_enabled", None)
        if callable(policy_getter):
            emergency_cancel_combo_enabled = bool(policy_getter())
        dialog = ComboEditorDialog(
            self,
            combo,
            profile_name=target_profile.config.name,
            sibling_combos=target_profile.config.combos,
            emergency_cancel_combo_enabled=emergency_cancel_combo_enabled,
        )
        dialog.connect(
            "combo-saved",
            self._on_combo_saved,
            target_profile,
            deepcopy(combo) if combo is not None else None,
        )
        dialog.present(self)

    def _on_combo_saved(
        self,
        _dialog: ComboEditorDialog,
        combo: ComboConfig,
        target_profile: ProfileInfo | None = None,
        original_combo: ComboConfig | None = None,
    ) -> None:
        profile_name = target_profile.config.name if target_profile is not None else ""
        target_profile = self._resolve_combo_target_profile(target_profile)
        if target_profile is None:
            if profile_name:
                self._show_missing_combo_target_profile(profile_name)
            self._combo_list.render()
            return
        combos = target_profile.config.combos
        existing_index = self._combo_index(combos, combo.id)
        if original_combo is not None:
            existing_index = self._combo_index(combos, original_combo.id)
            if existing_index is None:
                self._show_profile_error_dialog(_DELETED_COMBO_EDIT_MESSAGE)
                self._combo_list.render()
                return
            if combos[existing_index] != original_combo:
                self._show_profile_error_dialog(_STALE_COMBO_EDIT_MESSAGE)
                self._combo_list.render()
                return
        ignore_combo_id = original_combo.id if original_combo is not None else combo.id
        if self._has_duplicate_combo_trigger(combo, combos, ignore_combo_id=ignore_combo_id):
            self._show_profile_error_dialog(_DUPLICATE_COMBO_TRIGGER_MESSAGE)
            self._combo_list.render()
            return
        if existing_index is None:
            combos.append(combo)
        else:
            combos[existing_index] = combo
        self._save_specific_profile(target_profile)
        self._combo_list.render()

    def _combo_index(self, combos: list[ComboConfig], combo_id: str) -> int | None:
        for index, combo in enumerate(combos):
            if combo.id == combo_id:
                return index
        return None

    def _has_duplicate_combo_trigger(
        self,
        combo: ComboConfig,
        combos: list[ComboConfig],
        *,
        ignore_combo_id: str,
    ) -> bool:
        current_steps = combo_trigger_signature(
            combo,
            match_across_devices=bool(combo.match_across_devices),
        )
        if not current_steps:
            return False
        for existing in combos:
            if existing.id == ignore_combo_id:
                continue
            existing_steps = combo_trigger_signature(
                existing,
                match_across_devices=bool(existing.match_across_devices),
            )
            if existing_steps == current_steps:
                return True
        return False

    def _on_add_combo_clicked(self, _button: Gtk.Button) -> None:
        if self._selected_profile is None:
            return
        self._open_combo_editor()

    def _on_delete_combo_clicked(self, _button: Gtk.Button, combo_id: str) -> None:
        target_profile = self._selected_profile
        if target_profile is None:
            return
        combo = next(
            (combo for combo in target_profile.config.combos if combo.id == combo_id), None
        )
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
        dialog.connect("response", self._on_delete_combo_response, combo_id, target_profile)
        dialog.present(self.get_root())

    def _on_delete_combo_response(
        self,
        _dialog: Adw.AlertDialog,
        response: str,
        combo_id: str,
        target_profile: ProfileInfo | None = None,
    ) -> None:
        if response != "delete":
            return
        self._delete_combo(combo_id, target_profile)

    def _delete_combo(
        self,
        combo_id: str,
        target_profile: ProfileInfo | None = None,
    ) -> None:
        profile_name = target_profile.config.name if target_profile is not None else ""
        target_profile = self._resolve_combo_target_profile(target_profile)
        if target_profile is None:
            if profile_name:
                self._show_missing_combo_target_profile(profile_name)
            self._combo_list.render()
            return
        combos = target_profile.config.combos
        combos[:] = [combo for combo in combos if combo.id != combo_id]
        self._save_specific_profile(target_profile)
        self._combo_list.render()
