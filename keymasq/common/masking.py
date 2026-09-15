"""Masking phases shared by the daemon and GUI; values are persistent IPC data."""

from enum import StrEnum


class MaskPhase(StrEnum):
    UNMASKED = "unmasked"
    APPLYING = "applying"
    ACQUIRING = "acquiring"
    TRIAL = "trial"
    MASKED = "masked"
    RESTORING = "restoring"
    RESTORED = "restored"
    RECOVERY_FAILED = "recovery_failed"


def is_active(phase: object) -> bool:
    return phase in {MaskPhase.APPLYING, MaskPhase.ACQUIRING, MaskPhase.TRIAL, MaskPhase.MASKED}


def is_transitional(phase: object) -> bool:
    """A replacement is being acquired or awaiting confirmation."""
    return phase in {MaskPhase.APPLYING, MaskPhase.ACQUIRING, MaskPhase.TRIAL}


def is_recovering(phase: object) -> bool:
    return phase in {MaskPhase.RESTORING, MaskPhase.RECOVERY_FAILED}
