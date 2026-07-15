"""Profile lifecycle-macro loading, selection, and event coordination."""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.widgets.profile_tab.state import LifecycleMacroOptions


class LifecycleMacroMixin:
    """Maintain lifecycle macro choices independently of profile selection."""

    def _on_lifecycle_macros_loaded(self: Any, result: dict | None) -> bool:
        self._profile_lifecycle_macro_names = list(
            LifecycleMacroOptions.from_payload(result).available
        )
        self._refresh_lifecycle_macro_dropdowns()
        return False

    def _load_lifecycle_macros(self: Any) -> None:
        if self.profile_manager is None or self.demo_mode:
            return
        self._request_session_async(
            {"command": "list_macros"},
            self._on_lifecycle_macros_loaded,
        )

    def _on_macro_list_changed(self: Any, _event: dict) -> None:
        self._load_lifecycle_macros()

    def _on_profile_managed_destroy(self: Any, _widget: object) -> None:
        if not self._registered_macro_event_handlers:
            return
        if self.main_window is not None and hasattr(self.main_window, "unregister_event_handler"):
            self.main_window.unregister_event_handler("macro_saved", self._on_macro_list_changed)
            self.main_window.unregister_event_handler("macro_deleted", self._on_macro_list_changed)
        self._registered_macro_event_handlers = False

    def _refresh_lifecycle_macro_dropdowns(self: Any) -> None:
        selected_names: list[str] = []
        if self._selected_profile:
            selected_names = [
                self._selected_profile.config.activation_macro_name or "",
                self._selected_profile.config.deactivation_macro_name or "",
            ]
        state = LifecycleMacroOptions(tuple(self._profile_lifecycle_macro_names))
        options = state.choices(*selected_names)
        self._profile_lifecycle_macro_options = list(options)

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

    def _select_lifecycle_macro(self: Any, dropdown: Gtk.DropDown, macro_name: str | None) -> None:
        dropdown.set_selected(
            LifecycleMacroOptions.index(
                tuple(self._profile_lifecycle_macro_options),
                macro_name,
            )
        )

    def _lifecycle_macro_name_for_dropdown(
        self: Any,
        dropdown: Gtk.DropDown,
    ) -> str | None:
        return LifecycleMacroOptions.selected_name(
            tuple(self._profile_lifecycle_macro_options),
            dropdown.get_selected(),
        )

    def _on_activation_macro_changed(
        self: Any,
        dropdown: Gtk.DropDown,
        _param: object,
    ) -> None:
        if self._suspend_lifecycle_macro_signal or not self._selected_profile:
            return
        self._selected_profile.config.activation_macro_name = (
            self._lifecycle_macro_name_for_dropdown(dropdown)
        )
        self._save_profile()

    def _on_deactivation_macro_changed(
        self: Any,
        dropdown: Gtk.DropDown,
        _param: object,
    ) -> None:
        if self._suspend_lifecycle_macro_signal or not self._selected_profile:
            return
        self._selected_profile.config.deactivation_macro_name = (
            self._lifecycle_macro_name_for_dropdown(dropdown)
        )
        self._save_profile()
