# pyright: reportUnusedFunction=false

from __future__ import annotations

from keymasq.common.models import HardwareConfig
from keymasq.gui.icons import combo_icon_names, device_icon_names, resolve_icon_name
from keymasq.gui.widgets.combo_tab import ComboTab
from keymasq.gui.widgets.device_tab import DeviceTab
from keymasq.gui.widgets.profile_managed_tab import ProfileManagedTab

from . import _runtime, chrome, profiles, tab_layout
from .constants import _COMBO_TAB_ID, _DEVICE_TAB_STATE_ICONS


def _device_status_for_hardware_id(window, hardware_id: str) -> dict[str, object]:
    devices = window._profile_runtime_state.get("devices", {})
    if not isinstance(devices, dict):
        return {}
    raw_device = devices.get(hardware_id, {})
    if not isinstance(raw_device, dict):
        return {}
    raw_status = raw_device.get("device_status", {})
    return dict(raw_status) if isinstance(raw_status, dict) else {}


def _device_tab_title(window, device: HardwareConfig) -> str:
    if window.demo_mode:
        return device.name
    status = _device_status_for_hardware_id(window, device.hardware_id)
    state = str(status.get("state", "unknown") or "unknown")
    icon = _DEVICE_TAB_STATE_ICONS.get(state, "⚪")
    return f"{icon} {device.name}"


def _sync_device_tab_title(window, hardware_id: str) -> None:
    page = tab_layout._page_for_hardware_id(window, hardware_id)
    child = page.get_child() if page is not None else None
    if page is None or not isinstance(child, DeviceTab):
        return
    page.set_title(_device_tab_title(window, child.device))


def _sync_device_tab_titles(window) -> None:
    for hardware_id in list(window._device_pages):
        _sync_device_tab_title(window, hardware_id)


def _refresh_device_tabs(
    window,
    preferred_profile_name: str | None = None,
    source_hardware_id: str | None = None,
    source_widget: _runtime.Gtk.Widget | None = None,
) -> None:
    for child in tab_layout._iter_profile_tabs(window):
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


def _ensure_placeholder_page(window) -> None:
    if tab_layout._page_for_child(window, window.placeholder) is not None:
        return
    if window.placeholder.get_parent() is not None:
        chrome._create_placeholder_widget(
            window,
            title_text="No devices configured",
            subtitle_text="Click + to add a new device",
        )
    window._placeholder_page = tab_layout._append_tab_page(
        window,
        window.placeholder,
        title="Welcome",
        icon_name=resolve_icon_name(*device_icon_names(False)),
    )


def _set_empty_placeholder_state(window) -> None:
    if window._placeholder_title is not None:
        window._placeholder_title.set_label("No devices configured")
    if window._placeholder_subtitle is not None:
        window._placeholder_subtitle.set_label("Click + to add a new device")


def _apply_loaded_devices(window, devices: list[HardwareConfig]) -> None:
    if window.demo_mode and not devices:
        _load_demo_devices(window)
        return

    if not devices:
        _set_empty_placeholder_state(window)
        tab_layout._select_saved_or_default_tab(window)
        return

    tab_layout._close_tab_page(window, tab_layout._page_for_child(window, window.placeholder))
    window._placeholder_page = None

    suppress_layout_was = window._suppress_tab_layout_save
    suppress_selected_was = window._suppress_selected_tab_save
    window._suppress_tab_layout_save = True
    window._suppress_selected_tab_save = True
    try:
        for device in tab_layout._order_devices_for_tabs(window, devices):
            if device.hardware_id in window._device_pages:
                continue
            _add_device_tab(window, device, persist_order=False)
        tab_layout._reorder_visible_pages_to_saved_order(window)
    finally:
        window._suppress_tab_layout_save = suppress_layout_was
        window._suppress_selected_tab_save = suppress_selected_was
    tab_layout._select_saved_or_default_tab(window)


def _load_demo_devices(window) -> None:
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
            ButtonDefinition(id="btn_right", label="Right Click", evdev="btn_right", zone="right"),
            ButtonDefinition(
                id="btn_middle", label="Middle Click", evdev="btn_middle", zone="wheel"
            ),
            ButtonDefinition(id="btn_back", label="Back", evdev="btn_side", zone="thumb"),
            ButtonDefinition(id="btn_forward", label="Forward", evdev="btn_extra", zone="thumb"),
        ],
    )

    tab_layout._close_tab_page(window, tab_layout._page_for_child(window, window.placeholder))
    window._placeholder_page = None
    suppress_selected_was = window._suppress_selected_tab_save
    window._suppress_selected_tab_save = True
    try:
        if demo_device.hardware_id not in window._device_pages:
            _add_device_tab(window, demo_device, persist_order=False)
        tab_layout._reorder_visible_pages_to_saved_order(window)
    finally:
        window._suppress_selected_tab_save = suppress_selected_was
    tab_layout._select_saved_or_default_tab(window)


