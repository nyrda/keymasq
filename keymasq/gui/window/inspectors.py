# pyright: reportUnusedFunction=false

from __future__ import annotations

from . import _runtime, device_tabs


def open_device_inspector(window, device) -> None:
    hardware_id = str(getattr(device, "hardware_id", "") or "").strip()
    if not hardware_id:
        return
    existing = window._device_inspector_windows.get(hardware_id)
    if existing is not None:
        if bool(getattr(existing, "_closing", False)):
            window._device_inspector_windows.pop(hardware_id, None)
        else:
            existing.present()
            return

    if window.demo_mode:
        device_tabs._show_demo_notification(
            window, "Device inspector is not available in demo mode"
        )
        return

    from keymasq.gui.widgets.device_inspector_window import DeviceInspectorWindow

    inspector = DeviceInspectorWindow(window, device)
    window._device_inspector_windows[hardware_id] = inspector

    def on_close_request(inspector_window: _runtime.Gtk.Window) -> bool:
        if window._device_inspector_windows.get(hardware_id) is inspector_window:
            window._device_inspector_windows.pop(hardware_id, None)
        return False

    def on_destroy(inspector_window: _runtime.Gtk.Window) -> None:
        if window._device_inspector_windows.get(hardware_id) is inspector_window:
            window._device_inspector_windows.pop(hardware_id, None)

    inspector.connect("close-request", on_close_request)
    inspector.connect("destroy", on_destroy)
    inspector.present()


def open_combo_inspector(window) -> None:
    existing = window._combo_inspector_window
    if existing is not None:
        if bool(getattr(existing, "_closing", False)):
            window._combo_inspector_window = None
        else:
            existing.present()
            return

    if window.demo_mode:
        device_tabs._show_demo_notification(window, "Combo inspector is not available in demo mode")
        return

    from keymasq.gui.widgets.combo_inspector_window import ComboInspectorWindow

    inspector = ComboInspectorWindow(window)
    window._combo_inspector_window = inspector

    def on_close_request(inspector_window: _runtime.Gtk.Window) -> bool:
        if window._combo_inspector_window is inspector_window:
            window._combo_inspector_window = None
        return False

    def on_destroy(inspector_window: _runtime.Gtk.Window) -> None:
        if window._combo_inspector_window is inspector_window:
            window._combo_inspector_window = None

    inspector.connect("close-request", on_close_request)
    inspector.connect("destroy", on_destroy)
    inspector.present()
