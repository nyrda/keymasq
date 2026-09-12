import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from keymasq.common.ipc import CommandType
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.hardware_masking import (
    DECK_RESERVATION_ID,
    HardwareMasking,
    adopt_masked_interfaces,
    release_masked_configuration,
)
from keymasq.keymasqd.runtime.grabbed_device.types import InputAccessMode
from keymasq.keymasqd.runtime.topology import reconcile_topology_unlocked


@pytest.mark.asyncio
async def test_session_inventory_starts_saved_mask_without_gui(monkeypatch):
    masking = HardwareMasking(DeviceManager())
    request = AsyncMock(
        side_effect=[
            {"state": "applying", "remapping_suspended": False},
            {"state": "applying"},
        ]
    )
    monkeypatch.setattr(masking, "request", request)
    monkeypatch.setattr(masking, "start_monitor", Mock())
    await masking.handle(CommandType.HARDWARE_INVENTORY, {"uid": 9999}, uid=1000)
    assert request.await_args_list[0].args == ("startup", {"uid": 1000})
    assert masking.manager.masking_suspended
    masking.start_monitor.assert_called_once()


@pytest.mark.asyncio
async def test_clean_close_preserves_startup_intent_and_emergency_restore_does_not(monkeypatch):
    masking = HardwareMasking(DeviceManager())
    masking.writer = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
    masking.state = {"state": "masked"}
    request = AsyncMock()
    monkeypatch.setattr(masking, "request", request)
    monkeypatch.setattr(masking, "release_runtime", AsyncMock())
    await masking.restore()
    request.assert_awaited_with("restore", {"reason": "user_restore"})
    await masking.close()
    request.assert_awaited_with("restore", {"reason": "lifecycle_stop"})
    assert masking.writer is None
    assert masking.session_uid is None


@pytest.mark.asyncio
async def test_recovery_rejects_a_grab_already_waiting_for_the_runtime_lock() -> None:
    manager = DeviceManager()
    manager.masking_suspended = False
    async with manager._op_lock:
        pending = asyncio.create_task(manager.grab_device("28de:1205", ["/dev/input/event5"], {}))
        await asyncio.sleep(0)
        assert not pending.done()
        manager.masking_suspended = True
    result = await pending
    assert result == {"grabbed": False, "reason": "Hardware recovery suspended remapping"}
    assert manager.grabbed_devices == {}
    assert manager.grab_state.desired_grabs == {}


def test_reserved_nodes_remain_discoverable_when_access_filter_omits_them(monkeypatch):
    from keymasq.keymasqd import device_manager as module

    manager = DeviceManager()
    manager.masked_hardware_paths[DECK_RESERVATION_ID] = ["/dev/input/event5"]
    monkeypatch.setattr(module, "_device_paths", lambda: ["/dev/input/event11"])
    assert set(manager._discoverable_input_paths()) == {"/dev/input/event5", "/dev/input/event11"}
    assert set(manager._input_resolver_deps().device_paths_fn()) == {
        "/dev/input/event5",
        "/dev/input/event11",
    }


@pytest.mark.asyncio
async def test_evdev_rescan_does_not_end_physical_hardware_ownership() -> None:
    device = SimpleNamespace(path="/dev/input/event5")
    manager = SimpleNamespace(
        masked_hardware_paths={"28de:1205": [device.path]},
        grabbed_devices={"28de:1205": [device]},
    )
    release = AsyncMock()
    deps = SimpleNamespace(release_interface_fn=release)
    await reconcile_topology_unlocked(manager, {}, deps=deps)  # type: ignore[arg-type]
    release.assert_not_awaited()
    manager.masked_hardware_paths.clear()
    await reconcile_topology_unlocked(manager, {}, deps=deps)  # type: ignore[arg-type]
    release.assert_awaited_once_with(manager, "28de:1205", device.path)


