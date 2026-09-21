"""Physical force-feedback calls for use in the proxy's worker threads."""

import ctypes
import fcntl
from typing import Final, Protocol, cast

import evdev

# Linux _IOW('E', nr, type), using the native ff_effect layout, including
# its pointer-sized periodic custom_data field. x86_64 and aarch64 share
# this ioctl encoding.
_EVIOCSFF: Final = (1 << 30) | (ctypes.sizeof(evdev.ff.Effect) << 16) | (ord("E") << 8) | 0x80
_EVIOCRMFF: Final = (1 << 30) | (ctypes.sizeof(ctypes.c_int) << 16) | (ord("E") << 8) | 0x81


class EvdevFeedbackSource(Protocol):
    fd: int
    path: str
    ff_effects_count: int

    def write(self, event_type: int, code: int, value: int) -> None: ...


class EvdevFeedbackDevice:
    def __init__(self, device: EvdevFeedbackSource) -> None:
        self.device = device

    @property
    def path(self) -> str:
        return self.device.path

    @property
    def ff_effects_count(self) -> int:
        return self.device.ff_effects_count

    def upload_effect(self, effect: object) -> int:
        # python-evdev's upload_effect holds the GIL across EVIOCSFF, even
        # in a worker thread. fcntl.ioctl releases it for a bytes argument.
        # Keep effect alive through the call in case custom_data points into
        # memory owned by it. The kernel writes the allocated id into our copy.
        data = bytes(cast(evdev.ff.Effect, effect))
        result = fcntl.ioctl(self.device.fd, _EVIOCSFF, data)
        return int(evdev.ff.Effect.from_buffer_copy(result).id)

    def erase_effect(self, ff_id: int) -> None:
        # EVIOCRMFF takes the id by value, despite its _IOW encoding.
        # The integer form of fcntl.ioctl also releases the GIL.
        fcntl.ioctl(self.device.fd, _EVIOCRMFF, ff_id)

    def write(self, event_type: int, code: int, value: int) -> None:
        self.device.write(event_type, code, value)
