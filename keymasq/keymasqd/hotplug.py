"""Kernel device notifications that wake the masking monitor without privileges."""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Callable

log = logging.getLogger("keymasqd.masking")
NETLINK_KOBJECT_UEVENT = 15
KERNEL_GROUP = 1
ACTIONS = frozenset({b"add", b"remove", b"bind", b"unbind", b"change"})
SUBSYSTEMS = frozenset({b"usb", b"hid", b"input", b"hidraw", b"bluetooth"})
BUFFER = 1 << 16


def relevant(datagram: bytes) -> bool:
    """True for kernel uevents that can change physical input attachments."""
    header, _, environment = datagram.partition(b"\0")
    action, separator, _path = header.partition(b"@")
    if not separator or action not in ACTIONS:
        return False  # libudev's own multicast frames and malformed data.
    for field in environment.split(b"\0"):
        if field.startswith(b"SUBSYSTEM="):
            return field[len(b"SUBSYSTEM=") :] in SUBSYSTEMS
    return False


class HotplugWatcher:
    """Subscribe to kernel uevents and call back on relevant device changes."""

    def __init__(self, changed: Callable[[], None]) -> None:
        self.changed = changed
        self.socket: socket.socket | None = None

    @property
    def available(self) -> bool:
        return self.socket is not None

    @staticmethod
    def open_socket() -> socket.socket:
        """Bind to the kernel uevent multicast group, which needs no privileges."""
        sock = socket.socket(
            socket.AF_NETLINK, socket.SOCK_RAW | socket.SOCK_NONBLOCK, NETLINK_KOBJECT_UEVENT
        )
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, BUFFER)
            sock.bind((0, KERNEL_GROUP))
        except BaseException:
            sock.close()
            raise
        return sock

    def start(self) -> bool:
        if self.socket is not None:
            return True
        try:
            sock = self.open_socket()
            try:
                asyncio.get_running_loop().add_reader(sock.fileno(), self._readable)
            except BaseException:
                sock.close()
                raise
        except (OSError, AttributeError, RuntimeError) as exc:
            log.info("Hotplug notifications unavailable; polling for hardware changes: %s", exc)
            return False
        self.socket = sock
        return True

    def _readable(self) -> None:
        sock = self.socket
        if sock is None:
            return
        wake = False
        while True:
            try:
                datagram = sock.recv(BUFFER)
            except BlockingIOError:
                break
            except OSError as exc:
                log.warning("Hotplug notifications stopped; polling instead: %s", exc)
                self.stop()
                return
            if not datagram:
                break
            wake = wake or relevant(datagram)
        if wake:
            self.changed()

    def stop(self) -> None:
        sock, self.socket = self.socket, None
        if sock is None:
            return
        try:
            asyncio.get_running_loop().remove_reader(sock.fileno())
        except (RuntimeError, ValueError, OSError):
            pass
        sock.close()
