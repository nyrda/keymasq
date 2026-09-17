import asyncio
import os
import threading
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from keymasq.common.ipc import CommandType
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.hardware_masking import (
    HardwareMasking,
    MaskRuntime,
    adopt_masked_interfaces,
    release_masked_configuration,
)
from keymasq.keymasqd.masking_registry import MaskRegistry
from keymasq.keymasqd.runtime.grabbed_device.types import InputAccessMode
from keymasq.keymasqd.runtime.topology import reconcile_topology_unlocked

RESERVATION_ID = "@masked:test"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TypeError("bad coordinator state"), OSError("job failed")])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_monitor_survives_errors_and_failed_cleanup(monkeypatch, error, cleanup_fails):
    manager = DeviceManager()
    masking = HardwareMasking(manager)
    calls = 0

    async def tick():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error
        masking.monitor_stop.set()

    monkeypatch.setattr(masking, "monitor_once", tick)
    release = AsyncMock(side_effect=OSError("reader cleanup failed") if cleanup_fails else None)
    monkeypatch.setattr(masking, "release_runtime", release)
    monkeypatch.setattr(manager, "neutralize_runtime", AsyncMock())
    monkeypatch.setattr(manager, "release_all_devices", AsyncMock())
    monkeypatch.setattr(manager, "broadcast_hardware_recovery", Mock())
    request = AsyncMock()
    monkeypatch.setattr(masking, "request", request)
    await asyncio.wait_for(masking.monitor(), 3)
    assert calls == 2
    release.assert_awaited_once()
    if isinstance(error, TypeError):
        assert manager.masking_suspended
        manager.release_all_devices.assert_awaited_once()
        request.assert_awaited_once_with("restore", {"reason": "monitor_failed"})
    else:
        request.assert_not_awaited()


@pytest.mark.asyncio
async def test_monitor_cancellation_still_propagates(monkeypatch):
    masking = HardwareMasking(DeviceManager())
    monkeypatch.setattr(masking, "monitor_once", AsyncMock(side_effect=asyncio.CancelledError))
    cleanup = AsyncMock()
    monkeypatch.setattr(masking, "release_runtime", cleanup)
    with pytest.raises(asyncio.CancelledError):
        await masking.monitor()
    cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_emergency_reset_releases_local_readers_when_helper_disconnected(monkeypatch):
    manager = DeviceManager()
    masking = HardwareMasking(manager)
    manager.mask_registry.hardware_paths[RESERVATION_ID] = ["/dev/input/event5"]
    manager.masking_recovery = masking.recover_if_needed
    monkeypatch.setattr(masking, "release_runtime", AsyncMock())
    monkeypatch.setattr(manager, "release_all_devices", AsyncMock())
    monkeypatch.setattr(manager, "broadcast_hardware_recovery", Mock())
    monkeypatch.setattr(masking, "request", AsyncMock(side_effect=OSError("operation failed")))
    with pytest.raises(OSError, match="operation failed"):
        await manager.emergency_reset()
    masking.release_runtime.assert_awaited_once()
    manager.release_all_devices.assert_awaited_once()
    manager.broadcast_hardware_recovery.assert_called_once()
    assert manager.masking_suspended


def runtime(manager):
    manager.mask_registry.reservation_paths.setdefault(
        RESERVATION_ID, list(manager.mask_registry.hardware_paths.get(RESERVATION_ID, []))
    )
    return MaskRuntime(manager, "test", AsyncMock())


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
    assert not masking.manager.masking_suspended
    masking.start_monitor.assert_called_once()


@pytest.mark.asyncio
async def test_clean_close_preserves_startup_intent_and_emergency_restore_does_not(monkeypatch):
    masking = HardwareMasking(DeviceManager())
    masking.initialized = True
    masking.state = {"state": "masked"}
    request = AsyncMock()
    monkeypatch.setattr(masking, "request", request)
    monkeypatch.setattr(masking, "release_runtime", AsyncMock())
    await masking.restore()
    request.assert_awaited_with("restore", {"reason": "user_restore"})
    await masking.close()
    request.assert_awaited_with("restore", {"reason": "lifecycle_stop"})
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
    manager.mask_registry.hardware_paths[RESERVATION_ID] = ["/dev/input/event5"]
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
        mask_registry=MaskRegistry(hardware_paths={"28de:1205": [device.path]}),
        grabbed_devices={"28de:1205": [device]},
    )
    release = AsyncMock()
    deps = SimpleNamespace(release_interface_fn=release)
    await reconcile_topology_unlocked(manager, {}, deps=deps)  # type: ignore[arg-type]
    release.assert_not_awaited()
    manager.mask_registry.hardware_paths.clear()
    await reconcile_topology_unlocked(manager, {}, deps=deps)  # type: ignore[arg-type]
    release.assert_awaited_once_with(manager, "28de:1205", device.path)


