import logging
import os
import subprocess
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.models import HardwareConfig
from keymasq.common.paths import KEYMASQ_RECORD_HELPER_PATH, resolve_keymasq_record_helper_path
from keymasq.common.recording_guard import resolve_unlock_status
from keymasq.gui.icons import (
    combo_icon_names,
    device_icon_names,
    image_from_icon_names,
    resolve_icon_name,
)
from keymasq.gui.preferences import (
    AppearanceMode,
    load_appearance_mode,
    load_hidden_tabs,
    load_tab_order,
    save_tab_layout,
)
from keymasq.gui.session_client import (
    GuiTaskResult,
    register_session_event_callback,
    run_gui_task,
    session_request,
    session_request_async,
    session_request_with_hooks,
    unregister_session_event_callback,
)
from keymasq.gui.widgets.combo_tab import ComboTab
from keymasq.gui.widgets.device_tab import DeviceTab
from keymasq.gui.widgets.gnome_setup_dialog import GnomeSetupDialog
from keymasq.gui.widgets.profile_managed_tab import ProfileManagedTab
from keymasq.session.compositor import (
    detect_compositor_sync,
    get_compositor_capabilities,
    get_compositor_name,
    get_compositor_support_details_sync,
    is_compositor_supported_sync,
)
from keymasq.session.hardware import HardwareManager
from keymasq.session.profiles import ProfileManager

log = logging.getLogger("keymasq.gui.window")

_COMBO_TAB_ID = "combos"
_DEVICE_TAB_PREFIX = "device:"


def _device_tab_id(hardware_id: str) -> str:
    return f"{_DEVICE_TAB_PREFIX}{hardware_id}"


