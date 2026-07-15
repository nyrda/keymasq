"""Device rename and release-before-delete inventory workflow."""

import logging
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.session_client import JsonDict
from keymasq.gui.widgets.device_tab import rename_dialogs

log = logging.getLogger(__name__)


class InventoryMixin:
    def _on_device_name_right_clicked(self: Any, click, n_press, x, y) -> None:
        if n_press != 1 or self.demo_mode or self.hardware_manager is None:
            return
        self._show_device_rename_dialog()

    def _show_device_rename_dialog(
        self: Any,
        on_saved: Callable[[], None] | None = None,
    ) -> None:
        if self.hardware_manager is None:
            return

        def save(new_name: str) -> bool:
            saved = self._rename_device(new_name)
            if saved and on_saved is not None:
                on_saved()
            return saved

        rename_dialogs.present_device_rename_dialog(
            parent=self.get_root(),
            current_name=self.device.name,
            on_save=save,
            on_close_clicked=self._on_close_dialog_clicked,
        )

    def _rename_device(self: Any, new_name: str) -> bool:
        new_name = new_name.strip()
        if not new_name:
            return False
        if new_name == self.device.name:
            return True
        if self.hardware_manager is None:
            log.warning(
                "Cannot rename device %s without a hardware manager",
                self.device.hardware_id,
            )
            return False

        self.device.name = new_name
        self.hardware_manager.save_hardware(self.device)
        self._request_session_async({"command": "reload"}, self._ignore_session_response)
        self._update_device_name_display()
        self._notify_device_renamed()
        return True

    def _update_device_name_display(self: Any) -> None:
        self.device_name_label.set_text(self.device.name)
        if hasattr(self, "always_grab_check"):
            self.always_grab_check.set_title(self._device_grab_label_text())
        self._sync_always_grab_device_list()

    def _notify_device_renamed(self: Any) -> None:
        target = self.main_window or self.get_root()
        updater = getattr(target, "update_device_display_name", None)
        if callable(updater):
            updater(self.device.hardware_id, self.device.name)

    def _on_delete_device(self: Any, _button: Gtk.Button) -> None:
        if self.hardware_manager is not None:
            self.present_delete_device_dialog()

    def present_delete_device_dialog(self: Any) -> None:
        rename_dialogs.present_delete_device_dialog(
            parent=self.get_root(),
            device_name=self.device.name,
            can_delete=self.hardware_manager is not None,
            can_delete_profiles=self.profile_manager is not None,
            on_confirm_clicked=self._on_confirm_delete_device,
            on_close_clicked=self._on_close_dialog_clicked,
        )

    def _on_confirm_delete_device(
        self: Any,
        button: Gtk.Button,
        dialog: Adw.Dialog,
        delete_profiles_check: Gtk.CheckButton,
        error_label: Gtk.Label | None = None,
    ) -> None:
        delete_profiles = delete_profiles_check.get_active()
        hardware_id = self.device.hardware_id
        if self.hardware_manager is None or (delete_profiles and self.profile_manager is None):
            if error_label is not None:
                error_label.set_label("Action unavailable: missing manager.")
                error_label.set_visible(True)
            return

        button.set_sensitive(False)
        if error_label is not None:
            error_label.set_visible(False)

        def on_released(result: JsonDict | None) -> bool:
            return self._on_delete_device_release_response(
                result,
                button,
                hardware_id,
                delete_profiles,
                dialog,
                error_label,
            )

        self._request_session_async(
            {
                "command": "release_device",
                "hardware_id": hardware_id,
                "immediate": True,
            },
            on_released,
        )

    def _on_delete_device_release_response(
        self: Any,
        result: JsonDict | None,
        button: Gtk.Button,
        hardware_id: str,
        delete_profiles: bool,
        dialog: Adw.Dialog,
        error_label: Gtk.Label | None,
    ) -> bool:
        if isinstance(result, dict) and result.get("status") == "ok":
            return self._delete_device_after_release(hardware_id, delete_profiles, dialog)

        message = "No response from keymasq-session"
        if isinstance(result, dict):
            message = str(result.get("error") or result.get("message") or "release failed")
        log.warning("Failed to release device %s before delete: %s", hardware_id, message)
        button.set_sensitive(True)
        if error_label is not None:
            error_label.set_label(f"Failed to release device: {message}")
            error_label.set_visible(True)
        return False

    def _delete_device_after_release(
        self: Any,
        hardware_id: str,
        delete_profiles: bool,
        dialog: Adw.Dialog,
    ) -> bool:
        hardware_manager = self.hardware_manager
        profile_manager = self.profile_manager
        if hardware_manager is None or (delete_profiles and profile_manager is None):
            log.warning("Cannot delete device %s without required managers", hardware_id)
            return False
        if delete_profiles and profile_manager is not None:
            profile_manager.remove_device_layers(hardware_id)

        hardware_manager.delete_hardware(hardware_id)
        self._request_session_async({"command": "reload"}, self._ignore_session_response)
        dialog.close()
        settings_dialog = self._hardware_settings_dialog
        if settings_dialog is not None:
            settings_dialog.close()
            self._hardware_settings_dialog = None

        root = self.main_window or self.get_root()
        remove_device_tab = getattr(root, "remove_device_tab", None)
        if callable(remove_device_tab):
            remove_device_tab(hardware_id)
        elif root and hasattr(root, "stack"):
            root.stack.remove(self)
            root._check_empty_state()
        return False
