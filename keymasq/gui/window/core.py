from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from keymasq.common.models import HardwareConfig
from keymasq.gui.preferences import (
    AppearanceMode,
    load_hidden_tabs,
    load_selected_tab,
    load_tab_order,
)
from keymasq.gui.widgets.combo_tab import ComboTab
from keymasq.gui.widgets.gnome_setup_dialog import GnomeSetupDialog
from keymasq.session.hardware import HardwareManager
from keymasq.session.profiles import ProfileManager

from . import (
    _runtime,
    chrome,
    compositor,
    connection,
    device_tabs,
    gnome_setup,
    inspectors,
    macro_recording,
    profiles,
    recording_unlock,
    tab_layout,
)

GLib = _runtime.GLib

if TYPE_CHECKING:
    from gi.repository import Adw, Gio, Gtk  # pyright: ignore[reportAttributeAccessIssue]

    from keymasq.gui.session_client import GuiTaskResult


class MainWindow(_runtime.Adw.ApplicationWindow):
    def __init__(self, demo_mode: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)

        self.demo_mode = demo_mode
        self.hardware_manager = HardwareManager()
        self.profile_manager = ProfileManager(auto_create_default_if_empty=True)
        self._session_connected: bool | None = None
        self._keymasqd_via_session: bool | None = None
        self._compositor_id: str | None = None
        self._compositor_supported = False
        self._compositor_capabilities: list[str] = []
        self._listener_name = ""
        self._compositor_dispatch_available = False
        self._compositor_support_details: dict[str, bool | str] = {
            "supported": False,
            "warning": "",
        }
        self._event_handlers: dict[str, list[Callable[[dict], bool | None]]] = {}
        self._status_query_inflight = False
        self._status_query_id = 0
        self._connection_dialog: _runtime.Adw.Dialog | None = None
        self._connection_issue: str | None = None
        self._connection_title_label: _runtime.Gtk.Label | None = None
        self._connection_body_label: _runtime.Gtk.Label | None = None
        self._gnome_setup_dialog: GnomeSetupDialog | None = None
        self._gnome_setup_dialog_prompted = False
        self._gnome_setup_poll_source_id = 0
        self._macro_manager_dialog: _runtime.Adw.Dialog | None = None
        self._record_macro_dialog: _runtime.Adw.Dialog | None = None
        self._save_macro_dialog: _runtime.Adw.Dialog | None = None
        self._recording_unlocked = False
        self._recording_unlock_required = True
        self._recording_unlock_source = "none"
        self._recording_unlock_expires_at = 0
        self._recording_refresh_owner = False
        self._macro_recording_enabled = False
        self._macro_recording_source = "none"
        self._macro_recording_expires_at = 0
        self._emergency_cancel_combo_enabled = True
        self._recording_refresh_lease_id: str = ""
        self._recording_claim_attempt_key: tuple[str, int] | None = None
        self._unlock_request_inflight = False
        self._macro_recording_enable_inflight = False
        self._unlock_refresh_inflight = False
        self._placeholder_subtitle: _runtime.Gtk.Label | None = None
        self._menu_unlock_btn: _runtime.Gtk.Button | None = None
        self._menu_unlock_separator: _runtime.Gtk.Widget | None = None
        self._appearance_buttons: dict[AppearanceMode, _runtime.Gtk.ToggleButton] = {}
        self._syncing_appearance = False
        self._unlock_status_label: _runtime.Gtk.Label | None = None
        self._selected_profile_name: str | None = None
        self._syncing_profile_selection = False
        self._startup_probe_done = False
        self._lease_claim_inflight = False
        self._placeholder_title: _runtime.Gtk.Label | None = None
        self._session_reconnect_source_id = 0
        self._unlock_refresh_source_id = 0
        self._profile_runtime_state: dict[str, object] = {
            "active_profiles": [],
            "devices": {},
            "window": {},
        }
        self._profile_reload_inflight = False
        self._profile_reload_pending = False
        self._destroyed = False
        self._tab_order = load_tab_order()
        self._hidden_tabs = load_hidden_tabs()
        self._selected_tab = load_selected_tab()
        self._device_pages: dict[str, _runtime.Adw.TabPage] = {}
        self._combo_page: _runtime.Adw.TabPage | None = None
        self._placeholder_page: _runtime.Adw.TabPage | None = None
        self._allow_tab_page_close = False
        self._suppress_tab_layout_save = False
        self._suppress_selected_tab_save = True
        self._initial_tab_selection_pending = True
        self.combo_tab: ComboTab | None = None
        self._device_inspector_windows: dict[str, _runtime.Gtk.Window] = {}
        self._combo_inspector_window: _runtime.Gtk.Window | None = None

        self.set_title("Keymasq")
        self.set_default_size(760, 1000)

        self._setup_content()
        self._suppress_selected_tab_save = False
        self._start_startup_probe()

        if not self.demo_mode:
            connection.register(self)
            self._session_reconnect_source_id = GLib.timeout_add(2000, self._reconnect_session)
            self._unlock_refresh_source_id = GLib.timeout_add_seconds(
                30, self._refresh_unlock_lease
            )
            self._update_status_from_session()

        self.connect("destroy", self._on_destroy)

    def _on_destroy(self, *_args) -> None:
        self._destroyed = True
        self._session_reconnect_source_id = self._remove_timeout_source(
            self._session_reconnect_source_id
        )
        self._unlock_refresh_source_id = self._remove_timeout_source(self._unlock_refresh_source_id)
        self._gnome_setup_poll_source_id = self._remove_timeout_source(
            self._gnome_setup_poll_source_id
        )
        for inspector_window in list(self._device_inspector_windows.values()):
            inspector_window.close()
        self._device_inspector_windows.clear()
        if self._combo_inspector_window is not None:
            self._combo_inspector_window.close()
            self._combo_inspector_window = None

        if self.demo_mode:
            return

        recording_unlock.lock_lease_on_close(self)
        connection.unregister(self)

    def _remove_timeout_source(self, source_id: int) -> int:
        if source_id <= 0:
            return 0
        GLib.source_remove(source_id)
        return 0

    def _probe_startup_state(self) -> tuple[dict[str, object], list[HardwareConfig]]:
        return compositor._probe_startup_state(self)

    def _start_startup_probe(self) -> None:
        return compositor._start_startup_probe(self)

    def _on_startup_probe_finished(
        self, result: GuiTaskResult[tuple[dict[str, object], list[HardwareConfig]]]
    ) -> bool:
        return compositor._on_startup_probe_finished(self, result)

    def _apply_compositor_state(self, state: dict[str, object]) -> None:
        return compositor._apply_compositor_state(self, state)

    def _icon_from_name(self, icon_name: str) -> Gio.Icon:
        return chrome._icon_from_name(self, icon_name)

    def _iter_tab_pages(self):
        return tab_layout._iter_tab_pages(self)

    def _iter_tab_children(self):
        return tab_layout._iter_tab_children(self)

    def _iter_profile_tabs(self):
        return tab_layout._iter_profile_tabs(self)

    def _page_for_child(self, widget: Gtk.Widget | None) -> Adw.TabPage | None:
        return tab_layout._page_for_child(self, widget)

    def _page_for_hardware_id(self, hardware_id: str) -> Adw.TabPage | None:
        return tab_layout._page_for_hardware_id(self, hardware_id)

    def _child_for_hardware_id(self, hardware_id: str) -> Gtk.Widget | None:
        return tab_layout._child_for_hardware_id(self, hardware_id)

    def _append_tab_page(
        self, child: Gtk.Widget, *, title: str, icon_name: str, pinned: bool = False
    ) -> Adw.TabPage:
        return tab_layout._append_tab_page(
            self, child, title=title, icon_name=icon_name, pinned=pinned
        )

    def _device_status_for_hardware_id(self, hardware_id: str) -> dict[str, object]:
        return device_tabs._device_status_for_hardware_id(self, hardware_id)

    def _device_tab_title(self, device: HardwareConfig) -> str:
        return device_tabs._device_tab_title(self, device)

    def _sync_device_tab_title(self, hardware_id: str) -> None:
        return device_tabs._sync_device_tab_title(self, hardware_id)

    def _sync_device_tab_titles(self) -> None:
        return device_tabs._sync_device_tab_titles(self)

    def _walk_widget_tree(self, widget: Gtk.Widget):
        return tab_layout._walk_widget_tree(self, widget)

    def _sync_tab_close_tooltips(self) -> bool:
        return tab_layout._sync_tab_close_tooltips(self)

    def _close_tab_page(self, page: Adw.TabPage | None) -> None:
        return tab_layout._close_tab_page(self, page)

    def _on_tab_close_page(self, _tab_view: Adw.TabView, page: Adw.TabPage) -> bool:
        return tab_layout._on_tab_close_page(self, _tab_view, page)

    def _hide_combo_tab(self, page: Adw.TabPage) -> None:
        return tab_layout._hide_combo_tab(self, page)

    def _on_tab_page_reordered(
        self, _tab_view: Adw.TabView, _page: Adw.TabPage, _position: int
    ) -> None:
        return tab_layout._on_tab_page_reordered(self, _tab_view, _page, _position)

    def _tab_id_for_child(self, child: Gtk.Widget | None) -> str | None:
        return tab_layout._tab_id_for_child(self, child)

    def _selected_tab_for_child(self, child: Gtk.Widget | None) -> str | None:
        return tab_layout._selected_tab_for_child(self, child)

    def _current_tab_order(self) -> list[str]:
        return tab_layout._current_tab_order(self)

    def list_device_tab_configs(self) -> list[HardwareConfig]:
        return tab_layout.list_device_tab_configs(self)

    def _merge_hidden_tabs_into_order(self, visible_order: list[str]) -> list[str]:
        return tab_layout._merge_hidden_tabs_into_order(self, visible_order)

    def _save_tab_layout(self) -> None:
        return tab_layout._save_tab_layout(self)

    def _page_for_tab_id(self, tab_id: str) -> Adw.TabPage | None:
        return tab_layout._page_for_tab_id(self, tab_id)

    def _page_for_selected_tab(self, selected_tab: str) -> Adw.TabPage | None:
        return tab_layout._page_for_selected_tab(self, selected_tab)

    def _default_selected_tab_page(self) -> Adw.TabPage | None:
        return tab_layout._default_selected_tab_page(self)

    def _select_saved_or_default_tab(self) -> None:
        return tab_layout._select_saved_or_default_tab(self)

    def _default_visible_tab_order(self) -> list[str]:
        return tab_layout._default_visible_tab_order(self)

    def _desired_visible_tab_order(self) -> list[str]:
        return tab_layout._desired_visible_tab_order(self)

    def _reorder_visible_pages_to_saved_order(self) -> None:
        return tab_layout._reorder_visible_pages_to_saved_order(self)

    def _order_devices_for_tabs(self, devices: list[HardwareConfig]) -> list[HardwareConfig]:
        return tab_layout._order_devices_for_tabs(self, devices)

    def _refresh_device_tabs(
        self,
        preferred_profile_name: str | None = None,
        source_hardware_id: str | None = None,
        source_widget: Gtk.Widget | None = None,
    ) -> None:
        return device_tabs._refresh_device_tabs(
            self, preferred_profile_name, source_hardware_id, source_widget
        )

    def _set_selected_profile_name(self, profile_name: str | None) -> None:
        return profiles._set_selected_profile_name(self, profile_name)

    def _sync_selected_profile_name(
        self,
        profile_name: str | None,
        source_hardware_id: str | None = None,
        source_widget: Gtk.Widget | None = None,
    ) -> None:
        return profiles._sync_selected_profile_name(
            self, profile_name, source_hardware_id, source_widget
        )

    def _on_selected_tab_changed(self, _tab_view, _pspec) -> None:
        return tab_layout._on_selected_tab_changed(self, _tab_view, _pspec)

    def _on_session_event(self, event: dict) -> bool:
        return connection._on_session_event(self, event)

    def _reconnect_session(self) -> bool:
        return connection._reconnect_session(self)

    def register_event_handler(self, event_type: str, callback: Callable[[dict], None]) -> None:
        return connection.register_event_handler(self, event_type, callback)

    def unregister_event_handler(self, event_type: str, callback: Callable[[dict], None]) -> None:
        return connection.unregister_event_handler(self, event_type, callback)

    def _handle_session_event(self, event: dict) -> None:
        return connection._handle_session_event(self, event)

    def _on_recording_stopped(self, event: dict) -> None:
        return macro_recording._on_recording_stopped(self, event)

    def present_pending_macro_save_dialog(self, recording_data: dict | None = None) -> bool:
        return macro_recording.present_pending_macro_save_dialog(self, recording_data)

    def _on_save_macro_dialog_closed(self, dialog) -> None:
        return macro_recording._on_save_macro_dialog_closed(self, dialog)

    def _close_dialogs_for_recording_start(self) -> None:
        return macro_recording._close_dialogs_for_recording_start(self)

    def set_macro_manager_dialog(self, dialog: Adw.Dialog | None) -> None:
        return macro_recording.set_macro_manager_dialog(self, dialog)

    def _update_status_from_keymasqd_event(self, keymasqd_connected: bool) -> None:
        return connection._update_status_from_keymasqd_event(self, keymasqd_connected)

    def _update_status_from_session(self) -> None:
        return connection._update_status_from_session(self)

    def _on_status_response(self, data: dict | None, query_id: int) -> bool:
        return connection._on_status_response(self, data, query_id)

    def _update_status_disconnected(self) -> None:
        return connection._update_status_disconnected(self)

    def _update_compositor_dispatch_state(self, status_data: dict | None) -> None:
        return compositor._update_compositor_dispatch_state(self, status_data)

    def get_compositor_action_status(self) -> dict[str, object]:
        return compositor.get_compositor_action_status(self)

    def _update_unlock_state(self, status_data: dict | None) -> None:
        return recording_unlock._update_unlock_state(self, status_data)

    def _update_macro_recording_state(self, status_data: dict | None) -> None:
        return macro_recording._update_macro_recording_state(self, status_data)

    def macro_recording_enabled(self) -> bool:
        return macro_recording.macro_recording_enabled(self)

    def _refresh_macro_recording_state_from_session(
        self, on_status: Callable[[dict | None], None] | None = None
    ) -> None:
        return macro_recording._refresh_macro_recording_state_from_session(self, on_status)

    def emergency_cancel_combo_enabled(self) -> bool:
        return recording_unlock.emergency_cancel_combo_enabled(self)

    def _set_connection_issue(self, issue: str | None) -> None:
        return connection._set_connection_issue(self, issue)

    def _on_connection_dialog_closed(self, dialog) -> None:
        return connection._on_connection_dialog_closed(self, dialog)

    def _retry_connection_check(self) -> None:
        return connection._retry_connection_check(self)

    def _setup_content(self) -> None:
        return chrome._setup_content(self)

    def _create_menu_separator(self) -> Gtk.Widget:
        return chrome._create_menu_separator(self)

    def _configure_menu_button(self, button: Gtk.Button) -> None:
        return chrome._configure_menu_button(self, button)

    def _on_appearance_mode_toggled(self, button: Gtk.ToggleButton, mode: AppearanceMode) -> None:
        return chrome._on_appearance_mode_toggled(self, button, mode)

    def _on_menu_action_clicked(
        self, _button: Gtk.Button, action_name: str, popover: Gtk.Popover
    ) -> None:
        return chrome._on_menu_action_clicked(self, _button, action_name, popover)

    def _on_combos_menu_clicked(self, _button: Gtk.Button, popover: Gtk.Popover) -> None:
        return chrome._on_combos_menu_clicked(self, _button, popover)

    def _on_menu_unlock_clicked(self, _button: Gtk.Button, popover: Gtk.Popover) -> None:
        return chrome._on_menu_unlock_clicked(self, _button, popover)

    def _setup_placeholder(self) -> None:
        return chrome._setup_placeholder(self)

    def _create_placeholder_widget(self, *, title_text: str, subtitle_text: str) -> None:
        return chrome._create_placeholder_widget(
            self, title_text=title_text, subtitle_text=subtitle_text
        )

    def _ensure_placeholder_page(self) -> None:
        return device_tabs._ensure_placeholder_page(self)

    def _set_empty_placeholder_state(self) -> None:
        return device_tabs._set_empty_placeholder_state(self)

    def _apply_loaded_devices(self, devices: list[HardwareConfig]) -> None:
        return device_tabs._apply_loaded_devices(self, devices)

    def _load_demo_devices(self) -> None:
        return device_tabs._load_demo_devices(self)

    def _setup_combo_tab(self) -> None:
        return device_tabs._setup_combo_tab(self)

    def _ensure_combo_tab_page(self) -> Adw.TabPage | None:
        return device_tabs._ensure_combo_tab_page(self)

    def show_combo_tab(self) -> None:
        return device_tabs.show_combo_tab(self)

    def _add_device_tab(self, device: HardwareConfig, *, persist_order: bool = True) -> None:
        return device_tabs._add_device_tab(self, device, persist_order=persist_order)

    def update_device_display_name(self, hardware_id: str, name: str) -> None:
        return device_tabs.update_device_display_name(self, hardware_id, name)

    def open_device_inspector(self, device) -> None:
        return inspectors.open_device_inspector(self, device)

    def open_combo_inspector(self) -> None:
        return inspectors.open_combo_inspector(self)

    def _device_inspector_unlock_ready(self) -> bool:
        return inspectors._device_inspector_unlock_ready(self)

    def remove_device_tab(self, hardware_id: str) -> None:
        return device_tabs.remove_device_tab(self, hardware_id)

    def _queue_profile_reload(self) -> None:
        return profiles._queue_profile_reload(self)

    def _load_profile_manager_snapshot(self) -> ProfileManager:
        return profiles._load_profile_manager_snapshot(self)

    def _on_profile_reload_finished(self, result: GuiTaskResult[ProfileManager]) -> bool:
        return profiles._on_profile_reload_finished(self, result)

    def _set_profile_manager(self, profile_manager: ProfileManager) -> None:
        return profiles._set_profile_manager(self, profile_manager)

    def _normalize_profile_runtime_state(self, state: dict | None) -> dict[str, object]:
        return profiles._normalize_profile_runtime_state(self, state)

    def _apply_profile_runtime_state_to_widget(self, widget: Gtk.Widget | None) -> None:
        return profiles._apply_profile_runtime_state_to_widget(self, widget)

    def _apply_profile_runtime_state(self, state: dict | None) -> None:
        return profiles._apply_profile_runtime_state(self, state)

    def _mark_device_runtime_unknown(self) -> None:
        return profiles._mark_device_runtime_unknown(self)

    def _runtime_status_int(self, status: dict[str, object], key: str) -> int:
        return profiles._runtime_status_int(self, status, key)

    def _on_add_device(self, button: Gtk.Button) -> None:
        return device_tabs._on_add_device(self, button)

    def _on_add_device_clicked(self, _button: Gtk.Button) -> None:
        return device_tabs._on_add_device_clicked(self, _button)

    def present_unlock_dialog(self, on_success=None) -> None:
        return recording_unlock.present_unlock_dialog(self, on_success)

    def present_macro_recording_enable_dialog(self, on_success=None) -> None:
        return macro_recording.present_macro_recording_enable_dialog(self, on_success)

    def present_macro_recording_disable_dialog(self, on_success=None) -> None:
        return macro_recording.present_macro_recording_disable_dialog(self, on_success)

    def _on_quit_clicked(self, _button: Gtk.Button) -> None:
        return connection._on_quit_clicked(self, _button)

    def _on_retry_connection_clicked(self, _button: Gtk.Button) -> None:
        return connection._on_retry_connection_clicked(self, _button)

    def _start_recording_unlock(self, on_success=None) -> None:
        return recording_unlock._start_recording_unlock(self, on_success)

    def _on_macro_recording_enable_response(
        self, _dialog: Adw.AlertDialog, response: str, on_success
    ) -> None:
        return macro_recording._on_macro_recording_enable_response(
            self, _dialog, response, on_success
        )

    def _on_macro_recording_disable_response(
        self, _dialog: Adw.AlertDialog, response: str, on_success
    ) -> None:
        return macro_recording._on_macro_recording_disable_response(
            self, _dialog, response, on_success
        )

    def _start_macro_recording_enable(self, on_success=None) -> None:
        return macro_recording._start_macro_recording_enable(self, on_success)

    def _start_macro_recording_disable(self, on_success=None) -> None:
        return macro_recording._start_macro_recording_disable(self, on_success)

    def _on_macro_recording_enable_finished(
        self, success: bool, error_msg: str, on_success, status: dict | None = None
    ) -> bool:
        return macro_recording._on_macro_recording_enable_finished(
            self, success, error_msg, on_success, status
        )

    def _on_unlock_finished(
        self, success: bool, error_msg: str, on_success, claim_response: dict | None = None
    ) -> bool:
        return recording_unlock._on_unlock_finished(
            self, success, error_msg, on_success, claim_response
        )

    def _show_unlock_error_dialog(self, message: str) -> None:
        return recording_unlock._show_unlock_error_dialog(self, message)

    def _refresh_macro_menu_state(self) -> None:
        return recording_unlock._refresh_macro_menu_state(self)

    def _refresh_unlock_status_label(self) -> None:
        return recording_unlock._refresh_unlock_status_label(self)

    def present_recording_settings_dialog(self, reason: str = "settings") -> None:
        return macro_recording.present_recording_settings_dialog(self, reason)

    def _on_record_macro_dialog_closed(self, dialog) -> None:
        return macro_recording._on_record_macro_dialog_closed(self, dialog)

    def _refresh_unlock_lease(self) -> bool:
        return recording_unlock._refresh_unlock_lease(self)

    def _on_refresh_unlock_finished(self, result: dict | None) -> bool:
        return recording_unlock._on_refresh_unlock_finished(self, result)

    def _request_recording_refresh_lease(self) -> None:
        return recording_unlock._request_recording_refresh_lease(self)

    def _on_claim_recording_refresh_lease_done(self) -> None:
        return recording_unlock._on_claim_recording_refresh_lease_done(self)

    def _on_claim_recording_refresh_lease_finished(self, response: dict | None) -> bool:
        return recording_unlock._on_claim_recording_refresh_lease_finished(self, response)

    def _on_device_created(self, dialog, device) -> None:
        return device_tabs._on_device_created(self, dialog, device)

    def _show_demo_notification(self, message: str) -> None:
        return device_tabs._show_demo_notification(self, message)

    def _check_empty_state(self) -> None:
        return device_tabs._check_empty_state(self)

    def _update_compositor_status(self) -> None:
        return compositor._update_compositor_status(self)

    def _update_compositor_warning_banner(self) -> None:
        return compositor._update_compositor_warning_banner(self)

    def _gnome_setup_needed(self) -> bool:
        return gnome_setup._gnome_setup_needed(self)

    def _maybe_present_gnome_setup_dialog(self) -> None:
        return gnome_setup._maybe_present_gnome_setup_dialog(self)

    def _on_compositor_status_released(
        self, _gesture: Gtk.GestureClick, _n_press: int, _x: float, _y: float
    ) -> None:
        return gnome_setup._on_compositor_status_released(self, _gesture, _n_press, _x, _y)

    def _present_gnome_setup_dialog(self) -> bool:
        return gnome_setup._present_gnome_setup_dialog(self)

    def _on_gnome_setup_dialog_closed(self, dialog: Adw.Dialog) -> None:
        return gnome_setup._on_gnome_setup_dialog_closed(self, dialog)

    def _on_gnome_setup_action_completed(self, action: str) -> None:
        return gnome_setup._on_gnome_setup_action_completed(self, action)

    def _schedule_gnome_setup_status_poll(self) -> None:
        return gnome_setup._schedule_gnome_setup_status_poll(self)

    def _poll_gnome_setup_status(self) -> bool:
        return gnome_setup._poll_gnome_setup_status(self)

    def _close_gnome_setup_dialog_if_ready(self) -> None:
        return gnome_setup._close_gnome_setup_dialog_if_ready(self)