@pytest.mark.asyncio
async def test_setup_adopts_selected_reserved_interfaces_and_preserves_output(monkeypatch):
    from keymasq.keymasqd import hardware_masking as masking

    manager = DeviceManager()
    gamepad = SimpleNamespace(
        path="/dev/input/event5",
        hardware_id=DECK_RESERVATION_ID,
        interface_id="if02",
        reset_mapping_runtime_state=AsyncMock(),
        release_tracked_outputs=Mock(),
        update_default_output=AsyncMock(),
        update_button_map=Mock(),
        update_analog_inputs=Mock(),
        update_motion_sensors=Mock(),
        running=True,
        device=object(),
        task=SimpleNamespace(done=lambda: False),
        access_mode=InputAccessMode.EXCLUSIVE,
        default_output=None,
        uinput=object(),
        stable_path="/dev/input/event5",
    )
    keyboard = SimpleNamespace(
        path="/dev/input/event11",
        hardware_id=DECK_RESERVATION_ID,
        interface_id="kbd",
        running=True,
        device=object(),
        task=SimpleNamespace(done=lambda: False),
        access_mode=InputAccessMode.EXCLUSIVE,
        default_output=None,
        uinput=object(),
    )
    original_output = gamepad.uinput
    manager.grabbed_devices[DECK_RESERVATION_ID] = [gamepad, keyboard]
    manager.masked_hardware_paths[DECK_RESERVATION_ID] = [gamepad.path, keyboard.path]
    monkeypatch.setattr(masking, "resolve_stable_path", lambda path: path)
    monkeypatch.setattr(
        masking.device_path_resolver,
        "resolve_evdev_interfaces",
        lambda *_args, **_kwargs: [SimpleNamespace(path=gamepad.path)],
    )
    descriptors = [{"id": "gamepad", "path": gamepad.path}]
    result = await adopt_masked_interfaces(
        manager,
        "28de:1205@2",
        [gamepad.path],
        descriptors,
        Mock(),
    )
    assert result == descriptors
    assert manager.grabbed_devices["28de:1205@2"] == [gamepad]
    assert manager.grabbed_devices[DECK_RESERVATION_ID] == [keyboard]
    assert gamepad.uinput is original_output
    assert await HardwareMasking(manager).runtime_ready()
    manager.active_mappings["28de:1205@2"] = {"a": "test"}
    assert gamepad.mapping_getter() == {"a": "test"}

    result = await release_masked_configuration(manager, "28de:1205@2")
    assert result["released"] and result["reserved"]
    gamepad.update_default_output.assert_awaited_once_with(None)
    assert gamepad.hardware_id == DECK_RESERVATION_ID
    assert gamepad.mapping_getter() == {}
    assert gamepad.uinput is original_output
    assert gamepad.running
    assert await HardwareMasking(manager).runtime_ready()
    assert "28de:1205@2" not in manager.masked_hardware_paths


@pytest.fixture
async def reserved_runtime(monkeypatch):
    import evdev

    from keymasq.common.model.core import DeviceType
    from keymasq.common.virtual_device_templates import VirtualDeviceConfig, resolve_virtual_devices
    from keymasq.keymasqd.runtime.grabbed_device import device as device_module
    from tests.keymasqd.device_manager_support import FakeUInput, make_grabbed_device

    manager = DeviceManager()
    target = FakeUInput()
    manager.output_state.virtual_gamepad_uinputs = {"virtual-gamepad-1": target}
    manager.output_state.virtual_device_specs = {
        spec.output_id: spec for spec in resolve_virtual_devices(1, VirtualDeviceConfig())
    }
    device = make_grabbed_device(
        monkeypatch,
        hardware_id=DECK_RESERVATION_ID,
        device_type=DeviceType.GAMEPAD,
        source_reserved=True,
        gamepad_output_resolver=lambda output_id, context: manager.resolve_gamepad_output(
            output_id, context=context
        ),
    )
    physical = Mock()
    physical.capabilities.return_value = {
        evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SOUTH],
        evdev.ecodes.EV_ABS: [(evdev.ecodes.ABS_X, evdev.AbsInfo(0, -100, 100, 0, 0, 0))],
    }
    physical.input_props.return_value = []
    physical.active_keys.return_value = []
    physical.absinfo.return_value = evdev.AbsInfo(0, -100, 100, 0, 0, 0)
    physical.info = SimpleNamespace(vendor=0x28DE, product=0x1205, version=1, bustype=3)
    physical.name = "Steam Deck"
    physical.ff_effects_count = 0

    async def read_loop(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(device_module, "_device_input", lambda _: physical)
    monkeypatch.setattr(device_module.evdev, "UInput", FakeUInput)
    monkeypatch.setattr(device_module.pipeline, "event_loop", read_loop)
    monkeypatch.setattr(device_module.source_hiding, "hide_source", AsyncMock())
    await device.grab()
    manager.grabbed_devices[DECK_RESERVATION_ID] = [device]
    manager.masked_hardware_paths[DECK_RESERVATION_ID] = [device.path]
    try:
        yield manager, device, physical, target
    finally:
        await device.release()


@pytest.mark.asyncio
async def test_masked_route_changes_preserve_grab_and_release_outputs(reserved_runtime):
    import evdev

    from keymasq.keymasqd.runtime.grabbed_device.event.pipeline import process_event
    from tests.keymasqd.device_manager_support import grabbed_event_processing_deps

    manager, device, physical, target = reserved_runtime
    masking = HardwareMasking(manager)
    clone = device.uinput
    assert clone is not None
    assert await masking.runtime_ready()
    await device.update_default_output("virtual-gamepad-1")
    assert device.uinput is None
    assert await masking.runtime_ready()
    await process_event(
        device,
        evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 1),
        deps=grabbed_event_processing_deps(),
    )
    assert (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 1) in target.writes
    await device.update_default_output("passthrough")
    assert device.state.passthrough_frame_output is None
    assert (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 0) in target.writes
    assert device.uinput is not None and device.uinput is not clone
    assert device.default_route is None
    assert await masking.runtime_ready()
    physical.grab.assert_called_once()
    physical.ungrab.assert_not_called()
    physical.close.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_output", "stopped_reader", "missing_interface"])
