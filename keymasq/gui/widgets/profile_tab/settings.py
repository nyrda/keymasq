"""Profile settings widget construction and dialog lifecycle."""

from __future__ import annotations

import logging
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq import __version__
from keymasq.gui.widgets.docs_links import docs_page_url

log = logging.getLogger("keymasq.gui.widgets.profile_managed_tab")


class ProfileSettingsMixin:
    """Build and coordinate the reusable profile settings surface."""

    def _setup_profile_settings(self: Any) -> None:
        settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        settings_box.set_margin_top(12)
        settings_box.set_margin_bottom(12)
        settings_box.set_margin_start(12)
        settings_box.set_margin_end(12)

        settings_group = Adw.PreferencesGroup()

        self.name_entry = Adw.EntryRow(title="Name")
        self.name_entry.connect("entry-activated", self._on_name_changed)
        self._name_focus_controller = Gtk.EventControllerFocus()
        self._name_focus_controller.connect("leave", self._on_name_focus_leave)
        self.name_entry.add_controller(self._name_focus_controller)
        settings_group.add(self.name_entry)

        priority_row = Adw.ActionRow(title="Priority")
        self.priority_spin = Gtk.SpinButton()
        self.priority_spin.set_adjustment(
            Gtk.Adjustment(value=0, lower=0, upper=100, step_increment=1)
        )
        self.priority_spin.set_valign(Gtk.Align.CENTER)
        self.priority_spin.set_tooltip_text(
            "Higher priority wins when multiple conditional profiles match"
        )
        self.priority_spin.connect("value-changed", self._on_priority_changed)
        priority_row.add_suffix(self.priority_spin)
        settings_group.add(priority_row)

        self.window_rules_row = Adw.ActionRow(
            title="Window Rules",
            subtitle="No rules",
            subtitle_lines=0,
        )
        self.window_rules_row.set_tooltip_text(
            "Profiles are always active unless window rules are configured."
        )
        edit_rules_btn = Gtk.Button(label="Edit")
        edit_rules_btn.set_valign(Gtk.Align.CENTER)
        edit_rules_btn.connect("clicked", self._on_edit_window_rules)
        self.window_rules_row.add_suffix(edit_rules_btn)
        self.window_rules_row.set_activatable_widget(edit_rules_btn)
        settings_group.add(self.window_rules_row)

        self.notify_switch = Adw.SwitchRow(title="Notify on activation")
        self.notify_switch.set_tooltip_text(
            "Show a desktop notification when this profile becomes active."
        )
        self.notify_switch.set_active(True)
        self.notify_switch.connect("notify::active", self._on_notify_toggled)
        settings_group.add(self.notify_switch)

        self.activation_macro_dropdown = Adw.ComboRow(title="Activation Macro")
        self.activation_macro_dropdown.set_tooltip_text(
            "Macro to play once when this profile becomes active."
        )
        self.activation_macro_dropdown.connect(
            "notify::selected",
            self._on_activation_macro_changed,
        )
        settings_group.add(self.activation_macro_dropdown)

        self.deactivation_macro_dropdown = Adw.ComboRow(title="Deactivation Macro")
        self.deactivation_macro_dropdown.set_tooltip_text(
            "Macro to play once when this profile stops being active."
        )
        self.deactivation_macro_dropdown.connect(
            "notify::selected",
            self._on_deactivation_macro_changed,
        )
        settings_group.add(self.deactivation_macro_dropdown)

        self._refresh_lifecycle_macro_dropdowns()
        self._load_lifecycle_macros()

        settings_box.append(settings_group)
        self._append_profile_settings_groups(settings_box)

        self._profile_settings_content = settings_box
        self._profile_settings_dialog: Adw.Dialog | None = None
        self.settings_frame = self.settings_btn

    def _on_name_focus_leave(self: Any, _controller: object) -> None:
        self._on_name_changed(self.name_entry)

    def _update_profile_settings(self: Any) -> None:
        if not self._selected_profile:
            return

        config = self._selected_profile.config
        self.name_entry.handler_block_by_func(self._on_name_changed)
        self.name_entry.set_text(config.name)
        self.name_entry.handler_unblock_by_func(self._on_name_changed)

        self.priority_spin.handler_block_by_func(self._on_priority_changed)
        self.priority_spin.set_value(config.priority)
        self.priority_spin.handler_unblock_by_func(self._on_priority_changed)

        self.notify_switch.handler_block_by_func(self._on_notify_toggled)
        self.notify_switch.set_active(config.notify_on_activation)
        self.notify_switch.handler_unblock_by_func(self._on_notify_toggled)

        self._refresh_lifecycle_macro_dropdowns()
        self._update_rules_label()
        self._update_extra_profile_settings()

    def _on_profile_settings_clicked(self: Any, _button: Gtk.Button) -> None:
        if not self._selected_profile:
            return
        if self._profile_settings_dialog is not None:
            self._profile_settings_dialog.present(self.get_root())
            return

        dialog = Adw.Dialog(title="Profile Settings", content_width=640, content_height=620)
        dialog.connect("closed", self._on_profile_settings_dialog_closed)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_propagate_natural_height(True)
        scrolled.set_child(self._profile_settings_content)
        content.append(scrolled)
        content.append(Gtk.Separator())

        footer = Gtk.CenterBox(orientation=Gtk.Orientation.HORIZONTAL)
        footer.set_margin_top(6)
        footer.set_margin_bottom(6)
        footer.set_margin_start(12)
        footer.set_margin_end(12)

        docs_btn = Gtk.Button(label="?")
        docs_btn.add_css_class("flat")
        docs_btn.add_css_class("actions-docs-button")
        docs_btn.set_tooltip_text("Open Profiles documentation")
        docs_btn.connect("clicked", self._on_profiles_docs_clicked)
        footer.set_start_widget(docs_btn)

        end_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        delete_btn = Gtk.Button(label="Delete Profile")
        delete_btn.add_css_class("destructive-action")
        delete_btn.set_sensitive(not self.demo_mode)
        delete_btn.connect("clicked", self._on_delete_profile, dialog)
        end_box.append(delete_btn)

        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", self._on_close_dialog_clicked, dialog)
        end_box.append(close_btn)

        footer.set_end_widget(end_box)
        content.append(footer)

        dialog.set_child(content)
        self._profile_settings_dialog = dialog
        self._update_profile_settings()
        dialog.present(self.get_root())

    def _on_profile_dropdown_right_clicked(
        self: Any,
        _click: Gtk.GestureClick,
        _n_press: int,
        _x: float,
        _y: float,
    ) -> None:
        self._on_profile_settings_clicked(self.settings_btn)

    def _on_profile_settings_dialog_closed(self: Any, dialog: Adw.Dialog) -> None:
        if dialog is self._profile_settings_dialog:
            parent = self._profile_settings_content.get_parent()
            if isinstance(parent, Gtk.ScrolledWindow):
                parent.set_child(None)
            dialog.set_child(None)
            self._profile_settings_dialog = None

    def _on_profiles_docs_clicked(self: Any, _button: Gtk.Button) -> None:
        url = docs_page_url("PROFILES", version=__version__)
        try:
            launcher = Gtk.UriLauncher.new(url)
            launcher.launch(None, None, None)
        except Exception:
            log.exception("Could not open Profiles documentation %s", url)

    def _on_priority_changed(self: Any, spin: Gtk.SpinButton) -> None:
        if not self._selected_profile:
            return
        self._selected_profile.config.priority = int(spin.get_value())
        self._save_profile()

    def _on_notify_toggled(self: Any, switch_row: Adw.SwitchRow, _param: object) -> None:
        if not self._selected_profile:
            return
        self._selected_profile.config.notify_on_activation = switch_row.get_active()
        self._save_profile()