@pytest.mark.asyncio
async def test_setup_adopts_selected_reserved_interfaces_and_preserves_output(monkeypatch):
    from keymasq.keymasqd import hardware_masking as masking

    manager = DeviceManager()
    gamepad = SimpleNamespace(
        path="/dev/input/event5",
        hardware_id=RESERVATION_ID,
        interface_id="if02",
        reset_mapping_runtime_state=AsyncMock(),
        release_tracked_outputs=Mock(),
        update_default_output=AsyncMock(),
        update_button_map=Mock(),
        update_analog_inputs=Mock(),
        update_motion_sensors=Mock(),
        running=True,
        device=SimpleNamespace(info=SimpleNamespace(vendor=0x28DE, product=0x1205)),
        task=SimpleNamespace(done=lambda: False),
        access_mode=InputAccessMode.EXCLUSIVE,
        default_output=None,
        uinput=object(),
        stable_path="/dev/input/event5",
    )
    keyboard = SimpleNamespace(
        path="/dev/input/event11",
        hardware_id=RESERVATION_ID,
        interface_id="kbd",
        running=True,
        device=object(),
        task=SimpleNamespace(done=lambda: False),
        access_mode=InputAccessMode.EXCLUSIVE,
        default_output=None,
        uinput=object(),
    )
    original_output = gamepad.uinput
    manager.mask_registry.reservation_paths[RESERVATION_ID] = [gamepad.path, keyboard.path]
    manager.grabbed_devices[RESERVATION_ID] = [gamepad, keyboard]
    manager.mask_registry.hardware_paths[RESERVATION_ID] = [gamepad.path, keyboard.path]
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
    assert manager.grabbed_devices[RESERVATION_ID] == [keyboard]
    assert gamepad.uinput is original_output
    assert await runtime(manager).runtime_ready()
    manager.active_mappings["28de:1205@2"] = {"a": "test"}
    assert gamepad.mapping_getter() == {"a": "test"}

    result = await release_masked_configuration(manager, "28de:1205@2")
    assert result["released"] and result["reserved"]
    gamepad.update_default_output.assert_awaited_once_with(None)
    assert gamepad.hardware_id == RESERVATION_ID
    assert gamepad.mapping_getter() == {}
    assert gamepad.uinput is original_output
    assert gamepad.running
    assert await runtime(manager).runtime_ready()
    assert "28de:1205@2" not in manager.mask_registry.hardware_paths


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
        hardware_id=RESERVATION_ID,
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
    manager.grabbed_devices[RESERVATION_ID] = [device]
    manager.mask_registry.hardware_paths[RESERVATION_ID] = [device.path]
    manager.mask_registry.reservation_paths[RESERVATION_ID] = [device.path]
    try:
        yield manager, device, physical, target
    finally:
        await device.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["single", "all"])
@pytest.mark.parametrize("paused", [False, True])
async def test_unmask_reapplies_after_physical_recovery_without_topology_change(
    reserved_runtime, monkeypatch, scope, paused
):
    manager, _device, physical, _target = reserved_runtime
    manager.masking_suspended = paused
    masking = HardwareMasking(manager)
    mask = masking.runtime("test")
    notify = Mock()
    monkeypatch.setattr(manager, "broadcast_hardware_mask_ready", notify)
    monkeypatch.setattr(masking, "startup", AsyncMock())
    monkeypatch.setattr(masking, "start_monitor", Mock())
    status = {"id": "test", "state": "masked", "token": "trial"}

    async def request(command, data=None):
        if command == "restore":
            status["state"] = "restoring" if scope == "single" else "restored"
        return {"masks": [status.copy()], "remapping_suspended": paused}

    monkeypatch.setattr(masking, "request", request)
    data = {"persist": False}
    if scope == "single":
        data.update(id="test", token="trial")
    await masking.handle(CommandType.RESTORE_HARDWARE, data, uid=1000)
    physical.close.assert_called_once()
    assert not manager.mask_registry.reservation_paths
    notify.assert_not_called()

    # No device-discovery event is needed to recover the logical routes.
    for phase in ("restoring", "recovery_failed"):
        await mask.update({**status, "state": phase})
        notify.assert_not_called()
    await mask.update({**status, "state": "restored"})
    assert notify.call_count == (0 if paused else 1)
    await mask.update({**status, "state": "restored"})
    assert notify.call_count == (0 if paused else 1)
    assert manager.masking_suspended is paused


