# pyright: reportUnusedFunction=false

from __future__ import annotations

import logging

from . import _runtime

log = logging.getLogger("keymasq.gui.window")


def lock_lease_on_close(window) -> None:
    if not window._recording_refresh_lease_id:
        return
    try:
        log.info("Window closing: locking runtime capture unlock lease")
        result = _runtime.session_request(
            {
                "command": "lock_recording_unlock",
                "lease_id": window._recording_refresh_lease_id,
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
        log.exception("Failed to lock runtime unlock lease on close")


def _update_unlock_state(window, status_data: dict | None) -> None:
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
        window._emergency_cancel_combo_enabled = bool(
            status_data.get("emergency_cancel_combo_enabled", True)
        )

    if unlocked is None or unlock_required is None or source is None or expires_at is None:
        local_status = _runtime.resolve_unlock_status(_runtime.os.getuid())
        unlocked = local_status.get("unlocked", False)
        unlock_required = True
        source = local_status.get("source", "none")
        expires_at = local_status.get("expires_at", 0)
        refresh_owner = bool(window._recording_refresh_lease_id)

    raw_unlocked = bool(unlocked)
    window._recording_unlock_required = bool(unlock_required)
    window._recording_unlocked = raw_unlocked or not window._recording_unlock_required
    window._recording_unlock_source = str(source or "none")
    window._recording_unlock_expires_at = int(expires_at or 0)
    window._recording_refresh_owner = (
        bool(refresh_owner) if window._recording_unlock_required else False
    )

    log.debug(
        (
            "Unlock status updated: unlocked=%s required=%s source=%s "
            "expires_at=%s owner=%s lease_claimed=%s"
        ),
        window._recording_unlocked,
        window._recording_unlock_required,
        window._recording_unlock_source,
        window._recording_unlock_expires_at,
        window._recording_refresh_owner,
        bool(window._recording_refresh_lease_id),
    )

    if not window._recording_unlocked:
        window._recording_refresh_lease_id = ""
        window._recording_claim_attempt_key = None
    elif not window._recording_unlock_required:
        window._recording_refresh_lease_id = ""
        window._recording_claim_attempt_key = None
    elif not window._recording_refresh_lease_id and raw_unlocked:
        claim_key = (window._recording_unlock_source, window._recording_unlock_expires_at)
        if window._recording_claim_attempt_key != claim_key:
            window._recording_claim_attempt_key = claim_key
            window._request_recording_refresh_lease()

    window._refresh_macro_menu_state()
    window._refresh_unlock_status_label()


def emergency_cancel_combo_enabled(window) -> bool:
    return bool(window._emergency_cancel_combo_enabled)


def present_unlock_dialog(window, on_success=None) -> None:
    window._start_recording_unlock(on_success=on_success)


def _start_recording_unlock(window, on_success=None) -> None:
    if window._unlock_request_inflight:
        return

    if window.demo_mode:
        window._show_unlock_error_dialog("Capture unlock is unavailable in demo mode.")
        return

    window._unlock_request_inflight = True
    if window._menu_unlock_btn is not None:
        window._menu_unlock_btn.set_sensitive(False)

    def worker() -> tuple[str, dict | None]:
        error_msg = ""
        claim_response: dict | None = None
        uid = _runtime.os.getuid()
        helper_path = _runtime.resolve_keymasq_record_helper_path()
        if helper_path is None:
            return f"Recording helper not found at {_runtime.KEYMASQ_RECORD_HELPER_PATH}", None

        runtime_cmd = [
            "pkexec",
            helper_path,
            "unlock-runtime",
            "--uid",
            str(uid),
            "--ttl",
            "60",
        ]
        runtime_completed = _runtime.subprocess.run(runtime_cmd, capture_output=True, text=True)
        if runtime_completed.returncode != 0:
            error_msg = (
                runtime_completed.stderr.strip()
                or runtime_completed.stdout.strip()
                or "Authorization failed"
            )

        if not error_msg:
            claim_response = _runtime.session_request(
                {"command": "claim_recording_unlock_refresh"},
                timeout=3.0,
            )
            if not isinstance(claim_response, dict):
                error_msg = (
                    "Authorization succeeded, but keymasq-session did not grant "
                    "the capture unlock lease."
                )
            elif claim_response.get("status") != "ok":
                error_msg = str(
                    claim_response.get("message")
                    or "keymasq-session did not grant the capture unlock lease."
                )

        return error_msg, claim_response

    def on_worker_done(result: _runtime.GuiTaskResult[tuple[str, dict | None]]) -> bool:
        success = result.ok and isinstance(result.value, tuple) and result.value[0] == ""
        error_msg = (
            result.value[0]
            if result.ok and isinstance(result.value, tuple)
            else "Authorization failed"
        )
        claim_response = result.value[1] if result.ok and isinstance(result.value, tuple) else None
        return window._on_unlock_finished(
            success,
            error_msg,
            on_success,
            claim_response,
        )

    _runtime.run_gui_task(
        worker,
        on_worker_done,
    )


def _on_unlock_finished(
    window,
    success: bool,
    error_msg: str,
    on_success,
    claim_response: dict | None = None,
) -> bool:
    window._unlock_request_inflight = False
    if window._menu_unlock_btn is not None:
        window._menu_unlock_btn.set_sensitive(True)

    if success:
        lease_id = ""
        if isinstance(claim_response, dict) and claim_response.get("status") == "ok":
            lease_id = str(claim_response.get("lease_id", "") or "").strip()

        if lease_id:
            window._recording_refresh_lease_id = lease_id
            window._recording_claim_attempt_key = None
            window._update_unlock_state(claim_response)
        else:
            window._update_unlock_state(None)
            window._request_recording_refresh_lease()
        if callable(on_success):
            on_success()
        return False

    window._show_unlock_error_dialog(error_msg or "Authorization failed")
    return False


def _show_unlock_error_dialog(window, message: str) -> None:
    dialog = _runtime.Adw.AlertDialog(
        heading="Unlock Failed",
        body=(message or "Keymasq could not authorize raw original-input capture for this GUI."),
    )
    dialog.add_response("ok", "OK")
    dialog.present(window)


def _refresh_macro_menu_state(window) -> None:
    if window._menu_unlock_btn is not None:
        window._menu_unlock_btn.set_visible(not window._recording_unlocked)
    if window._menu_unlock_separator is not None:
        window._menu_unlock_separator.set_visible(not window._recording_unlocked)

    app = window.get_application()
    if app is not None:
        action = app.lookup_action("record-macro")
        if action is not None:
            action.set_enabled(window._recording_unlocked)


def _refresh_unlock_status_label(window) -> None:
    if window._unlock_status_label is None:
        return

    show_unlock_status = window._recording_unlock_required and window._recording_unlocked
    window._unlock_status_label.set_visible(show_unlock_status)
    if not show_unlock_status:
        window._unlock_status_label.set_label("")
        window._unlock_status_label.set_tooltip_text(None)
        return

    if window._recording_unlock_source == "runtime":
        text = "capture: 🟢 runtime unlock"
    elif window._recording_unlock_source == "persistent":
        text = "capture: 🟢 persistent unlock"
    else:
        text = "capture: 🟢 unlocked"

    owner_text = "owner=yes" if window._recording_refresh_owner else "owner=no"
    lease_text = "lease=claimed" if window._recording_refresh_lease_id else "lease=none"
    tooltip = (
        f"{owner_text}\n"
        f"{lease_text}\n"
        f"source={window._recording_unlock_source}\n"
        f"expires_at={window._recording_unlock_expires_at}"
    )
    window._unlock_status_label.set_label(text)
    window._unlock_status_label.set_tooltip_text(tooltip)


def _refresh_unlock_lease(window) -> bool:
    if window.demo_mode:
        return False

    if not window._recording_unlock_required:
        return True

    if not window._recording_unlocked:
        return True

    if window._recording_unlock_source != "runtime":
        return True

    if window._unlock_refresh_inflight:
        return True

    if not window._recording_refresh_lease_id:
        window._request_recording_refresh_lease()
        if not window._recording_refresh_lease_id:
            return True

    window._unlock_refresh_inflight = True

    _runtime.session_request_async(
        {
            "command": "refresh_recording_unlock",
            "lease_id": window._recording_refresh_lease_id,
        },
        window._on_refresh_unlock_finished,
        timeout=3.0,
    )
    return True


def _on_refresh_unlock_finished(window, result: dict | None) -> bool:
    window._unlock_refresh_inflight = False

    if result and result.get("status") == "ok":
        log.debug("Runtime unlock refresh succeeded")
        window._update_unlock_state(result)
        return False

    if result:
        log.warning(
            "Runtime unlock refresh failed: error_code=%s message=%s",
            result.get("error_code"),
            result.get("message"),
        )
    else:
        log.warning("Runtime unlock refresh failed: no response from session")
    window._recording_refresh_lease_id = ""
    window._update_unlock_state(None)
    return False


def _request_recording_refresh_lease(window) -> None:
    if window.demo_mode:
        return

    if not window._recording_unlock_required:
        return

    if not window._recording_unlocked:
        return

    if window._lease_claim_inflight:
        return

    log.debug("Requesting recording refresh lease from session")
    window._lease_claim_inflight = True
    _runtime.session_request_async(
        {"command": "claim_recording_unlock_refresh"},
        window._on_claim_recording_refresh_lease_finished,
        timeout=3.0,
        on_done=window._on_claim_recording_refresh_lease_done,
    )


def _on_claim_recording_refresh_lease_done(window) -> None:
    window._lease_claim_inflight = False


def _on_claim_recording_refresh_lease_finished(window, response: dict | None) -> bool:
    if not response or response.get("status") != "ok":
        if response:
            log.warning(
                "Recording refresh lease claim failed: error_code=%s message=%s",
                response.get("error_code"),
                response.get("message"),
            )
        else:
            log.warning("Recording refresh lease claim failed: no response from session")
        window._recording_refresh_lease_id = ""
        return False

    lease_id = str(response.get("lease_id", "") or "").strip()
    if not lease_id:
        log.warning("Recording refresh lease claim failed: missing lease_id")
        window._recording_refresh_lease_id = ""
        return False

    window._recording_refresh_lease_id = lease_id
    log.info("Recording refresh lease claimed successfully")
    window._update_unlock_state(response)
    window._recording_claim_attempt_key = None
    return False
