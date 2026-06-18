# pyright: reportUnusedFunction=false

from __future__ import annotations

from collections.abc import Callable

from . import _runtime


def register(window) -> None:
    _runtime.register_session_event_callback("*", window._on_session_event)


def unregister(window) -> None:
    _runtime.unregister_session_event_callback("*", window._on_session_event)


def _on_session_event(window, event: dict) -> bool:
    window._handle_session_event(event)
    return False


def _reconnect_session(window) -> bool:
    window._update_status_from_session()
    return True


def register_event_handler(window, event_type: str, callback: Callable[[dict], None]) -> None:
    if event_type not in window._event_handlers:
        window._event_handlers[event_type] = []
    window._event_handlers[event_type].append(callback)


def unregister_event_handler(window, event_type: str, callback: Callable[[dict], None]) -> None:
    if event_type in window._event_handlers:
        try:
            window._event_handlers[event_type].remove(callback)
        except ValueError:
            pass


def _handle_session_event(window, event: dict) -> None:
    event_type_raw = event.get("event")
    event_type = event_type_raw if isinstance(event_type_raw, str) else ""

    if event_type == "keymasqd_status":
        connected = event.get("connected", False)
        window._update_status_from_keymasqd_event(connected)
    elif event_type == "profiles_changed":
        window._apply_profile_runtime_state(event)
        if not bool(event.get("runtime_only", False)):
            window._queue_profile_reload()
    elif event_type == "recording_started":
        window._close_dialogs_for_recording_start()
        window._recording_overlay.set_visible(True)
        window._recording_overlay.on_started(event)
    elif event_type == "recording_stopped":
        window._recording_overlay.on_stopped()
        window._recording_overlay.set_visible(False)
        window._on_recording_stopped(event)
    elif event_type == "recording_progress":
        window._recording_overlay.on_progress(event)
    elif event_type == "recording_auth_requested":
        window.present_recording_settings_dialog(reason="recording_locked")
    elif event_type == "macro_recording_disabled":
        window._update_macro_recording_state(event)

    callbacks = window._event_handlers.get(event_type)
    if callbacks is None:
        return
    for cb in list(callbacks):
        cb(event)


def _update_status_from_keymasqd_event(window, keymasqd_connected: bool) -> None:
    if keymasqd_connected:
        window.session_status.set_label("session: 🟢")
        window.keymasqd_status.set_label("keymasqd: 🟢")
        window._set_connection_issue(None)
    else:
        window.session_status.set_label("session: 🟡")
        window.keymasqd_status.set_label("keymasqd: 🔴")
        window._mark_device_runtime_unknown()
        window._set_connection_issue("keymasqd")


def _update_status_from_session(window) -> None:
    if window._status_query_inflight:
        return

    window._status_query_inflight = True
    window._status_query_id += 1
    query_id = window._status_query_id

    def on_status_response(data: dict | None, qid: int = query_id) -> bool:
        return window._on_status_response(data, qid)

    _runtime.session_request_async(
        {"command": "get_status"},
        on_status_response,
        timeout=2.0,
    )


def _on_status_response(window, data: dict | None, query_id: int) -> bool:
    if window._destroyed:
        return False
    if query_id != window._status_query_id:
        return False

    window._status_query_inflight = False

    try:
        session_ok = bool(data and data.get("status") == "ok")
        keymasqd_ok = bool(data and data.get("keymasqd_connected") is True)
        window._update_unlock_state(data if isinstance(data, dict) else None)
        window._update_macro_recording_state(data if isinstance(data, dict) else None)
        window._update_compositor_dispatch_state(data if isinstance(data, dict) else None)
        if isinstance(data, dict) and data.get("status") == "ok":
            window._apply_profile_runtime_state(data)
            compositor_id = data.get("compositor_id")
            if compositor_id is not None:
                window._compositor_id = compositor_id
            details = data.get("compositor_details")
            if isinstance(details, dict):
                window._compositor_support_details = details
                window._compositor_supported = bool(details.get("supported", False))
            window._update_compositor_warning_banner()
            window._update_compositor_status()
            window._close_gnome_setup_dialog_if_ready()

        if session_ok:
            if keymasqd_ok:
                window.session_status.set_label("session: 🟢")
                window.keymasqd_status.set_label("keymasqd: 🟢")
                window._set_connection_issue(None)
            else:
                window.session_status.set_label("session: 🟡")
                window.keymasqd_status.set_label("keymasqd: 🔴")
                window._set_connection_issue("keymasqd")
        else:
            window._update_status_disconnected()
    except (OSError, RuntimeError, TypeError, ValueError):
        window._update_unlock_state(None)
        window._update_macro_recording_state(None)
        window._update_status_disconnected()

    return False


def _update_status_disconnected(window) -> None:
    window.session_status.set_label("session: 🔴")
    window.keymasqd_status.set_label("keymasqd: ⚪")
    window._mark_device_runtime_unknown()
    window._update_compositor_dispatch_state(None)
    window._set_connection_issue("session")


def _set_connection_issue(window, issue: str | None) -> None:
    if window.demo_mode:
        return

    if issue is None:
        window._connection_issue = None
        if window._connection_dialog:
            window._connection_dialog.close()
        return

    if issue == window._connection_issue and window._connection_dialog:
        return

    window._connection_issue = issue

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

    if not window._connection_dialog:
        dialog = _runtime.Adw.Dialog(title="Connection Error", content_width=560, content_height=-1)
        if hasattr(dialog, "set_modal"):
            dialog.set_modal(True)

        box = _runtime.Gtk.Box(orientation=_runtime.Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(20)
        box.set_margin_end(20)

        title_label = _runtime.Gtk.Label()
        title_label.add_css_class("title-2")
        title_label.set_halign(_runtime.Gtk.Align.START)
        box.append(title_label)

        body_label = _runtime.Gtk.Label()
        body_label.set_halign(_runtime.Gtk.Align.START)
        body_label.set_wrap(True)
        box.append(body_label)

        btn_row = _runtime.Gtk.Box(orientation=_runtime.Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(_runtime.Gtk.Align.END)

        quit_btn = _runtime.Gtk.Button(label="Quit")
        quit_btn.connect("clicked", window._on_quit_clicked)
        btn_row.append(quit_btn)

        retry_btn = _runtime.Gtk.Button(label="Retry")
        retry_btn.add_css_class("suggested-action")
        retry_btn.connect("clicked", window._on_retry_connection_clicked)
        btn_row.append(retry_btn)

        box.append(btn_row)
        dialog.set_child(box)
        dialog.connect("closed", window._on_connection_dialog_closed)

        window._connection_dialog = dialog
        window._connection_title_label = title_label
        window._connection_body_label = body_label

    if window._connection_title_label:
        window._connection_title_label.set_text(f"⚠️ {title}")
    if window._connection_body_label:
        window._connection_body_label.set_text(body)

    dialog = window._connection_dialog
    if dialog is not None:
        dialog.present(window)


def _on_connection_dialog_closed(window, dialog) -> None:
    if dialog is window._connection_dialog:
        window._connection_dialog = None
        window._connection_title_label = None
        window._connection_body_label = None


def _retry_connection_check(window) -> None:
    window._update_status_from_session()


def _on_quit_clicked(window, _button: _runtime.Gtk.Button) -> None:
    window.get_application().quit()


def _on_retry_connection_clicked(window, _button: _runtime.Gtk.Button) -> None:
    window._retry_connection_check()
