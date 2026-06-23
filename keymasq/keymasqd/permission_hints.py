import errno

PERMISSION_TROUBLESHOOTING_REF = (
    "docs/TROUBLESHOOTING.md#uinput-or-input-device-access-problems"
)
INPUT_DEVICE_PERMISSION_HINT = (
    "Check that keymasqd can read /dev/input/event*; see "
    f"{PERMISSION_TROUBLESHOOTING_REF}."
)
UINPUT_PERMISSION_HINT = (
    "Check that keymasqd can write /dev/uinput; see "
    f"{PERMISSION_TROUBLESHOOTING_REF}."
)


def is_permission_error(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    return isinstance(exc, OSError) and exc.errno in {errno.EACCES, errno.EPERM}


def has_permission_hint(message: object) -> bool:
    text = str(message)
    return "/dev/input/event*" in text or "/dev/uinput" in text


def input_device_permission_message(message: str) -> str:
    return _append_hint(message, INPUT_DEVICE_PERMISSION_HINT)


def uinput_permission_message(message: str) -> str:
    return _append_hint(message, UINPUT_PERMISSION_HINT)


def _append_hint(message: str, hint: str) -> str:
    if has_permission_hint(message):
        return message
    return f"{message}. {hint}"
