from __future__ import annotations

from collections.abc import Callable

from keymasq.common.models import HardwareConfig
from keymasq.gui.preferences import (
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
    inspectors,
    macro_recording,
    recording_unlock,
    tab_layout,
)

GLib = _runtime.GLib


class MainWindow(_runtime.Adw.ApplicationWindow):
    def __init__(self, demo_mode: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)

        self.demo_mode = demo_mode
        self.hardware_manager = HardwareManager()
        self.profile_manager = ProfileManager()
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
        self._session_event_callback: Callable[[dict], bool] | None = None
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
        self._appearance_buttons: dict[str, _runtime.Gtk.ToggleButton] = {}
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
        self._initial_status_profile_reload_done = False
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

        chrome._setup_content(self)
        self._suppress_selected_tab_save = False
        compositor._start_startup_probe(self)

        if not self.demo_mode:
            connection.register(self)
            self._session_reconnect_source_id = GLib.timeout_add(
                2000, lambda: connection._reconnect_session(self)
            )
            self._unlock_refresh_source_id = GLib.timeout_add_seconds(
                30, lambda: recording_unlock._refresh_unlock_lease(self)
            )
            connection._update_status_from_session(self)

        self.connect("destroy", self._on_destroy)

    def _on_destroy(self, *_args) -> None:
        self._destroyed = True
        self._session_reconnect_source_id = _runtime.remove_timeout_source(
            self._session_reconnect_source_id
        )
        self._unlock_refresh_source_id = _runtime.remove_timeout_source(
            self._unlock_refresh_source_id
        )
        self._gnome_setup_poll_source_id = _runtime.remove_timeout_source(
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

    def list_device_tab_configs(self) -> list[HardwareConfig]:
        return tab_layout.list_device_tab_configs(self)

    def register_event_handler(self, event_type: str, callback: Callable[[dict], None]) -> None:
        connection.register_event_handler(self, event_type, callback)

    def unregister_event_handler(self, event_type: str, callback: Callable[[dict], None]) -> None:
        connection.unregister_event_handler(self, event_type, callback)

    def present_pending_macro_save_dialog(self, recording_data: dict | None = None) -> bool:
        return macro_recording.present_pending_macro_save_dialog(self, recording_data)

    def set_macro_manager_dialog(self, dialog: _runtime.Adw.Dialog | None) -> None:
        macro_recording.set_macro_manager_dialog(self, dialog)

    def get_compositor_action_status(self) -> dict[str, object]:
        return compositor.get_compositor_action_status(self)

    def macro_recording_enabled(self) -> bool:
        return macro_recording.macro_recording_enabled(self)

    def emergency_cancel_combo_enabled(self) -> bool:
        return recording_unlock.emergency_cancel_combo_enabled(self)

    def show_combo_tab(self) -> None:
        device_tabs.show_combo_tab(self)

    def update_device_display_name(self, hardware_id: str, name: str) -> None:
        device_tabs.update_device_display_name(self, hardware_id, name)

    def open_device_inspector(self, device) -> None:
        inspectors.open_device_inspector(self, device)

    def open_combo_inspector(self) -> None:
        inspectors.open_combo_inspector(self)

    def remove_device_tab(self, hardware_id: str) -> None:
        device_tabs.remove_device_tab(self, hardware_id)

    def present_unlock_dialog(self, on_success=None) -> None:
        recording_unlock.present_unlock_dialog(self, on_success)

    def present_macro_recording_enable_dialog(self, on_success=None) -> None:
        macro_recording.present_macro_recording_enable_dialog(self, on_success)

    def present_macro_recording_disable_dialog(self, on_success=None) -> None:
        macro_recording.present_macro_recording_disable_dialog(self, on_success)

    def present_recording_settings_dialog(self, reason: str = "settings") -> None:
        macro_recording.present_recording_settings_dialog(self, reason)
