# pyright: reportUnusedFunction=false

from __future__ import annotations

from keymasq.common.models import HardwareConfig
from keymasq.gui.preferences import save_selected_tab, save_tab_layout
from keymasq.gui.widgets.combo_tab import ComboTab
from keymasq.gui.widgets.device_tab import DeviceTab
from keymasq.gui.widgets.profile_managed_tab import ProfileManagedTab

from . import _runtime, chrome, device_tabs
from .constants import _COMBO_TAB_ID, _DEVICE_TAB_PREFIX, _device_tab_id


def _iter_tab_pages(window):
    for position in range(window.tab_view.get_n_pages()):
        yield window.tab_view.get_nth_page(position)


def _iter_tab_children(window):
    for page in _iter_tab_pages(window):
        child = page.get_child()
        if child is not None:
            yield child


def _iter_profile_tabs(window):
    yield from _iter_tab_children(window)


def _page_for_child(window, widget: _runtime.Gtk.Widget | None) -> _runtime.Adw.TabPage | None:
    if widget is None:
        return None
    if widget is window.placeholder:
        return window._placeholder_page
    if widget is window.combo_tab:
        return window._combo_page
    for page in window._device_pages.values():
        if page.get_child() is widget:
            return page
    return None


def _page_for_hardware_id(window, hardware_id: str) -> _runtime.Adw.TabPage | None:
    return window._device_pages.get(hardware_id)


def _child_for_hardware_id(window, hardware_id: str) -> _runtime.Gtk.Widget | None:
    page = _page_for_hardware_id(window, hardware_id)
    return page.get_child() if page is not None else None


def _append_tab_page(
    window,
    child: _runtime.Gtk.Widget,
    *,
    title: str,
    icon_name: str,
    pinned: bool = False,
) -> _runtime.Adw.TabPage:
    page = window.tab_view.append_pinned(child) if pinned else window.tab_view.append(child)
    page.set_title(title)
    page.set_icon(chrome._icon_from_name(window, icon_name))
    _runtime.GLib.idle_add(lambda: _sync_tab_close_tooltips(window))
    return page


