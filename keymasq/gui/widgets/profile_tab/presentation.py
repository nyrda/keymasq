"""Active-profile and selected-profile presentation."""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.widgets.profile_tab.state import (
    ActiveProfiles,
    profile_state,
    profile_state_icon,
    profile_type_icon,
)


class ProfilePresentationMixin:
    """Render profile state without owning selection or persistence workflows."""

    def _update_active_profiles_summary(self: Any) -> None:
        if not hasattr(self, "active_profiles_label"):
            return

        active_profiles = ActiveProfiles(tuple(self._active_profile_names))
        self.active_profiles_label.set_text(active_profiles.summary())
        if not active_profiles.names:
            self.active_profiles_label.set_tooltip_text(self._active_profiles_empty_tooltip())
            return
        self.active_profiles_label.set_tooltip_text(self._active_profiles_layer_tooltip())

    def _update_profile_state_display(self: Any) -> None:
        if not self._selected_profile:
            self.status_label.set_text("")
            return

        config = self._selected_profile.config
        for css_class in (
            "status-active",
            "status-waiting",
            "status-inactive",
            "status-standby",
        ):
            self.status_label.remove_css_class(css_class)

        if self._has_unsupported_rules(config):
            self.status_label.set_text("unsupported rules")
            self.status_label.add_css_class("status-inactive")
            return

        state = profile_state(config, tuple(self._active_profile_names))
        self.status_label.add_css_class(
            {
                "active": "status-active",
                "waiting": "status-waiting",
                "inactive": "status-inactive",
                "standby": "status-standby",
            }.get(state.value, "status-standby")
        )
        self.status_label.set_text(state.value)

    def apply_active_profile_response(self: Any, data: dict | None) -> None:
        active_profiles = self._active_profile_names_from_response(data or {})
        if active_profiles != self._active_profile_names:
            self._active_profile_names = active_profiles
            self._refresh_profile_dropdown_states()
            self._update_active_profiles_summary()
        self._update_profile_state_display()
        self._after_active_profiles_changed()

    def _on_active_profile_response(self: Any, data: dict | None) -> bool:
        self.apply_active_profile_response(data)
        return False

    def _refresh_profile_dropdown_states(self: Any) -> None:
        current_selected = self.profile_dropdown.get_selected()
        strings = Gtk.StringList()
        strings.append("Passthrough")

        for profile in self.profiles:
            config = profile.config
            state_icon = profile_state_icon(
                config,
                tuple(self._active_profile_names),
                unsupported_rules=self._has_unsupported_rules(config),
            )
            strings.append(f"{state_icon} {profile_type_icon(config)} {config.name}".strip())

        self._suspend_profile_signal = True
        try:
            self.profile_dropdown.set_model(strings)
            self.profile_dropdown.set_selected(current_selected)
        finally:
            self._suspend_profile_signal = False
