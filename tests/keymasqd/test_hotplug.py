"""Kernel hotplug notifications wake the masking monitor; parsing is strict."""

import asyncio
import socket

import pytest

from keymasq.keymasqd import hotplug


@pytest.mark.parametrize(
    ("datagram", "expected"),
    [
        (b"add@/devices/pci0000:00/usb1/1-3\0ACTION=add\0SUBSYSTEM=usb\0SEQNUM=1\0", True),
        (b"remove@/devices/x/0003:1532:00B4.0007\0ACTION=remove\0SUBSYSTEM=hid\0", True),
        (b"bind@/devices/x/input/input9\0ACTION=bind\0SUBSYSTEM=input\0", True),
        (b"change@/devices/x/hidraw/hidraw3\0ACTION=change\0SUBSYSTEM=hidraw\0", True),
        (b"add@/devices/x/hci0\0ACTION=add\0SUBSYSTEM=bluetooth\0", True),
        (b"change@/devices/virtual/block/loop0\0ACTION=change\0SUBSYSTEM=block\0", False),
        (b"online@/devices/system/cpu/cpu1\0ACTION=online\0SUBSYSTEM=cpu\0", False),
        (b"add@/devices/x\0ACTION=add\0", False),
        (b"libudev\xfe\xed\xca\xfe\0\0\0SUBSYSTEM=usb\0", False),
        (b"", False),
    ],
)
def test_only_input_related_kernel_events_are_relevant(datagram, expected):
    assert hotplug.relevant(datagram) is expected


@pytest.mark.asyncio
async def test_watcher_reports_unavailable_sockets_without_raising(monkeypatch):
    def refuse():
        raise PermissionError("netlink refused")

    watcher = hotplug.HotplugWatcher(lambda: None)
    monkeypatch.setattr(watcher, "open_socket", refuse)
    assert watcher.start() is False
    assert not watcher.available
    watcher.stop()


@pytest.mark.asyncio
async def test_watcher_wakes_only_for_relevant_datagrams(monkeypatch):
    left, right = socket.socketpair(type=socket.SOCK_DGRAM)
    right.setblocking(False)
    wakes = []
    watcher = hotplug.HotplugWatcher(lambda: wakes.append(True))
    monkeypatch.setattr(watcher, "open_socket", lambda: right)
    try:
        assert watcher.start() is True
        left.send(b"change@/devices/virtual/block/loop0\0ACTION=change\0SUBSYSTEM=block\0")
        await asyncio.sleep(0.05)
        assert wakes == []
        left.send(b"add@/devices/pci0000:00/usb1/1-3\0ACTION=add\0SUBSYSTEM=usb\0")
        left.send(b"add@/devices/pci0000:00/usb1/1-3/1-3:1.0\0ACTION=add\0SUBSYSTEM=usb\0")
        await asyncio.sleep(0.05)
        assert wakes == [True]
    finally:
        watcher.stop()
        left.close()
    assert not watcher.available


@pytest.mark.asyncio
async def test_hotplug_marks_the_coordinator_and_wakes_the_heartbeat():
    from types import SimpleNamespace

    from keymasq.keymasqd.hardware_masking import HardwareMasking

    masking = HardwareMasking(SimpleNamespace(masking_suspended=False))
    masking.coordinator.hardware_changed = False
    assert not masking.wake.is_set()
    masking.hardware_changed()
    assert masking.coordinator.hardware_changed
    assert masking.wake.is_set()
