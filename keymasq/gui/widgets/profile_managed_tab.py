from __future__ import annotations

import copy
import logging
import re
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk, Pango  # pyright: ignore[reportAttributeAccessIssue]

from keymasq import __version__
from keymasq.common.models import ProfileConfig, ProfileState, WindowRule
from keymasq.gui.session_client import (
    get_active_window_async,
    session_request_async,
)
from keymasq.gui.session_reload import notify_session_reload, notify_session_reload_async
from keymasq.gui.wizards.profile_create import ProfileCreateDialog
from keymasq.session.profiles import ProfileInfo, ProfileManager

PROFILE_TYPE_ICONS = {
    "permanent": "⭐",
    "conditional": "🪟",
}

log = logging.getLogger("keymasq.gui.widgets.profile_managed_tab")


def _docs_version() -> str:
    version = __version__.strip()
    if not version:
        return "master"
    if "dev" in version:
        return "master"
    return f"v{version.removeprefix('v')}"


def _profiles_docs_url() -> str:
    return f"https://keymasq.tools/docs/{_docs_version()}/PROFILES/"


class ProfileManagedTab(Gtk.Box):
    def __init__(
        self,
        profile_manager: ProfileManager | None,
        main_window=None,
        demo_mode: bool = False,
        compositor_capabilities: list[str] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.profile_manager = profile_manager
        self.main_window = main_window
        self.demo_mode = demo_mode
        self._compositor_capabilities = compositor_capabilities or []
        self.profiles = self.profile_manager.list_profiles() if self.profile_manager else []
        self._selected_profile: ProfileInfo | None = None
        self._profile_names: list[str] = []
        self._profile_items: list[ProfileInfo | None] = []
        self._active_profile_names: list[str] = []
        self._suspend_profile_signal = False
        self._window_rule_capture_pending = False
        self._window_rule_capture_timeout_id = 0
        self._window_rule_capture_generation = 0
        self._window_rules_target_profile_name: str | None = None
        self._profile_lifecycle_macro_names: list[str] = []
        self._profile_lifecycle_macro_options: list[str] = [""]
        self._suspend_lifecycle_macro_signal = False
        self._registered_macro_event_handlers = False
        if self.main_window is not None and hasattr(self.main_window, "register_event_handler"):
            self.main_window.register_event_handler("macro_saved", self._on_macro_list_changed)
            self.main_window.register_event_handler("macro_deleted", self._on_macro_list_changed)
            self._registered_macro_event_handlers = True
        self.connect("destroy", self._on_profile_managed_destroy)

        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_margin_start(12)
        self.set_margin_end(12)

    def _window_selected_profile_name(self) -> str | None:
        if self.main_window is not None:
            return self.main_window._selected_profile_name
        root = self.get_root()
        if root and hasattr(root, "_selected_profile_name"):
            return root._selected_profile_name
        return None

    def selected_profile_name(self) -> str | None:
        if self._selected_profile:
            return self._selected_profile.config.name
        return None

    def _setup_profile_selector(self) -> None:
        profile_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        profile_box.set_margin_top(12)

        profile_label = Gtk.Label(label="Profile:")
        profile_label.set_halign(Gtk.Align.START)
        profile_box.append(profile_label)

        self.profile_dropdown = Gtk.DropDown()
        self._setup_profile_dropdown()
        self.profile_dropdown.set_hexpand(True)
        self.profile_dropdown.connect("notify::selected", self._on_profile_selected)
        profile_dropdown_click = Gtk.GestureClick()
        profile_dropdown_click.set_button(3)
        profile_dropdown_click.connect("released", self._on_profile_dropdown_right_clicked)
        self.profile_dropdown.add_controller(profile_dropdown_click)
        profile_box.append(self.profile_dropdown)

        self.enabled_check = Gtk.CheckButton(label="Enabled")
        self.enabled_check.set_sensitive(False)
        self.enabled_check.connect("toggled", self._on_enabled_toggled)
        profile_box.append(self.enabled_check)

        btn_group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        btn_group.add_css_class("linked")

        new_btn = Gtk.Button(icon_name="list-add-symbolic")
        new_btn.set_tooltip_text("New profile")
        new_btn.connect("clicked", self._on_new_profile)
        btn_group.append(new_btn)

        copy_btn = Gtk.Button(icon_name="edit-copy-symbolic")
        copy_btn.set_tooltip_text("Copy profile")
        copy_btn.connect("clicked", self._on_copy_profile)
        btn_group.append(copy_btn)

        settings_btn = Gtk.Button(icon_name="emblem-system-symbolic")
        settings_btn.set_tooltip_text("Profile settings")
        settings_btn.connect("clicked", self._on_profile_settings_clicked)
        btn_group.append(settings_btn)
        self.settings_btn = settings_btn

        profile_box.append(btn_group)

        self.status_label = Gtk.Label()
        self.status_label.add_css_class("status-pill")
        self.status_label.set_tooltip_text(
            "State of the currently selected profile. "
            "Other profiles can be active at the same time."
        )
        profile_box.append(self.status_label)

        self.append(profile_box)

        active_profile_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        active_profile_box.add_css_class("active-profiles-summary")
        active_profile_box.set_tooltip_text(self._active_profiles_summary_tooltip())

        active_profile_title = Gtk.Label(label=self._active_profiles_summary_title())
        active_profile_title.add_css_class("caption")
        active_profile_title.add_css_class("dim-label")
        active_profile_box.append(active_profile_title)
        self.active_profiles_title_label = active_profile_title

        self.active_profiles_label = Gtk.Label(label="None")
        self.active_profiles_label.add_css_class("caption")
        self.active_profiles_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.active_profiles_label.set_hexpand(True)
        self.active_profiles_label.set_halign(Gtk.Align.START)
        active_profile_box.append(self.active_profiles_label)

        self.append(active_profile_box)
        self._update_active_profiles_summary()

        self._setup_profile_settings()

    def _setup_profile_settings(self) -> None:
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

        self.window_rules_row = Adw.ActionRow(title="Window Rules")
        self.window_rules_row.set_tooltip_text(
            "Profiles are always active unless window rules are configured."
        )
        self.rules_list_label = Gtk.Label(label="No rules")
        self.rules_list_label.add_css_class("dim-label")
        self.rules_list_label.set_valign(Gtk.Align.CENTER)
        self.window_rules_row.add_suffix(self.rules_list_label)
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

    def _append_profile_settings_groups(self, container: Gtk.Box) -> None:
        _ = container

    def _selected_layer(self, create: bool = False) -> object | None:
        _ = create
        return None

    def _has_unsupported_rules(self, config: ProfileConfig) -> bool:
        has_tag_support = "window_tags" in self._compositor_capabilities
        return any(rule.field == "tag" and not has_tag_support for rule in config.window_rules)

    def _active_profile_names_from_response(self, data: dict) -> list[str]:
        active_profiles = data.get("active_profiles", [])
        if not isinstance(active_profiles, list):
            return []
        return [str(name) for name in active_profiles]

    def _active_profiles_summary_title(self) -> str:
        return "Active profiles:"

    def _active_profiles_summary_tooltip(self) -> str:
        return (
            "Profiles are layered. All listed profiles are active; "
            "later profiles override earlier ones."
        )

    def _active_profiles_empty_tooltip(self) -> str:
        return "No profiles are active for this view."

    def _active_profiles_layer_tooltip(self) -> str:
        return "Layer order: " + " -> ".join(self._active_profile_names)

    def _after_profile_selection_applied(self) -> None:
        return

    def _after_active_profiles_changed(self) -> None:
        return

    def _update_extra_profile_settings(self) -> None:
        return

    def _refresh_other_profile_tabs(self, preferred_profile_name: str | None = None) -> None:
        root = self.main_window or self.get_root()
        if root and hasattr(root, "_refresh_device_tabs"):
            root._refresh_device_tabs(
                preferred_profile_name=preferred_profile_name,
                source_widget=self,
            )

    def _publish_profile_selection(self) -> None:
        root = self.main_window or self.get_root()
        if root and hasattr(root, "_sync_selected_profile_name"):
            root._sync_selected_profile_name(
                self._selected_profile.config.name if self._selected_profile else None,
                source_widget=self,
            )
        elif root and hasattr(root, "_set_selected_profile_name"):
            root._set_selected_profile_name(
                self._selected_profile.config.name if self._selected_profile else None
            )

    def _on_name_changed(self, entry: Gtk.Entry) -> None:
        if not self._selected_profile or self.profile_manager is None:
            return

        new_name = entry.get_text().strip()
        if not new_name or new_name == self._selected_profile.config.name:
            return

        old_name = self._selected_profile.config.name
        try:
            renamed = self.profile_manager.rename_profile(old_name, new_name)
        except ValueError as exc:
            self._show_profile_error_dialog(str(exc))
            return

        self._selected_profile = renamed
        if old_name in self._profile_names:
            self._profile_names[self._profile_names.index(old_name)] = new_name

        self._refresh_profile_dropdown_states()
        self._refresh_other_profile_tabs(preferred_profile_name=new_name)
        notify_session_reload_async()

    def _on_name_focus_leave(self, _controller) -> None:
        self._on_name_changed(self.name_entry)

    def _build_profile_copy_config(self) -> ProfileConfig | None:
        if not self._selected_profile or self.profile_manager is None:
            return None

        base_name = self._selected_profile.config.name
        match = re.match(r"^(.+?)_(\d+)$", base_name)
        if match:
            base = match.group(1)
            num = int(match.group(2))
            new_name = f"{base}_{num + 1}"
        else:
            new_name = f"{base_name}_1"

        existing_names = [profile.config.name for profile in self.profiles]
        while new_name in existing_names:
            match = re.match(r"^(.+?)_(\d+)$", new_name)
            if match:
                base = match.group(1)
                num = int(match.group(2))
                new_name = f"{base}_{num + 1}"
            else:
                new_name = f"{new_name}_1"

        return ProfileConfig(
            name=new_name,
            enabled=self._selected_profile.config.enabled,
            is_permanent=self._selected_profile.config.is_permanent,
            priority=self.profile_manager.get_next_priority(),
            notify_on_activation=self._selected_profile.config.notify_on_activation,
            activation_macro_name=self._selected_profile.config.activation_macro_name,
            deactivation_macro_name=self._selected_profile.config.deactivation_macro_name,
            window_rules=copy.deepcopy(self._selected_profile.config.window_rules),
            device_layers=copy.deepcopy(self._selected_profile.config.device_layers),
            combos=copy.deepcopy(self._selected_profile.config.combos),
            created_at=datetime.now(),
        )

    def _on_copy_profile(self, _button: Gtk.Button) -> None:
        if self.demo_mode or self.profile_manager is None:
            return
        new_config = self._build_profile_copy_config()
        if new_config is None:
            return
        try:
            self.profile_manager.save_profile(new_config)
        except ValueError as exc:
            self._show_profile_error_dialog(str(exc))
            return

        self._refresh_other_profile_tabs(preferred_profile_name=new_config.name)
        self.refresh_profiles(preferred_profile_name=new_config.name, publish_selection=False)
        notify_session_reload_async()

    def _on_profile_settings_clicked(self, _button: Gtk.Button) -> None:
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
        self,
        _click: Gtk.GestureClick,
        _n_press: int,
        _x: float,
        _y: float,
    ) -> None:
        self._on_profile_settings_clicked(self.settings_btn)

    def _on_profile_settings_dialog_closed(self, dialog: Adw.Dialog) -> None:
        if dialog is self._profile_settings_dialog:
            parent = self._profile_settings_content.get_parent()
            if isinstance(parent, Gtk.ScrolledWindow):
                parent.set_child(None)
            dialog.set_child(None)
            self._profile_settings_dialog = None

    def _on_profiles_docs_clicked(self, _button: Gtk.Button) -> None:
        url = _profiles_docs_url()
        try:
            launcher = Gtk.UriLauncher.new(url)
            launcher.launch(None, None, None)
        except Exception as exc:
            log.warning("Could not open Profiles documentation %s: %s", url, exc)

    def _on_delete_profile(
        self,
        _button: Gtk.Button,
        close_after_delete: Adw.Dialog | None = None,
    ) -> None:
        if not self._selected_profile or self.demo_mode:
            return

        profile_name = self._selected_profile.config.name
        dialog = Adw.Dialog(title="Delete Profile", content_width=360, content_height=-1)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_margin_top(20)
        content.set_margin_bottom(20)
        content.set_margin_start(20)
        content.set_margin_end(20)

        message = Gtk.Label(label=f"Delete profile '{profile_name}'?\nThis cannot be undone.")
        message.set_halign(Gtk.Align.START)
        content.append(message)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.END)
        btn_box.set_margin_top(8)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_close_dialog_clicked, dialog)
        btn_box.append(cancel_btn)

        delete_btn = Gtk.Button(label="Delete")
        delete_btn.add_css_class("destructive-action")
        delete_btn.connect(
            "clicked",
            self._on_confirm_delete_profile,
            dialog,
            close_after_delete,
        )
        btn_box.append(delete_btn)

        content.append(btn_box)
        dialog.set_child(content)
        dialog.present(self.get_root())

    def _on_confirm_delete_profile(
        self,
        _button: Gtk.Button,
        dialog: Adw.Dialog,
        close_after_delete: Adw.Dialog | None = None,
    ) -> None:
        if not self._selected_profile or self.profile_manager is None:
            dialog.close()
            return

        self.profile_manager.delete_profile(self._selected_profile.config.name)
        self._refresh_other_profile_tabs()
        self.refresh_profiles(publish_selection=False)
        dialog.close()
        if close_after_delete is not None:
            close_after_delete.close()
        notify_session_reload_async()

    def _on_priority_changed(self, spin: Gtk.SpinButton) -> None:
        if not self._selected_profile:
            return
        self._selected_profile.config.priority = int(spin.get_value())
        self._save_profile()

    def _on_notify_toggled(self, switch_row: Adw.SwitchRow, _param) -> None:
        if not self._selected_profile:
            return
        self._selected_profile.config.notify_on_activation = switch_row.get_active()
        self._save_profile()

    def _on_lifecycle_macros_loaded(self, result: dict | None) -> bool:
        macros = (result or {}).get("macros", [])
        names: list[str] = []
        if isinstance(macros, list):
            for macro in macros:
                if not isinstance(macro, dict):
                    continue
                name = str(macro.get("name", "") or "").strip()
                if name:
                    names.append(name)
        self._profile_lifecycle_macro_names = sorted(set(names), key=str.casefold)
        self._refresh_lifecycle_macro_dropdowns()
        return False

    def _load_lifecycle_macros(self) -> None:
        if self.profile_manager is None or self.demo_mode:
            return
        session_request_async({"command": "list_macros"}, self._on_lifecycle_macros_loaded)

    def _on_macro_list_changed(self, _event: dict) -> None:
        self._load_lifecycle_macros()

    def _on_profile_managed_destroy(self, _widget) -> None:
        if not self._registered_macro_event_handlers:
            return
        if self.main_window is not None and hasattr(self.main_window, "unregister_event_handler"):
            self.main_window.unregister_event_handler("macro_saved", self._on_macro_list_changed)
            self.main_window.unregister_event_handler("macro_deleted", self._on_macro_list_changed)
        self._registered_macro_event_handlers = False

    def _refresh_lifecycle_macro_dropdowns(self) -> None:
        selected_names = []
        if self._selected_profile:
            selected_names = [
                self._selected_profile.config.activation_macro_name or "",
                self._selected_profile.config.deactivation_macro_name or "",
            ]
        options = [""]
        for name in self._profile_lifecycle_macro_names + selected_names:
            if name and name not in options:
                options.append(name)
        self._profile_lifecycle_macro_options = options

        activation_model = Gtk.StringList()
        deactivation_model = Gtk.StringList()
        for name in options:
            label = name or "None"
            activation_model.append(label)
            deactivation_model.append(label)

        self._suspend_lifecycle_macro_signal = True
        try:
            self.activation_macro_dropdown.set_model(activation_model)
            self.deactivation_macro_dropdown.set_model(deactivation_model)
            self._select_lifecycle_macro(
                self.activation_macro_dropdown,
                self._selected_profile.config.activation_macro_name
                if self._selected_profile
                else None,
            )
            self._select_lifecycle_macro(
                self.deactivation_macro_dropdown,
                self._selected_profile.config.deactivation_macro_name
                if self._selected_profile
                else None,
            )
        finally:
            self._suspend_lifecycle_macro_signal = False

    def _select_lifecycle_macro(self, dropdown: Gtk.DropDown, macro_name: str | None) -> None:
        selected_name = macro_name or ""
        try:
            index = self._profile_lifecycle_macro_options.index(selected_name)
        except ValueError:
            index = 0
        dropdown.set_selected(index)

    def _lifecycle_macro_name_for_dropdown(self, dropdown: Gtk.DropDown) -> str | None:
        selected = dropdown.get_selected()
        if selected >= len(self._profile_lifecycle_macro_options):
            return None
        return self._profile_lifecycle_macro_options[selected] or None

    def _on_activation_macro_changed(self, dropdown: Gtk.DropDown, _param) -> None:
        if self._suspend_lifecycle_macro_signal or not self._selected_profile:
            return
        self._selected_profile.config.activation_macro_name = (
            self._lifecycle_macro_name_for_dropdown(dropdown)
        )
        self._save_profile()

    def _on_deactivation_macro_changed(self, dropdown: Gtk.DropDown, _param) -> None:
        if self._suspend_lifecycle_macro_signal or not self._selected_profile:
            return
        self._selected_profile.config.deactivation_macro_name = (
            self._lifecycle_macro_name_for_dropdown(dropdown)
        )
        self._save_profile()

    def _on_edit_window_rules(self, _button: Gtk.Button) -> None:
        if not self._selected_profile:
            return
        self._show_window_rules_dialog()

    def _show_window_rules_dialog(self) -> None:
        if not self._selected_profile:
            return
        self._window_rules_target_profile_name = self._selected_profile.config.name
        self._current_rules_dialog = Adw.Dialog(
            title="Window Rules", content_width=500, content_height=450
        )
        self._current_rules_dialog.connect("closed", self._on_window_rules_dialog_closed)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_margin_top(20)
        content.set_margin_bottom(20)
        content.set_margin_start(20)
        content.set_margin_end(20)

        help_label = Gtk.Label(
            label="Rules are matched with AND logic.\nUse Regex patterns for class/title/tag."
        )
        help_label.add_css_class("dim-label")
        help_label.add_css_class("caption")
        help_label.set_halign(Gtk.Align.START)
        content.append(help_label)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_margin_top(8)

        self._rules_list_box = Gtk.ListBox()
        self._rules_list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._rules_list_box.add_css_class("boxed-list")
        self._rule_rows = []

        rules = self._selected_profile.config.window_rules
        for index, rule in enumerate(rules):
            row_widget = self._create_rule_row(rule, is_first=(index == 0))
            self._rules_list_box.append(row_widget)
            self._rule_rows.append(row_widget)

        if not rules:
            empty_label = self._create_empty_row()
            self._rules_list_box.append(empty_label)
            self._rule_rows.append(empty_label)
        else:
            self._update_first_rule_delete_button()

        scrolled.set_child(self._rules_list_box)
        content.append(scrolled)

        actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._window_rule_capture_btn = Gtk.Button(label="Capture Window (2)")
        self._window_rule_capture_btn.connect("clicked", self._on_capture_window_rules_clicked)
        actions_box.append(self._window_rule_capture_btn)

        self._window_rule_capture_status = Gtk.Label(label="")
        self._window_rule_capture_status.add_css_class("dim-label")
        self._window_rule_capture_status.set_hexpand(True)
        self._window_rule_capture_status.set_halign(Gtk.Align.START)
        actions_box.append(self._window_rule_capture_status)

        add_btn = Gtk.Button(label="Add Rule")
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", self._on_add_window_rule)
        actions_box.append(add_btn)

        content.append(actions_box)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        remove_rules_btn = Gtk.Button(label="Remove Window Rules")
        remove_rules_btn.add_css_class("destructive-action")
        remove_rules_btn.connect("clicked", self._on_remove_window_rules_clicked)
        btn_box.append(remove_rules_btn)
        self._remove_window_rules_btn = remove_rules_btn

        btn_spacer = Gtk.Box()
        btn_spacer.set_hexpand(True)
        btn_box.append(btn_spacer)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_close_current_rules_dialog_clicked)
        btn_box.append(cancel_btn)

        apply_btn = Gtk.Button(label="Apply")
        apply_btn.add_css_class("suggested-action")
        apply_btn.connect("clicked", self._on_apply_window_rules)
        btn_box.append(apply_btn)

        content.append(btn_box)
        self._current_rules_dialog.set_child(content)
        self._update_window_rules_remove_button()
        self._current_rules_dialog.present(self.get_root())

    def _on_window_rules_dialog_closed(self, dialog: Adw.Dialog) -> None:
        _ = dialog
        self._window_rules_target_profile_name = None
        self._cancel_window_rule_capture("")
        self._remove_window_rules_btn = None

    def _window_rules_target_profile(self) -> ProfileInfo | None:
        if self.profile_manager is None:
            return self._selected_profile
        if self._window_rules_target_profile_name:
            return self.profile_manager.get_profile(self._window_rules_target_profile_name)
        return self._selected_profile

    def _on_capture_window_rules_clicked(self, _button: Gtk.Button) -> None:
        self._cancel_window_rule_capture("")
        self._window_rule_capture_pending = True
        self._window_rule_capture_generation += 1
        self._window_rule_capture_btn.set_sensitive(False)
        self._window_rule_capture_status.set_text("Activate the target window now...")
        self._window_rule_capture_timeout_id = GLib.timeout_add(
            2000,
            self._capture_window_rules_after_delay,
        )

    def _capture_window_rules_after_delay(self) -> bool:
        self._window_rule_capture_timeout_id = 0
        if not self._window_rule_capture_pending:
            return False
        self._window_rule_capture_status.set_text("Reading active window...")
        generation = self._window_rule_capture_generation
        get_active_window_async(
            lambda response, generation=generation: self._on_capture_window_rules_response(
                response,
                generation,
            ),
            timeout=5.0,
        )
        return False

    def _on_capture_window_rules_response(self, response: dict | None, generation: int) -> bool:
        if (
            not self._window_rule_capture_pending
            or generation != self._window_rule_capture_generation
        ):
            return False
        self._window_rule_capture_pending = False
        if hasattr(self, "_window_rule_capture_btn"):
            self._window_rule_capture_btn.set_sensitive(True)

        if not response or response.get("status") != "ok":
            message = (
                (response or {}).get("message") or (response or {}).get("error") or "Capture failed"
            )
            if "Unknown command: get_active_window" in message:
                message = "Please restart Keymasq Session, then try again"
            if hasattr(self, "_window_rule_capture_status"):
                self._window_rule_capture_status.set_text(message)
            return False

        rules = self._build_captured_window_rules(response)
        if not rules:
            if hasattr(self, "_window_rule_capture_status"):
                self._window_rule_capture_status.set_text("No active window details available")
            return False

        self._set_window_rule_rows(rules)
        if hasattr(self, "_window_rule_capture_status"):
            self._window_rule_capture_status.set_text(f"Captured {len(rules)} rule(s)")
        return False

    def _build_captured_window_rules(self, window_info: dict) -> list[WindowRule]:
        rules: list[WindowRule] = []

        window_class = str(window_info.get("class", "") or "").strip()
        if window_class:
            rules.append(WindowRule(field="class", pattern=re.escape(window_class)))

        window_title = str(window_info.get("title", "") or "").strip()
        if window_title:
            rules.append(WindowRule(field="title", pattern=re.escape(window_title)))

        if "window_tags" in self._compositor_capabilities:
            tags = [
                str(tag).strip().replace("*", "")
                for tag in list(window_info.get("tags", []) or [])
                if str(tag or "").strip().replace("*", "")
            ]
            if tags:
                rules.append(WindowRule(field="tag", pattern=re.escape(tags[0])))

        return rules

    def _set_window_rule_rows(self, rules: list[WindowRule]) -> None:
        if not hasattr(self, "_rules_list_box"):
            return

        for row in list(getattr(self, "_rule_rows", [])):
            self._remove_rule_row_widget(row)

        self._rule_rows = []
        if not rules:
            empty_label = self._create_empty_row()
            self._rules_list_box.append(empty_label)
            self._rule_rows.append(empty_label)
            self._update_window_rules_remove_button()
            return

        for index, rule in enumerate(rules):
            row_widget = self._create_rule_row(rule, is_first=(index == 0))
            self._rules_list_box.append(row_widget)
            self._rule_rows.append(row_widget)

        self._update_first_rule_delete_button()
        self._update_window_rules_remove_button()

    def _remove_rule_row_widget(self, row: Gtk.Widget) -> None:
        if not hasattr(self, "_rules_list_box"):
            return
        list_box_row = row.get_parent()
        if isinstance(list_box_row, Gtk.ListBoxRow):
            self._rules_list_box.remove(list_box_row)
        else:
            self._rules_list_box.remove(row)

    def _create_rule_row(self, rule: WindowRule, is_first: bool = False) -> Gtk.Box:
        _ = is_first
        is_tag = rule.field == "tag"
        type_indicator = "🏷️" if is_tag else "🌐"

        row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        row_box.add_css_class("card")
        row_box.set_margin_top(4)
        row_box.set_margin_bottom(4)
        row_box.set_margin_start(4)
        row_box.set_margin_end(4)

        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header_box.set_margin_top(8)
        header_box.set_margin_bottom(4)
        header_box.set_margin_start(12)
        header_box.set_margin_end(12)

        title_label = Gtk.Label(label=f"🪟 {rule.field}: {type_indicator} {rule.pattern}")
        title_label.set_hexpand(True)
        title_label.set_halign(Gtk.Align.START)
        header_box.append(title_label)

        delete_btn = Gtk.Button(icon_name="user-trash-symbolic")
        delete_btn.add_css_class("destructive-action")
        delete_btn.add_css_class("flat")
        delete_btn.connect("clicked", self._on_delete_rule, row_box)
        header_box.append(delete_btn)

        row_box.append(header_box)

        content_grid = Gtk.Grid()
        content_grid.set_column_spacing(12)
        content_grid.set_row_spacing(8)
        content_grid.set_margin_start(12)
        content_grid.set_margin_end(12)
        content_grid.set_margin_bottom(12)

        field_label = Gtk.Label(label="Field:")
        field_label.set_halign(Gtk.Align.START)
        content_grid.attach(field_label, 0, 0, 1, 1)

        field_dropdown = Gtk.DropDown()
        field_model = Gtk.StringList()
        field_model.append("class")
        field_model.append("title")

        has_tag_support = "window_tags" in self._compositor_capabilities
        if has_tag_support:
            field_model.append("tag")

        field_dropdown.set_model(field_model)
        field_dropdown.set_hexpand(True)
        if rule.field == "class":
            field_dropdown.set_selected(0)
        elif rule.field == "title":
            field_dropdown.set_selected(1)
        elif rule.field == "tag" and has_tag_support:
            field_dropdown.set_selected(2)
        else:
            field_dropdown.set_selected(0)
        content_grid.attach(field_dropdown, 1, 0, 1, 1)

        pattern_label = Gtk.Label(label="Pattern:")
        pattern_label.set_halign(Gtk.Align.START)
        content_grid.attach(pattern_label, 0, 1, 1, 1)

        pattern_entry = Gtk.Entry()
        pattern_entry.set_text(rule.pattern)
        if is_tag:
            pattern_entry.set_placeholder_text("e.g., game|browser|work")
        else:
            pattern_entry.set_placeholder_text("e.g., .*cs2.*")
        pattern_entry.set_hexpand(True)
        content_grid.attach(pattern_entry, 1, 1, 1, 1)
        row_box.append(content_grid)

        row_box._field_dropdown = field_dropdown
        row_box._pattern_entry = pattern_entry
        row_box._delete_btn = delete_btn
        row_box._title_label = title_label
        row_box._is_rule_row = True

        def on_field_changed(dropdown, _param) -> None:
            is_tag_field = has_tag_support and dropdown.get_selected() == 2
            if is_tag_field:
                pattern_entry.set_placeholder_text("e.g., game|browser|work")
            else:
                pattern_entry.set_placeholder_text("e.g., .*cs2.*")
            self._update_rule_row_title(row_box)

        field_dropdown.connect("notify::selected", on_field_changed)
        pattern_entry.connect("changed", self._on_rule_pattern_changed, row_box)
        self._update_rule_row_title(row_box)
        return row_box

    def _on_close_dialog_clicked(self, _button: Gtk.Button, dialog: Adw.Dialog) -> None:
        dialog.close()

    def _on_close_current_rules_dialog_clicked(self, _button: Gtk.Button) -> None:
        self._current_rules_dialog.close()

    def _on_rule_pattern_changed(self, _entry: Gtk.Entry, row_box: Gtk.Box) -> None:
        self._update_rule_row_title(row_box)

    def _update_rule_row_title(self, row: Gtk.Box) -> None:
        if not hasattr(row, "_title_label") or not hasattr(row, "_field_dropdown"):
            return

        field_idx = row._field_dropdown.get_selected()
        if field_idx == 0:
            field = "class"
        elif field_idx == 1:
            field = "title"
        elif "window_tags" in self._compositor_capabilities and field_idx == 2:
            field = "tag"
        else:
            field = "class"

        pattern = row._pattern_entry.get_text().strip() if hasattr(row, "_pattern_entry") else ""
        type_indicator = "🏷️" if field == "tag" else "🌐"
        row._title_label.set_label(f"🪟 {field}: {type_indicator} {pattern or '...'}")

    def _create_empty_row(self) -> Gtk.Label:
        label = Gtk.Label(label="No window rules configured")
        label.add_css_class("dim-label")
        label.set_margin_top(12)
        label.set_margin_bottom(12)
        return label

    def _on_add_window_rule(self, _button: Gtk.Button) -> None:
        if not hasattr(self, "_rules_list_box"):
            return

        for row in list(self._rule_rows):
            if isinstance(row, Gtk.Label) and "No window rules" in (row.get_text() or ""):
                self._rules_list_box.remove(row)
                self._rule_rows.remove(row)
                break

        new_row = self._create_rule_row(WindowRule(field="class", pattern=".*"), is_first=False)
        self._rules_list_box.append(new_row)
        self._rule_rows.append(new_row)
        self._update_first_rule_delete_button()
        self._update_window_rules_remove_button()

    def _on_remove_window_rules_clicked(self, _button: Gtk.Button) -> None:
        if not hasattr(self, "_rules_list_box"):
            return

        for row in list(self._rule_rows):
            self._remove_rule_row_widget(row)
        self._rule_rows = []
        empty_label = self._create_empty_row()
        self._rules_list_box.append(empty_label)
        self._rule_rows.append(empty_label)
        self._update_window_rules_remove_button()
        self._on_apply_window_rules(_button)

    def _on_delete_rule(self, _button: Gtk.Button, row: Gtk.Box) -> None:
        if not hasattr(self, "_rules_list_box"):
            return
        if row in self._rule_rows:
            self._rule_rows.remove(row)
        self._remove_rule_row_widget(row)

        rule_rows = [item for item in self._rule_rows if hasattr(item, "_is_rule_row")]
        if not rule_rows:
            empty_label = self._create_empty_row()
            self._rules_list_box.append(empty_label)
            self._rule_rows.append(empty_label)
        else:
            self._update_first_rule_delete_button()
        self._update_window_rules_remove_button()

    def _update_first_rule_delete_button(self) -> None:
        rule_rows = [row for row in self._rule_rows if hasattr(row, "_is_rule_row")]
        show_delete = len(rule_rows) > 1
        for row in rule_rows:
            if hasattr(row, "_delete_btn"):
                row._delete_btn.set_visible(show_delete)

    def _update_window_rules_remove_button(self) -> None:
        if not hasattr(self, "_remove_window_rules_btn") or self._remove_window_rules_btn is None:
            return
        has_rules = any(hasattr(row, "_is_rule_row") for row in getattr(self, "_rule_rows", []))
        self._remove_window_rules_btn.set_sensitive(has_rules)

    def _on_apply_window_rules(self, _button: Gtk.Button) -> None:
        target_profile = self._window_rules_target_profile()
        if not target_profile or self.profile_manager is None:
            if hasattr(self, "_current_rules_dialog"):
                self._current_rules_dialog.close()
            return

        new_rules = []
        has_tag_support = "window_tags" in self._compositor_capabilities
        for row in self._rule_rows:
            if not hasattr(row, "_is_rule_row") or not hasattr(row, "_field_dropdown"):
                continue
            field_idx = row._field_dropdown.get_selected()
            if field_idx == 0:
                field = "class"
            elif field_idx == 1:
                field = "title"
            elif has_tag_support and field_idx == 2:
                field = "tag"
            else:
                field = "class"

            pattern = row._pattern_entry.get_text().strip()
            if pattern:
                new_rules.append(WindowRule(field=field, pattern=pattern))

        try:
            self.profile_manager.validate_window_rules(new_rules)
        except ValueError as exc:
            self._show_profile_error_dialog(str(exc))
            return

        target_profile.config.window_rules = new_rules
        target_profile.config.is_permanent = not new_rules
        if not self._save_specific_profile(target_profile):
            return
        if (
            self._selected_profile
            and self._selected_profile.config.name == target_profile.config.name
        ):
            self._update_rules_label()
            self._update_profile_state_display()

        if hasattr(self, "_current_rules_dialog"):
            self._cancel_window_rule_capture("")
            self._current_rules_dialog.close()

    def _cancel_window_rule_capture(self, status_text: str) -> None:
        if self._window_rule_capture_timeout_id:
            GLib.source_remove(self._window_rule_capture_timeout_id)
            self._window_rule_capture_timeout_id = 0
        self._window_rule_capture_pending = False
        if hasattr(self, "_window_rule_capture_btn"):
            self._window_rule_capture_btn.set_sensitive(True)
        if hasattr(self, "_window_rule_capture_status"):
            self._window_rule_capture_status.set_text(status_text)

    def _update_rules_label(self) -> None:
        if not self._selected_profile:
            self.rules_list_label.set_text("No profile selected")
            return

        rules = self._selected_profile.config.window_rules
        if not rules:
            self.rules_list_label.set_text("No rules - always active")
            return

        parts = [f"{rule.field}={rule.pattern}" for rule in rules[:2]]
        if len(rules) > 2:
            parts.append(f"... (+{len(rules) - 2})")
        self.rules_list_label.set_text(f"{', '.join(parts)} - conditional")

    def _setup_profile_dropdown(self) -> None:
        current_selected = (
            self.profile_dropdown.get_selected() if hasattr(self, "profile_dropdown") else 0
        )
        strings = Gtk.StringList()
        strings.append("Passthrough")
        self._profile_names = ["__passthrough__"]
        self._profile_items = [None]

        for profile in self.profiles:
            config = profile.config
            if self._has_unsupported_rules(config):
                state_icon = "❗"
            elif config.name in self._active_profile_names:
                state_icon = "🟢"
            elif not config.enabled:
                state_icon = "🔴"
            elif config.is_permanent:
                state_icon = "⚪"
            elif config.window_rules:
                state_icon = "🟡"
            else:
                state_icon = "⚪"

            type_icon = PROFILE_TYPE_ICONS.get(
                "permanent" if config.is_permanent else "conditional",
                "",
            )
            strings.append(f"{state_icon} {type_icon} {config.name}".strip())
            self._profile_names.append(config.name)
            self._profile_items.append(profile)

        self._suspend_profile_signal = True
        try:
            self.profile_dropdown.set_model(strings)
            if len(self._profile_names) > 1:
                if current_selected <= 0 or current_selected >= len(self._profile_names):
                    self.profile_dropdown.set_selected(1)
                else:
                    self.profile_dropdown.set_selected(current_selected)
            else:
                self.profile_dropdown.set_selected(0)
        finally:
            self._suspend_profile_signal = False

    def _current_selected_name(self) -> str | None:
        if self._selected_profile:
            return self._selected_profile.config.name
        if hasattr(self, "profile_dropdown"):
            selected = self.profile_dropdown.get_selected()
            if 0 <= selected < len(self._profile_names):
                name = self._profile_names[selected]
                if name != "__passthrough__":
                    return name
        return None

    def _apply_profile_selection(self, publish_selection: bool = True) -> None:
        selected = self.profile_dropdown.get_selected()
        if selected < 0 or selected >= len(self._profile_names):
            selected = 0
            self._suspend_profile_signal = True
            try:
                self.profile_dropdown.set_selected(0)
            finally:
                self._suspend_profile_signal = False

        profile_name = self._profile_names[selected]
        self._selected_profile = None
        if profile_name != "__passthrough__" and selected < len(self._profile_items):
            self._selected_profile = self._profile_items[selected]

        if self._selected_profile is None:
            self.status_label.set_text("")
            self.enabled_check.set_sensitive(False)
            self.enabled_check.set_active(False)
            self.settings_frame.set_sensitive(False)
        else:
            self._update_profile_state_display()
            self.enabled_check.set_sensitive(True)
            self.enabled_check.handler_block_by_func(self._on_enabled_toggled)
            self.enabled_check.set_active(self._selected_profile.config.enabled)
            self.enabled_check.handler_unblock_by_func(self._on_enabled_toggled)
            self.settings_frame.set_sensitive(True)
            self._update_profile_settings()

        self._after_profile_selection_applied()
        if publish_selection:
            self._publish_profile_selection()

    def _update_active_profiles_summary(self) -> None:
        if not hasattr(self, "active_profiles_label"):
            return

        if not self._active_profile_names:
            self.active_profiles_label.set_text("None")
            self.active_profiles_label.set_tooltip_text(self._active_profiles_empty_tooltip())
            return

        visible_names = self._active_profile_names[:3]
        summary = ", ".join(visible_names)
        if len(self._active_profile_names) > len(visible_names):
            summary += f", +{len(self._active_profile_names) - len(visible_names)}"
        self.active_profiles_label.set_text(summary)
        self.active_profiles_label.set_tooltip_text(self._active_profiles_layer_tooltip())

    def refresh_profiles(
        self,
        preferred_profile_name: str | None = None,
        publish_selection: bool = True,
    ) -> None:
        if not self.profile_manager:
            return

        selected_name = (
            preferred_profile_name
            or self._window_selected_profile_name()
            or self._current_selected_name()
        )
        self.profiles = self.profile_manager.list_profiles()
        self._setup_profile_dropdown()

        if selected_name:
            self._suspend_profile_signal = True
            try:
                for index, name in enumerate(self._profile_names):
                    if name == selected_name:
                        self.profile_dropdown.set_selected(index)
                        break
            finally:
                self._suspend_profile_signal = False

        self._apply_profile_selection(publish_selection=publish_selection)

    def _on_profile_selected(self, _dropdown, _param) -> None:
        if self._suspend_profile_signal:
            return
        self._apply_profile_selection()

    def _update_profile_state_display(self) -> None:
        if not self._selected_profile:
            self.status_label.set_text("")
            return

        config = self._selected_profile.config
        for css_class in ("status-active", "status-waiting", "status-inactive", "status-standby"):
            self.status_label.remove_css_class(css_class)

        if self._has_unsupported_rules(config):
            self.status_label.set_text("unsupported rules")
            self.status_label.add_css_class("status-inactive")
            return

        if config.name in self._active_profile_names:
            state = ProfileState.ACTIVE
        elif not config.enabled:
            state = ProfileState.INACTIVE
        elif config.is_permanent:
            state = ProfileState.STANDBY
        elif config.window_rules:
            state = ProfileState.WAITING
        else:
            state = ProfileState.INACTIVE

        self.status_label.add_css_class(
            {
                ProfileState.ACTIVE: "status-active",
                ProfileState.WAITING: "status-waiting",
                ProfileState.INACTIVE: "status-inactive",
                ProfileState.STANDBY: "status-standby",
            }.get(state, "status-standby")
        )
        self.status_label.set_text(state.value)

    def _update_profile_settings(self) -> None:
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

    def _on_new_profile(self, _button: Gtk.Button) -> None:
        if self.demo_mode or self.profile_manager is None:
            return
        dialog = ProfileCreateDialog(self.get_root(), self.profile_manager)
        dialog.connect("profile-created", self._on_profile_created)
        dialog.present()

    def _on_profile_created(self, _dialog, profile_name: str) -> None:
        root = self.main_window or self.get_root()
        if root and hasattr(root, "_set_selected_profile_name"):
            root._set_selected_profile_name(profile_name)
        if self.profile_manager is not None:
            self.profile_manager.reload()
        self.refresh_profiles(preferred_profile_name=profile_name)
        self._on_profile_settings_clicked(self.settings_btn)
        notify_session_reload_async()

    def _save_specific_profile(self, profile: ProfileInfo | None) -> bool:
        if profile is None or self.profile_manager is None or self.demo_mode:
            return True
        try:
            self.profile_manager.save_profile(profile.config)
        except ValueError as exc:
            self._show_profile_error_dialog(str(exc))
            return False
        session_request_async(
            {"command": "reevaluate_profiles"},
            self._on_profile_reevaluate_finished,
        )
        return True

    def _save_profile(self) -> bool:
        return self._save_specific_profile(self._selected_profile)

    def _on_profile_reevaluate_finished(self, result: dict | None) -> bool:
        if not isinstance(result, dict) or result.get("status") != "ok":
            notify_session_reload()
        return False

    def _on_enabled_toggled(self, check: Gtk.CheckButton) -> None:
        if not self._selected_profile or self.demo_mode:
            return
        self._selected_profile.config.enabled = check.get_active()
        if self._save_profile():
            self._update_profile_state_display()

    def apply_active_profile_response(self, data: dict | None) -> None:
        active_profiles = self._active_profile_names_from_response(data or {})
        if active_profiles != self._active_profile_names:
            self._active_profile_names = active_profiles
            self._refresh_profile_dropdown_states()
            self._update_active_profiles_summary()
        self._update_profile_state_display()
        self._after_active_profiles_changed()

    def _on_active_profile_response(self, data: dict | None) -> bool:
        self.apply_active_profile_response(data)

        return False

    def _refresh_profile_dropdown_states(self) -> None:
        current_selected = self.profile_dropdown.get_selected()
        strings = Gtk.StringList()
        strings.append("Passthrough")

        for profile in self.profiles:
            config = profile.config
            if self._has_unsupported_rules(config):
                state_icon = "❗"
            elif config.name in self._active_profile_names:
                state_icon = "🟢"
            elif not config.enabled:
                state_icon = "🔴"
            elif config.is_permanent:
                state_icon = "⚪"
            elif config.window_rules:
                state_icon = "🟡"
            else:
                state_icon = "⚪"
            type_icon = PROFILE_TYPE_ICONS.get(
                "permanent" if config.is_permanent else "conditional",
                "",
            )
            strings.append(f"{state_icon} {type_icon} {config.name}".strip())

        self._suspend_profile_signal = True
        try:
            self.profile_dropdown.set_model(strings)
            self.profile_dropdown.set_selected(current_selected)
        finally:
            self._suspend_profile_signal = False

    def _show_profile_error_dialog(self, message: str) -> None:
        dialog = Adw.AlertDialog(
            heading="Invalid Profile Configuration",
            body=message,
        )
        dialog.add_response("ok", "OK")
        dialog.present(self.get_root())
