import errno

PERMISSION_TROUBLESHOOTING_REF = "docs/TROUBLESHOOTING.md#uinput-or-input-device-access-problems"
INPUT_DEVICE_PERMISSION_HINT = (
    f"Check that keymasqd can read /dev/input/event*; see {PERMISSION_TROUBLESHOOTING_REF}."
)
UINPUT_PERMISSION_HINT = (
    f"Check that keymasqd can write /dev/uinput; see {PERMISSION_TROUBLESHOOTING_REF}."
)
UINPUT_PERMISSION_ERROR_MARKERS = (
    "cannot be opened for writing",
    "permission",
    "not permitted",
    "access denied",
)
CAPABILITY_TROUBLESHOOTING_REF = (
    "docs/TROUBLESHOOTING.md#missing-cap_dac_override-capability"
)
CAPABILITY_PERMISSION_HINT = (
    "Check that keymasqd.service grants AmbientCapabilities=CAP_DAC_OVERRIDE; see "
    f"{CAPABILITY_TROUBLESHOOTING_REF}."
)
CAPABILITY_PERMISSION_ERROR_MARKERS = (
    "permission denied",
    "operation not permitted",
)


def is_permission_error(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    return isinstance(exc, OSError) and exc.errno in {errno.EACCES, errno.EPERM}


def is_uinput_permission_error(exc: BaseException) -> bool:
    if is_permission_error(exc):
        return True
    if exc.__class__.__name__ != "UInputError":
        return False
    message = str(exc).lower()
    return "/dev/uinput" in message and any(
        marker in message for marker in UINPUT_PERMISSION_ERROR_MARKERS
    )


def has_permission_hint(message: object) -> bool:
    text = str(message)
    return PERMISSION_TROUBLESHOOTING_REF in text


def is_capability_permission_failure(stderr_text: object) -> bool:
    text = str(stderr_text).lower()
    return any(marker in text for marker in CAPABILITY_PERMISSION_ERROR_MARKERS)


def input_device_permission_message(message: str) -> str:
    return _append_hint(message, INPUT_DEVICE_PERMISSION_HINT)


def uinput_permission_message(message: str) -> str:
    return _append_hint(message, UINPUT_PERMISSION_HINT)


def capability_permission_message(message: str) -> str:
    return _append_hint(
        message,
        CAPABILITY_PERMISSION_HINT,
        ref=CAPABILITY_TROUBLESHOOTING_REF,
    )


def _append_hint(
    message: str,
    hint: str,
    *,
    ref: str = PERMISSION_TROUBLESHOOTING_REF,
) -> str:
    if ref in message:
        return message
    return f"{message}. {hint}"
