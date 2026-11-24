from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.widgets.device_tab.preset_dialog import (
    ApplyPresetDialog,
    preset_applied_feedback,
)
from keymasq.session.presets import PresetPlan, ProfilePreset


class PresetMixin:
    def _append_profile_toolbar_buttons(self: Any, btn_group: Gtk.Box) -> None:
        self.apply_preset_btn = Gtk.Button(icon_name="view-list-symbolic")
        self.apply_preset_btn.set_tooltip_text("Apply preset")
        self.apply_preset_btn.connect("clicked", self._on_apply_preset_clicked)
        btn_group.append(self.apply_preset_btn)
        self._update_apply_preset_button_state()

    def _can_apply_preset(self: Any) -> bool:
        return (
            not self.demo_mode
            and self.profile_manager is not None
            and self._selected_profile is not None
            and getattr(self, "device", None) is not None
        )

    def _update_apply_preset_button_state(self: Any) -> None:
        button = getattr(self, "apply_preset_btn", None)
        if button is None:
            return
        button.set_sensitive(self._can_apply_preset())

    def _on_apply_preset_clicked(self: Any, _button: Gtk.Button) -> None:
        if not self._can_apply_preset():
            return
        if getattr(self, "_apply_preset_dialog", None) is not None:
            return

        selected_profile = self._selected_profile
        assert selected_profile is not None
        assert self.profile_manager is not None
        profile_name = selected_profile.config.name
        dialog = ApplyPresetDialog(
            self.device,
            profile_name,
            self._selected_layer(),
            profile_manager=self.profile_manager,
            on_applied=lambda preset, plan: self._on_preset_applied(profile_name, preset, plan),
        )
        self._apply_preset_dialog = dialog
        dialog.connect("closed", self._on_apply_preset_dialog_closed)
        dialog.present(self.get_root())

    def _on_apply_preset_dialog_closed(self: Any, dialog: Adw.Dialog) -> None:
        if getattr(self, "_apply_preset_dialog", None) is dialog:
            self._apply_preset_dialog = None

    def _on_preset_applied(
        self: Any,
        profile_name: str,
        preset: ProfilePreset,
        plan: PresetPlan,
    ) -> None:
        if getattr(self, "_device_tab_destroyed", False):
            return
        self.refresh_profiles(preferred_profile_name=profile_name, publish_selection=False)
        self._refresh_other_profile_tabs(preferred_profile_name=profile_name)
        self._notify_session_reload_async()
        self._show_preset_feedback(preset_applied_feedback(preset.name, plan))

    def _show_preset_feedback(self: Any, message: str) -> None:
        root = self.main_window or self.get_root()
        show_toast = getattr(root, "show_toast", None)
        if callable(show_toast):
            show_toast(message)
