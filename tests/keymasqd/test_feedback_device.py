import errno
from types import SimpleNamespace
from typing import cast

import evdev
import pytest

from keymasq.keymasqd.runtime import feedback_device
from keymasq.keymasqd.runtime.feedback_device import EvdevFeedbackDevice, EvdevFeedbackSource


@pytest.mark.parametrize("effect_id", [-1, 23])
def test_upload_preserves_effect_data_and_returns_allocated_id(
    monkeypatch: pytest.MonkeyPatch, effect_id: int
) -> None:
    effect = evdev.ff.Effect()
    effect.type = evdev.ecodes.FF_RUMBLE
    effect.id = effect_id
    effect.direction = 0x4000
    effect.ff_replay.length = 500
    effect.u.ff_rumble_effect.strong_magnitude = 0x1234
    effect.u.ff_rumble_effect.weak_magnitude = 0x5678
    original = bytes(effect)
    calls: list[tuple[int, int]] = []

    def ioctl(fd: int, request: int, data: object) -> bytes:
        calls.append((fd, request))
        # Immutable bytes select fcntl's GIL-releasing buffer path. The
        # payload must preserve the full effect, including replacement ids.
        assert isinstance(data, bytes)
        assert data == original
        uploaded = evdev.ff.Effect.from_buffer_copy(data)
        uploaded.id = 71
        return bytes(uploaded)

    monkeypatch.setattr(feedback_device.fcntl, "ioctl", ioctl)
    device = EvdevFeedbackDevice(cast(EvdevFeedbackSource, SimpleNamespace(fd=42)))

    assert device.upload_effect(effect) == 71
    assert calls == [(42, feedback_device._EVIOCSFF)]
    assert bytes(effect) == original


def test_erase_passes_effect_id_by_value(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int, object]] = []

    def ioctl(fd: int, request: int, effect_id: object) -> int:
        calls.append((fd, request, effect_id))
        assert isinstance(effect_id, int)
        return 0

    monkeypatch.setattr(feedback_device.fcntl, "ioctl", ioctl)
    device = EvdevFeedbackDevice(cast(EvdevFeedbackSource, SimpleNamespace(fd=42)))

    device.erase_effect(23)

    assert calls == [(42, feedback_device._EVIOCRMFF, 23)]


@pytest.mark.parametrize("operation", ["upload", "erase"])
def test_feedback_ioctl_preserves_kernel_errno(operation: str) -> None:
    with open("/dev/null", "rb") as device_file:
        device = EvdevFeedbackDevice(
            cast(EvdevFeedbackSource, SimpleNamespace(fd=device_file.fileno()))
        )
        with pytest.raises(OSError) as error:
            if operation == "upload":
                device.upload_effect(evdev.ff.Effect())
            else:
                device.erase_effect(23)
        assert error.value.errno == errno.ENOTTY
