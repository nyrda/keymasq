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
SOURCE_HIDING_TROUBLESHOOTING_REF = "docs/TROUBLESHOOTING.md#source-hiding-jobs-fail"
SOURCE_HIDING_JOB_HINT = (
    "Check that keymasq-hardware@.service and its Polkit rule are installed and that "
    f"the keymasq user may start it; see {SOURCE_HIDING_TROUBLESHOOTING_REF}."
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


def input_device_permission_message(message: str) -> str:
    return _append_hint(message, INPUT_DEVICE_PERMISSION_HINT)


def uinput_permission_message(message: str) -> str:
    return _append_hint(message, UINPUT_PERMISSION_HINT)


def source_hiding_job_message(message: str) -> str:
    return _append_hint(
        message,
        SOURCE_HIDING_JOB_HINT,
        ref=SOURCE_HIDING_TROUBLESHOOTING_REF,
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
