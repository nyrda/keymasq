from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk, Pango  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.profiles import ProfileConfig
from keymasq.gui.session_client import session_request_async
from keymasq.gui.session_reload import notify_session_reload, notify_session_reload_async
from keymasq.gui.widgets.profile_tab.lifecycle import LifecycleMacroMixin
from keymasq.gui.widgets.profile_tab.presentation import ProfilePresentationMixin
from keymasq.gui.widgets.profile_tab.repository import ProfileRepositoryMixin
from keymasq.gui.widgets.profile_tab.rules import WindowRulesMixin
from keymasq.gui.widgets.profile_tab.selection import ProfileSelectionMixin
from keymasq.gui.widgets.profile_tab.settings import ProfileSettingsMixin
from keymasq.gui.widgets.profile_tab.state import ActiveProfiles
from keymasq.session.profile.manager import ProfileManager
from keymasq.session.profile.types import ProfileInfo


class ProfileManagedTab(
    WindowRulesMixin,
    ProfileSettingsMixin,
    ProfileRepositoryMixin,
    LifecycleMacroMixin,
    ProfilePresentationMixin,
    ProfileSelectionMixin,
    Gtk.Box,
):
    """Compose profile selection, settings, rules, persistence, and lifecycle controllers."""

    def __init__(
        self,
        profile_manager: ProfileManager | None,
        main_window=None,
        demo_mode: bool = False,
        compositor_capabilities: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.set_orientation(Gtk.Orientation.VERTICAL)
        self.set_spacing(12)
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
        from keymasq.gui.window.profiles import selected_profile_name

        if self.main_window is not None:
            return selected_profile_name(self.main_window)
        root = self.get_root()
        if root and hasattr(root, "_selected_profile_name"):
            return selected_profile_name(root)
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

        self.settings_btn = Gtk.Button(icon_name="emblem-system-symbolic")
        self.settings_btn.set_tooltip_text("Profile settings")
        self.settings_btn.connect("clicked", self._on_profile_settings_clicked)
        btn_group.append(self.settings_btn)
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

        self.active_profiles_title_label = Gtk.Label(label=self._active_profiles_summary_title())
        self.active_profiles_title_label.add_css_class("caption")
        self.active_profiles_title_label.add_css_class("dim-label")
        active_profile_box.append(self.active_profiles_title_label)

        self.active_profiles_label = Gtk.Label(label="None")
        self.active_profiles_label.add_css_class("caption")
        self.active_profiles_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.active_profiles_label.set_hexpand(True)
        self.active_profiles_label.set_halign(Gtk.Align.START)
        active_profile_box.append(self.active_profiles_label)

        self.append(active_profile_box)
        self._update_active_profiles_summary()
        self._setup_profile_settings()

    def _append_profile_settings_groups(self, container: Gtk.Box) -> None:
        _ = container

    def _selected_layer(self, create: bool = False) -> object | None:
        _ = create
        return None

    def _has_unsupported_rules(self, config: ProfileConfig) -> bool:
        has_tag_support = "window_tags" in self._compositor_capabilities
        return any(rule.field == "tag" and not has_tag_support for rule in config.window_rules)

    def _active_profile_names_from_response(self, data: dict) -> list[str]:
        return list(ActiveProfiles.from_payload(data).names)

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
        return ActiveProfiles(tuple(self._active_profile_names)).layer_tooltip()

    def _after_profile_selection_applied(self) -> None:
        return

    def _after_active_profiles_changed(self) -> None:
        return

    def _update_extra_profile_settings(self) -> None:
        return

    def _refresh_other_profile_tabs(self, preferred_profile_name: str | None = None) -> None:
        from keymasq.gui.window.device_tabs import _refresh_device_tabs

        root = self.main_window or self.get_root()
        if root and hasattr(root, "_device_pages"):
            _refresh_device_tabs(
                root,
                preferred_profile_name=preferred_profile_name,
                source_widget=self,
            )

    def _publish_profile_selection(self) -> None:
        from keymasq.gui.window.profiles import _sync_selected_profile_name

        root = self.main_window or self.get_root()
        if root and hasattr(root, "_selected_profile_name"):
            _sync_selected_profile_name(
                root,
                self._selected_profile.config.name if self._selected_profile else None,
                source_widget=self,
            )

    def _request_session_async(
        self,
        payload: dict,
        callback,
        timeout: float | None = None,
    ) -> object:
        if timeout is None:
            return session_request_async(payload, callback)
        return session_request_async(payload, callback, timeout=timeout)

    def _notify_session_reload(self) -> bool:
        return notify_session_reload()

    def _notify_session_reload_async(self) -> None:
        notify_session_reload_async()