class MainWindow(Adw.ApplicationWindow):
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
        self._connection_dialog: Adw.Dialog | None = None
        self._connection_issue: str | None = None
        self._connection_title_label: Gtk.Label | None = None
        self._connection_body_label: Gtk.Label | None = None
        self._gnome_setup_dialog: GnomeSetupDialog | None = None
        self._gnome_setup_dialog_prompted = False
        self._gnome_setup_poll_source_id = 0
        self._macro_manager_dialog: Adw.Dialog | None = None
        self._record_macro_dialog: Adw.Dialog | None = None
        self._save_macro_dialog: Adw.Dialog | None = None
        self._recording_unlocked = False
        self._recording_unlock_required = True
        self._recording_unlock_source = "none"
        self._recording_unlock_expires_at = 0
        self._recording_refresh_owner = False
        self._emergency_cancel_combo_enabled = True
        self._recording_refresh_lease_id: str = ""
        self._recording_claim_attempt_key: tuple[str, int] | None = None
        self._unlock_request_inflight = False
        self._unlock_refresh_inflight = False
        self._placeholder_subtitle: Gtk.Label | None = None
        self._menu_unlock_btn: Gtk.Button | None = None
        self._menu_unlock_separator: Gtk.Widget | None = None
        self._appearance_buttons: dict[AppearanceMode, Gtk.ToggleButton] = {}
        self._syncing_appearance = False
        self._unlock_status_label: Gtk.Label | None = None
        self._selected_profile_name: str | None = None
        self._syncing_profile_selection = False
        self._startup_probe_done = False
        self._lease_claim_inflight = False
        self._placeholder_title: Gtk.Label | None = None
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
        self._device_pages: dict[str, Adw.TabPage] = {}
        self._combo_page: Adw.TabPage | None = None
        self._placeholder_page: Adw.TabPage | None = None
        self._allow_tab_page_close = False
        self._suppress_tab_layout_save = False
        self.combo_tab: ComboTab | None = None
        self._device_inspector_windows: dict[str, Gtk.Window] = {}

        self.set_title("Keymasq")
        self.set_default_size(760, 1000)

        self._setup_content()
        self._start_startup_probe()

        if not self.demo_mode:
            register_session_event_callback("*", self._on_session_event)
            self._session_reconnect_source_id = GLib.timeout_add(2000, self._reconnect_session)
            self._unlock_refresh_source_id = GLib.timeout_add_seconds(
                30, self._refresh_unlock_lease
            )
            self._update_status_from_session()

        self.connect("destroy", self._on_destroy)

    def _probe_startup_state(self) -> tuple[dict[str, object], list[HardwareConfig]]:
        compositor_id = detect_compositor_sync()
        support_details = get_compositor_support_details_sync(compositor_id)
        supported = bool(support_details.get("supported", False))
        if compositor_id != "gnome":
            supported = is_compositor_supported_sync(compositor_id)
        return (
            {
                "compositor_id": compositor_id,
                "support_details": support_details,
                "supported": supported,
                "capabilities": get_compositor_capabilities(compositor_id),
            },
            self.hardware_manager.list_hardware(),
        )

    def _start_startup_probe(self) -> None:
        run_gui_task(
            self._probe_startup_state,
            self._on_startup_probe_finished,
        )

    def _on_startup_probe_finished(
        self,
        result: GuiTaskResult[tuple[dict[str, object], list[HardwareConfig]]],
    ) -> bool:
        if result.ok and result.value is not None:
            compositor_state, devices = result.value
        else:
            compositor_state, devices = ({}, [])
        self._startup_probe_done = True
        self._apply_compositor_state(compositor_state)
        self._apply_loaded_devices(devices)
        return False

    def _apply_compositor_state(self, state: dict[str, object]) -> None:
        compositor_id = state.get("compositor_id")
        self._compositor_id = compositor_id if isinstance(compositor_id, str) else None
        details = state.get("support_details")
        self._compositor_support_details = details if isinstance(details, dict) else {
            "supported": False,
            "warning": "",
        }
        self._compositor_supported = bool(state.get("supported", False))
        caps = state.get("capabilities")
        self._compositor_capabilities = list(caps) if isinstance(caps, list) else []
        if self.combo_tab is not None:
            self.combo_tab._compositor_capabilities = self._compositor_capabilities
            self.combo_tab.refresh_profiles(
                preferred_profile_name=self._selected_profile_name,
                publish_selection=False,
            )
        self._update_compositor_warning_banner()
        self._update_compositor_status()
        self._close_gnome_setup_dialog_if_ready()
        self._maybe_present_gnome_setup_dialog()

    def _icon_from_name(self, icon_name: str) -> Gio.Icon:
        return Gio.ThemedIcon.new(icon_name)

    def _iter_tab_pages(self):
        for position in range(self.tab_view.get_n_pages()):
            yield self.tab_view.get_nth_page(position)

    def _iter_tab_children(self):
        for page in self._iter_tab_pages():
            child = page.get_child()
            if child is not None:
                yield child

    def _iter_profile_tabs(self):
        yield from self._iter_tab_children()

    def _page_for_child(self, widget: Gtk.Widget | None) -> Adw.TabPage | None:
        if widget is None:
            return None
        if widget is self.placeholder:
            return self._placeholder_page
        if widget is self.combo_tab:
            return self._combo_page
        for page in self._device_pages.values():
            if page.get_child() is widget:
                return page
        return None

    def _page_for_hardware_id(self, hardware_id: str) -> Adw.TabPage | None:
        return self._device_pages.get(hardware_id)

    def _child_for_hardware_id(self, hardware_id: str) -> Gtk.Widget | None:
        page = self._page_for_hardware_id(hardware_id)
        return page.get_child() if page is not None else None

    def _append_tab_page(
        self,
        child: Gtk.Widget,
        *,
        title: str,
        icon_name: str,
        pinned: bool = False,
    ) -> Adw.TabPage:
        page = self.tab_view.append_pinned(child) if pinned else self.tab_view.append(child)
        page.set_title(title)
        page.set_icon(self._icon_from_name(icon_name))
        GLib.idle_add(self._sync_tab_close_tooltips)
        return page

    def _walk_widget_tree(self, widget: Gtk.Widget):
        yield widget
        child = widget.get_first_child()
        while child is not None:
            yield from self._walk_widget_tree(child)
            child = child.get_next_sibling()

    def _sync_tab_close_tooltips(self) -> bool:
        for widget in self._walk_widget_tree(self.tab_bar):
            if "tab-close-button" in widget.get_css_classes():
                widget.set_tooltip_text("Close tab")
        return False

    def _close_tab_page(self, page: Adw.TabPage | None) -> None:
        if page is None:
            return
        self._allow_tab_page_close = True
        try:
            self.tab_view.close_page(page)
        finally:
            self._allow_tab_page_close = False

    def _on_tab_close_page(self, _tab_view: Adw.TabView, page: Adw.TabPage) -> bool:
        if self._allow_tab_page_close:
            child = page.get_child()
            self.tab_view.close_page_finish(page, True)
            if isinstance(child, ComboTab):
                self._combo_page = None
                self.combo_tab = None
            return True

        child = page.get_child()
        if isinstance(child, ComboTab):
            self._hide_combo_tab(page)
            return True

        self.tab_view.close_page_finish(page, False)
        if isinstance(child, DeviceTab):
            child.present_delete_device_dialog()
        return True

    def _hide_combo_tab(self, page: Adw.TabPage) -> None:
        self._tab_order = self._current_tab_order()
        self._hidden_tabs.add(_COMBO_TAB_ID)
        save_tab_layout(self._tab_order, self._hidden_tabs)
        self.tab_view.close_page_finish(page, True)
        self._combo_page = None
        self.combo_tab = None
        self._check_empty_state()

    def _on_tab_page_reordered(
        self,
        _tab_view: Adw.TabView,
        _page: Adw.TabPage,
        _position: int,
    ) -> None:
        if not self._suppress_tab_layout_save:
            self._save_tab_layout()

    def _tab_id_for_child(self, child: Gtk.Widget | None) -> str | None:
        if isinstance(child, DeviceTab):
            return _device_tab_id(child.device.hardware_id)
        if isinstance(child, ComboTab):
            return _COMBO_TAB_ID
        return None

    def _current_tab_order(self) -> list[str]:
        order: list[str] = []
        for page in self._iter_tab_pages():
            tab_id = self._tab_id_for_child(page.get_child())
            if tab_id is not None:
                order.append(tab_id)
        return order

    def list_device_tab_configs(self) -> list[HardwareConfig]:
        devices: list[HardwareConfig] = []
        for page in self._iter_tab_pages():
            child = page.get_child()
            if isinstance(child, DeviceTab):
                devices.append(child.device)
        return devices

    def _merge_hidden_tabs_into_order(self, visible_order: list[str]) -> list[str]:
        order = list(visible_order)
        for hidden_tab_id in self._tab_order:
            if hidden_tab_id not in self._hidden_tabs or hidden_tab_id in order:
                continue

            previous = next(
                (
                    tab_id
                    for tab_id in reversed(self._tab_order[: self._tab_order.index(hidden_tab_id)])
                    if tab_id in order
                ),
                None,
            )
            if previous is not None:
                order.insert(order.index(previous) + 1, hidden_tab_id)
                continue

            following = next(
                (
                    tab_id
                    for tab_id in self._tab_order[self._tab_order.index(hidden_tab_id) + 1 :]
                    if tab_id in order
                ),
                None,
            )
            if following is not None:
                order.insert(order.index(following), hidden_tab_id)
                continue

            order.append(hidden_tab_id)

        for hidden_tab_id in sorted(self._hidden_tabs - set(order)):
            order.append(hidden_tab_id)
        return order

    def _save_tab_layout(self) -> None:
        self._tab_order = self._merge_hidden_tabs_into_order(self._current_tab_order())
        save_tab_layout(self._tab_order, self._hidden_tabs)

    def _page_for_tab_id(self, tab_id: str) -> Adw.TabPage | None:
        if tab_id == _COMBO_TAB_ID:
            return self._combo_page
        if tab_id.startswith(_DEVICE_TAB_PREFIX):
            hardware_id = tab_id.removeprefix(_DEVICE_TAB_PREFIX)
            return self._device_pages.get(hardware_id)
        return None

    def _default_visible_tab_order(self) -> list[str]:
        device_ids: list[str] = []
        other_ids: list[str] = []
        for tab_id in self._current_tab_order():
            if tab_id.startswith(_DEVICE_TAB_PREFIX):
                device_ids.append(tab_id)
            else:
                other_ids.append(tab_id)
        return device_ids + other_ids

    def _desired_visible_tab_order(self) -> list[str]:
        current_order = self._current_tab_order()
        if not self._tab_order:
            return self._default_visible_tab_order()

        visible_order = [
            tab_id
            for tab_id in self._tab_order
            if tab_id not in self._hidden_tabs and tab_id in current_order
        ]
        missing_order = [tab_id for tab_id in current_order if tab_id not in visible_order]
        missing_devices = [
            tab_id for tab_id in missing_order if tab_id.startswith(_DEVICE_TAB_PREFIX)
        ]
        missing_other = [
            tab_id for tab_id in missing_order if not tab_id.startswith(_DEVICE_TAB_PREFIX)
        ]
        if (
            missing_devices
            and visible_order
            and visible_order[-1] == _COMBO_TAB_ID
            and _COMBO_TAB_ID not in self._hidden_tabs
        ):
            visible_order[-1:-1] = missing_devices
        else:
            visible_order.extend(missing_devices)
        visible_order.extend(missing_other)
        return visible_order

    def _reorder_visible_pages_to_saved_order(self) -> None:
        desired_order = self._desired_visible_tab_order()
        if not desired_order:
            return

        suppress_was = self._suppress_tab_layout_save
        self._suppress_tab_layout_save = True
        try:
            position = self.tab_view.get_n_pinned_pages()
            if self._placeholder_page is not None:
                self.tab_view.reorder_page(self._placeholder_page, position)
                position += 1
            for tab_id in desired_order:
                page = self._page_for_tab_id(tab_id)
                if page is None:
                    continue
                self.tab_view.reorder_page(page, position)
                position += 1
        finally:
            self._suppress_tab_layout_save = suppress_was

    def _order_devices_for_tabs(self, devices: list[HardwareConfig]) -> list[HardwareConfig]:
        order_index = {
            tab_id.removeprefix(_DEVICE_TAB_PREFIX): index
            for index, tab_id in enumerate(self._tab_order)
            if tab_id.startswith(_DEVICE_TAB_PREFIX)
        }
        fallback_offset = len(order_index)
        indexed_devices: list[tuple[int, HardwareConfig]] = list(enumerate(devices))
        indexed_devices.sort(
            key=lambda item: (
                order_index.get(getattr(item[1], "hardware_id", ""), fallback_offset),
                item[0],
            )
        )
        return [device for _index, device in indexed_devices]

    def _refresh_device_tabs(
        self,
        preferred_profile_name: str | None = None,
        source_hardware_id: str | None = None,
        source_widget: Gtk.Widget | None = None,
    ) -> None:
        for child in self._iter_profile_tabs():
            if isinstance(child, ProfileManagedTab):
                if source_widget is not None and child is source_widget:
                    continue
                preferred = None
                if (
                    isinstance(child, DeviceTab)
                    and source_hardware_id is not None
                    and child.device.hardware_id == source_hardware_id
                ):
                    preferred = preferred_profile_name
                elif preferred_profile_name is not None:
                    preferred = preferred_profile_name
                child.refresh_profiles(preferred_profile_name=preferred)

    def _set_selected_profile_name(self, profile_name: str | None) -> None:
        self._selected_profile_name = profile_name

    def _sync_selected_profile_name(
        self,
        profile_name: str | None,
        source_hardware_id: str | None = None,
        source_widget: Gtk.Widget | None = None,
    ) -> None:
        if self._syncing_profile_selection:
            self._selected_profile_name = profile_name
            return

        self._selected_profile_name = profile_name
        self._syncing_profile_selection = True
        try:
            for child in self._iter_profile_tabs():
                if isinstance(child, ProfileManagedTab):
                    if source_widget is not None and child is source_widget:
                        continue
                    if (
                        isinstance(child, DeviceTab)
                        and source_hardware_id is not None
                        and child.device.hardware_id == source_hardware_id
                    ):
                        continue
                    child.refresh_profiles(
                        preferred_profile_name=profile_name,
                        publish_selection=False,
                    )
        finally:
            self._syncing_profile_selection = False

    def _on_selected_tab_changed(self, _tab_view, _pspec) -> None:
        page = self.tab_view.get_selected_page()
        child = page.get_child() if page is not None else None
        if isinstance(child, ProfileManagedTab):
            child.refresh_profiles(
                preferred_profile_name=self._selected_profile_name,
                publish_selection=False,
            )

    def _on_session_event(self, event: dict) -> bool:
        self._handle_session_event(event)
        return False

    def _reconnect_session(self) -> bool:
        self._update_status_from_session()
        return True

    def register_event_handler(self, event_type: str, callback: Callable[[dict], None]) -> None:
        if event_type not in self._event_handlers:
            self._event_handlers[event_type] = []
        self._event_handlers[event_type].append(callback)

    def unregister_event_handler(self, event_type: str, callback: Callable[[dict], None]) -> None:
        if event_type in self._event_handlers:
            try:
                self._event_handlers[event_type].remove(callback)
            except ValueError:
                pass

    def _handle_session_event(self, event: dict) -> None:
        event_type_raw = event.get("event")
        event_type = event_type_raw if isinstance(event_type_raw, str) else ""

        if event_type == "keymasqd_status":
            connected = event.get("connected", False)
            self._update_status_from_keymasqd_event(connected)
        elif event_type == "profiles_changed":
            self._apply_profile_runtime_state(event)
            self._queue_profile_reload()
        elif event_type == "recording_started":
            self._close_dialogs_for_recording_start()
            self._recording_overlay.set_visible(True)
            self._recording_overlay.on_started(event)
        elif event_type == "recording_stopped":
            self._recording_overlay.on_stopped()
            self._recording_overlay.set_visible(False)
            self._on_recording_stopped(event)
        elif event_type == "recording_progress":
            self._recording_overlay.on_progress(event)
        elif event_type == "recording_auth_requested":
            self.present_recording_settings_dialog(reason="recording_locked")
        elif event_type == "macro_save_pending":
            self.present_pending_macro_save_dialog()

        callbacks = self._event_handlers.get(event_type)
        if callbacks is None:
            return
        for cb in list(callbacks):
            cb(event)

    def _on_recording_stopped(self, event: dict) -> None:
        from keymasq.gui.widgets.save_macro_dialog import SaveMacroDialog

        if self._save_macro_dialog is not None:
            self._save_macro_dialog.present(self)
            return

        dialog = SaveMacroDialog(self, event)
        dialog.connect("closed", self._on_save_macro_dialog_closed)
        self._save_macro_dialog = dialog
        dialog.present(self)

    def present_pending_macro_save_dialog(self) -> bool:
        if self._save_macro_dialog is None:
            return False
        self._save_macro_dialog.present(self)
        return True

    def _on_save_macro_dialog_closed(self, dialog) -> None:
        if dialog is self._save_macro_dialog:
            self._save_macro_dialog = None

    def _close_dialogs_for_recording_start(self) -> None:
        for dialog in (self._record_macro_dialog, self._macro_manager_dialog):
            if dialog is not None:
                dialog.close()

    def set_macro_manager_dialog(self, dialog: Adw.Dialog | None) -> None:
        self._macro_manager_dialog = dialog

    def _update_status_from_keymasqd_event(self, keymasqd_connected: bool) -> None:
        if keymasqd_connected:
            self.session_status.set_label("session: 🟢")
            self.keymasqd_status.set_label("keymasqd: 🟢")
            self._set_connection_issue(None)
        else:
            self.session_status.set_label("session: 🟡")
            self.keymasqd_status.set_label("keymasqd: 🔴")
            self._set_connection_issue("keymasqd")

    def _update_status_from_session(self) -> None:
        if self._status_query_inflight:
            return

        self._status_query_inflight = True
        self._status_query_id += 1
        query_id = self._status_query_id
        session_request_async(
            {"command": "get_status"},
            lambda data, qid=query_id: self._on_status_response(data, qid),
            timeout=2.0,
        )

    def _on_status_response(self, data: dict | None, query_id: int) -> bool:
        if self._destroyed:
            return False
        if query_id != self._status_query_id:
            return False

        self._status_query_inflight = False

        try:
            session_ok = bool(data and data.get("status") == "ok")
            keymasqd_ok = bool(data and data.get("keymasqd_connected") is True)
            self._update_unlock_state(data if isinstance(data, dict) else None)
            self._update_compositor_dispatch_state(data if isinstance(data, dict) else None)
            if isinstance(data, dict) and data.get("status") == "ok":
                self._apply_profile_runtime_state(data)
                compositor_id = data.get("compositor_id")
                if compositor_id is not None:
                    self._compositor_id = compositor_id
                details = data.get("compositor_details")
                if isinstance(details, dict):
                    self._compositor_support_details = details
                    self._compositor_supported = bool(details.get("supported", False))
                self._update_compositor_warning_banner()
                self._update_compositor_status()
                self._close_gnome_setup_dialog_if_ready()

            if session_ok:
                if keymasqd_ok:
                    self.session_status.set_label("session: 🟢")
                    self.keymasqd_status.set_label("keymasqd: 🟢")
                    self._set_connection_issue(None)
                else:
                    self.session_status.set_label("session: 🟡")
                    self.keymasqd_status.set_label("keymasqd: 🔴")
                    self._set_connection_issue("keymasqd")
            else:
                self._update_status_disconnected()
        except Exception:
            self._update_unlock_state(None)
            self._update_status_disconnected()

        return False

    def _update_status_disconnected(self) -> None:
        self.session_status.set_label("session: 🔴")
        self.keymasqd_status.set_label("keymasqd: ⚪")
        self._update_compositor_dispatch_state(None)
        self._set_connection_issue("session")

    def _update_compositor_dispatch_state(self, status_data: dict | None) -> None:
        if isinstance(status_data, dict) and status_data.get("status") == "ok":
            self._listener_name = str(status_data.get("listener_name", "") or "")
            self._compositor_dispatch_available = bool(
                status_data.get("compositor_dispatch_available", False)
            )
            compositor_id = status_data.get("compositor_id")
            if compositor_id is not None:
                self._compositor_id = compositor_id
            return

        self._listener_name = ""
        self._compositor_dispatch_available = False

    def get_compositor_action_status(self) -> dict[str, object]:
        return {
            "compositor_id": self._compositor_id,
            "listener_name": self._listener_name,
            "compositor_dispatch_available": self._compositor_dispatch_available,
        }

    def _update_unlock_state(self, status_data: dict | None) -> None:
        unlocked = None
        unlock_required = None
        source = None
        expires_at = None
        refresh_owner = None

        if isinstance(status_data, dict) and status_data.get("status") == "ok":
            unlocked = status_data.get("recording_unlocked")
            unlock_required = status_data.get("recording_unlock_required")
            source = status_data.get("recording_unlock_source")
            expires_at = status_data.get("recording_unlock_expires_at")
            refresh_owner = status_data.get("recording_refresh_owner")
            self._emergency_cancel_combo_enabled = bool(
                status_data.get("emergency_cancel_combo_enabled", True)
            )

        if unlocked is None or unlock_required is None or source is None or expires_at is None:
            local_status = resolve_unlock_status(os.getuid())
            unlocked = local_status.get("unlocked", False)
            unlock_required = True
            source = local_status.get("source", "none")
            expires_at = local_status.get("expires_at", 0)
            refresh_owner = bool(self._recording_refresh_lease_id)

        raw_unlocked = bool(unlocked)
        self._recording_unlock_required = bool(unlock_required)
        self._recording_unlocked = raw_unlocked or not self._recording_unlock_required
        self._recording_unlock_source = str(source or "none")
        self._recording_unlock_expires_at = int(expires_at or 0)
        self._recording_refresh_owner = (
            bool(refresh_owner) if self._recording_unlock_required else False
        )

        log.debug(
            (
                "Unlock status updated: unlocked=%s required=%s source=%s "
                "expires_at=%s owner=%s lease_claimed=%s"
            ),
            self._recording_unlocked,
            self._recording_unlock_required,
            self._recording_unlock_source,
            self._recording_unlock_expires_at,
            self._recording_refresh_owner,
            bool(self._recording_refresh_lease_id),
        )

        if not self._recording_unlocked:
            self._recording_refresh_lease_id = ""
            self._recording_claim_attempt_key = None
        elif not self._recording_unlock_required:
            self._recording_refresh_lease_id = ""
            self._recording_claim_attempt_key = None
        elif not self._recording_refresh_lease_id and raw_unlocked:
            claim_key = (self._recording_unlock_source, self._recording_unlock_expires_at)
            if self._recording_claim_attempt_key != claim_key:
                self._recording_claim_attempt_key = claim_key
                self._request_recording_refresh_lease()

        self._refresh_macro_menu_state()
        self._refresh_unlock_status_label()

    def emergency_cancel_combo_enabled(self) -> bool:
        return bool(self._emergency_cancel_combo_enabled)

    def _set_connection_issue(self, issue: str | None) -> None:
        if self.demo_mode:
            return

        if issue is None:
            self._connection_issue = None
            if self._connection_dialog:
                self._connection_dialog.close()
            return

        if issue == self._connection_issue and self._connection_dialog:
            return

        self._connection_issue = issue

        if issue == "session":
            title = "Cannot Reach keymasq-session"
            body = (
                "The GUI is disconnected from keymasq-session.\n\n"
                "Start or restart the user service and try again:\n"
                "systemctl --user restart keymasq-session"
            )
        else:
            title = "keymasqd Is Not Connected"
            body = (
                "keymasq-session is running, but it cannot reach keymasqd.\n\n"
                "Start or restart the system service and try again:\n"
                "sudo systemctl restart keymasqd"
            )

        if not self._connection_dialog:
            dialog = Adw.Dialog(title="Connection Error", content_width=560, content_height=-1)
            if hasattr(dialog, "set_modal"):
                dialog.set_modal(True)

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            box.set_margin_top(20)
            box.set_margin_bottom(20)
            box.set_margin_start(20)
            box.set_margin_end(20)

            title_label = Gtk.Label()
            title_label.add_css_class("title-2")
            title_label.set_halign(Gtk.Align.START)
            box.append(title_label)

            body_label = Gtk.Label()
            body_label.set_halign(Gtk.Align.START)
            body_label.set_wrap(True)
            box.append(body_label)

            btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            btn_row.set_halign(Gtk.Align.END)

            quit_btn = Gtk.Button(label="Quit")
            quit_btn.connect("clicked", self._on_quit_clicked)
            btn_row.append(quit_btn)

            retry_btn = Gtk.Button(label="Retry")
            retry_btn.add_css_class("suggested-action")
            retry_btn.connect("clicked", self._on_retry_connection_clicked)
            btn_row.append(retry_btn)

            box.append(btn_row)
            dialog.set_child(box)
            dialog.connect("closed", self._on_connection_dialog_closed)

            self._connection_dialog = dialog
            self._connection_title_label = title_label
            self._connection_body_label = body_label

        if self._connection_title_label:
            self._connection_title_label.set_text(f"⚠️ {title}")
        if self._connection_body_label:
            self._connection_body_label.set_text(body)

        dialog = self._connection_dialog
        if dialog is not None:
            dialog.present(self)

    def _on_connection_dialog_closed(self, dialog) -> None:
        if dialog is self._connection_dialog:
            self._connection_dialog = None
            self._connection_title_label = None
            self._connection_body_label = None

    def _retry_connection_check(self) -> None:
        self._update_status_from_session()

    def _on_destroy(self, *_args) -> None:
        self._destroyed = True
        self._session_reconnect_source_id = self._remove_timeout_source(
            self._session_reconnect_source_id
        )
        self._unlock_refresh_source_id = self._remove_timeout_source(
            self._unlock_refresh_source_id
        )
        self._gnome_setup_poll_source_id = self._remove_timeout_source(
            self._gnome_setup_poll_source_id
        )
        for inspector_window in list(self._device_inspector_windows.values()):
            inspector_window.close()
        self._device_inspector_windows.clear()

        if self.demo_mode:
            return

        if self._recording_refresh_lease_id:
            try:
                log.info("Window closing: locking runtime recording unlock lease")
                result = session_request(
                    {
                        "command": "lock_recording_unlock",
                        "lease_id": self._recording_refresh_lease_id,
                    },
                    timeout=1.5,
                )
                if not result or result.get("status") != "ok":
                    log.warning(
                        "Failed to lock runtime unlock on close: error_code=%s message=%s",
                        (result or {}).get("error_code"),
                        (result or {}).get("message"),
                    )
                else:
                    log.info("Runtime unlock lease locked on close")
            except Exception:
                pass

        unregister_session_event_callback("*", self._on_session_event)

    def _remove_timeout_source(self, source_id: int) -> int:
        if source_id <= 0:
            return 0
        GLib.source_remove(source_id)
        return 0

    def _setup_content(self) -> None:
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self.tab_view = Adw.TabView()
        self.tab_view.set_vexpand(True)
        self.tab_view.set_shortcuts(
            Adw.TabViewShortcuts.CONTROL_TAB
            | Adw.TabViewShortcuts.CONTROL_SHIFT_TAB
            | Adw.TabViewShortcuts.CONTROL_PAGE_UP
            | Adw.TabViewShortcuts.CONTROL_PAGE_DOWN
        )
        self.tab_view.connect("notify::selected-page", self._on_selected_tab_changed)
        self.tab_view.connect("close-page", self._on_tab_close_page)
        self.tab_view.connect("page-reordered", self._on_tab_page_reordered)

        self.tab_bar = Adw.TabBar()
        self.tab_bar.set_view(self.tab_view)
        self.tab_bar.set_autohide(False)
        self.tab_bar.set_expand_tabs(False)
        self.tab_bar.set_hexpand(True)
        self.tab_bar.set_halign(Gtk.Align.FILL)

        add_device_button = Gtk.Button(icon_name="list-add-symbolic")
        add_device_button.set_tooltip_text("Add device")
        add_device_button.connect("clicked", self._on_add_device_clicked)
        self.tab_bar.set_start_action_widget(add_device_button)

        header = Gtk.WindowHandle()
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        header_box.add_css_class("titlebar")
        header_box.add_css_class("keymasq-main-header")
        header_box.set_hexpand(True)

        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name("open-menu-symbolic")
        menu_button.add_css_class("flat")

        menu_popover = Gtk.Popover()
        menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        menu_box.set_margin_top(6)
        menu_box.set_margin_bottom(6)
        menu_box.set_margin_start(6)
        menu_box.set_margin_end(6)

        appearance_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        appearance_box.add_css_class("linked")
        appearance_box.set_margin_bottom(6)
        appearance_group: Gtk.ToggleButton | None = None
        appearance_options: tuple[tuple[AppearanceMode, str], ...] = (
            ("system", "System"),
            ("light", "Light"),
            ("dark", "Dark"),
        )
        for mode, label in appearance_options:
            button = Gtk.ToggleButton(label=label)
            button.set_hexpand(True)
            if appearance_group is not None:
                button.set_group(appearance_group)
            else:
                appearance_group = button
            button.connect("toggled", self._on_appearance_mode_toggled, mode)
            self._appearance_buttons[mode] = button
            appearance_box.append(button)

        current_appearance = load_appearance_mode()
        self._syncing_appearance = True
        self._appearance_buttons[current_appearance].set_active(True)
        self._syncing_appearance = False
        menu_box.append(appearance_box)
        menu_box.append(self._create_menu_separator())

        combos_btn = Gtk.Button(label="Combos")
        self._configure_menu_button(combos_btn)
        combos_btn.connect("clicked", self._on_combos_menu_clicked, menu_popover)
        menu_box.append(combos_btn)

        superkeys_btn = Gtk.Button(label="Super Keys")
        self._configure_menu_button(superkeys_btn)
        superkeys_btn.connect("clicked", self._on_menu_action_clicked, "superkeys", menu_popover)
        menu_box.append(superkeys_btn)

        macros_btn = Gtk.Button(label="Macros")
        self._configure_menu_button(macros_btn)
        macros_btn.connect("clicked", self._on_menu_action_clicked, "macros", menu_popover)
        menu_box.append(macros_btn)

        analog_controls_btn = Gtk.Button(label="Analog Controls")
        self._configure_menu_button(analog_controls_btn)
        analog_controls_btn.connect(
            "clicked",
            self._on_menu_action_clicked,
            "analog-controls",
            menu_popover,
        )
        menu_box.append(analog_controls_btn)

        diagnostics_btn = Gtk.Button(label="Diagnostics")
        self._configure_menu_button(diagnostics_btn)
        diagnostics_btn.connect(
            "clicked",
            self._on_menu_action_clicked,
            "diagnostics",
            menu_popover,
        )
        menu_box.append(diagnostics_btn)

        settings_btn = Gtk.Button(label="Settings")
        self._configure_menu_button(settings_btn)
        settings_btn.connect(
            "clicked",
            self._on_menu_action_clicked,
            "settings",
            menu_popover,
        )
        menu_box.append(settings_btn)

        menu_box.append(self._create_menu_separator())

        menu_unlock_btn = Gtk.Button(label="Unlock Recording")
        self._configure_menu_button(menu_unlock_btn)
        menu_unlock_btn.set_tooltip_text(
            "Authorize raw original-input capture for adding inputs, combo capture, "
            "and live macro recording. Uses Polkit and stays tied to this GUI session."
        )
        menu_unlock_btn.connect("clicked", self._on_menu_unlock_clicked, menu_popover)
        menu_box.append(menu_unlock_btn)
        self._menu_unlock_btn = menu_unlock_btn

        menu_unlock_separator = self._create_menu_separator()
        menu_box.append(menu_unlock_separator)
        self._menu_unlock_separator = menu_unlock_separator

        feedback_btn = Gtk.Button(label="Feedback")
        self._configure_menu_button(feedback_btn)
        feedback_btn.connect("clicked", self._on_menu_action_clicked, "feedback", menu_popover)
        menu_box.append(feedback_btn)

        about_btn = Gtk.Button(label="About")
        self._configure_menu_button(about_btn)
        about_btn.connect("clicked", self._on_menu_action_clicked, "about", menu_popover)
        menu_box.append(about_btn)

        quit_btn = Gtk.Button(label="Quit")
        self._configure_menu_button(quit_btn)
        quit_btn.connect("clicked", self._on_menu_action_clicked, "quit", menu_popover)
        menu_box.append(quit_btn)

        menu_popover.set_child(menu_box)
        menu_button.set_popover(menu_popover)

        if self.demo_mode:
            demo_label = Gtk.Label(label="DEMO MODE")
            demo_label.add_css_class("error")
            header_box.append(demo_label)

        header_box.append(self.tab_bar)
        header_box.append(menu_button)

        window_controls = Gtk.WindowControls(side=Gtk.PackType.END)
        header_box.append(window_controls)
        header.set_child(header_box)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self.warning_banner = Adw.Banner()
        self.warning_banner.set_visible(False)
        self.warning_banner.set_revealed(False)
        content_box.append(self.warning_banner)

        content_box.append(self.tab_view)

        from keymasq.gui.widgets.recording_overlay import RecordingOverlay

        self._recording_overlay = RecordingOverlay(self)
        self._recording_overlay.set_halign(Gtk.Align.FILL)
        self._recording_overlay.set_valign(Gtk.Align.FILL)
        self._recording_overlay.set_visible(False)

        content_overlay = Gtk.Overlay()
        content_overlay.set_child(content_box)
        content_overlay.add_overlay(self._recording_overlay)

        toolbar.set_content(content_overlay)

        self.status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.status_bar.set_margin_top(6)
        self.status_bar.set_margin_bottom(6)
        self.status_bar.set_margin_start(12)
        self.status_bar.set_margin_end(12)

        self.keymasqd_status = Gtk.Label(label="keymasqd: ⚪")
        self.keymasqd_status.add_css_class("caption")
        self.keymasqd_status.set_tooltip_text(
            "keymasqd status (via session):\n"
            "🟢 Running\n"
            "🔴 Not running\n"
            "⚪ Unknown (session not connected)"
        )
        self.status_bar.append(self.keymasqd_status)

        self.session_status = Gtk.Label(label="session: ⚪")
        self.session_status.add_css_class("caption")
        self.session_status.set_tooltip_text(
            "keymasq-session status:\n"
            "🟢 Running and connected to keymasqd\n"
            "🟡 Running but NOT connected to keymasqd\n"
            "🔴 Not running"
        )
        self.status_bar.append(self.session_status)

        self.compositor_status = Gtk.Label()
        self.compositor_status.add_css_class("caption")
        compositor_click = Gtk.GestureClick()
        compositor_click.connect("released", self._on_compositor_status_released)
        self.compositor_status.add_controller(compositor_click)
        self._update_compositor_status()
        self.status_bar.append(self.compositor_status)

        status_spacer = Gtk.Box()
        status_spacer.set_hexpand(True)
        self.status_bar.append(status_spacer)

        unlock_status_label = Gtk.Label(label="")
        unlock_status_label.add_css_class("caption")
        unlock_status_label.set_halign(Gtk.Align.END)
        unlock_status_label.set_visible(False)
        self.status_bar.append(unlock_status_label)
        self._unlock_status_label = unlock_status_label

        toolbar.add_bottom_bar(self.status_bar)

        self.set_content(toolbar)

        self._setup_placeholder()
        self._setup_combo_tab()
        self._update_unlock_state(None)

    def _create_menu_separator(self) -> Gtk.Widget:
        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        separator.set_margin_top(9)
        separator.set_margin_bottom(9)
        separator.set_margin_start(4)
        separator.set_margin_end(4)
        separator.set_size_request(-1, 2)
        return separator

    def _configure_menu_button(self, button: Gtk.Button) -> None:
        button.set_halign(Gtk.Align.FILL)
        button.set_margin_top(2)
        button.set_margin_bottom(2)

    def _on_appearance_mode_toggled(
        self,
        button: Gtk.ToggleButton,
        mode: AppearanceMode,
    ) -> None:
        if self._syncing_appearance or not button.get_active():
            return

        app = self.get_application()
        apply_appearance_mode = getattr(app, "apply_appearance_mode", None)
        if callable(apply_appearance_mode):
            apply_appearance_mode(mode)

    def _on_menu_action_clicked(
        self,
        _button: Gtk.Button,
        action_name: str,
        popover: Gtk.Popover,
    ) -> None:
        popover.popdown()
        app = self.get_application()
        if app is None:
            return
        app.activate_action(action_name, None)

    def _on_combos_menu_clicked(self, _button: Gtk.Button, popover: Gtk.Popover) -> None:
        popover.popdown()
        self.show_combo_tab()

    def _on_menu_unlock_clicked(self, _button: Gtk.Button, popover: Gtk.Popover) -> None:
        popover.popdown()
        self.present_unlock_dialog()

    def _setup_placeholder(self) -> None:
        self._create_placeholder_widget(
            title_text="Loading devices...",
            subtitle_text="Checking compositor support and loading saved hardware",
        )
        self._ensure_placeholder_page()

    def _create_placeholder_widget(self, *, title_text: str, subtitle_text: str) -> None:
        self.placeholder = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            valign=Gtk.Align.CENTER,
            halign=Gtk.Align.CENTER,
            spacing=12,
        )

        icon = image_from_icon_names(*device_icon_names(False), pixel_size=96)
        icon.add_css_class("dim-label")
        self.placeholder.append(icon)

        title = Gtk.Label(label=title_text)
        title.add_css_class("title-1")
        self.placeholder.append(title)
        self._placeholder_title = title

        subtitle = Gtk.Label(label=subtitle_text)
        subtitle.add_css_class("dim-label")
        self.placeholder.append(subtitle)
        self._placeholder_subtitle = subtitle

    def _ensure_placeholder_page(self) -> None:
        if self._page_for_child(self.placeholder) is not None:
            return
        if self.placeholder.get_parent() is not None:
            self._create_placeholder_widget(
                title_text="No devices configured",
                subtitle_text="Click + to add a new device",
            )
        self._placeholder_page = self._append_tab_page(
            self.placeholder,
            title="Welcome",
            icon_name=resolve_icon_name(*device_icon_names(False)),
        )

    def _set_empty_placeholder_state(self) -> None:
        if self._placeholder_title is not None:
            self._placeholder_title.set_label("No devices configured")
        if self._placeholder_subtitle is not None:
            self._placeholder_subtitle.set_label("Click + to add a new device")

    def _apply_loaded_devices(self, devices: list[HardwareConfig]) -> None:
        if self.demo_mode and not devices:
            self._load_demo_devices()
            return

        if not devices:
            self._set_empty_placeholder_state()
            return

        self._close_tab_page(self._page_for_child(self.placeholder))
        self._placeholder_page = None

        self._suppress_tab_layout_save = True
        try:
            for device in self._order_devices_for_tabs(devices):
                if device.hardware_id in self._device_pages:
                    continue
                self._add_device_tab(device, persist_order=False)
            self._reorder_visible_pages_to_saved_order()
        finally:
            self._suppress_tab_layout_save = False

    def _load_demo_devices(self) -> None:
        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig

        demo_device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Demo Mouse",
            evdev_devices=[
                EvdevDevice(path="/dev/input/event0", device_type=DeviceType.MOUSE),
            ],
            buttons=[
                ButtonDefinition(id="btn_left", label="Left Click", evdev="btn_left", zone="left"),
                ButtonDefinition(
                    id="btn_right", label="Right Click", evdev="btn_right", zone="right"
                ),
                ButtonDefinition(
                    id="btn_middle", label="Middle Click", evdev="btn_middle", zone="wheel"
                ),
                ButtonDefinition(id="btn_back", label="Back", evdev="btn_side", zone="thumb"),
                ButtonDefinition(
                    id="btn_forward", label="Forward", evdev="btn_extra", zone="thumb"
                ),
            ],
        )

        self._close_tab_page(self._page_for_child(self.placeholder))
        self._placeholder_page = None
        if demo_device.hardware_id not in self._device_pages:
            self._add_device_tab(demo_device, persist_order=False)
        self._reorder_visible_pages_to_saved_order()

    def _setup_combo_tab(self) -> None:
        if _COMBO_TAB_ID in self._hidden_tabs:
            return
        self._ensure_combo_tab_page()

    def _ensure_combo_tab_page(self) -> Adw.TabPage | None:
        if self._combo_page is not None:
            return self._combo_page

        self.combo_tab = ComboTab(
            profile_manager=self.profile_manager,
            main_window=self,
            demo_mode=self.demo_mode,
            compositor_capabilities=self._compositor_capabilities,
        )
        self._combo_page = self._append_tab_page(
            self.combo_tab,
            title="Combos",
            icon_name=resolve_icon_name(*combo_icon_names()),
        )
        if self._selected_profile_name:
            self.combo_tab.refresh_profiles(
                preferred_profile_name=self._selected_profile_name,
                publish_selection=False,
            )
        self._apply_profile_runtime_state_to_widget(self.combo_tab)
        self._reorder_visible_pages_to_saved_order()
        return self._combo_page

    def show_combo_tab(self) -> None:
        self._hidden_tabs.discard(_COMBO_TAB_ID)
        page = self._ensure_combo_tab_page()
        if page is None:
            return
        self._save_tab_layout()
        self.tab_view.set_selected_page(page)

    def _add_device_tab(self, device: HardwareConfig, *, persist_order: bool = True) -> None:
        if self._placeholder_page is not None:
            self._close_tab_page(self._placeholder_page)
            self._placeholder_page = None

        tab = DeviceTab(
            device=device,
            profile_manager=self.profile_manager,
            hardware_manager=self.hardware_manager,
            main_window=self,
            demo_mode=self.demo_mode,
            compositor_capabilities=self._compositor_capabilities,
        )

        icon = resolve_icon_name(*device_icon_names(device_kind=tab.device_layout_kind()))
        page = self._append_tab_page(tab, title=device.name, icon_name=icon)
        self._device_pages[device.hardware_id] = page
        if self._selected_profile_name:
            tab.refresh_profiles(
                preferred_profile_name=self._selected_profile_name,
                publish_selection=False,
            )
        self._apply_profile_runtime_state_to_widget(tab)
        if self.combo_tab is not None:
            self.combo_tab.refresh_profiles(
                preferred_profile_name=self._selected_profile_name,
                publish_selection=False,
            )
        self._reorder_visible_pages_to_saved_order()
        if persist_order:
            self._save_tab_layout()

    def update_device_display_name(self, hardware_id: str, name: str) -> None:
        page = self._page_for_hardware_id(hardware_id)
        if page is not None:
            page.set_title(name)

    def open_device_inspector(self, device) -> None:
        hardware_id = str(getattr(device, "hardware_id", "") or "").strip()
        if not hardware_id:
            return
        existing = self._device_inspector_windows.get(hardware_id)
        if existing is not None:
            if bool(getattr(existing, "_closing", False)):
                self._device_inspector_windows.pop(hardware_id, None)
            else:
                existing.present()
                return

        if self.demo_mode:
            self._show_demo_notification("Device inspector is not available in demo mode")
            return

        if not self._device_inspector_unlock_ready():
            self.present_unlock_dialog(on_success=lambda: self.open_device_inspector(device))
            return

        from keymasq.gui.widgets.device_inspector_window import DeviceInspectorWindow

        inspector = DeviceInspectorWindow(self, device)
        self._device_inspector_windows[hardware_id] = inspector

        def on_close_request(window: Gtk.Window) -> bool:
            if self._device_inspector_windows.get(hardware_id) is window:
                self._device_inspector_windows.pop(hardware_id, None)
            return False

        def on_destroy(window: Gtk.Window) -> None:
            if self._device_inspector_windows.get(hardware_id) is window:
                self._device_inspector_windows.pop(hardware_id, None)

        inspector.connect("close-request", on_close_request)
        inspector.connect("destroy", on_destroy)
        inspector.present()

    def _device_inspector_unlock_ready(self) -> bool:
        if not self._recording_unlock_required:
            return True
        return self._recording_unlocked and self._recording_refresh_owner

    def remove_device_tab(self, hardware_id: str) -> None:
        page = self._device_pages.pop(hardware_id, None)
        self._close_tab_page(page)
        self._save_tab_layout()
        self._check_empty_state()

    def _queue_profile_reload(self) -> None:
        if self._destroyed:
            return
        if self._profile_reload_inflight:
            self._profile_reload_pending = True
            return

        self._profile_reload_inflight = True
        run_gui_task(
            self._load_profile_manager_snapshot,
            self._on_profile_reload_finished,
        )

    def _load_profile_manager_snapshot(self) -> ProfileManager:
        return ProfileManager(auto_create_default_if_empty=True)

    def _on_profile_reload_finished(self, result: GuiTaskResult[ProfileManager]) -> bool:
        self._profile_reload_inflight = False
        rerun = self._profile_reload_pending
        self._profile_reload_pending = False

        if not self._destroyed and result.ok and isinstance(result.value, ProfileManager):
            self._set_profile_manager(result.value)
            self._refresh_device_tabs()
            self._apply_profile_runtime_state(self._profile_runtime_state)

        if rerun:
            self._queue_profile_reload()
        return False

    def _set_profile_manager(self, profile_manager: ProfileManager) -> None:
        from keymasq.gui.widgets.profile_managed_tab import ProfileManagedTab

        self.profile_manager = profile_manager
        for child in self._iter_profile_tabs():
            if isinstance(child, ProfileManagedTab):
                child.profile_manager = profile_manager

    def _normalize_profile_runtime_state(self, state: dict | None) -> dict[str, object]:
        if not isinstance(state, dict):
            return dict(self._profile_runtime_state)

        normalized = dict(self._profile_runtime_state)
        if "active_profiles" in state:
            active_profiles_raw = state.get("active_profiles")
            normalized["active_profiles"] = (
                [str(name) for name in active_profiles_raw]
                if isinstance(active_profiles_raw, list)
                else []
            )
        if "devices" in state:
            devices_raw = state.get("devices")
            normalized["devices"] = devices_raw if isinstance(devices_raw, dict) else {}
        if "window" in state:
            window_raw = state.get("window")
            normalized["window"] = window_raw if isinstance(window_raw, dict) else {}
        return normalized

    def _apply_profile_runtime_state_to_widget(self, widget: Gtk.Widget | None) -> None:
        from keymasq.gui.widgets.profile_managed_tab import ProfileManagedTab

        if widget is None or not isinstance(widget, ProfileManagedTab):
            return
        widget.apply_active_profile_response(self._profile_runtime_state)

    def _apply_profile_runtime_state(self, state: dict | None) -> None:
        self._profile_runtime_state = self._normalize_profile_runtime_state(state)
        for child in self._iter_profile_tabs():
            self._apply_profile_runtime_state_to_widget(child)

    def _on_add_device(self, button: Gtk.Button) -> None:
        if self.demo_mode:
            self._show_demo_notification("Device setup not available in demo mode")
            return

        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        dialog = HardwareSetupDialog(self, self.hardware_manager)
        dialog.connect("device-created", self._on_device_created)
        dialog.present(self)

    def _on_add_device_clicked(self, _button: Gtk.Button) -> None:
        self._on_add_device(_button)

    def present_unlock_dialog(self, on_success=None) -> None:
        self._start_recording_unlock(on_success=on_success)

    def _on_quit_clicked(self, _button: Gtk.Button) -> None:
        self.get_application().quit()

    def _on_retry_connection_clicked(self, _button: Gtk.Button) -> None:
        self._retry_connection_check()

    def _start_recording_unlock(self, on_success=None) -> None:
        if self._unlock_request_inflight:
            return

        if self.demo_mode:
            self._show_unlock_error_dialog("Recording unlock is unavailable in demo mode.")
            return

        self._unlock_request_inflight = True
        if self._menu_unlock_btn is not None:
            self._menu_unlock_btn.set_sensitive(False)

        def worker() -> tuple[str, dict | None]:
            error_msg = ""
            claim_response: dict | None = None
            uid = os.getuid()
            helper_path = resolve_keymasq_record_helper_path()
            if helper_path is None:
                return f"Recording helper not found at {KEYMASQ_RECORD_HELPER_PATH}", None

            runtime_cmd = [
                "pkexec",
                helper_path,
                "unlock-runtime",
                "--uid",
                str(uid),
                "--ttl",
                "60",
            ]
            runtime_completed = subprocess.run(runtime_cmd, capture_output=True, text=True)
            if runtime_completed.returncode != 0:
                error_msg = (
                    runtime_completed.stderr.strip()
                    or runtime_completed.stdout.strip()
                    or "Authorization failed"
                )

            if not error_msg:
                claim_response = session_request(
                    {"command": "claim_recording_unlock_refresh"},
                    timeout=3.0,
                )
                if not isinstance(claim_response, dict):
                    error_msg = (
                        "Authorization succeeded, but keymasq-session did not grant "
                        "the recording unlock lease."
                    )
                elif claim_response.get("status") != "ok":
                    error_msg = str(
                        claim_response.get("message")
                        or "keymasq-session did not grant the recording unlock lease."
                    )

            return error_msg, claim_response

        def on_worker_done(result: GuiTaskResult[tuple[str, dict | None]]) -> bool:
            success = result.ok and isinstance(result.value, tuple) and result.value[0] == ""
            error_msg = (
                result.value[0]
                if result.ok and isinstance(result.value, tuple)
                else "Authorization failed"
            )
            claim_response = (
                result.value[1] if result.ok and isinstance(result.value, tuple) else None
            )
            return self._on_unlock_finished(
                success,
                error_msg,
                on_success,
                claim_response,
            )

        run_gui_task(
            worker,
            on_worker_done,
        )

    def _on_unlock_finished(
        self,
        success: bool,
        error_msg: str,
        on_success,
        claim_response: dict | None = None,
    ) -> bool:
        self._unlock_request_inflight = False
        if self._menu_unlock_btn is not None:
            self._menu_unlock_btn.set_sensitive(True)

        if success:
            lease_id = ""
            if isinstance(claim_response, dict) and claim_response.get("status") == "ok":
                lease_id = str(claim_response.get("lease_id", "") or "").strip()

            if lease_id:
                self._recording_refresh_lease_id = lease_id
                self._recording_claim_attempt_key = None
                self._update_unlock_state(claim_response)
            else:
                self._update_unlock_state(None)
                self._request_recording_refresh_lease()
            if callable(on_success):
                on_success()
            return False

        self._show_unlock_error_dialog(error_msg or "Authorization failed")
        return False

    def _show_unlock_error_dialog(self, message: str) -> None:
        dialog = Adw.AlertDialog(
            heading="Unlock Failed",
            body=(
                message
                or "Keymasq could not authorize raw original-input capture for this GUI."
            ),
        )
        dialog.add_response("ok", "OK")
        dialog.present(self)

    def _refresh_macro_menu_state(self) -> None:
        if self._menu_unlock_btn is not None:
            self._menu_unlock_btn.set_visible(not self._recording_unlocked)
        if self._menu_unlock_separator is not None:
            self._menu_unlock_separator.set_visible(not self._recording_unlocked)

        app = self.get_application()
        if app is not None:
            action = app.lookup_action("record-macro")
            if action is not None:
                action.set_enabled(self._recording_unlocked)

    def _refresh_unlock_status_label(self) -> None:
        if self._unlock_status_label is None:
            return

        show_unlock_status = self._recording_unlock_required and self._recording_unlocked
        self._unlock_status_label.set_visible(show_unlock_status)
        if not show_unlock_status:
            self._unlock_status_label.set_label("")
            self._unlock_status_label.set_tooltip_text(None)
            return

        if self._recording_unlock_source == "runtime":
            text = "capture: 🟢 runtime unlock"
        elif self._recording_unlock_source == "persistent":
            text = "capture: 🟢 persistent unlock"
        else:
            text = "capture: 🟢 unlocked"

        owner_text = "owner=yes" if self._recording_refresh_owner else "owner=no"
        lease_text = "lease=claimed" if self._recording_refresh_lease_id else "lease=none"
        tooltip = (
            f"{owner_text}\n"
            f"{lease_text}\n"
            f"source={self._recording_unlock_source}\n"
            f"expires_at={self._recording_unlock_expires_at}"
        )
        self._unlock_status_label.set_label(text)
        self._unlock_status_label.set_tooltip_text(tooltip)

    def present_recording_settings_dialog(self, reason: str = "settings") -> None:
        if self._record_macro_dialog is not None:
            self._record_macro_dialog.set_presentation_reason(reason)
            self._record_macro_dialog.present(self)
            return

        from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

        dialog = RecordMacroDialog(self, reason=reason)
        dialog.connect("closed", self._on_record_macro_dialog_closed)
        self._record_macro_dialog = dialog
        dialog.present(self)

    def _on_record_macro_dialog_closed(self, dialog) -> None:
        if dialog is self._record_macro_dialog:
            self._record_macro_dialog = None

    def _refresh_unlock_lease(self) -> bool:
        if self.demo_mode:
            return False

        if not self._recording_unlock_required:
            return True

        if not self._recording_unlocked:
            return True

        if self._recording_unlock_source != "runtime":
            return True

        if self._unlock_refresh_inflight:
            return True

        if not self._recording_refresh_lease_id:
            self._request_recording_refresh_lease()
            if not self._recording_refresh_lease_id:
                return True

        self._unlock_refresh_inflight = True

        session_request_with_hooks(
            {
                "command": "refresh_recording_unlock",
                "lease_id": self._recording_refresh_lease_id,
            },
            self._on_refresh_unlock_finished,
            timeout=3.0,
        )
        return True

    def _on_refresh_unlock_finished(self, result: dict | None) -> bool:
        self._unlock_refresh_inflight = False

        if result and result.get("status") == "ok":
            log.debug("Runtime unlock refresh succeeded")
            self._update_unlock_state(result)
            return False

        if result:
            log.warning(
                "Runtime unlock refresh failed: error_code=%s message=%s",
                result.get("error_code"),
                result.get("message"),
            )
        else:
            log.warning("Runtime unlock refresh failed: no response from session")
        self._recording_refresh_lease_id = ""
        self._update_unlock_state(None)
        return False

    def _request_recording_refresh_lease(self) -> None:
        if self.demo_mode:
            return

        if not self._recording_unlock_required:
            return

        if not self._recording_unlocked:
            return

        if self._lease_claim_inflight:
            return

        log.debug("Requesting recording refresh lease from session")
        self._lease_claim_inflight = True
        session_request_with_hooks(
            {"command": "claim_recording_unlock_refresh"},
            self._on_claim_recording_refresh_lease_finished,
            timeout=3.0,
            on_done=self._on_claim_recording_refresh_lease_done,
        )

    def _on_claim_recording_refresh_lease_done(self) -> None:
        self._lease_claim_inflight = False

    def _on_claim_recording_refresh_lease_finished(self, response: dict | None) -> bool:
        if not response or response.get("status") != "ok":
            if response:
                log.warning(
                    "Recording refresh lease claim failed: error_code=%s message=%s",
                    response.get("error_code"),
                    response.get("message"),
                )
            else:
                log.warning("Recording refresh lease claim failed: no response from session")
            self._recording_refresh_lease_id = ""
            return False

        lease_id = str(response.get("lease_id", "") or "").strip()
        if not lease_id:
            log.warning("Recording refresh lease claim failed: missing lease_id")
            self._recording_refresh_lease_id = ""
            return False

        self._recording_refresh_lease_id = lease_id
        log.info("Recording refresh lease claimed successfully")
        self._update_unlock_state(response)
        self._recording_claim_attempt_key = None
        return False

    def _on_device_created(self, dialog, device) -> None:
        if self.placeholder:
            self._close_tab_page(self._page_for_child(self.placeholder))
            self._placeholder_page = None

        if device.hardware_id not in self._device_pages:
            self._add_device_tab(device)
        page = self._page_for_hardware_id(device.hardware_id)
        if page is not None:
            self.tab_view.set_selected_page(page)
        session_request_async({"command": "reload"}, lambda _result: False)

    def _show_demo_notification(self, message: str) -> None:
        dialog = Adw.AlertDialog(heading="Demo Mode", body=message)
        dialog.add_response("ok", "OK")
        dialog.present(self)

    def _check_empty_state(self) -> None:
        if not self._device_pages:
            self._ensure_placeholder_page()
            self._set_empty_placeholder_state()

    def _update_compositor_status(self) -> None:
        if not self._startup_probe_done:
            self.compositor_status.set_label("compositor: ⚪ checking")
            self.compositor_status.set_tooltip_text("Checking compositor support")
            return
        if self._compositor_supported:
            icon = "🟢"
            name = get_compositor_name(self._compositor_id)
            self.compositor_status.set_label(f"compositor: {icon} {name}")
            self.compositor_status.set_tooltip_text(f"{name} support is active")
        elif self._compositor_id:
            icon = "🟡"
            name = get_compositor_name(self._compositor_id)
            self.compositor_status.set_label(f"compositor: {icon} {name} (limited)")
            if self._compositor_id == "gnome" and self._gnome_setup_needed():
                self.compositor_status.set_tooltip_text("Click to set up the GNOME Shell bridge")
            else:
                self.compositor_status.set_tooltip_text(f"{name} support is limited")
        else:
            self.compositor_status.set_label("compositor: 🔴 none")
            self.compositor_status.set_tooltip_text("No supported compositor detected")

    def _update_compositor_warning_banner(self) -> None:
        if self.demo_mode or not self._startup_probe_done:
            self.warning_banner.set_visible(False)
            self.warning_banner.set_revealed(False)
            return

        if self._compositor_id == "gnome":
            self.warning_banner.set_visible(False)
            self.warning_banner.set_revealed(False)
            return

        compositor_name = get_compositor_name(self._compositor_id)
        msg = str(self._compositor_support_details.get("warning", "") or "")
        if msg:
            self.warning_banner.set_title(msg)
            self.warning_banner.set_visible(True)
            self.warning_banner.set_revealed(True)
            return

        if self._compositor_supported:
            self.warning_banner.set_visible(False)
            self.warning_banner.set_revealed(False)
            return

        if self._compositor_id:
            msg = (
                f"⚠️ Compositor '{compositor_name}' has limited support. "
                "Window rules are unavailable on this setup."
            )
        else:
            msg = "⚠️ No supported compositor detected. Window rules are unavailable."
        self.warning_banner.set_title(msg)
        self.warning_banner.set_visible(True)
        self.warning_banner.set_revealed(True)

    def _gnome_setup_needed(self) -> bool:
        if self.demo_mode or self._compositor_id != "gnome":
            return False
        state = str(self._compositor_support_details.get("gnome_bridge_state", "") or "")
        action = str(self._compositor_support_details.get("gnome_bridge_action", "") or "")
        warning = str(self._compositor_support_details.get("warning", "") or "")
        if state in {"", "ready", "not_gnome"} and not action and not warning:
            return False
        return True

    def _maybe_present_gnome_setup_dialog(self) -> None:
        if self._gnome_setup_dialog_prompted:
            return
        if not self._gnome_setup_needed():
            return
        self._gnome_setup_dialog_prompted = True
        GLib.idle_add(self._present_gnome_setup_dialog)

    def _on_compositor_status_released(
        self,
        _gesture: Gtk.GestureClick,
        _n_press: int,
        _x: float,
        _y: float,
    ) -> None:
        if self._gnome_setup_needed():
            self._present_gnome_setup_dialog()

    def _present_gnome_setup_dialog(self) -> bool:
        if not self._gnome_setup_needed():
            return False
        if self._gnome_setup_dialog is None:
            dialog = GnomeSetupDialog(
                self,
                dict(self._compositor_support_details),
                on_action_completed=self._on_gnome_setup_action_completed,
            )
            dialog.connect("closed", self._on_gnome_setup_dialog_closed)
            self._gnome_setup_dialog = dialog
        self._gnome_setup_dialog.present(self)
        return False

    def _on_gnome_setup_dialog_closed(self, dialog: Adw.Dialog) -> None:
        if dialog is self._gnome_setup_dialog:
            self._gnome_setup_dialog = None
            self._gnome_setup_poll_source_id = self._remove_timeout_source(
                self._gnome_setup_poll_source_id
            )

    def _on_gnome_setup_action_completed(self, action: str) -> None:
        if action in {"enable_bridge", "enable_extensions", "refresh", "restart_session"}:
            self._schedule_gnome_setup_status_poll()
        self._start_startup_probe()

    def _schedule_gnome_setup_status_poll(self) -> None:
        if self._gnome_setup_poll_source_id:
            return
        self._gnome_setup_poll_source_id = GLib.timeout_add(1000, self._poll_gnome_setup_status)

    def _poll_gnome_setup_status(self) -> bool:
        if self._destroyed or self._gnome_setup_dialog is None:
            self._gnome_setup_poll_source_id = 0
            return False
        self._update_status_from_session()
        return True

    def _close_gnome_setup_dialog_if_ready(self) -> None:
        if self._gnome_setup_dialog is None:
            return
        if self._gnome_setup_needed():
            return
        dialog = self._gnome_setup_dialog
        self._gnome_setup_dialog = None
        self._gnome_setup_poll_source_id = self._remove_timeout_source(
            self._gnome_setup_poll_source_id
        )
        dialog.close()
