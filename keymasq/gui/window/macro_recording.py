# pyright: reportUnusedFunction=false

from __future__ import annotations

import logging
from collections.abc import Callable

from . import _runtime, connection, recording_unlock

log = logging.getLogger("keymasq.gui.window")


def _on_recording_stopped(window, event: dict) -> None:
    present_pending_macro_save_dialog(window, event)


def present_pending_macro_save_dialog(window, recording_data: dict | None = None) -> bool:
    from keymasq.gui.widgets.save_macro_dialog import SaveMacroDialog

    if window._save_macro_dialog is not None:
        window._save_macro_dialog.present(window)
        return True

    if recording_data is None:
        return False

    dialog = SaveMacroDialog(window, recording_data)

    def on_save_macro_dialog_closed(dialog) -> None:
        _on_save_macro_dialog_closed(window, dialog)

    dialog.connect("closed", on_save_macro_dialog_closed)
    window._save_macro_dialog = dialog
    dialog.present(window)
    return True


def _on_save_macro_dialog_closed(window, dialog) -> None:
    if dialog is window._save_macro_dialog:
        window._save_macro_dialog = None


def _close_dialogs_for_recording_start(window) -> None:
    for dialog in (window._record_macro_dialog, window._macro_manager_dialog):
        if dialog is not None:
            dialog.close()


def set_macro_manager_dialog(window, dialog: _runtime.Adw.Dialog | None) -> None:
    window._macro_manager_dialog = dialog


def _update_macro_recording_state(window, status_data: dict | None) -> None:
    enabled = None
    source = None
    expires_at = None

    if isinstance(status_data, dict) and status_data.get("status", "ok") == "ok":
        enabled = status_data.get("macro_recording_enabled")
        source = status_data.get("macro_recording_source")
        expires_at = status_data.get("macro_recording_expires_at")

    if enabled is None or source is None or expires_at is None:
        return

    window._macro_recording_enabled = bool(enabled)
    window._macro_recording_source = str(source or "none")
    window._macro_recording_expires_at = int(expires_at or 0)
    log.debug(
        "Macro recording status updated: enabled=%s source=%s expires_at=%s",
        window._macro_recording_enabled,
        window._macro_recording_source,
        window._macro_recording_expires_at,
    )
    recording_unlock._refresh_macro_menu_state(window)


def macro_recording_enabled(window) -> bool:
    return bool(window._macro_recording_enabled)


def _refresh_macro_recording_state_from_session(
    window,
    on_status: Callable[[dict | None], None] | None = None,
) -> None:
    def _on_status(status: dict | None) -> bool:
        status_data = status if isinstance(status, dict) else None
        if status_data is not None:
            _update_macro_recording_state(window, status_data)
        if on_status is not None:
            on_status(status_data)
        return False

    _runtime.session_request_async({"command": "get_status"}, _on_status, timeout=1.0)


