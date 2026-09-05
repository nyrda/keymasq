"""Profile rename, copy, and deletion workflows."""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.profiles import ProfileConfig
from keymasq.gui.widgets.profile_tab.state import next_copy_name
from keymasq.gui.wizards.profile_create import ProfileCreateDialog
from keymasq.session.profile.types import ProfileInfo


class ProfileRepositoryMixin:
    """Coordinate profile repository mutations with session refreshes."""

    def _on_new_profile(self: Any, _button: Gtk.Button) -> None:
        if self.demo_mode or self.profile_manager is None:
            return
        dialog = ProfileCreateDialog(self.get_root(), self.profile_manager)
        dialog.connect("profile-created", self._on_profile_created)
        dialog.present()

    def _on_profile_created(self: Any, _dialog: object, profile_name: str) -> None:
        from keymasq.gui.window.profiles import _set_selected_profile_name

        root = self.main_window or self.get_root()
        if root and hasattr(root, "_selected_profile_name"):
            _set_selected_profile_name(root, profile_name)
        if self.profile_manager is not None:
            self.profile_manager.reload()
        self.refresh_profiles(preferred_profile_name=profile_name)
        self._on_profile_settings_clicked(self.settings_btn)
        self._notify_session_reload_async()

    def _save_specific_profile(self: Any, profile: ProfileInfo | None) -> bool:
        if profile is None or self.profile_manager is None or self.demo_mode:
            return True
        try:
            self.profile_manager.save_profile(profile.config)
        except ValueError as exc:
            self._show_profile_error_dialog(str(exc))
            return False
        self._request_session_async(
            {"command": "reevaluate_profiles"},
            self._on_profile_reevaluate_finished,
        )
        return True

    def _save_profile(self: Any) -> bool:
        return self._save_specific_profile(self._selected_profile)

    def _on_profile_reevaluate_finished(self: Any, result: dict | None) -> bool:
        if not isinstance(result, dict) or result.get("status") != "ok":
            self._notify_session_reload_async()
        return False

    def _on_enabled_toggled(self: Any, check: Gtk.CheckButton) -> None:
        if not self._selected_profile or self.demo_mode:
            return
        self._selected_profile.config.enabled = check.get_active()
        if self._save_profile():
            self._update_profile_state_display()

    def _on_name_changed(self: Any, entry: Gtk.Entry) -> None:
        if not self._selected_profile or self.profile_manager is None:
            return

        new_name = entry.get_text().strip()
        if not new_name or new_name == self._selected_profile.config.name:
            return

        old_profile = self._selected_profile
        old_name = old_profile.config.name
        try:
            renamed = self.profile_manager.rename_profile(old_name, new_name)
        except ValueError as exc:
            self._show_profile_error_dialog(str(exc))
            return

        self._selected_profile = renamed
        for index, profile in enumerate(self.profiles):
            if profile is old_profile or profile.config.name == old_name:
                self.profiles[index] = renamed
                break
        if old_name in self._profile_names:
            profile_index = self._profile_names.index(old_name)
            self._profile_names[profile_index] = new_name
            if profile_index < len(self._profile_items):
                self._profile_items[profile_index] = renamed

        self._refresh_profile_dropdown_states()
        self._refresh_other_profile_tabs(preferred_profile_name=new_name)
        self._notify_session_reload_async()

    def _build_profile_copy_config(self: Any) -> ProfileConfig | None:
        if not self._selected_profile or self.profile_manager is None:
            return None

        new_name = next_copy_name(
            self._selected_profile.config.name,
            {profile.config.name for profile in self.profiles},
        )
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

    def _on_copy_profile(self: Any, _button: Gtk.Button) -> None:
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
        self._notify_session_reload_async()

    def _on_delete_profile(
        self: Any,
        _button: Gtk.Button,
        close_after_delete: Adw.Dialog | None = None,
    ) -> None:
        if not self._selected_profile or self.demo_mode:
            return
        if not self._can_delete_selected_profile():
            self._show_last_profile_error()
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
        self: Any,
        _button: Gtk.Button,
        dialog: Adw.Dialog,
        close_after_delete: Adw.Dialog | None = None,
    ) -> None:
        if not self._selected_profile or self.profile_manager is None:
            dialog.close()
            return
        if not self._can_delete_selected_profile():
            dialog.close()
            self._show_last_profile_error()
            return

        self.profile_manager.delete_profile(self._selected_profile.config.name)
        self._refresh_other_profile_tabs()
        self.refresh_profiles(publish_selection=False)
        dialog.close()
        if close_after_delete is not None:
            close_after_delete.close()
        self._notify_session_reload_async()

    def _can_delete_selected_profile(self: Any) -> bool:
        if self.profile_manager is None:
            return False
        return len(self.profile_manager.list_profiles()) > 1

    def _show_last_profile_error(self: Any) -> None:
        self._show_profile_error_dialog(
            "At least one profile is required. Create another profile before deleting this one."
        )

    def _show_profile_error_dialog(self: Any, message: str) -> None:
        dialog = Adw.AlertDialog(
            heading="Invalid Profile Configuration",
            body=message,
        )
        dialog.add_response("ok", "OK")
        dialog.present(self.get_root())
