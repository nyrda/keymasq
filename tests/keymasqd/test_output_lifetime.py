"""Output handles survive physical-reader and logical-owner transitions."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import evdev
import pytest

from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.hardware_masking import MaskRuntime, _move_reserved_interface
from keymasq.keymasqd.runtime.grab.outputs import ensure_global_outputs
from keymasq.keymasqd.runtime.grab.release import release_device_unlocked
from tests.keymasqd.device_manager_support import FakeUInput


class CheckedOutput(FakeUInput):
    closed = False
    close_count = 0

    def write(self, event_type: int, code: int, value: int) -> None:
        if self.closed:
            raise ValueError("file descriptor cannot be a negative integer (-1)")
        super().write(event_type, code, value)

    def close(self) -> None:
        self.closed = True
        self.close_count += 1


def acquire(manager, owner, paths):
    ensure_global_outputs(manager, log=logging.getLogger("test"))
    devices = [
        SimpleNamespace(
            path=path,
            hardware_id=owner,
            interface_id=path,
            stop_event_loop=AsyncMock(),
            release_tracked_outputs=Mock(),
            release=AsyncMock(),
        )
        for path in paths
    ]
    manager.grabbed_devices[owner] = devices
    return devices


@pytest.mark.asyncio
async def test_repeated_mask_splits_and_suspend_keep_original_outputs_writable(monkeypatch):
    monkeypatch.setattr(evdev, "UInput", CheckedOutput)
    manager = DeviceManager()
    manager.initialize_output_devices()
    keyboard = acquire(manager, "keyboard", ["/dev/input/ordinary"])[0]
    original = manager.output_state.keyboard_uinput
    keyboard.keyboard_uinput = original
    for _ in range(3):
        owner = "@masked:deck"
        paths = ["/dev/input/deck-pad", "/dev/input/deck-extra"]
        devices = acquire(manager, owner, paths)
        manager.mask_registry.register(owner, paths, "")
        _move_reserved_interface(manager, devices[0], "deck-config")
        await MaskRuntime(manager, "deck", AsyncMock()).release_runtime()
        assert manager.grabbed_devices == {"keyboard": [keyboard]}
        assert manager.output_state.keyboard_uinput is original
        keyboard.keyboard_uinput.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F24, 1)
        keyboard.keyboard_uinput.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F24, 0)
    assert original.close_count == 0
    await manager.release_all_devices()
    assert manager.output_state.keyboard_uinput is original
    manager.initialize_output_devices()
    assert manager.output_state.keyboard_uinput is original
    manager.shutdown_output_devices()
    manager.shutdown_output_devices()
    assert original.close_count == 1


@pytest.mark.asyncio
async def test_merged_reservations_and_failed_release_preserve_outputs(monkeypatch):
    monkeypatch.setattr(evdev, "UInput", CheckedOutput)
    manager = DeviceManager()
    first = acquire(manager, "@masked:first", ["/dev/input/first"])[0]
    second = acquire(manager, "@masked:second", ["/dev/input/second"])[0]
    original = manager.output_state.keyboard_uinput
    _move_reserved_interface(manager, first, "shared")
    _move_reserved_interface(manager, second, "shared")
    first.release.side_effect = OSError("temporary release failure")
    with pytest.raises(OSError, match="temporary release failure"):
        await release_device_unlocked(manager, "shared", log=logging.getLogger("test"))
    assert manager.grabbed_devices == {"shared": [first]}
    original.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F24, 0)
    first.release.side_effect = None
    await release_device_unlocked(manager, "shared", log=logging.getLogger("test"))
    assert not manager.grabbed_devices
    assert original.close_count == 0
    manager.shutdown_output_devices()
    assert original.close_count == 1