def present_macro_recording_enable_dialog(window, on_success=None) -> None:
    def _after_refresh(_status: dict | None) -> None:
        if window._macro_recording_enabled:
            if callable(on_success):
                on_success()
            return

        dialog = _runtime.Adw.AlertDialog(
            heading="Enable Macro Recording",
            body=(
                "Macro recording is disabled until you opt in. Enabling it allows "
                "recording triggers and the Macro Manager record button to create "
                "temporary recording slots."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("enable", "Enable")
        dialog.set_default_response("enable")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("enable", _runtime.Adw.ResponseAppearance.SUGGESTED)

        def on_response(dialog: _runtime.Adw.AlertDialog, response: str) -> None:
            _on_macro_recording_enable_response(window, dialog, response, on_success)

        dialog.connect("response", on_response)
        dialog.present(window)

    _refresh_macro_recording_state_from_session(window, _after_refresh)


def present_macro_recording_disable_dialog(window, on_success=None) -> None:
    def _after_refresh(_status: dict | None) -> None:
        if not window._macro_recording_enabled:
            if callable(on_success):
                on_success()
            return

        dialog = _runtime.Adw.AlertDialog(
            heading="Disable Macro Recording",
            body=(
                "This turns off macro recording opt-in. Existing saved macros can still be played."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("disable", "Disable")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("disable", _runtime.Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(dialog: _runtime.Adw.AlertDialog, response: str) -> None:
            _on_macro_recording_disable_response(window, dialog, response, on_success)

        dialog.connect("response", on_response)
        dialog.present(window)

    _refresh_macro_recording_state_from_session(window, _after_refresh)


def _on_macro_recording_enable_response(
    window,
    _dialog: _runtime.Adw.AlertDialog,
    response: str,
    on_success,
) -> None:
    if response != "enable":
        return
    _start_macro_recording_enable(window, on_success=on_success)


def _on_macro_recording_disable_response(
    window,
    _dialog: _runtime.Adw.AlertDialog,
    response: str,
    on_success,
) -> None:
    if response != "disable":
        return
    _start_macro_recording_disable(window, on_success=on_success)


def _start_macro_recording_enable(window, on_success=None) -> None:
    if window._macro_recording_enable_inflight:
        return

    if window.demo_mode:
        recording_unlock._show_unlock_error_dialog(
            window, "Macro recording opt-in is unavailable in demo mode."
        )
        return

    window._macro_recording_enable_inflight = True

    def worker() -> tuple[str, dict | None]:
        uid = _runtime.os.getuid()
        helper_path = _runtime.resolve_keymasq_record_helper_path()
        if helper_path is None:
            return f"Recording helper not found at {_runtime.KEYMASQ_RECORD_HELPER_PATH}", None

        cmd = [
            "pkexec",
            helper_path,
            "enable-macro-recording-persistent",
            "--uid",
            str(uid),
        ]
        completed = _runtime.subprocess.run(cmd, capture_output=True, text=True)
        if completed.returncode != 0:
            error_msg = (
                completed.stderr.strip() or completed.stdout.strip() or "Authorization failed"
            )
            return error_msg, None

        status = _runtime.session_request({"command": "get_status"}, timeout=3.0)
        if not isinstance(status, dict):
            return "Macro recording was enabled, but keymasq-session did not respond.", None
        if status.get("status") != "ok":
            message = status.get("message") or "Failed to refresh macro recording status."
            return str(message), None
        return "", status

    def on_worker_done(result: _runtime.GuiTaskResult[tuple[str, dict | None]]) -> bool:
        success = result.ok and isinstance(result.value, tuple) and result.value[0] == ""
        error_msg = (
            result.value[0]
            if result.ok and isinstance(result.value, tuple)
            else "Authorization failed"
        )
        status = result.value[1] if result.ok and isinstance(result.value, tuple) else None
        return _on_macro_recording_enable_finished(
            window,
            success,
            error_msg,
            on_success,
            status,
        )

    _runtime.run_gui_task(worker, on_worker_done)


def _start_macro_recording_disable(window, on_success=None) -> None:
    if window._macro_recording_enable_inflight:
        return

    if window.demo_mode:
        recording_unlock._show_unlock_error_dialog(
            window, "Macro recording opt-out is unavailable in demo mode."
        )
        return

    window._macro_recording_enable_inflight = True

    def worker() -> tuple[str, dict | None]:
        uid = _runtime.os.getuid()
        helper_path = _runtime.resolve_keymasq_record_helper_path()
        if helper_path is None:
            return f"Recording helper not found at {_runtime.KEYMASQ_RECORD_HELPER_PATH}", None

        cmd = [
            "pkexec",
            helper_path,
            "disable-macro-recording",
            "--uid",
            str(uid),
        ]
        completed = _runtime.subprocess.run(cmd, capture_output=True, text=True)
        if completed.returncode != 0:
            error_msg = (
                completed.stderr.strip() or completed.stdout.strip() or "Authorization failed"
            )
            return error_msg, None

        status = _runtime.session_request({"command": "get_status"}, timeout=3.0)
        if not isinstance(status, dict):
            return "Macro recording was disabled, but keymasq-session did not respond.", None
        if status.get("status") != "ok":
            message = status.get("message") or "Failed to refresh macro recording status."
            return str(message), None
        return "", status

    def on_worker_done(result: _runtime.GuiTaskResult[tuple[str, dict | None]]) -> bool:
        success = result.ok and isinstance(result.value, tuple) and result.value[0] == ""
        error_msg = (
            result.value[0]
            if result.ok and isinstance(result.value, tuple)
            else "Authorization failed"
        )
        status = result.value[1] if result.ok and isinstance(result.value, tuple) else None
        return _on_macro_recording_enable_finished(
            window,
            success,
            error_msg,
            on_success,
            status,
        )

    _runtime.run_gui_task(worker, on_worker_done)


def _on_macro_recording_enable_finished(
    window,
    success: bool,
    error_msg: str,
    on_success,
    status: dict | None = None,
) -> bool:
    window._macro_recording_enable_inflight = False
    if success:
        _update_macro_recording_state(window, status)
        connection._update_status_from_session(window)
        if callable(on_success):
            on_success()
        return False

    recording_unlock._show_unlock_error_dialog(window, error_msg or "Authorization failed")
    return False


def present_recording_settings_dialog(window, reason: str = "settings") -> None:
    if window._record_macro_dialog is not None:
        window._record_macro_dialog.set_presentation_reason(reason)
        window._record_macro_dialog.present(window)
        return

    from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

    dialog = RecordMacroDialog(window, reason=reason)

    def on_record_macro_dialog_closed(dialog) -> None:
        _on_record_macro_dialog_closed(window, dialog)

    dialog.connect("closed", on_record_macro_dialog_closed)
    window._record_macro_dialog = dialog
    dialog.present(window)


def _on_record_macro_dialog_closed(window, dialog) -> None:
    if dialog is window._record_macro_dialog:
        window._record_macro_dialog = None
