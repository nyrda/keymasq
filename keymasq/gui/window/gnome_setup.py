# pyright: reportUnusedFunction=false

from __future__ import annotations

from keymasq.gui.widgets.gnome_setup_dialog import GnomeSetupDialog

from . import _runtime


def _gnome_setup_needed(window) -> bool:
    if window.demo_mode or window._compositor_id != "gnome":
        return False
    state = str(window._compositor_support_details.get("gnome_bridge_state", "") or "")
    action = str(window._compositor_support_details.get("gnome_bridge_action", "") or "")
    warning = str(window._compositor_support_details.get("warning", "") or "")
    if state in {"", "ready", "not_gnome"} and not action and not warning:
        return False
    return True


def _maybe_present_gnome_setup_dialog(window) -> None:
    if window._gnome_setup_dialog_prompted:
        return
    if not window._gnome_setup_needed():
        return
    window._gnome_setup_dialog_prompted = True
    _runtime.GLib.idle_add(window._present_gnome_setup_dialog)


def _on_compositor_status_released(
    window,
    _gesture: _runtime.Gtk.GestureClick,
    _n_press: int,
    _x: float,
    _y: float,
) -> None:
    if window._gnome_setup_needed():
        window._present_gnome_setup_dialog()


def _present_gnome_setup_dialog(window) -> bool:
    if not window._gnome_setup_needed():
        return False
    if window._gnome_setup_dialog is None:
        dialog = GnomeSetupDialog(
            window,
            dict(window._compositor_support_details),
            on_action_completed=window._on_gnome_setup_action_completed,
        )
        dialog.connect("closed", window._on_gnome_setup_dialog_closed)
        window._gnome_setup_dialog = dialog
    window._gnome_setup_dialog.present(window)
    return False


def _on_gnome_setup_dialog_closed(window, dialog: _runtime.Adw.Dialog) -> None:
    if dialog is window._gnome_setup_dialog:
        window._gnome_setup_dialog = None
        window._gnome_setup_poll_source_id = window._remove_timeout_source(
            window._gnome_setup_poll_source_id
        )


def _on_gnome_setup_action_completed(window, action: str) -> None:
    if action in {"enable_bridge", "enable_extensions", "refresh", "restart_session"}:
        window._schedule_gnome_setup_status_poll()
    window._start_startup_probe()


def _schedule_gnome_setup_status_poll(window) -> None:
    if window._gnome_setup_poll_source_id:
        return
    window._gnome_setup_poll_source_id = _runtime.GLib.timeout_add(
        1000, window._poll_gnome_setup_status
    )


def _poll_gnome_setup_status(window) -> bool:
    if window._destroyed or window._gnome_setup_dialog is None:
        window._gnome_setup_poll_source_id = 0
        return False
    window._update_status_from_session()
    return True


def _close_gnome_setup_dialog_if_ready(window) -> None:
    if window._gnome_setup_dialog is None:
        return
    if window._gnome_setup_needed():
        return
    dialog = window._gnome_setup_dialog
    window._gnome_setup_dialog = None
    window._gnome_setup_poll_source_id = window._remove_timeout_source(
        window._gnome_setup_poll_source_id
    )
    dialog.close()
