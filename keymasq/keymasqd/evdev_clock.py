import fcntl
import logging
import struct
import time
from collections.abc import Callable
from typing import Final, cast

log = logging.getLogger("keymasqd.evdev_clock")

_IOC_NRBITS: Final = 8
_IOC_TYPEBITS: Final = 8
_IOC_SIZEBITS: Final = 14

_IOC_NRSHIFT: Final = 0
_IOC_TYPESHIFT: Final = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT: Final = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT: Final = _IOC_SIZESHIFT + _IOC_SIZEBITS

_IOC_WRITE: Final = 1

CLOCK_MONOTONIC_ID: Final = int(getattr(time, "CLOCK_MONOTONIC", 1))
EVIOCSCLOCKID: Final = (
    (_IOC_WRITE << _IOC_DIRSHIFT)
    | (ord("E") << _IOC_TYPESHIFT)
    | (0xA0 << _IOC_NRSHIFT)
    | (struct.calcsize("i") << _IOC_SIZESHIFT)
)


def set_evdev_clock_monotonic(
    device: object,
    *,
    device_path: str | None = None,
    logger: logging.Logger | None = None,
) -> bool:
    """Ask evdev to use CLOCK_MONOTONIC timestamps for this client fd."""
    fileno = getattr(device, "fileno", None)
    if not callable(fileno):
        return False

    try:
        fd = int(cast(Callable[[], int], fileno)())
        fcntl.ioctl(fd, EVIOCSCLOCKID, struct.pack("i", CLOCK_MONOTONIC_ID))
    except OSError as exc:
        active_log = logger or log
        suffix = f" for {device_path}" if device_path else ""
        active_log.debug("Failed to set monotonic evdev clock%s: %s", suffix, exc)
        return False
    return True
