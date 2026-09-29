"""Shared masking phases and request budgets; phase values are persistent IPC data."""

import time
from enum import StrEnum

# A restore can wait for an in-flight job before starting its own recovery job.
# Leave transport headroom at each outer boundary, including inventory requests
# queued behind serialized administrative work.
HARDWARE_JOB_TIMEOUT = 75.0
HARDWARE_COMMAND_TIMEOUT = 2 * HARDWARE_JOB_TIMEOUT + 10.0
HARDWARE_GUI_TIMEOUT = HARDWARE_COMMAND_TIMEOUT + 5.0


class MaskPhase(StrEnum):
    UNMASKED = "unmasked"
    APPLYING = "applying"
    ACQUIRING = "acquiring"
    TRIAL = "trial"
    MASKED = "masked"
    RESTORING = "restoring"
    RESTORED = "restored"
    RECOVERY_FAILED = "recovery_failed"


class SavedMaskLifecycle(StrEnum):
    """What the daemon is doing about a saved mask; derived, never authorizing."""

    OFF = "off"
    STARTING = "starting"
    WAITING_FOR_DEVICE = "waiting_for_device"
    SAVED_INCOMPLETE = "saved_incomplete"
    ATTENTION = "attention"
    ACTIVATING = "activating"
    TRIAL = "trial"
    MASKED = "masked"
    RECOVERING = "recovering"


def is_active(phase: object) -> bool:
    return phase in {MaskPhase.APPLYING, MaskPhase.ACQUIRING, MaskPhase.TRIAL, MaskPhase.MASKED}


def is_recovering(phase: object) -> bool:
    return phase in {MaskPhase.RESTORING, MaskPhase.RECOVERY_FAILED}


def application_display_name(name: object) -> str:
    app = str(name or "Another app")
    return {
        "steam": "Steam",
        "steamwebhelper": "Steam",
        "winedevice.exe": "Wine",
        "wine64-preloader": "Wine",
        "wine-preloader": "Wine",
    }.get(app, app)


def last_seen_text(mask: dict) -> str:
    seen = mask.get("last_seen")
    if not isinstance(seen, (int, float)) or isinstance(seen, bool) or seen <= 0:
        return ""
    return "Last seen " + time.strftime("%Y-%m-%d %H:%M", time.localtime(seen))


def retry_suffix(mask: dict) -> str:
    seconds = mask.get("next_retry_seconds", 0)
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds > 0:
        return f" · retrying in {int(seconds)}s"
    return ""


def device_in_use(mask: dict) -> bool:
    return mask.get("error_code") == "device_in_use" or "direct USB or auxiliary HID" in str(
        mask.get("error", "")
    )


def failure_message(mask: dict) -> str:
    if mask.get("state") == MaskPhase.RECOVERY_FAILED:
        return "Device access could not be restored yet. Keymasq is retrying."
    if mask.get("lifecycle") == SavedMaskLifecycle.SAVED_INCOMPLETE:
        # Not a failure: the record predates root-recorded selectors and is
        # completed by the next connected confirmation.
        return ""
    if mask.get("state") == MaskPhase.RESTORED and mask.get("reason") == "user_restore":
        return ""
    error = str(mask.get("error", ""))
    if device_in_use(mask):
        app = application_display_name(mask.get("blocking_application"))
        return f"{app} is using this device directly. Close it, then retry."
    return (
        "Masking could not be started. Retry, or open device details for more information."
        if error
        else ""
    )


def mask_enabled(mask: dict) -> bool:
    return bool(
        mask.get(
            "enabled",
            is_active(mask.get("state", MaskPhase.UNMASKED))
            or (
                mask.get("has_saved_mask")
                and mask.get("persist")
                and not mask.get("remapping_suspended")
            ),
        )
    )


def mask_status(
    device: dict,
    mask: dict,
    *,
    paused: bool,
    busy: bool = False,
    request_error: str = "",
) -> tuple[str, str]:
    """Return the user-facing status line and failure message for one device."""
    state = mask.get("state", MaskPhase.UNMASKED)
    connected = bool(device.get("supported"))
    enabled = mask_enabled(mask)
    error = request_error or failure_message(mask)
    lifecycle = str(mask.get("lifecycle", ""))
    # Losing the radio/USB connection is an expected state, not a failure
    # dialog. The saved switch remains on until the user turns it off.
    if not connected and enabled and not is_recovering(state) and not request_error:
        if lifecycle == SavedMaskLifecycle.SAVED_INCOMPLETE:
            return "Waiting for device · confirm again when connected", ""
        if lifecycle == SavedMaskLifecycle.ATTENTION and error:
            # Arming saved rules failed while unplugged; unlike a missing
            # device this is actionable, so keep the failure visible.
            return "Waiting for device" + retry_suffix(mask), error
        return "Waiting for device", ""
    if busy:
        return "Updating…", error
    if state == MaskPhase.RESTORING:
        return "Turning off…", error
    if error:
        if state == MaskPhase.RECOVERY_FAILED:
            return "Couldn’t restore access", error
        return "Couldn’t mask" + retry_suffix(mask), error
    if state in {MaskPhase.APPLYING, MaskPhase.ACQUIRING}:
        status = "Reconnecting device…"
    elif state == MaskPhase.TRIAL:
        status = "Masked · awaiting confirmation"
    elif state == MaskPhase.MASKED:
        status = "Masked"
    elif paused:
        status = "Off · remapping stopped by recovery"
    elif not connected:
        status = "Not connected"
    elif enabled:
        status = "Not masked"
    elif mask.get("reason") == "trial_expired" and not mask.get("automatic"):
        status = "Off · confirmation timed out"
    else:
        status = "Off"
    return status, ""