@pytest.mark.asyncio
async def test_masked_route_changes_preserve_grab_and_release_outputs(reserved_runtime):
    import evdev

    from keymasq.keymasqd.runtime.grabbed_device.event.pipeline import process_event
    from tests.keymasqd.device_manager_support import grabbed_event_processing_deps

    manager, device, physical, target = reserved_runtime
    masking = runtime(manager)
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
async def test_disabling_virtual_output_keeps_masked_input_readable(reserved_runtime):
    import evdev

    from keymasq.keymasqd.runtime.grabbed_device.event.pipeline import process_event
    from tests.keymasqd.device_manager_support import grabbed_event_processing_deps

    manager, device, physical, target = reserved_runtime
    manager.output_state.device_count = 1
    await device.update_default_output("virtual-gamepad-1")
    reader = device.task
    masking = runtime(manager)
    await process_event(
        device,
        evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 1),
        deps=grabbed_event_processing_deps(),
    )

    await manager.set_virtual_gamepads(0)
    assert (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 0) in target.writes
    assert "virtual-gamepad-1" not in manager.output_state.virtual_device_specs
    assert await masking.runtime_ready()
    writes = list(target.writes)
    await process_event(
        device,
        evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 0),
        deps=grabbed_event_processing_deps(),
    )
    assert target.writes == writes
    assert device.task is reader
    physical.ungrab.assert_not_called()
    physical.close.assert_not_called()

    await manager.set_virtual_gamepads(1)
    replacement = manager.output_state.virtual_gamepad_uinputs["virtual-gamepad-1"]
    assert replacement is not target
    assert await masking.runtime_ready()
    await process_event(
        device,
        evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 1),
        deps=grabbed_event_processing_deps(),
    )
    assert (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 1) in replacement.writes
    assert device.task is reader
    physical.grab.assert_called_once()
    physical.ungrab.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_output", "stopped_reader", "missing_interface"])
async def test_mask_readiness_rejects_incomplete_runtime(reserved_runtime, failure):
    manager, device, _, _ = reserved_runtime
    await device.update_default_output("virtual-gamepad-1")
    assert await runtime(manager).runtime_ready()
    if failure == "missing_output":
        manager.output_state.virtual_gamepad_uinputs.clear()
    elif failure == "stopped_reader":
        await device.stop_event_loop()
    else:
        manager.mask_registry.reservation_paths[RESERVATION_ID].append("/dev/input/missing")
    assert not await runtime(manager).runtime_ready()