def _walk_widget_tree(window, widget: _runtime.Gtk.Widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _walk_widget_tree(window, child)
        child = child.get_next_sibling()


def _sync_tab_close_tooltips(window) -> bool:
    for widget in _walk_widget_tree(window, window.tab_bar):
        if "tab-close-button" in widget.get_css_classes():
            widget.set_tooltip_text("Close tab")
    return False


def _close_tab_page(window, page: _runtime.Adw.TabPage | None) -> None:
    if page is None:
        return
    window._allow_tab_page_close = True
    try:
        window.tab_view.close_page(page)
    finally:
        window._allow_tab_page_close = False


def _on_tab_close_page(window, _tab_view: _runtime.Adw.TabView, page: _runtime.Adw.TabPage) -> bool:
    if window._allow_tab_page_close:
        child = page.get_child()
        window.tab_view.close_page_finish(page, True)
        if isinstance(child, ComboTab):
            window._combo_page = None
            window.combo_tab = None
        return True

    child = page.get_child()
    if isinstance(child, ComboTab):
        _hide_combo_tab(window, page)
        return True

    window.tab_view.close_page_finish(page, False)
    if isinstance(child, DeviceTab):
        child.present_delete_device_dialog()
    return True


def _hide_combo_tab(window, page: _runtime.Adw.TabPage) -> None:
    window._tab_order = _current_tab_order(window)
    window._hidden_tabs.add(_COMBO_TAB_ID)
    save_tab_layout(window._tab_order, window._hidden_tabs)
    window.tab_view.close_page_finish(page, True)
    window._combo_page = None
    window.combo_tab = None
    device_tabs._check_empty_state(window)


def _on_tab_page_reordered(
    window,
    _tab_view: _runtime.Adw.TabView,
    _page: _runtime.Adw.TabPage,
    _position: int,
) -> None:
    if not window._suppress_tab_layout_save:
        _save_tab_layout(window)


def _tab_id_for_child(window, child: _runtime.Gtk.Widget | None) -> str | None:
    if isinstance(child, DeviceTab):
        return _device_tab_id(child.device.hardware_id)
    if isinstance(child, ComboTab):
        return _COMBO_TAB_ID
    return None


def _selected_tab_for_child(window, child: _runtime.Gtk.Widget | None) -> str | None:
    if isinstance(child, DeviceTab):
        return child.device.hardware_id
    if isinstance(child, ComboTab):
        return _COMBO_TAB_ID
    return None


def _current_tab_order(window) -> list[str]:
    order: list[str] = []
    for page in _iter_tab_pages(window):
        tab_id = _tab_id_for_child(window, page.get_child())
        if tab_id is not None:
            order.append(tab_id)
    return order


def list_device_tab_configs(window) -> list[HardwareConfig]:
    devices: list[HardwareConfig] = []
    for page in _iter_tab_pages(window):
        child = page.get_child()
        if isinstance(child, DeviceTab):
            devices.append(child.device)
    return devices


def _merge_hidden_tabs_into_order(window, visible_order: list[str]) -> list[str]:
    order = list(visible_order)
    for hidden_tab_id in window._tab_order:
        if hidden_tab_id not in window._hidden_tabs or hidden_tab_id in order:
            continue

        previous = next(
            (
                tab_id
                for tab_id in reversed(window._tab_order[: window._tab_order.index(hidden_tab_id)])
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
                for tab_id in window._tab_order[window._tab_order.index(hidden_tab_id) + 1 :]
                if tab_id in order
            ),
            None,
        )
        if following is not None:
            order.insert(order.index(following), hidden_tab_id)
            continue

        order.append(hidden_tab_id)

    for hidden_tab_id in sorted(window._hidden_tabs - set(order)):
        order.append(hidden_tab_id)
    return order


def _save_tab_layout(window) -> None:
    window._tab_order = _merge_hidden_tabs_into_order(window, _current_tab_order(window))
    save_tab_layout(window._tab_order, window._hidden_tabs)


def _page_for_tab_id(window, tab_id: str) -> _runtime.Adw.TabPage | None:
    if tab_id == _COMBO_TAB_ID:
        return window._combo_page
    if tab_id.startswith(_DEVICE_TAB_PREFIX):
        hardware_id = tab_id.removeprefix(_DEVICE_TAB_PREFIX)
        return window._device_pages.get(hardware_id)
    return None


def _page_for_selected_tab(window, selected_tab: str) -> _runtime.Adw.TabPage | None:
    selected_tab = selected_tab.strip()
    if selected_tab == _COMBO_TAB_ID:
        return window._combo_page
    return window._device_pages.get(selected_tab)


def _default_selected_tab_page(window) -> _runtime.Adw.TabPage | None:
    if not window._device_pages and window._placeholder_page is not None:
        return window._placeholder_page

    for tab_id in _desired_visible_tab_order(window):
        page = _page_for_tab_id(window, tab_id)
        if page is not None:
            return page

    for page in _iter_tab_pages(window):
        if _tab_id_for_child(window, page.get_child()) is not None:
            return page
    return None


def _select_empty_placeholder_tab(window) -> bool:
    if window._device_pages or window._placeholder_page is None:
        return False
    window.tab_view.set_selected_page(window._placeholder_page)
    window._initial_tab_selection_pending = False
    return True


def _select_saved_or_default_tab(window) -> None:
    if _select_empty_placeholder_tab(window):
        return

    page = _page_for_selected_tab(window, window._selected_tab)
    if page is None:
        page = _default_selected_tab_page(window)
    if page is None:
        window._initial_tab_selection_pending = False
        return

    window.tab_view.set_selected_page(page)
    selected_tab = _selected_tab_for_child(window, page.get_child())
    if selected_tab is None:
        window._initial_tab_selection_pending = False
        return

    window._selected_tab = selected_tab
    save_selected_tab(selected_tab)
    window._initial_tab_selection_pending = False


def _default_visible_tab_order(window) -> list[str]:
    device_ids: list[str] = []
    other_ids: list[str] = []
    for tab_id in _current_tab_order(window):
        if tab_id.startswith(_DEVICE_TAB_PREFIX):
            device_ids.append(tab_id)
        else:
            other_ids.append(tab_id)
    return device_ids + other_ids


def _desired_visible_tab_order(window) -> list[str]:
    current_order = _current_tab_order(window)
    if not window._tab_order:
        return _default_visible_tab_order(window)

    visible_order = [
        tab_id
        for tab_id in window._tab_order
        if tab_id not in window._hidden_tabs and tab_id in current_order
    ]
    missing_order = [tab_id for tab_id in current_order if tab_id not in visible_order]
    missing_devices = [tab_id for tab_id in missing_order if tab_id.startswith(_DEVICE_TAB_PREFIX)]
    missing_other = [
        tab_id for tab_id in missing_order if not tab_id.startswith(_DEVICE_TAB_PREFIX)
    ]
    if (
        missing_devices
        and visible_order
        and visible_order[-1] == _COMBO_TAB_ID
        and _COMBO_TAB_ID not in window._hidden_tabs
    ):
        visible_order[-1:-1] = missing_devices
    else:
        visible_order.extend(missing_devices)
    visible_order.extend(missing_other)
    return visible_order


def _reorder_visible_pages_to_saved_order(window) -> None:
    desired_order = _desired_visible_tab_order(window)
    if not desired_order:
        return

    suppress_was = window._suppress_tab_layout_save
    window._suppress_tab_layout_save = True
    try:
        position = window.tab_view.get_n_pinned_pages()
        if window._placeholder_page is not None:
            window.tab_view.reorder_page(window._placeholder_page, position)
            position += 1
        for tab_id in desired_order:
            page = _page_for_tab_id(window, tab_id)
            if page is None:
                continue
            window.tab_view.reorder_page(page, position)
            position += 1
    finally:
        window._suppress_tab_layout_save = suppress_was


def _order_devices_for_tabs(window, devices: list[HardwareConfig]) -> list[HardwareConfig]:
    order_index = {
        tab_id.removeprefix(_DEVICE_TAB_PREFIX): index
        for index, tab_id in enumerate(window._tab_order)
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


def _on_selected_tab_changed(window, _tab_view, _pspec) -> None:
    page = window.tab_view.get_selected_page()
    child = page.get_child() if page is not None else None
    selected_tab = _selected_tab_for_child(window, child)
    if (
        selected_tab is not None
        and not window._suppress_selected_tab_save
        and not window._initial_tab_selection_pending
    ):
        window._selected_tab = selected_tab
        save_selected_tab(selected_tab)
    if isinstance(child, ProfileManagedTab):
        child.refresh_profiles(
            preferred_profile_name=window._selected_profile_name,
            publish_selection=False,
        )
