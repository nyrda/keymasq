# pyright: reportUnusedFunction=false

from __future__ import annotations

from keymasq.gui.preferences import save_selected_profile
from keymasq.gui.widgets.device_tab.tab import DeviceTab
from keymasq.gui.widgets.profile_managed_tab import ProfileManagedTab
from keymasq.session.profile.manager import ProfileManager

from . import _runtime, device_tabs, tab_layout


def _set_selected_profile_name(window, profile_name: str | None) -> None:
    window._selected_profile_name = profile_name


def selected_profile_name(window) -> str | None:
    return window._selected_profile_name


def _sync_selected_profile_name(
    window,
    profile_name: str | None,
    source_hardware_id: str | None = None,
    source_widget: _runtime.Gtk.Widget | None = None,
) -> None:
    if window._syncing_profile_selection:
        window._selected_profile_name = profile_name
        return

    window._selected_profile_name = profile_name
    save_selected_profile(profile_name)
    window._syncing_profile_selection = True
    try:
        for child in tab_layout._iter_profile_tabs(window):
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
        window._syncing_profile_selection = False


def _queue_profile_reload(window) -> None:
    if window._destroyed:
        return
    if window._profile_reload_inflight:
        window._profile_reload_pending = True
        return

    window._profile_reload_inflight = True
    _runtime.run_gui_task(
        lambda: _load_profile_manager_snapshot(window),
        lambda result: _on_profile_reload_finished(window, result),
    )


def _load_profile_manager_snapshot(window) -> ProfileManager:
    return ProfileManager()


def _on_profile_reload_finished(window, result: _runtime.GuiTaskResult[ProfileManager]) -> bool:
    window._profile_reload_inflight = False
    rerun = window._profile_reload_pending
    window._profile_reload_pending = False

    if not window._destroyed and result.ok and isinstance(result.value, ProfileManager):
        _set_profile_manager(window, result.value)
        device_tabs._refresh_device_tabs(window)
        _apply_profile_runtime_state(window, window._profile_runtime_state)

    if rerun:
        _queue_profile_reload(window)
    return False


def _set_profile_manager(window, profile_manager: ProfileManager) -> None:
    from keymasq.gui.widgets.profile_managed_tab import ProfileManagedTab

    window.profile_manager = profile_manager
    for child in tab_layout._iter_profile_tabs(window):
        if isinstance(child, ProfileManagedTab):
            child.profile_manager = profile_manager


def _normalize_profile_runtime_state(window, state: dict | None) -> dict[str, object]:
    if not isinstance(state, dict):
        return dict(window._profile_runtime_state)

    normalized = dict(window._profile_runtime_state)
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


def _apply_profile_runtime_state_to_widget(window, widget: _runtime.Gtk.Widget | None) -> None:
    from keymasq.gui.widgets.profile_managed_tab import ProfileManagedTab

    if widget is None or not isinstance(widget, ProfileManagedTab):
        return
    widget.apply_active_profile_response(window._profile_runtime_state)


def _apply_profile_runtime_state(window, state: dict | None) -> None:
    window._profile_runtime_state = _normalize_profile_runtime_state(window, state)
    for child in tab_layout._iter_profile_tabs(window):
        _apply_profile_runtime_state_to_widget(window, child)
    device_tabs._sync_device_tab_titles(window)


def _mark_device_runtime_unknown(window) -> None:
    devices = window._profile_runtime_state.get("devices", {})
    if not isinstance(devices, dict):
        return
    updated_devices: dict[str, object] = {}
    for hardware_id, raw_device in devices.items():
        if not isinstance(raw_device, dict):
            continue
        device_payload = dict(raw_device)
        old_status = raw_device.get("device_status", {})
        old_status = old_status if isinstance(old_status, dict) else {}
        device_payload["device_status"] = {
            "state": "unknown",
            "configured_count": _runtime_status_int(window, old_status, "configured_count"),
            "connected_count": 0,
            "requested_count": _runtime_status_int(window, old_status, "requested_count"),
            "grabbed_count": 0,
            "interfaces": [],
            "runtime_ready": False,
            "grab_status": {},
        }
        updated_devices[str(hardware_id)] = device_payload
    if updated_devices:
        _apply_profile_runtime_state(window, {"devices": updated_devices})


def _runtime_status_int(window, status: dict[str, object], key: str) -> int:
    value = status.get(key, 0)
    if not isinstance(value, int | float | str):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