def _setup_combo_tab(window) -> None:
    if _COMBO_TAB_ID in window._hidden_tabs:
        return
    _ensure_combo_tab_page(window)


def _ensure_combo_tab_page(window) -> _runtime.Adw.TabPage | None:
    if window._combo_page is not None:
        return window._combo_page

    window.combo_tab = ComboTab(
        profile_manager=window.profile_manager,
        main_window=window,
        demo_mode=window.demo_mode,
        compositor_capabilities=window._compositor_capabilities,
    )
    window._combo_page = tab_layout._append_tab_page(
        window,
        window.combo_tab,
        title="Combos",
        icon_name=resolve_icon_name(*combo_icon_names()),
    )
    if window._selected_profile_name:
        window.combo_tab.refresh_profiles(
            preferred_profile_name=window._selected_profile_name,
            publish_selection=False,
        )
    profiles._apply_profile_runtime_state_to_widget(window, window.combo_tab)
    tab_layout._reorder_visible_pages_to_saved_order(window)
    return window._combo_page


def show_combo_tab(window) -> None:
    window._hidden_tabs.discard(_COMBO_TAB_ID)
    page = _ensure_combo_tab_page(window)
    if page is None:
        return
    tab_layout._save_tab_layout(window)
    window.tab_view.set_selected_page(page)


def _add_device_tab(window, device: HardwareConfig, *, persist_order: bool = True) -> None:
    if window._placeholder_page is not None:
        tab_layout._close_tab_page(window, window._placeholder_page)
        window._placeholder_page = None

    tab = DeviceTab(
        device=device,
        profile_manager=window.profile_manager,
        hardware_manager=window.hardware_manager,
        main_window=window,
        demo_mode=window.demo_mode,
        compositor_capabilities=window._compositor_capabilities,
    )

    icon = resolve_icon_name(*device_icon_names(device_kind=tab.device_layout_kind()))
    page = tab_layout._append_tab_page(window, tab, title=device.name, icon_name=icon)
    window._device_pages[device.hardware_id] = page
    if window._selected_profile_name:
        tab.refresh_profiles(
            preferred_profile_name=window._selected_profile_name,
            publish_selection=False,
        )
    profiles._apply_profile_runtime_state_to_widget(window, tab)
    if window.combo_tab is not None:
        window.combo_tab.refresh_profiles(
            preferred_profile_name=window._selected_profile_name,
            publish_selection=False,
        )
    tab_layout._reorder_visible_pages_to_saved_order(window)
    _sync_device_tab_title(window, device.hardware_id)
    if persist_order:
        tab_layout._save_tab_layout(window)


def update_device_display_name(window, hardware_id: str, name: str) -> None:
    page = tab_layout._page_for_hardware_id(window, hardware_id)
    if page is not None:
        child = page.get_child()
        if isinstance(child, DeviceTab):
            child.device.name = name
            page.set_title(_device_tab_title(window, child.device))
        else:
            page.set_title(name)


def remove_device_tab(window, hardware_id: str) -> None:
    page = window._device_pages.pop(hardware_id, None)
    tab_layout._close_tab_page(window, page)
    tab_layout._save_tab_layout(window)
    _check_empty_state(window)


def _on_add_device(window, button: _runtime.Gtk.Button) -> None:
    if window.demo_mode:
        _show_demo_notification(window, "Device setup not available in demo mode")
        return

    from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

    dialog = HardwareSetupDialog(window, window.hardware_manager)

    def on_device_created(dialog, device: HardwareConfig) -> None:
        _on_device_created(window, dialog, device)

    dialog.connect("device-created", on_device_created)
    dialog.present(window)


def _on_add_device_clicked(window, _button: _runtime.Gtk.Button) -> None:
    _on_add_device(window, _button)


def _on_device_created(window, dialog, device) -> None:
    if window.placeholder:
        tab_layout._close_tab_page(window, tab_layout._page_for_child(window, window.placeholder))
        window._placeholder_page = None

    if device.hardware_id not in window._device_pages:
        _add_device_tab(window, device)
    page = tab_layout._page_for_hardware_id(window, device.hardware_id)
    if page is not None:
        window.tab_view.set_selected_page(page)
    _runtime.session_request_async({"command": "reload"}, lambda _result: False)


def _show_demo_notification(window, message: str) -> None:
    dialog = _runtime.Adw.AlertDialog(heading="Demo Mode", body=message)
    dialog.add_response("ok", "OK")
    dialog.present(window)


def _check_empty_state(window) -> None:
    if not window._device_pages:
        _ensure_placeholder_page(window)
        _set_empty_placeholder_state(window)
