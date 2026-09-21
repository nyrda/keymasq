"""Shared outputs must outlive every reader, including adopted mask interfaces."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import evdev
import pytest

from keymasq.keymasqd import hardware_masking
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.runtime import adapters, outputs
from keymasq.keymasqd.runtime.grab.release import release_device_unlocked
from keymasq.keymasqd.runtime.grab.state import DesiredGrabConfig


class Output:
    def __init__(self, **_kwargs):
        self.closed = False
        self.writes = []

    def write(self, *event):
        assert not self.closed, "another device still needs this output"
        self.writes.append(event)

    def syn(self):
        assert not self.closed

    def close(self):
        self.closed = True


def reserve(manager, identity):
    owner = f"@masked:{identity}"
    outputs.create_global_uinputs(
        manager,
        evdev_mod=evdev,
        log=logging.getLogger(__name__),
        uinput_writer=adapters.identity_uinput_writer,
    )
    devices = [
        SimpleNamespace(
            hardware_id=owner,
            path=f"/dev/input/masking-test-{identity}-{index}",
            interface_id=f"if{index}",
            device=SimpleNamespace(info=SimpleNamespace(vendor=0xCAFE, product=1)),
            reset_mapping_runtime_state=AsyncMock(),
            release_tracked_outputs=Mock(),
            stop_event_loop=AsyncMock(),
            release=AsyncMock(),
            update_button_map=Mock(),
            update_analog_inputs=Mock(),
            update_motion_sensors=Mock(),
            update_default_output=AsyncMock(),
        )
        for index in range(2)
    ]
    manager.grabbed_devices[owner] = devices.copy()
    manager.mask_registry.register(owner, [device.path for device in devices], "")
    return devices


async def adopt(monkeypatch, manager, devices, owner):
    monkeypatch.setattr(
        hardware_masking.device_path_resolver,
        "resolve_evdev_interfaces",
        lambda *_args, **_kwargs: [SimpleNamespace(path=device.path) for device in devices],
    )
    async with manager._op_lock:
        await hardware_masking.adopt_masked_interfaces(
            manager, owner, [device.path for device in devices], None, Mock()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("adoption", ["none", "whole", "partial", "both_partial", "merged"])
@pytest.mark.parametrize("first_removed", ["first", "second"])
async def test_disconnect_keeps_other_masked_devices_shared_outputs(
    monkeypatch, adoption, first_removed
):
    monkeypatch.setattr(evdev, "UInput", Output)
    manager = DeviceManager()
    first = reserve(manager, "first")
    second = reserve(manager, "second")
    shared = [
        manager.output_state.keyboard_uinput,
        manager.output_state.mouse_uinput,
        manager.output_state.gamepad_uinput,
    ]
    if adoption == "whole":
        await adopt(monkeypatch, manager, first, "cafe:0001@first")
    elif adoption in {"partial", "both_partial"}:
        await adopt(monkeypatch, manager, first[:1], "cafe:0001@first")
        if adoption == "both_partial":
            await adopt(monkeypatch, manager, second[:1], "cafe:0001@second")
    elif adoption == "merged":
        await adopt(monkeypatch, manager, first + second, "cafe:0001")

    other = "second" if first_removed == "first" else "first"
    await hardware_masking.MaskRuntime(manager, first_removed, AsyncMock()).release_runtime()
    for output in shared:
        output.write(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 1)
        output.write(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 0)
        output.syn()
        assert not output.closed
    assert manager.grabbed_devices
    await hardware_masking.MaskRuntime(manager, other, AsyncMock()).release_runtime()
    assert not manager.grabbed_devices
    assert all(not output.closed for output in shared)
    outputs.destroy_global_uinputs(manager, log=logging.getLogger(__name__))
    assert all(output.closed for output in shared)


@pytest.mark.asyncio
async def test_removing_profile_returns_readers_without_leaking_output_ownership(monkeypatch):
    monkeypatch.setattr(evdev, "UInput", Output)
    manager = DeviceManager()
    devices = reserve(manager, "first")
    shared = manager.output_state.gamepad_uinput
    for _ in range(3):
        await adopt(monkeypatch, manager, devices[:1], "cafe:0001")
        await hardware_masking.release_masked_configuration(manager, "cafe:0001")
        shared.write(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 0)
    await hardware_masking.MaskRuntime(manager, "first", AsyncMock()).release_runtime()
    assert not shared.closed
    outputs.destroy_global_uinputs(manager, log=logging.getLogger(__name__))
    assert shared.closed


@pytest.mark.asyncio
async def test_failed_release_keeps_shared_outputs_until_successful_retry(monkeypatch):
    monkeypatch.setattr(evdev, "UInput", Output)
    manager = DeviceManager()
    devices = reserve(manager, "first")
    shared = manager.output_state.gamepad_uinput
    devices[0].release.side_effect = OSError("release failed")
    with pytest.raises(OSError, match="release failed"):
        await release_device_unlocked(manager, "@masked:first", log=logging.getLogger(__name__))
    shared.write(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 0)
    devices[0].release.side_effect = None
    await release_device_unlocked(manager, "@masked:first", log=logging.getLogger(__name__))
    assert not shared.closed
    outputs.destroy_global_uinputs(manager, log=logging.getLogger(__name__))
    assert shared.closed


@pytest.mark.asyncio
async def test_mapping_after_mask_quiescence_waits_for_reacquisition(monkeypatch):
    monkeypatch.setattr(evdev, "UInput", Output)
    manager = DeviceManager()
    devices = reserve(manager, "first")
    owner = "cafe:0001"
    await adopt(monkeypatch, manager, devices[:1], owner)
    paths = {devices[0].path}
    manager.grab_state.desired_paths[owner] = paths
    manager.grab_state.desired_grabs[owner] = DesiredGrabConfig(paths=paths, button_map={})
    await hardware_masking.MaskRuntime(manager, "first", AsyncMock()).release_runtime()
    result = await manager.set_mapping(owner, {"button": "key_a"})
    assert result == {"updated": False, "waiting_for_device": True, "hardware_id": owner}
    assert owner not in manager.active_mappings
    with pytest.raises(ValueError, match="not grabbed"):
        await manager.set_mapping("ffff:ffff", {})