async def test_mask_readiness_rejects_incomplete_runtime(reserved_runtime, failure):
    manager, device, _, _ = reserved_runtime
    await device.update_default_output("virtual-gamepad-1")
    assert await HardwareMasking(manager).runtime_ready()
    if failure == "missing_output":
        manager.output_state.virtual_gamepad_uinputs.clear()
    elif failure == "stopped_reader":
        await device.stop_event_loop()
    else:
        manager.masked_hardware_paths[DECK_RESERVATION_ID].append("/dev/input/missing")
    assert not await HardwareMasking(manager).runtime_ready()


@pytest.mark.asyncio
async def test_readiness_waits_for_output_transaction(reserved_runtime):
    manager, device, _, _ = reserved_runtime
    async with manager._op_lock:
        pending = asyncio.create_task(HardwareMasking(manager).runtime_ready())
        await asyncio.sleep(0)
        assert not pending.done()
        await device.update_default_output("virtual-gamepad-1")
    assert await pending


@pytest.mark.asyncio
async def test_failed_output_change_keeps_source_reserved_until_recovery(
    reserved_runtime, monkeypatch
):
    manager, device, physical, _ = reserved_runtime
    monkeypatch.setattr(
        device, "_configure_default_output", AsyncMock(side_effect=OSError("uinput failed"))
    )
    with pytest.raises(OSError, match="uinput failed"):
        await device.update_default_output("virtual-gamepad-1")
    assert not await HardwareMasking(manager).runtime_ready()
    physical.ungrab.assert_not_called()
    physical.close.assert_not_called()


@pytest.mark.asyncio
async def test_releasing_configured_route_returns_to_setup_passthrough(
    reserved_runtime, monkeypatch
):
    from keymasq.keymasqd import hardware_masking as module

    manager, device, physical, _ = reserved_runtime
    monkeypatch.setattr(module, "resolve_stable_path", lambda path: path)
    monkeypatch.setattr(
        module.device_path_resolver,
        "resolve_evdev_interfaces",
        lambda *_args, **_kwargs: [SimpleNamespace(path=device.path)],
    )
    configured_id = "28de:1205@2"
    await adopt_masked_interfaces(manager, configured_id, [device.path], None, Mock())
    await device.update_default_output("virtual-gamepad-1")
    assert device.hardware_id == configured_id
    result = await manager.release_device(configured_id)
    assert result["reserved"]
    assert device.hardware_id == DECK_RESERVATION_ID
    assert device.default_output is None
    assert device.uinput is not None
    assert await HardwareMasking(manager).runtime_ready()
    physical.grab.assert_called_once()
    physical.ungrab.assert_not_called()


@pytest.mark.asyncio
async def test_takeover_clears_old_grabs_before_acquiring_and_reapplying_routes(monkeypatch):
    manager = DeviceManager()
    masking = HardwareMasking(manager)
    order = []

    async def release_old():
        assert manager.masking_suspended
        order.append("released")

    async def grab_reserved(hardware_id, paths, mappings, **kwargs):
        assert order == ["released"]
        assert hardware_id == DECK_RESERVATION_ID
        assert paths == ["/dev/input/event5"]
        assert mappings == {}
        assert kwargs == {"force_grab_unmapped": True}
        assert not manager.masking_suspended
        order.append("acquired")

    monkeypatch.setattr(manager, "release_all_devices", release_old)
    monkeypatch.setattr(manager, "grab_device", grab_reserved)
    monkeypatch.setattr(masking, "runtime_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(masking, "request", AsyncMock())
    monkeypatch.setattr(manager, "broadcast_hardware_mask_ready", Mock())
    await masking.acquire({"event_nodes": ["/dev/input/event5"], "token": "trial"})
    assert order == ["released", "acquired"]
    masking.request.assert_awaited_once_with("ready", {"token": "trial"})
    manager.broadcast_hardware_mask_ready.assert_called_once()