@pytest.mark.asyncio
async def test_readiness_waits_for_output_transaction(reserved_runtime):
    manager, device, _, _ = reserved_runtime
    async with manager._op_lock:
        pending = asyncio.create_task(runtime(manager).runtime_ready())
        await asyncio.sleep(0)
        assert not pending.done()
        await device.update_default_output("virtual-gamepad-1")
    assert await pending


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [OSError("uinput failed"), asyncio.CancelledError()])
async def test_failed_output_change_keeps_source_reserved_until_recovery(
    reserved_runtime, monkeypatch, error
):
    manager, device, physical, _ = reserved_runtime
    monkeypatch.setattr(device, "_configure_default_output", AsyncMock(side_effect=error))
    with pytest.raises(type(error)):
        await device.update_default_output("virtual-gamepad-1")
    mask = runtime(manager)
    assert not await mask.runtime_ready()
    physical.ungrab.assert_not_called()
    physical.close.assert_not_called()
    mask.request = AsyncMock(return_value={"state": "restored"})
    await mask.update({"state": "masked", "token": "trial-token"})
    physical.close.assert_called_once()
    assert not any(device in devices for devices in manager.grabbed_devices.values())
    mask.request.assert_awaited_once_with(
        "restore", {"reason": "replacement_unavailable", "token": "trial-token"}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_model", ["9999:1205", "28de:9999"])
async def test_shared_path_is_adopted_only_by_matching_model(
    reserved_runtime, monkeypatch, wrong_model
):
    from keymasq.keymasqd import hardware_masking as module

    manager, device, physical, _ = reserved_runtime
    monkeypatch.setattr(module, "resolve_stable_path", lambda path: path)
    monkeypatch.setattr(
        module.device_path_resolver,
        "resolve_evdev_interfaces",
        lambda *_args, **_kwargs: [SimpleNamespace(path=device.path)],
    )
    await adopt_masked_interfaces(manager, wrong_model, [device.path], None, Mock())
    assert device.hardware_id == RESERVATION_ID
    assert wrong_model not in manager.grabbed_devices
    await adopt_masked_interfaces(manager, "28de:1205@2", [device.path], None, Mock())
    assert device.hardware_id == "28de:1205@2"
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
    assert device.hardware_id == RESERVATION_ID
    assert device.default_output is None
    assert device.uinput is not None
    assert await runtime(manager).runtime_ready()
    physical.grab.assert_called_once()
    physical.ungrab.assert_not_called()


@pytest.mark.asyncio
async def test_takeover_acquires_without_releasing_unrelated_devices(monkeypatch):
    manager = DeviceManager()
    masking = runtime(manager)
    order = []

    async def grab_reserved(hardware_id, paths, mappings, **kwargs):
        assert order == []
        assert hardware_id == RESERVATION_ID
        assert paths == ["/dev/input/event5"]
        assert mappings == {}
        assert kwargs == {"force_grab_unmapped": True}
        assert not manager.masking_suspended
        order.append("acquired")

    release = AsyncMock()
    monkeypatch.setattr(manager, "release_all_devices", release)
    monkeypatch.setattr(manager, "grab_device", grab_reserved)
    monkeypatch.setattr(masking, "runtime_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(masking, "request", AsyncMock())
    monkeypatch.setattr(manager, "broadcast_hardware_mask_ready", Mock())
    await masking.acquire({"event_nodes": ["/dev/input/event5"], "token": "trial"})
    assert order == ["acquired"]
    release.assert_not_awaited()
    cast(AsyncMock, masking.request).assert_awaited_once_with("ready", {"token": "trial"})
    manager.broadcast_hardware_mask_ready.assert_called_once()


@pytest.mark.asyncio
async def test_hidraw_only_reservation_does_not_need_a_controller_decoder(monkeypatch):
    manager = DeviceManager()
    masking = runtime(manager)
    monkeypatch.setattr(manager, "grab_device", AsyncMock())
    monkeypatch.setattr(masking, "request", AsyncMock())
    await masking.acquire({"event_nodes": [], "token": "raw-trial"})
    manager.grab_device.assert_not_awaited()
    cast(AsyncMock, masking.request).assert_awaited_once_with("ready", {"token": "raw-trial"})
    assert await masking.runtime_ready()
    await masking.release_runtime()
    assert not await masking.runtime_ready()


@pytest.mark.asyncio
async def test_quiesce_stops_only_selected_physical_and_native_readers(monkeypatch, tmp_path):
    from keymasq.keymasqd import hardware_masking as module
    from keymasq.keymasqd.input_sources.evdev_adapter import NativeInputDevice

    manager = DeviceManager()
    masking = runtime(manager)
    selected = tmp_path / "selected"
    native = object.__new__(NativeInputDevice)
    native.binding = SimpleNamespace(endpoint=SimpleNamespace(hid_parent=str(selected / "hid")))
    physical = SimpleNamespace(path="/dev/input/event42", device=object())
    motion = SimpleNamespace(path="/dev/keymasq-sources/test", device=native)
    unrelated = SimpleNamespace(path="/dev/input/event43", device=object())
    manager.grabbed_devices = {"selected": [physical, motion], "unrelated": [unrelated]}
    original_resolve = module.Path.resolve

    def resolve(path):
        if str(path) == "/sys/class/input/event42/device":
            return selected / "hid/input/input1"
        return original_resolve(path)

    release = AsyncMock()
    monkeypatch.setattr(module.Path, "resolve", resolve)
    monkeypatch.setattr(module, "release_interface_unlocked", release)
    monkeypatch.setattr(masking, "request", AsyncMock())
    await masking.quiesce({"attachment_path": str(selected), "token": "trial"})
    assert [call.args[2] for call in release.await_args_list] == [physical.path, motion.path]
    # Quiesced interfaces come back under the reservation's rules; arming the
    # model's hotplug-hiding flag would only make the hide rule race them.
    assert all(call.kwargs == {"arm_hotplug_hiding": False} for call in release.await_args_list)
    cast(AsyncMock, masking.request).assert_awaited_once_with("quiesced", {"token": "trial"})


@pytest.mark.asyncio
async def test_failed_mask_recovers_without_resume_from_gui(tmp_path, monkeypatch):
    from keymasq.masking.coordinator import MaskReservation
    from tests.common.test_hardware_mask_persistence import confirm
    from tests.common.test_hardware_masking import FakeBackend

    clock = [100.0]
    supervisor = MaskReservation(FakeBackend(tmp_path), lambda: clock[0])
    await confirm(supervisor)
    await supervisor.request({"command": "restore", "reason": "replacement_unavailable"})
    manager = DeviceManager()
    masking = runtime(manager)
    masking.clock = lambda: clock[0]

    async def request(command, data=None):
        return await supervisor.request({"command": command, **(data or {})})

    monkeypatch.setattr(masking, "request", request)
    monkeypatch.setattr(manager, "broadcast_hardware_recovery", Mock())
    monkeypatch.setattr(manager, "broadcast_hardware_mask_ready", Mock())
    await masking.recover_automatically(await supervisor.status())
    assert (await supervisor.status())["remapping_suspended"]
    manager.broadcast_hardware_recovery.assert_called_once_with(retrying=True)
    clock[0] += 2
    await masking.recover_automatically(await supervisor.status())
    assert not (await supervisor.status())["remapping_suspended"]
    assert not manager.masking_suspended
    manager.broadcast_hardware_mask_ready.assert_called_once()
    await supervisor.request({"command": "poll"})
    await supervisor.request({"command": "quiesced", "token": supervisor.state["token"]})
    assert supervisor.apply_task is not None
    await supervisor.apply_task
    result = await supervisor.request({"command": "ready", "token": supervisor.state["token"]})
    assert result["state"] == "masked"


@pytest.mark.asyncio
async def test_repeated_recovery_backs_off_and_explicit_stop_cancels_retry(monkeypatch):
    manager = DeviceManager()
    masking = runtime(manager)
    clock = [100.0]
    masking.clock = lambda: clock[0]
    request = AsyncMock(return_value={"state": "restored", "remapping_suspended": False})
    monkeypatch.setattr(masking, "request", request)
    monkeypatch.setattr(manager, "broadcast_hardware_recovery", Mock())
    monkeypatch.setattr(manager, "broadcast_hardware_mask_ready", Mock())
    failed = {"state": "restored", "remapping_suspended": True, "reason": "replacement_unavailable"}
    await masking.recover_automatically(failed)
    assert masking.retry_at == 102
    clock[0] = 102
    await masking.recover_automatically(failed)
    request.assert_awaited_once_with("resume")
    await masking.recover_automatically(failed)
    assert masking.retry_at == 106
    clock[0] = 106
    await masking.recover_automatically({**failed, "reason": "user_restore"})
    assert masking.retry_at is None
    request.assert_awaited_once()
    for reason in ("admin_restore", "trial_expired", "retry"):
        await masking.recover_automatically({**failed, "reason": reason})
    request.assert_awaited_once()
    await masking.recover_automatically({"state": "masked", "remapping_suspended": False})
    clock[0] += 30
    await masking.recover_automatically({"state": "masked", "remapping_suspended": False})
    assert masking.retry_delay == 2


@pytest.mark.asyncio
async def test_automatic_recovery_releases_only_reserved_hardware(monkeypatch):
    from keymasq.keymasqd import hardware_masking as module

    manager = DeviceManager()
    unrelated = SimpleNamespace(path="/dev/input/event99")
    manager.grabbed_devices["keyboard"] = [unrelated]
    manager.mask_registry.hardware_paths[RESERVATION_ID] = ["/dev/input/event5"]
    release = AsyncMock()
    selected = SimpleNamespace(path="/dev/input/event5")
    manager.grabbed_devices[RESERVATION_ID] = [selected]
    monkeypatch.setattr(module, "release_interface_unlocked", release)
    monkeypatch.setattr(manager, "release_all_devices", AsyncMock())
    await runtime(manager).release_runtime()
    release.assert_awaited_once_with(
        manager, RESERVATION_ID, selected.path, arm_hotplug_hiding=False
    )
    manager.release_all_devices.assert_not_awaited()
    assert manager.grabbed_devices["keyboard"] == [unrelated]


@pytest.mark.asyncio
async def test_failed_acquisition_finishes_its_own_recovery(monkeypatch):
    manager = DeviceManager()
    masking = runtime(manager)
    monkeypatch.setattr(masking, "runtime_ready", AsyncMock(return_value=False))
    monkeypatch.setattr(masking, "request", AsyncMock())
    masking.acquisition = asyncio.create_task(
        masking.acquire({"event_nodes": [], "token": "trial"})
    )
    await masking.acquisition
    cast(AsyncMock, masking.request).assert_awaited_once_with(
        "restore", {"reason": "replacement_unavailable", "token": "trial"}
    )
    assert not manager.mask_registry.hardware_paths


@pytest.mark.asyncio
async def test_registry_release_resolves_reconnected_aliases_and_preserves_other_reservations(
    tmp_path,
    monkeypatch,
):
    loop_thread = threading.get_ident()
    original_realpath = os.path.realpath

    def realpath(path, *args, **kwargs):
        assert threading.get_ident() != loop_thread
        return original_realpath(path, *args, **kwargs)

    first, second, replacement = (str(tmp_path / name) for name in ("event1", "event2", "event3"))
    alias = tmp_path / "controller"
    alias.symlink_to(first)
    registry = MaskRegistry()
    registry.register("first", [str(alias)], "/usb/first")
    registry.register("second", [second], "/usb/second")
    registry.hardware_paths["shared"] = [first, second]
    monkeypatch.setattr(os.path, "realpath", realpath)
    await registry.release("first", set())
    assert registry.hardware_paths == {"second": [second], "shared": [second]}

    alias.unlink()
    alias.symlink_to(replacement)
    registry.register("first", [str(alias)], "/usb/first")
    registry.hardware_paths["shared"] = [replacement, second]
    await registry.release("first", set())
    assert registry.hardware_paths == {"second": [second], "shared": [second]}
    assert registry.reservation_paths == {"second": [second]}
    assert registry.reservation_attachments == {"second": "/usb/second"}


@pytest.mark.asyncio
async def test_registry_release_includes_paths_registered_while_resolving(monkeypatch):
    from keymasq.keymasqd import masking_registry as module

    registry = MaskRegistry()
    registry.register("first", ["/input/first"], "/usb/first")
    resolve = module.resolve_paths
    calls = 0

    async def resolve_while_registering(paths):
        nonlocal calls
        calls += 1
        resolved = await resolve(paths)
        if calls == 1:
            registry.register("second", ["/input/second"], "/usb/second")
        return resolved

    monkeypatch.setattr(module, "resolve_paths", resolve_while_registering)
    await registry.release("first", set())
    assert registry.hardware_paths == {"second": ["/input/second"]}
    assert registry.reservation_paths == {"second": ["/input/second"]}


@pytest.mark.asyncio
async def test_two_reservations_can_share_a_configuration_and_release_independently(monkeypatch):
    from keymasq.keymasqd import hardware_masking as module

    manager = DeviceManager()
    first = SimpleNamespace(
        path="/dev/input/event5",
        hardware_id="@masked:first",
        interface_id="first",
        reset_mapping_runtime_state=AsyncMock(),
        release_tracked_outputs=Mock(),
        running=True,
        device=object(),
        task=SimpleNamespace(done=lambda: False),
        access_mode=InputAccessMode.EXCLUSIVE,
        default_output=None,
        uinput=object(),
    )
    second = SimpleNamespace(
        **{
            **vars(first),
            "path": "/dev/input/event6",
            "hardware_id": "@masked:second",
            "interface_id": "second",
        }
    )
    for device in (first, second):
        manager.grabbed_devices[device.hardware_id] = [device]
        manager.mask_registry.hardware_paths[device.hardware_id] = [device.path]
        manager.mask_registry.reservation_paths[device.hardware_id] = [device.path]
    monkeypatch.setattr(module, "resolve_stable_path", lambda path: path)
    monkeypatch.setattr(
        module.device_path_resolver,
        "resolve_evdev_interfaces",
        lambda *_args, **_kwargs: [
            SimpleNamespace(path=first.path),
            SimpleNamespace(path=second.path),
        ],
    )
    await adopt_masked_interfaces(manager, "shared-config", [first.path, second.path], None, Mock())
    assert manager.grabbed_devices["shared-config"] == [first, second]
    first_runtime = MaskRuntime(manager, "first", AsyncMock())
    second_runtime = MaskRuntime(manager, "second", AsyncMock())
    assert await first_runtime.runtime_ready()
    assert await second_runtime.runtime_ready()
    first.running = False
    assert not await first_runtime.runtime_ready()
    assert await second_runtime.runtime_ready()

    async def release(_manager, hardware_id, path, **_kwargs):
        manager.grabbed_devices[hardware_id] = [
            device for device in manager.grabbed_devices[hardware_id] if device.path != path
        ]

    monkeypatch.setattr(module, "release_interface_unlocked", release)
    await first_runtime.release_runtime()
    assert manager.grabbed_devices["shared-config"] == [second]
    assert manager.mask_registry.hardware_paths["shared-config"] == [second.path]
    assert await second_runtime.runtime_ready()
    assert not manager.masking_suspended


@pytest.mark.asyncio
async def test_stalled_device_update_does_not_delay_owner_heartbeat(monkeypatch):
    manager = DeviceManager()
    masking = HardwareMasking(manager)
    waiting, finish = asyncio.Event(), asyncio.Event()

    async def stalled(_status):
        waiting.set()
        await finish.wait()

    first = masking.runtime("first")
    second = masking.runtime("second")
    monkeypatch.setattr(first, "update", stalled)
    monkeypatch.setattr(second, "update", AsyncMock())
    request = AsyncMock(return_value={"masks": [{"id": "first"}, {"id": "second"}]})
    monkeypatch.setattr(masking, "request", request)
    try:
        await masking.monitor_once()
        await waiting.wait()
        await masking.monitor_once()
        assert request.await_count == 2
        assert first.update_task is not None and not first.update_task.done()
        await asyncio.sleep(0)
        second.update.assert_awaited()
    finally:
        finish.set()
        await masking.release_runtime()


@pytest.mark.asyncio
async def test_releasing_one_raw_only_mask_leaves_the_other_ready():
    manager = DeviceManager()
    first = MaskRuntime(manager, "first", AsyncMock())
    second = MaskRuntime(manager, "second", AsyncMock())
    await first.acquire({"event_nodes": [], "token": "first"})
    await second.acquire({"event_nodes": [], "token": "second"})
    assert await first.runtime_ready() and await second.runtime_ready()
    await first.release_runtime()
    assert not await first.runtime_ready()
    assert await second.runtime_ready()
    assert "@masked:second" in manager.mask_registry.reservation_paths


@pytest.mark.asyncio
@pytest.mark.parametrize("reader_failure", [False, True])
@pytest.mark.parametrize("recovery_state", ["readers", "records", "probe_error"])
async def test_emergency_release_precedes_blocked_controller_recovery(
    monkeypatch, reader_failure, recovery_state
):
    manager = DeviceManager()
    masking = HardwareMasking(manager)
    if recovery_state == "readers":
        manager.mask_registry.hardware_paths[RESERVATION_ID] = ["controller"]
    monkeypatch.setattr(
        masking.coordinator,
        "needs_recovery",
        AsyncMock(
            return_value=True,
            side_effect=OSError("state probe failed") if recovery_state == "probe_error" else None,
        ),
    )
    manager.masking_recovery = masking.recover_if_needed
    held = {"keyboard", "mouse"}
    entered, finish = asyncio.Event(), asyncio.Event()
    order = []

    async def neutralize():
        assert manager.masking_suspended
        order.append("neutralize")

    async def release():
        assert manager.masking_suspended
        held.clear()
        order.append("release")

    async def physical(*_):
        assert not held
        assert order == ["neutralize", "release"]
        entered.set()
        await finish.wait()
        return {}

    monkeypatch.setattr(manager, "neutralize_runtime", neutralize)
    monkeypatch.setattr(manager, "release_all_devices", release)
    monkeypatch.setattr(
        masking,
        "release_runtime",
        AsyncMock(side_effect=OSError("reader teardown failed") if reader_failure else None),
    )
    monkeypatch.setattr(masking, "request", physical)
    task = asyncio.create_task(manager.emergency_reset())
    await asyncio.wait_for(entered.wait(), 1)
    assert not held
    assert manager.masking_suspended
    assert not task.done()
    finish.set()
    if reader_failure:
        with pytest.raises(OSError, match="reader teardown failed"):
            await task
    else:
        await task


@pytest.mark.asyncio
async def test_one_device_release_failure_does_not_skip_other_devices(monkeypatch):
    from keymasq.keymasqd.runtime.grab import release as module

    manager = DeviceManager()
    manager.grabbed_devices.update({"keyboard": [], "mouse": [], "controller": []})
    attempted = set()

    async def release(_manager, hardware_id, **_):
        attempted.add(hardware_id)
        if hardware_id == "controller":
            raise OSError("controller teardown failed")

    monkeypatch.setattr(
        manager, "cancel_macro_playback", AsyncMock(side_effect=OSError("macro teardown failed"))
    )
    monkeypatch.setattr(module, "stop_device_event_loops", AsyncMock())
    monkeypatch.setattr(module.lifecycle, "clear_combo_runtime", AsyncMock())
    monkeypatch.setattr(module, "release_device_unlocked", release)
    with pytest.raises(OSError, match="macro teardown failed"):
        await manager.release_all_devices()
    assert attempted == {"keyboard", "mouse", "controller"}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["shutdown", "disconnect_without_restore", "suspend"])
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_failed_monitor_does_not_skip_lifecycle_cleanup(monkeypatch, operation, failure):
    masking = HardwareMasking(DeviceManager())
    masking.initialized = True
    masking.needs_startup = False
    masking.session_uid = masking.coordinator.session_uid = 1000

    async def failed_monitor():
        raise failure("monitor failed")

    masking.task = asyncio.create_task(failed_monitor())
    await asyncio.sleep(0)
    released, request = AsyncMock(), AsyncMock()
    monkeypatch.setattr(masking, "release_runtime", released)
    monkeypatch.setattr(masking, "request", request)
    with pytest.raises(failure, match="monitor failed"):
        if operation == "suspend":
            await masking.suspend()
        else:
            await masking.close(restore_hardware=operation == "shutdown")
    released.assert_awaited_once()
    if operation == "disconnect_without_restore":
        request.assert_not_awaited()
    else:
        request.assert_awaited_once_with("restore", {"reason": "lifecycle_stop"})
    assert masking.task is None
    assert masking.needs_startup
    expected_uid = 1000 if operation == "suspend" else None
    assert masking.session_uid == masking.coordinator.session_uid == expected_uid


@pytest.mark.asyncio
async def test_cancelled_monitor_does_not_cancel_daemon_disconnect_cleanup(monkeypatch):
    from keymasq.keymasqd.daemon import Daemon

    daemon = Daemon()
    daemon.running = True
    masking = daemon.hardware_masking
    masking.initialized = True

    async def cancelled_monitor():
        raise asyncio.CancelledError

    masking.task = asyncio.create_task(cancelled_monitor())
    await asyncio.sleep(0)
    order = []
    monkeypatch.setattr(
        masking, "release_runtime", AsyncMock(side_effect=lambda: order.append("readers"))
    )
    monkeypatch.setattr(
        masking, "request", AsyncMock(side_effect=lambda *_: order.append("physical"))
    )
    monkeypatch.setattr(daemon.recording_manager, "abort", AsyncMock())
    monkeypatch.setattr(daemon.recording_manager, "discard_all_pending_recordings", AsyncMock())
    monkeypatch.setattr(daemon.capture_manager, "close_all", Mock())
    monkeypatch.setattr(
        daemon.device_manager,
        "release_all_devices",
        AsyncMock(side_effect=lambda: order.append("ordinary")),
    )
    await daemon._on_client_disconnect()
    assert order == ["readers", "physical", "ordinary"]


@pytest.mark.asyncio
async def test_daemon_caller_cancellation_waits_for_masking_cleanup(monkeypatch):
    from keymasq.keymasqd.daemon import Daemon

    daemon = Daemon()
    masking = daemon.hardware_masking
    masking.initialized = True
    entered = asyncio.Event()

    async def monitor():
        entered.set()
        await asyncio.Event().wait()

    masking.task = asyncio.create_task(monitor())
    released, request = AsyncMock(), AsyncMock()
    monkeypatch.setattr(masking, "release_runtime", released)
    monkeypatch.setattr(masking, "request", request)
    cleanup = asyncio.create_task(daemon._run_async_cleanup("restore masks", masking.close))
    await entered.wait()
    cleanup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cleanup
    released.assert_awaited_once()
    request.assert_awaited_once_with("restore", {"reason": "lifecycle_stop"})
    assert masking.task is None


@pytest.mark.asyncio
async def test_status_snapshots_cannot_revoke_an_emergency_stop(monkeypatch):
    """Only an explicit global resume clears the admission latch."""
    manager = DeviceManager()
    masking = HardwareMasking(manager)
    poll_started, poll_release = asyncio.Event(), asyncio.Event()
    physical_entered, physical_finish = asyncio.Event(), asyncio.Event()
    resume_result = {"masks": [], "remapping_suspended": False}

    async def request(command, data=None):
        if command == "poll":
            poll_started.set()
            await poll_release.wait()
            return {"masks": [], "remapping_suspended": False}
        if command == "restore":
            physical_entered.set()
            await physical_finish.wait()
            return {"masks": [], "remapping_suspended": True}
        if command == "resume":
            return dict(resume_result)
        return {"masks": [], "remapping_suspended": False}

    monkeypatch.setattr(masking, "request", request)
    monkeypatch.setattr(masking, "startup", AsyncMock())
    monkeypatch.setattr(masking, "start_monitor", Mock())
    monkeypatch.setattr(masking.coordinator, "monitor_once", AsyncMock())
    monkeypatch.setattr(manager, "neutralize_runtime", AsyncMock())
    monkeypatch.setattr(manager, "release_all_devices", AsyncMock())
    monkeypatch.setattr(manager, "broadcast_hardware_recovery", Mock())

    async def grab_denied() -> bool:
        result = await manager.grab_device(hardware_id="kbd", evdev_paths=[], button_map={})
        return result.get("grabbed") is False and "suspended" in str(result.get("reason"))

    # A poll is in flight with a pre-stop snapshot when the emergency begins.
    poll = asyncio.create_task(masking.monitor_once())
    await asyncio.wait_for(poll_started.wait(), 1)
    stop = asyncio.create_task(masking.restore())
    await asyncio.wait_for(physical_entered.wait(), 1)
    assert manager.masking_suspended and await grab_denied()
    poll_release.set()
    await poll  # the stale snapshot completes while recovery is unfinished
    assert manager.masking_suspended and await grab_denied()
    physical_finish.set()
    await stop
    assert manager.masking_suspended and await grab_denied()
    # A fresh poll that reports no pause still cannot clear the latch.
    await masking.monitor_once()
    assert manager.masking_suspended and await grab_denied()
    # A per-reservation resume is not a global transition.
    await masking.handle(CommandType.RESUME_HARDWARE, {"id": "one"}, uid=1000)
    assert manager.masking_suspended
    # An explicit global resume clears it and reports the cleared state.
    result = await masking.handle(CommandType.RESUME_HARDWARE, {}, uid=1000)
    assert not manager.masking_suspended and not result["remapping_suspended"]
    # A stop whose marker could not be recorded still reaches the GUI.
    masking.emergency_latched = True
    masking._sync_admission()
    resume_result["remapping_suspended"] = False
    status = await masking.handle(CommandType.HARDWARE_INVENTORY, {}, uid=1000)
    assert status["remapping_suspended"] is True
