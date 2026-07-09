# pyright: reportUnusedFunction=false

from __future__ import annotations

from collections.abc import Callable

from . import _runtime, compositor, gnome_setup, macro_recording, profiles, recording_unlock


def register(window) -> None:
    callback = window._session_event_callback
    if callback is None:

        def callback(event: dict) -> bool:
            return _on_session_event(window, event)

        window._session_event_callback = callback
    _runtime.register_session_event_callback("*", callback)


def unregister(window) -> None:
    callback = window._session_event_callback
    if callback is not None:
        _runtime.unregister_session_event_callback("*", callback)


def _on_session_event(window, event: dict) -> bool:
    _handle_session_event(window, event)
    return False


def _reconnect_session(window) -> bool:
    _update_status_from_session(window)
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
        _update_status_from_keymasqd_event(window, connected)
    elif event_type == "profiles_changed":
        profiles._apply_profile_runtime_state(window, event)
        if not bool(event.get("runtime_only", False)):
            profiles._queue_profile_reload(window)
    elif event_type == "recording_started":
        macro_recording._close_dialogs_for_recording_start(window)
        window._recording_overlay.set_visible(True)
        window._recording_overlay.on_started(event)
    elif event_type == "recording_stopped":
        window._recording_overlay.on_stopped()
        window._recording_overlay.set_visible(False)
        macro_recording._on_recording_stopped(window, event)
    elif event_type == "recording_progress":
        window._recording_overlay.on_progress(event)
    elif event_type == "recording_auth_requested":
        macro_recording.present_recording_settings_dialog(window, reason="recording_locked")
    elif event_type == "macro_recording_disabled":
        macro_recording._update_macro_recording_state(window, event)

    callbacks = window._event_handlers.get(event_type)
    if callbacks is None:
        return
    for cb in list(callbacks):
        cb(event)


def _update_status_from_keymasqd_event(window, keymasqd_connected: bool) -> None:
    if keymasqd_connected:
        window.session_status.set_label("session: 🟢")
        window.keymasqd_status.set_label("keymasqd: 🟢")
        _set_connection_issue(window, None)
    else:
        window.session_status.set_label("session: 🟡")
        window.keymasqd_status.set_label("keymasqd: 🔴")
        profiles._mark_device_runtime_unknown(window)
        _set_connection_issue(window, "keymasqd")


def _update_status_from_session(window) -> None:
    if window._status_query_inflight:
        return

    window._status_query_inflight = True
    window._status_query_id += 1
    query_id = window._status_query_id

    def on_status_response(data: dict | None, qid: int = query_id) -> bool:
        return _on_status_response(window, data, qid)

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
        recording_unlock._update_unlock_state(window, data if isinstance(data, dict) else None)
        macro_recording._update_macro_recording_state(
            window, data if isinstance(data, dict) else None
        )
        compositor._update_compositor_dispatch_state(
            window, data if isinstance(data, dict) else None
        )
        if isinstance(data, dict) and data.get("status") == "ok":
            profiles._apply_profile_runtime_state(window, data)
            _queue_initial_status_profile_reload(window)
            compositor_id = data.get("compositor_id")
            if compositor_id is not None:
                window._compositor_id = compositor_id
            details = data.get("compositor_details")
            if isinstance(details, dict):
                window._compositor_support_details = details
                window._compositor_supported = bool(details.get("supported", False))
            capabilities = data.get("compositor_capabilities")
            if isinstance(capabilities, list) and capabilities != window._compositor_capabilities:
                window._compositor_capabilities = list(capabilities)
                if window.combo_tab is not None:
                    window.combo_tab._compositor_capabilities = window._compositor_capabilities
                    window.combo_tab.refresh_profiles(
                        preferred_profile_name=window._selected_profile_name,
                        publish_selection=False,
                    )
            compositor._update_compositor_warning_banner(window)
            compositor._update_compositor_status(window)
            gnome_setup._close_gnome_setup_dialog_if_ready(window)

        if session_ok:
            if keymasqd_ok:
                window.session_status.set_label("session: 🟢")
                window.keymasqd_status.set_label("keymasqd: 🟢")
                _set_connection_issue(window, None)
            else:
                window.session_status.set_label("session: 🟡")
                window.keymasqd_status.set_label("keymasqd: 🔴")
                _set_connection_issue(window, "keymasqd")
        else:
            _update_status_disconnected(window)
    except (OSError, RuntimeError, TypeError, ValueError):
        recording_unlock._update_unlock_state(window, None)
        macro_recording._update_macro_recording_state(window, None)
        _update_status_disconnected(window)

    return False


def _queue_initial_status_profile_reload(window) -> None:
    if window.demo_mode or window._initial_status_profile_reload_done:
        return
    window._initial_status_profile_reload_done = True
    profiles._queue_profile_reload(window)


def _update_status_disconnected(window) -> None:
    window.session_status.set_label("session: 🔴")
    window.keymasqd_status.set_label("keymasqd: ⚪")
    profiles._mark_device_runtime_unknown(window)
    compositor._update_compositor_dispatch_state(window, None)
    _set_connection_issue(window, "session")


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

        def on_quit_clicked(button: _runtime.Gtk.Button) -> None:
            _on_quit_clicked(window, button)

        quit_btn.connect("clicked", on_quit_clicked)
        btn_row.append(quit_btn)

        retry_btn = _runtime.Gtk.Button(label="Retry")
        retry_btn.add_css_class("suggested-action")

        def on_retry_connection_clicked(button: _runtime.Gtk.Button) -> None:
            _on_retry_connection_clicked(window, button)

        retry_btn.connect("clicked", on_retry_connection_clicked)
        btn_row.append(retry_btn)

        box.append(btn_row)
        dialog.set_child(box)

        def on_connection_dialog_closed(dialog: _runtime.Adw.Dialog) -> None:
            _on_connection_dialog_closed(window, dialog)

        dialog.connect("closed", on_connection_dialog_closed)

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
    _update_status_from_session(window)


def _on_quit_clicked(window, _button: _runtime.Gtk.Button) -> None:
    window.get_application().quit()


def _on_retry_connection_clicked(window, _button: _runtime.Gtk.Button) -> None:
    _retry_connection_check(window)
