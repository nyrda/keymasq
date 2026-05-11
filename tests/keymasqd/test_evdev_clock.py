import struct

from keymasq.keymasqd import evdev_clock


class _FdDevice:
    def fileno(self) -> int:
        return 42


def test_set_evdev_clock_monotonic_uses_eviocsclockid(monkeypatch) -> None:
    calls: list[tuple[int, int, bytes]] = []

    def fake_ioctl(fd: int, request: int, arg: bytes) -> None:
        calls.append((fd, request, arg))

    monkeypatch.setattr(evdev_clock.fcntl, "ioctl", fake_ioctl)

    assert evdev_clock.set_evdev_clock_monotonic(_FdDevice())
    assert calls == [
        (
            42,
            evdev_clock.EVIOCSCLOCKID,
            struct.pack("i", evdev_clock.CLOCK_MONOTONIC_ID),
        )
    ]


def test_set_evdev_clock_monotonic_skips_objects_without_fd() -> None:
    assert not evdev_clock.set_evdev_clock_monotonic(object())
