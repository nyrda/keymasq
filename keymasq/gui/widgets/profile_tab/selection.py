"""Profile selector model population and selection coordination."""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.widgets.profile_tab.state import profile_state_icon, profile_type_icon


class ProfileSelectionMixin:
    """Own only dropdown population, refresh, and selection application."""

    def _setup_profile_dropdown(self: Any) -> None:
        current_selected = (
            self.profile_dropdown.get_selected() if hasattr(self, "profile_dropdown") else 0
        )
        strings = Gtk.StringList()
        strings.append("Passthrough")
        self._profile_names = ["__passthrough__"]
        self._profile_items = [None]

        for profile in self.profiles:
            config = profile.config
            state_icon = profile_state_icon(
                config,
                tuple(self._active_profile_names),
                unsupported_rules=self._has_unsupported_rules(config),
            )
            strings.append(f"{state_icon} {profile_type_icon(config)} {config.name}".strip())
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

    def _current_selected_name(self: Any) -> str | None:
        if self._selected_profile:
            return self._selected_profile.config.name
        if hasattr(self, "profile_dropdown"):
            selected = self.profile_dropdown.get_selected()
            if 0 <= selected < len(self._profile_names):
                name = self._profile_names[selected]
                if name != "__passthrough__":
                    return name
        return None

    def _apply_profile_selection(self: Any, publish_selection: bool = True) -> None:
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

    def refresh_profiles(
        self: Any,
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

    def _on_profile_selected(self: Any, _dropdown: object, _param: object) -> None:
        if self._suspend_profile_signal:
            return
        self._apply_profile_selection()
