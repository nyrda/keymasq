"""Shared masking phases and request budgets; phase values are persistent IPC data."""

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
