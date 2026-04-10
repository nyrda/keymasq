import asyncio
import contextlib
import errno
import logging
import os
from collections import deque
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import evdev
import pytest

from keyforge.common.ipc import CommandType
from keyforge.common.models import ActionType, DeviceType, MappingAction, SuperkeyMode
from keyforge.keyforged import device_manager as dm
from keyforge.keyforged.combo_engine import ComboDecision, ComboInputEvent
from keyforge.keyforged.device_manager import DesiredGrabConfig, DeviceManager
from keyforge.keyforged.runtime import actions as adm
from keyforge.keyforged.runtime import combos as cdm
from keyforge.keyforged.runtime import grab_lifecycle as ldm
from keyforge.keyforged.runtime import grabbed_device as gdm
from keyforge.keyforged.runtime import grabbed_device_actions as gda
from keyforge.keyforged.runtime import grabbed_device_events as gde
from keyforge.keyforged.runtime import grabbed_device_grab as gdg
from keyforge.keyforged.runtime import grabbed_device_outputs as gdo
from keyforge.keyforged.runtime import grabbed_device_repeat as gdr
from keyforge.keyforged.runtime import grabbed_device_types as gdt
from keyforge.keyforged.runtime import macros as mdm
from keyforge.keyforged.runtime import topology as tdm
from keyforge.keyforged.runtime.grabbed_device import GrabbedDevice
from keyforge.keyforged.superkey_state import SuperkeyActionData, SuperkeyConfig


@pytest.mark.skipif(not os.access("/dev/uinput", os.W_OK), reason="No uinput access")
class TestDeviceManager:
    @pytest.fixture
    def manager(self):
        return DeviceManager()

    @pytest.mark.asyncio
    async def test_list_devices(self, manager):
        result = await manager.list_devices()

        assert "devices" in result
        assert isinstance(result["devices"], list)

    @pytest.mark.asyncio
    async def test_grab_virtual_device(self, manager, virtual_mouse):
        device_path = virtual_mouse.device.path

        result = await manager.grab_device(
            hardware_id="1234:5678",
            evdev_paths=[device_path],
            button_map={"btn_left": "btn_left", "btn_right": "btn_right"},
        )

        assert result["grabbed"] is True
        assert result["hardware_id"] == "1234:5678"

        assert "1234:5678" in manager.grabbed_devices

        await manager.release_device("1234:5678")

    @pytest.mark.asyncio
    async def test_grab_device_keeps_desired_state_when_interface_is_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        create_global_uinputs = Mock()
        destroy_global_uinputs = Mock()

        def _missing_input_device(_path: str):
            raise FileNotFoundError(errno.ENOENT, "missing")

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm.evdev, "InputDevice", _missing_input_device)
        monkeypatch.setattr(ldm.runtime_outputs, "create_global_uinputs", create_global_uinputs)
        monkeypatch.setattr(ldm.runtime_outputs, "destroy_global_uinputs", destroy_global_uinputs)

        result = await manager.grab_device(
            hardware_id="1234:5678",
            evdev_paths=["/dev/input/event404"],
            button_map={"btn_side": "btn_side"},
        )

        assert result == {
            "grabbed": True,
            "hardware_id": "1234:5678",
            "grabbed_count": 0,
            "skipped_count": 0,
            "waiting_for_device": True,
        }
        assert manager.grabbed_devices == {}
        assert manager.grab_state.desired_paths["1234:5678"] == {"/dev/input/event404"}
        assert manager.grab_state.desired_grabs["1234:5678"] == DesiredGrabConfig(
            paths={"/dev/input/event404"},
            button_map={"btn_side": "btn_side"},
            force_grab_unmapped=False,
        )
        create_global_uinputs.assert_not_called()

    @pytest.mark.asyncio
    async def test_grab_device_still_errors_when_present_interfaces_match_no_buttons(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()

        class _InputDevice:
            def __init__(self, path: str) -> None:
                self.path = path

            def capabilities(self) -> dict[int, list[int]]:
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_A],
                }

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm.evdev, "InputDevice", _InputDevice)

        with pytest.raises(ValueError, match="matched mapped buttons"):
            await manager.grab_device(
                hardware_id="1234:5678",
                evdev_paths=["/dev/input/event10"],
                button_map={"btn_side": "btn_side"},
            )

    @pytest.mark.asyncio
    async def test_release_device(self, manager, virtual_mouse):
        device_path = virtual_mouse.device.path

        await manager.grab_device(
            hardware_id="1234:5678",
            evdev_paths=[device_path],
            button_map={},
        )

        result = await manager.release_device("1234:5678", immediate=True)

        assert result["released"] is True
        assert "1234:5678" not in manager.grabbed_devices

    @pytest.mark.asyncio
    async def test_release_nonexistent_device(self, manager):
        result = await manager.release_device("ffff:ffff")

        assert result["released"] is True

    @pytest.mark.asyncio
    async def test_set_mapping(self, manager, virtual_mouse):
        device_path = virtual_mouse.device.path

        await manager.grab_device(
            hardware_id="1234:5678",
            evdev_paths=[device_path],
            button_map={"btn_side": "btn_side"},
        )

        mapping = {
            "btn_side": {"action": "keyboard", "target": "key_a"},
        }

        result = await manager.set_mapping("1234:5678", mapping)

        assert result["updated"] is True

        await manager.release_device("1234:5678")

    @pytest.mark.asyncio
    async def test_set_mapping_ungrabbed_device(self, manager):
        mapping = {"btn_side": {"action": "keyboard", "target": "key_a"}}

        with pytest.raises(ValueError, match="not grabbed"):
            await manager.set_mapping("ffff:ffff", mapping)

    @pytest.mark.asyncio
    async def test_release_all_devices(self, manager, virtual_mouse, virtual_keyboard):
        mouse_path = virtual_mouse.device.path
        keyboard_path = virtual_keyboard.device.path

        await manager.grab_device(
            hardware_id="1234:5678",
            evdev_paths=[mouse_path],
            button_map={"btn_left": "btn_left"},
        )
        await manager.grab_device(
            hardware_id="abcd:ef01",
            evdev_paths=[keyboard_path],
            button_map={"key_a": "key_a"},
        )

        assert len(manager.grabbed_devices) == 2

        await manager.release_all_devices()

        assert len(manager.grabbed_devices) == 0


class TestDeviceDetection:
    @pytest.mark.asyncio
    async def test_set_mapping_resets_existing_runtime_state(self) -> None:
        manager = DeviceManager()
        fake_device = SimpleNamespace(reset_mapping_runtime_state=AsyncMock())
        manager.grabbed_devices = {"1234:5678": [fake_device]}

        result = await manager.set_mapping(
            "1234:5678",
            {"btn_side": {"action": "suppress"}},
        )

        assert result == {"updated": True, "hardware_id": "1234:5678"}
        fake_device.reset_mapping_runtime_state.assert_awaited_once()

    def test_detect_device_type(self):
        manager = DeviceManager()

        class MockDevice:
            def capabilities(self):
                return {
                    evdev.ecodes.EV_REL: [evdev.ecodes.REL_X, evdev.ecodes.REL_Y],
                    evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_LEFT, evdev.ecodes.BTN_RIGHT],
                }

        result = manager._detect_device_type(MockDevice())
        assert result == DeviceType.MOUSE

        class MockKeyboard:
            def capabilities(self):
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_A, evdev.ecodes.KEY_Q],
                }

        result = manager._detect_device_type(MockKeyboard())
        assert result == DeviceType.KEYBOARD

        class MockComboDevice:
            def capabilities(self):
                return {
                    evdev.ecodes.EV_REL: [evdev.ecodes.REL_X, evdev.ecodes.REL_Y],
                    evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_A, evdev.ecodes.BTN_LEFT],
                }

            def input_props(self):
                return [evdev.ecodes.INPUT_PROP_POINTING_STICK]

        combo_types = manager._detect_device_types(MockComboDevice())
        assert combo_types == ["mouse", "keyboard", "pointstick"]
        assert manager._detect_device_type(MockComboDevice()) == DeviceType.MOUSE


class TestListDevices:
    @pytest.mark.asyncio
    async def test_list_devices_offloads_scan_to_thread(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        expected = {"devices": [{"path": "/dev/input/event0"}]}
        calls: list[object] = []

        def fake_scan() -> dict:
            calls.append("scan")
            return expected

        async def fake_to_thread(func, /, *args, **kwargs):
            calls.append(func)
            assert args == ()
            assert kwargs == {}
            return func()

        monkeypatch.setattr(manager, "_list_devices_sync", fake_scan)
        monkeypatch.setattr(dm.asyncio, "to_thread", fake_to_thread)

        result = await manager.list_devices()

        assert result == expected
        assert calls == [fake_scan, "scan"]

    @pytest.mark.asyncio
    async def test_diagnostics_loop_offloads_snapshot_to_thread(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        manager.diagnostics_state.enabled = True
        manager.diagnostics_state.samples = {"event": deque([1.0, 3.0])}
        snapshots: list[dict[str, list[float]]] = []
        calls: list[tuple[object, tuple[object, ...]]] = []

        async def fake_sleep(_delay: float) -> None:
            manager.diagnostics_state.enabled = False

        async def fake_to_thread(func, /, *args, **kwargs):
            assert kwargs == {}
            calls.append((func, args))
            return func(*args)

        monkeypatch.setattr(dm.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(dm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(manager, "_log_diagnostics_snapshot", snapshots.append)

        await manager._diagnostics_loop()

        assert snapshots == [{"event": [1.0, 3.0]}]
        assert calls == [(snapshots.append, ({"event": [1.0, 3.0]},))]

    @pytest.mark.asyncio
    async def test_topology_watch_loop_retries_when_live_and_reconciled_snapshots_differ(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager(topology_poll_s=0.01)
        snapshot = {
            "/dev/input/by-id/test-mouse": dm.LiveInterfaceInfo(
                hardware_id="1234:5678",
                vendor_id="1234",
                product_id="5678",
                stable_path="/dev/input/by-id/test-mouse",
                path="/dev/input/event10",
                interface_id="mouse",
            )
        }
        manager.topology_state.live_snapshot = dict(snapshot)
        manager.topology_state.reconciled_snapshot = {}
        schedule_topology_reconcile = Mock()
        sleep_calls = 0

        async def fake_sleep(_delay: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                raise asyncio.CancelledError()

        async def fake_to_thread(func, /, *args, **kwargs):
            return snapshot

        monkeypatch.setattr(dm.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(dm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(tdm, "schedule_topology_reconcile", schedule_topology_reconcile)

        with pytest.raises(asyncio.CancelledError):
            await _runtime_topology_watch_loop(manager)

        schedule_topology_reconcile.assert_called_once_with(
            manager,
            snapshot,
            asyncio_mod=dm._topology_asyncio_runtime(),
            cancelled_error=asyncio.CancelledError,
            log=dm.log,
        )

    @pytest.mark.asyncio
    async def test_topology_watch_loop_logs_scan_failures_and_keeps_running(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        manager = DeviceManager(topology_poll_s=0.01)
        snapshot = {
            "/dev/input/by-id/test-mouse": dm.LiveInterfaceInfo(
                hardware_id="1234:5678",
                vendor_id="1234",
                product_id="5678",
                stable_path="/dev/input/by-id/test-mouse",
                path="/dev/input/event10",
                interface_id="mouse",
            )
        }
        schedule_topology_reconcile = Mock()
        sleep_calls = 0
        scan_calls = 0

        async def fake_sleep(_delay: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 3:
                raise asyncio.CancelledError()

        async def fake_to_thread(func, /, *args, **kwargs):
            nonlocal scan_calls
            scan_calls += 1
            if scan_calls == 1:
                raise RuntimeError("scan boom")
            return snapshot

        monkeypatch.setattr(dm.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(dm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(tdm, "schedule_topology_reconcile", schedule_topology_reconcile)

        with caplog.at_level(logging.WARNING, logger="keyforged.devices"):
            with pytest.raises(asyncio.CancelledError):
                await _runtime_topology_watch_loop(manager)

        assert "Topology scan failed: scan boom" in caplog.text
        schedule_topology_reconcile.assert_called_once_with(
            manager,
            snapshot,
            asyncio_mod=dm._topology_asyncio_runtime(),
            cancelled_error=asyncio.CancelledError,
            log=dm.log,
        )


class TestMacroControlActions:
    @pytest.mark.asyncio
    async def test_run_macro_control_action_wait_fixed_uses_speed(self, monkeypatch):
        manager = DeviceManager()

        sleep_calls: list[float] = []

        async def fake_sleep(duration: float) -> None:
            sleep_calls.append(duration)

        monkeypatch.setattr(dm.asyncio, "sleep", fake_sleep)

        await _runtime_run_macro_control_action(
            manager,
            {"macro_action": "wait_fixed", "duration_ms": 20},
            2.0,
        )

        assert sleep_calls == [0.01]

    @pytest.mark.asyncio
    async def test_run_macro_control_action_wait_random_uses_random_range(self, monkeypatch):
        manager = DeviceManager()
        sleep_calls: list[float] = []

        async def fake_sleep(duration: float) -> None:
            sleep_calls.append(duration)

        monkeypatch.setattr(dm.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(dm.random, "randint", lambda _minimum, _maximum: 50)

        await _runtime_run_macro_control_action(
            manager,
            {"macro_action": "wait_random", "min_ms": 10, "max_ms": 80},
            10.0,
        )

        assert sleep_calls == [0.005]

    @pytest.mark.asyncio
    async def test_run_macro_control_action_exec_async_broadcasts(self):
        manager = DeviceManager()

        callback = AsyncMock()

        async def cb(command, data):
            await callback(command, data)

        manager.broadcast_callback = cb

        await _runtime_run_macro_control_action(
            manager,
            {
                "macro_action": "exec_async",
                "command": "echo hi",
            },
            1.0,
        )

        callback.assert_awaited_once()
        called_command, called_data = callback.await_args.args
        assert called_command == CommandType.ACTION_TRIGGER
        assert called_data["action_type"] == "exec"
        assert called_data["macro_exec_async"] is True

    @pytest.mark.asyncio
    async def test_run_macro_control_action_exec_sync_wait_id_and_cleanup(self, monkeypatch):
        manager = DeviceManager()
        callback = AsyncMock()

        async def cb(command, data):
            await callback(command, data)

        manager.broadcast_callback = cb
        begin_mouse_rel_suppression = Mock()
        end_mouse_rel_suppression = Mock()

        async def fake_sleep(duration: float) -> None:
            return None

        async def fake_wait_for(awaitable, timeout):
            raise TimeoutError

        monkeypatch.setattr(dm.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(dm.asyncio, "wait_for", fake_wait_for)
        monkeypatch.setattr(mdm, "begin_mouse_rel_suppression", begin_mouse_rel_suppression)
        monkeypatch.setattr(mdm, "end_mouse_rel_suppression", end_mouse_rel_suppression)

        await _runtime_run_macro_control_action(
            manager,
            {
                "macro_action": "exec_sync",
                "command": "echo hi",
                "inhibit_mouse": True,
                "timeout_ms": 100,
            },
            1.0,
        )

        assert begin_mouse_rel_suppression.called is True
        assert end_mouse_rel_suppression.called is True
        assert manager.macro_state.exec_waiters == {}
        callback.assert_awaited_once()
        assert callback.await_args.args[0] == CommandType.ACTION_TRIGGER


class TestReleaseScheduling:
    @pytest.mark.asyncio
    async def test_release_on_hold_state_is_retried_then_released(self):
        manager = DeviceManager(held_release_retry_s=0.001)
        fake_device = type("Device", (), {})()
        fake_device.release = AsyncMock()

        holds = {"count": 0}

        def has_held() -> bool:
            holds["count"] += 1
            return holds["count"] == 1

        manager.grabbed_devices = {"hw": [fake_device]}
        fake_device.has_held_source_inputs = has_held

        async def release_device(_manager, _hardware_id: str, *, log) -> None:
            await fake_device.release()

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(ldm, "release_device_unlocked", release_device)

        _runtime_schedule_hardware_release(manager, "hw", 0.001)
        task = manager.grab_state.pending_hardware_release["hw"]
        await task
        monkeypatch.undo()

        assert fake_device.release.await_count == 1
        assert holds["count"] >= 2


class _FakeUInput:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.writes: list[tuple[int, int, int]] = []

    def write(self, event_type: int, code: int, value: int) -> None:
        self.writes.append((int(event_type), int(code), int(value)))

    def syn(self) -> None:
        return

    def close(self) -> None:
        return


def _make_grabbed_device(
    monkeypatch: pytest.MonkeyPatch,
    **kwargs,
) -> GrabbedDevice:
    monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
    monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")
    button_map = kwargs.pop("button_map", {})
    button_codes = kwargs.pop("button_codes", None)
    button_values = kwargs.pop("button_values", None)
    keyboard_uinput = kwargs.pop("keyboard_uinput", _FakeUInput())
    mouse_uinput = kwargs.pop("mouse_uinput", _FakeUInput())
    gamepad_uinput = kwargs.pop("gamepad_uinput", _FakeUInput())
    return GrabbedDevice(
        path="/dev/input/event-test",
        hardware_id="1234:5678",
        button_map=button_map,
        button_codes=button_codes,
        button_values=button_values,
        mapping_getter=lambda: {},
        event_callback=AsyncMock(return_value=None),
        keyboard_uinput=keyboard_uinput,  # type: ignore[arg-type]
        mouse_uinput=mouse_uinput,  # type: ignore[arg-type]
        gamepad_uinput=gamepad_uinput,  # type: ignore[arg-type]
        **kwargs,
    )


async def _runtime_on_device_event(
    manager: DeviceManager,
    hardware_id: str,
    evdev_path: str,
    event_type: int,
    event_code: int,
    event_value: int,
    stable_path: str | None = None,
    source: str | None = None,
):
    return await cdm.on_device_event(
        manager,
        hardware_id,
        evdev_path,
        event_type,
        event_code,
        event_value,
        stable_path,
        source,
        resolve_stable_path_fn=dm.resolve_stable_path,
        get_interface_id_fn=dm.get_interface_id,
        combo_binding_cls=dm.RuntimeComboBinding,
        combo_input_event_cls=ComboInputEvent,
        int_value_fn=dm._int_value,
        str_value_fn=dm._str_value,
        time_mod=dm.time,
        action_type_enum=dm.ActionType,
        mapping_action_cls=dm.MappingAction,
        emit_mouse_move_fn=dm._combo_emit_mouse_move_fn(),
        get_trigger_axis_fn=dm.get_trigger_axis,
        resolve_code_fn=dm.resolve_output_code,
        fire_and_observe_fn=dm._fire_and_observe,
        command_type=dm.CommandType,
        asyncio_mod=dm._combo_asyncio_runtime(),
        contextlib_mod=dm.contextlib,
        evdev_mod=dm._combo_evdev_runtime(),
        uinput_writer=dm._combo_uinput_writer(),
    )


async def _runtime_clear_combo_runtime(manager: DeviceManager) -> None:
    await cdm.clear_combo_runtime(
        manager,
        asyncio_mod=dm._combo_asyncio_runtime(),
        contextlib_mod=dm.contextlib,
        mapping_action_cls=dm.MappingAction,
        evdev_mod=dm._combo_evdev_runtime(),
        uinput_writer=dm._combo_uinput_writer(),
        emit_mouse_move_fn=dm._combo_emit_mouse_move_fn(),
        get_trigger_axis_fn=dm.get_trigger_axis,
        resolve_code_fn=dm.resolve_output_code,
        fire_and_observe_fn=dm._fire_and_observe,
        command_type=dm.CommandType,
        action_type_enum=dm.ActionType,
        time_mod=dm.time,
    )


async def _runtime_clear_combo_scope(
    manager: DeviceManager, hardware_id: str, source: str | None = None
) -> None:
    await cdm.clear_combo_runtime_for_binding_scope(
        manager,
        hardware_id,
        source,
        asyncio_mod=dm._combo_asyncio_runtime(),
        contextlib_mod=dm.contextlib,
        mapping_action_cls=dm.MappingAction,
        evdev_mod=dm._combo_evdev_runtime(),
        uinput_writer=dm._combo_uinput_writer(),
        emit_mouse_move_fn=dm._combo_emit_mouse_move_fn(),
        get_trigger_axis_fn=dm.get_trigger_axis,
        resolve_code_fn=dm.resolve_output_code,
        fire_and_observe_fn=dm._fire_and_observe,
        command_type=dm.CommandType,
        action_type_enum=dm.ActionType,
        time_mod=dm.time,
    )


def _runtime_refresh_combo_watchdog(manager: DeviceManager) -> None:
    cdm.refresh_combo_timeout_watchdog(
        manager,
        asyncio_mod=dm._combo_asyncio_runtime(),
        time_mod=dm.time,
        action_type_enum=dm.ActionType,
        mapping_action_cls=dm.MappingAction,
        emit_mouse_move_fn=dm._combo_emit_mouse_move_fn(),
        get_trigger_axis_fn=dm.get_trigger_axis,
        resolve_code_fn=dm.resolve_output_code,
        fire_and_observe_fn=dm._fire_and_observe,
        command_type=dm.CommandType,
        contextlib_mod=dm.contextlib,
        evdev_mod=dm._combo_evdev_runtime(),
        uinput_writer=dm._combo_uinput_writer(),
    )


async def _runtime_combo_timeout_watchdog(manager: DeviceManager, deadline: float) -> None:
    await cdm.combo_timeout_watchdog(
        manager,
        deadline,
        asyncio_mod=dm._combo_asyncio_runtime(),
        time_mod=dm.time,
        action_type_enum=dm.ActionType,
        mapping_action_cls=dm.MappingAction,
        emit_mouse_move_fn=dm._combo_emit_mouse_move_fn(),
        get_trigger_axis_fn=dm.get_trigger_axis,
        resolve_code_fn=dm.resolve_output_code,
        fire_and_observe_fn=dm._fire_and_observe,
        command_type=dm.CommandType,
        contextlib_mod=dm.contextlib,
        evdev_mod=dm._combo_evdev_runtime(),
        uinput_writer=dm._combo_uinput_writer(),
    )


async def _runtime_run_macro_control_action(
    manager: DeviceManager, ev: dict[str, object], speed: float
) -> None:
    await mdm.run_macro_control_action(
        manager,
        ev,
        speed,
        asyncio_mod=dm._macro_asyncio_runtime(),
        contextlib_mod=dm.contextlib,
        random_mod=dm.random,
        uuid_mod=dm.uuid,
        command_type=dm._macro_command_type(),
        str_value_fn=dm._str_value,
        int_value_fn=dm._int_value,
    )


async def _runtime_process_grabbed_event(device: GrabbedDevice, event: evdev.InputEvent) -> None:
    await gde.process_event(
        device,
        event,
        evdev_mod=evdev,
        time_mod=gde.time,
        log=gdm.log,
        combo_decision_cls=ComboDecision,
        classify_event_device_type_fn=gde.classify_event_device_type,
        action_type_enum=ActionType,
    )


async def _runtime_execute_grabbed_action(
    device: GrabbedDevice,
    action: MappingAction,
    event: evdev.InputEvent | SimpleNamespace,
    event_name: str,
) -> None:
    await gda.execute_action(
        device,
        action,
        event,
        event_name,
        asyncio_mod=gdm.ASYNCIO_RUNTIME,
        command_type=dm.CommandType,
        fire_and_observe_fn=gde._fire_and_observe,
        action_type_enum=ActionType,
        superkey_machine_cls=gda.SuperkeyMachine,
        evdev_mod=evdev,
        uinput_writer=lambda device: cast(gdt.WritableUInput | None, device),
    )


async def _runtime_recover_grabbed_event_processing_error(device: GrabbedDevice) -> None:
    await gde.recover_from_event_processing_error(device)


async def _runtime_wait_for_grabbed_active_key_activity(
    device: GrabbedDevice,
    timeout_s: float,
) -> bool:
    return await gdg.wait_for_active_key_activity(
        device,
        timeout_s,
        asyncio_mod=gdm.ASYNCIO_RUNTIME,
        errno_mod=errno,
        log=gdm.log,
    )


async def _runtime_wait_for_grabbed_active_keys_to_clear(device: GrabbedDevice) -> None:
    await gdg.wait_for_active_keys_to_clear(
        device,
        asyncio_mod=gdm.ASYNCIO_RUNTIME,
        time_mod=gdm.time,
        log=gdm.log,
        active_key_idle_max_wait_s=gdm.ACTIVE_KEY_IDLE_MAX_WAIT_S,
        active_key_idle_log_interval_s=gdm.ACTIVE_KEY_IDLE_LOG_INTERVAL_S,
    )


def _runtime_find_grabbed_action_for_event(
    device: GrabbedDevice,
    event: evdev.InputEvent,
    mapping: dict[str, MappingAction],
) -> MappingAction | None:
    return gde.find_action_for_event(device, event, mapping)


def _runtime_write_grabbed_key(
    device: GrabbedDevice,
    uinput_dev: object | None,
    code: int,
    value: int,
) -> None:
    gdo.write_key(
        device,
        uinput_dev,
        code,
        value,
        evdev_mod=evdev,
        uinput_writer=lambda device: cast(gdt.WritableUInput | None, device),
    )


async def _runtime_tap_grabbed_key(
    device: GrabbedDevice,
    code: int,
    hold_ms: int,
    event_name: str,
    uinput_dev: object,
) -> None:
    await gdr.tap_key(
        device,
        code,
        hold_ms,
        event_name,
        uinput_dev,
        asyncio_mod=gdm.ASYNCIO_RUNTIME,
    )


async def _runtime_tap_grabbed_trigger(
    device: GrabbedDevice,
    axis_code: int,
    hold_ms: int,
    event_name: str,
) -> None:
    await gdr.tap_trigger(
        device,
        axis_code,
        hold_ms,
        event_name,
        asyncio_mod=gdm.ASYNCIO_RUNTIME,
        evdev_mod=evdev,
        uinput_writer=lambda device: cast(gdt.WritableUInput | None, device),
    )


async def _runtime_tap_grabbed_move(
    device: GrabbedDevice,
    action: MappingAction,
    event_name: str,
    hold_ms: int,
) -> None:
    await gdr.tap_move(device, action, event_name, hold_ms, asyncio_mod=gdm.ASYNCIO_RUNTIME)


async def _runtime_topology_watch_loop(manager: DeviceManager) -> None:
    await tdm.topology_watch_loop(
        dm._topology_manager(manager),
        asyncio_mod=dm._topology_asyncio_runtime(),
        cancelled_error=asyncio.CancelledError,
        log=dm.log,
        live_interface_info_cls=dm._topology_live_interface_info_factory(),
        clear_device_path_cache_fn=dm.clear_device_path_cache,
        device_paths_fn=dm._device_paths,
        device_input_fn=dm._topology_device_input_fn(),
        resolve_stable_path_fn=dm.resolve_stable_path,
        get_interface_id_fn=dm.get_interface_id,
    )


def _runtime_schedule_topology_reconcile(
    manager: DeviceManager,
    snapshot: dict[str, dm.LiveInterfaceInfo],
) -> None:
    tdm.schedule_topology_reconcile(
        dm._topology_manager(manager),
        snapshot,
        asyncio_mod=dm._topology_asyncio_runtime(),
        cancelled_error=asyncio.CancelledError,
        log=dm.log,
    )


def _runtime_parse_action(manager: DeviceManager, action: object) -> MappingAction:
    return adm.parse_action(
        manager,
        action,
        str_value=dm._str_value,
        optional_str=dm._optional_str,
        int_value=dm._int_value,
        int_or_none=dm._int_or_none,
        float_value=dm._float_value,
    )


def _runtime_schedule_hardware_release(
    manager: DeviceManager,
    hardware_id: str,
    grace_s: float | None,
) -> dict[str, object]:
    return ldm.schedule_hardware_release_unlocked(
        manager,
        hardware_id,
        grace_s,
        asyncio_mod=ldm.ASYNCIO_RUNTIME,
        log=dm.log,
    )


async def _runtime_release_device_unlocked(
    manager: DeviceManager,
    hardware_id: str,
) -> dict[str, object]:
    return await ldm.release_device_unlocked(manager, hardware_id, log=dm.log)


async def _runtime_delayed_interface_release(
    manager: DeviceManager,
    hardware_id: str,
    path: str,
    delay: float,
) -> None:
    await ldm.delayed_interface_release(
        manager,
        hardware_id,
        path,
        delay,
        asyncio_mod=ldm.ASYNCIO_RUNTIME,
    )


async def _runtime_release_interface_unlocked(
    manager: DeviceManager,
    hardware_id: str,
    path: str,
) -> None:
    await ldm.release_interface_unlocked(manager, hardware_id, path)


def _runtime_device_has_mapped_buttons(
    caps: dict[int, object],
    mapped_evdev_names: set[str],
    mapped_bindings: set[tuple[int, int]] | None,
) -> bool:
    return ldm.device_has_mapped_buttons(
        caps,
        mapped_evdev_names,
        mapped_bindings,
        evdev_mod=dm.evdev,
    )


async def _runtime_start_combo_action(
    manager: DeviceManager,
    combo_id: str,
    action: MappingAction,
    binding: dm.RuntimeComboBinding,
    *,
    trigger_bindings: tuple[dm.RuntimeComboBinding, ...] | None = None,
    resolve_code_fn: object = dm.resolve_output_code,
) -> None:
    await cdm.start_combo_action(
        manager,
        combo_id,
        action,
        binding,
        trigger_bindings or (binding,),
        action_type_enum=dm.ActionType,
        asyncio_mod=dm._combo_asyncio_runtime(),
        emit_mouse_move_fn=dm._combo_emit_mouse_move_fn(),
        get_trigger_axis_fn=dm.get_trigger_axis,
        resolve_code_fn=resolve_code_fn,
        fire_and_observe_fn=dm._fire_and_observe,
        command_type=dm.CommandType,
        evdev_mod=dm._combo_evdev_runtime(),
        uinput_writer=dm._combo_uinput_writer(),
    )


async def _runtime_stop_combo_action(manager: DeviceManager, combo_id: str) -> None:
    await cdm.stop_combo_action(
        manager,
        combo_id,
        asyncio_mod=dm._combo_asyncio_runtime(),
        contextlib_mod=dm.contextlib,
        mapping_action_cls=dm.MappingAction,
        evdev_mod=dm._combo_evdev_runtime(),
        uinput_writer=dm._combo_uinput_writer(),
        emit_mouse_move_fn=dm._combo_emit_mouse_move_fn(),
        get_trigger_axis_fn=dm.get_trigger_axis,
        resolve_code_fn=dm.resolve_output_code,
        fire_and_observe_fn=dm._fire_and_observe,
        command_type=dm.CommandType,
        action_type_enum=dm.ActionType,
    )


class TestRapidfireRelease:
    @pytest.mark.asyncio
    async def test_grab_waits_until_active_keys_clear_before_grabbing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")
        call_order: list[str] = []

        class _FakeInputDevice:
            def __init__(self) -> None:
                self._active_keys = [
                    [evdev.ecodes.KEY_L],
                    [],
                ]
                self.grab_calls = 0

            def capabilities(self) -> dict[int, list[int]]:
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_L],
                    evdev.ecodes.EV_SYN: [],
                }

            def active_keys(self) -> list[int]:
                call_order.append("active_keys")
                if len(self._active_keys) > 1:
                    return self._active_keys.pop(0)
                return self._active_keys[0]

            def grab(self) -> None:
                call_order.append("grab")
                self.grab_calls += 1

        fake_input = _FakeInputDevice()
        created_tasks: list[asyncio.Task] = []
        to_thread_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []
        wait_timeouts: list[float] = []
        original_create_task = asyncio.create_task
        original_sleep = asyncio.sleep

        async def fake_to_thread(func, /, *args, **kwargs):
            to_thread_calls.append((func, args, kwargs))
            return func(*args, **kwargs)

        async def fake_wait_for_active_key_activity(timeout_s: float) -> bool:
            wait_timeouts.append(timeout_s)
            return True

        def fake_create_task(coro):
            coro.close()
            task = original_create_task(original_sleep(0))
            created_tasks.append(task)
            return task

        monkeypatch.setattr(gdm.evdev, "InputDevice", lambda _path: fake_input)
        monkeypatch.setattr(
            gdm.evdev,
            "UInput",
            lambda *args, **kwargs: call_order.append("uinput") or _FakeUInput(*args, **kwargs),
        )
        monkeypatch.setattr(gdm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(gdm.asyncio, "create_task", fake_create_task)
        monkeypatch.setattr(
            gdg,
            "wait_for_active_key_activity",
            lambda _device, timeout_s, **_kwargs: fake_wait_for_active_key_activity(timeout_s),
        )

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=_FakeUInput(),  # type: ignore[arg-type]
        )

        await device.grab()
        await original_sleep(0)

        assert wait_timeouts == [gdm.ACTIVE_KEY_IDLE_LOG_INTERVAL_S]
        assert [call[0] for call in to_thread_calls] == [
            fake_input.active_keys,
            fake_input.active_keys,
        ]
        assert fake_input.grab_calls == 1
        assert isinstance(device.uinput, _FakeUInput)
        assert call_order == ["uinput", "active_keys", "active_keys", "grab"]
        assert created_tasks

    @pytest.mark.asyncio
    async def test_wait_for_active_keys_logs_progress_while_delaying_grab(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        class _FakeInputDevice:
            def __init__(self) -> None:
                self._active_keys = [
                    [evdev.ecodes.KEY_L],
                    [evdev.ecodes.KEY_L],
                    [evdev.ecodes.KEY_L],
                    [],
                ]

            def active_keys(self) -> list[int]:
                if len(self._active_keys) > 1:
                    return self._active_keys.pop(0)
                return self._active_keys[0]

        fake_input = _FakeInputDevice()
        to_thread_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []
        wait_timeouts: list[float] = []
        monotonic_values = iter([0.0, 0.0, 0.2, 1.3])
        monotonic_last = {"value": 1.3}

        async def fake_to_thread(func, /, *args, **kwargs):
            to_thread_calls.append((func, args, kwargs))
            return func(*args, **kwargs)

        async def fake_wait_for_active_key_activity(timeout_s: float) -> bool:
            wait_timeouts.append(timeout_s)
            return len(wait_timeouts) != 2

        def fake_monotonic() -> float:
            try:
                monotonic_last["value"] = next(monotonic_values)
            except StopIteration:
                pass
            return monotonic_last["value"]

        monkeypatch.setattr(gdm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(gdm.time, "monotonic", fake_monotonic)
        monkeypatch.setattr(
            gdg,
            "wait_for_active_key_activity",
            lambda _device, timeout_s, **_kwargs: fake_wait_for_active_key_activity(timeout_s),
        )

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=_FakeUInput(),  # type: ignore[arg-type]
        )
        device.device = fake_input  # type: ignore[assignment]

        with caplog.at_level(logging.INFO, logger="keyforged.devices"):
            await _runtime_wait_for_grabbed_active_keys_to_clear(device)

        assert wait_timeouts == pytest.approx([1.0, 0.8, 1.0])
        assert len(to_thread_calls) == 4
        assert all(call[0] == fake_input.active_keys for call in to_thread_calls)
        assert "delaying grab until keys are released: key_l" in caplog.text
        assert "still waiting to grab; active keys still down: key_l" in caplog.text
        assert "active keys cleared, proceeding with grab" in caplog.text

    @pytest.mark.asyncio
    async def test_wait_for_active_keys_logs_read_failure_and_proceeds(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        class _FakeInputDevice:
            def active_keys(self) -> list[int]:
                raise RuntimeError("broken active_keys")

        to_thread_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

        async def fake_to_thread(func, /, *args, **kwargs):
            to_thread_calls.append((func, args, kwargs))
            return func(*args, **kwargs)

        monkeypatch.setattr(gdm.asyncio, "to_thread", fake_to_thread)

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=_FakeUInput(),  # type: ignore[arg-type]
        )
        device.device = _FakeInputDevice()  # type: ignore[assignment]

        with caplog.at_level(logging.WARNING, logger="keyforged.devices"):
            await _runtime_wait_for_grabbed_active_keys_to_clear(device)

        assert [call[0] for call in to_thread_calls] == [device.device.active_keys]
        assert "failed to read active keys before grab: broken active_keys" in caplog.text
        assert "proceeding with grab" in caplog.text

    @pytest.mark.asyncio
    async def test_wait_for_active_keys_times_out_with_clear_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")
        monkeypatch.setattr(gdm, "ACTIVE_KEY_IDLE_MAX_WAIT_S", 60.0)

        class _FakeInputDevice:
            def active_keys(self) -> list[int]:
                return [evdev.ecodes.KEY_L]

        wait_timeouts: list[float] = []
        monotonic_values = iter([0.0, 61.0])
        monotonic_last = {"value": 61.0}

        async def fake_to_thread(func, /, *args, **kwargs):
            return func(*args, **kwargs)

        async def fake_wait_for_active_key_activity(timeout_s: float) -> bool:
            wait_timeouts.append(timeout_s)
            return False

        def fake_monotonic() -> float:
            try:
                monotonic_last["value"] = next(monotonic_values)
            except StopIteration:
                pass
            return monotonic_last["value"]

        monkeypatch.setattr(gdm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(gdm.time, "monotonic", fake_monotonic)
        monkeypatch.setattr(
            gdg,
            "wait_for_active_key_activity",
            lambda _device, timeout_s, **_kwargs: fake_wait_for_active_key_activity(timeout_s),
        )

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=_FakeUInput(),  # type: ignore[arg-type]
        )
        device.device = _FakeInputDevice()  # type: ignore[assignment]

        with caplog.at_level(logging.ERROR, logger="keyforged.devices"):
            with pytest.raises(TimeoutError, match="timed out waiting 60.0s"):
                await _runtime_wait_for_grabbed_active_keys_to_clear(device)

        assert wait_timeouts == []
        assert "timed out waiting 60.0s for active keys to clear before grab" in caplog.text

    @pytest.mark.asyncio
    async def test_grab_closes_precreated_uinput_when_wait_times_out(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")
        monkeypatch.setattr(gdm, "ACTIVE_KEY_IDLE_MAX_WAIT_S", 60.0)

        class _FakeInputDevice:
            def capabilities(self) -> dict[int, list[int]]:
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_L],
                    evdev.ecodes.EV_SYN: [],
                }

            def active_keys(self) -> list[int]:
                return [evdev.ecodes.KEY_L]

        class _ClosableUInput(_FakeUInput):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.close_calls = 0

            def close(self) -> None:
                self.close_calls += 1

        monotonic_values = iter([0.0, 61.0])
        monotonic_last = {"value": 61.0}
        created_uinputs: list[_ClosableUInput] = []

        async def fake_to_thread(func, /, *args, **kwargs):
            return func(*args, **kwargs)

        def fake_monotonic() -> float:
            try:
                monotonic_last["value"] = next(monotonic_values)
            except StopIteration:
                pass
            return monotonic_last["value"]

        def fake_uinput(*args, **kwargs) -> _ClosableUInput:
            uinput = _ClosableUInput(*args, **kwargs)
            created_uinputs.append(uinput)
            return uinput

        monkeypatch.setattr(gdm.evdev, "InputDevice", lambda _path: _FakeInputDevice())
        monkeypatch.setattr(gdm.evdev, "UInput", fake_uinput)
        monkeypatch.setattr(gdm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(gdm.time, "monotonic", fake_monotonic)

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=_FakeUInput(),  # type: ignore[arg-type]
        )

        with pytest.raises(TimeoutError, match="timed out waiting 60.0s"):
            await device.grab()

        assert len(created_uinputs) == 1
        assert created_uinputs[0].close_calls == 1
        assert device.uinput is None

    @pytest.mark.asyncio
    async def test_grab_uses_explicit_passthrough_test_uinput_identity(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KEYFORGE_TEST_UINPUT", "1")
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        class _FakeInputDevice:
            def capabilities(self) -> dict[int, list[int]]:
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_L],
                    evdev.ecodes.EV_SYN: [],
                }

            def active_keys(self) -> list[int]:
                return []

            def grab(self) -> None:
                return

        created_tasks: list[asyncio.Task[None]] = []
        original_create_task = asyncio.create_task

        async def fake_to_thread(func, /, *args, **kwargs):
            return func(*args, **kwargs)

        def fake_create_task(coro):
            coro.close()
            task = original_create_task(asyncio.sleep(0))
            created_tasks.append(task)
            return task

        monkeypatch.setattr(gdm.evdev, "InputDevice", lambda _path: _FakeInputDevice())
        monkeypatch.setattr(gdm.evdev, "UInput", _FakeUInput)
        monkeypatch.setattr(gdm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(gdm.asyncio, "create_task", fake_create_task)

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=_FakeUInput(),  # type: ignore[arg-type]
        )

        await device.grab()
        await asyncio.sleep(0)

        assert isinstance(device.uinput, _FakeUInput)
        assert device.uinput.kwargs["name"] == "keyforge-test-passthrough-1234:5678"
        assert device.uinput.kwargs["vendor"] == 0x4B46
        assert device.uinput.kwargs["product"] == 0x1004

        for task in created_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @pytest.mark.asyncio
    async def test_rapidfire_key_releases_before_exiting_when_stopped_during_hold(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        fake_uinput = _FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=fake_uinput,  # type: ignore[arg-type]
        )
        device._running = True
        device.state.rapidfire_active["btn_side"] = True

        async def fake_sleep(_delay: float) -> None:
            device.state.rapidfire_active["btn_side"] = False

        monkeypatch.setattr(gdm.asyncio, "sleep", fake_sleep)

        await gdr.rapidfire_key(
            device,
            evdev.ecodes.KEY_A,
            50,
            50,
            "btn_side",
            fake_uinput,  # type: ignore[arg-type]
            asyncio_mod=gdm.ASYNCIO_RUNTIME,
        )

        assert fake_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]

    @pytest.mark.asyncio
    async def test_start_rapidfire_task_stops_existing_state_before_creating_new_task(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        fake_uinput = _FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=fake_uinput,  # type: ignore[arg-type]
        )

        calls: list[str] = []
        original_stop = gdr.stop_rapidfire

        def wrapped_stop(device_runtime: GrabbedDevice, event_name: str) -> None:
            calls.append(f"stop:{event_name}")
            original_stop(device_runtime, event_name)

        monkeypatch.setattr(gdr, "stop_rapidfire", wrapped_stop)

        def task_factory() -> asyncio.Task:
            calls.append("factory")
            return asyncio.create_task(asyncio.sleep(0))

        gdr.start_rapidfire_task(
            device,
            "btn_side",
            "key",
            task_factory,
            code=evdev.ecodes.KEY_A,
            uinput=fake_uinput,  # type: ignore[arg-type]
            axis_code=None,
        )
        await asyncio.sleep(0)
        gdr.stop_rapidfire(device, "btn_side")

        assert calls[:2] == ["stop:btn_side", "factory"]

    @pytest.mark.asyncio
    async def test_rapidfire_quick_release_and_repress_does_not_leave_task_or_key_stuck(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        fake_uinput = _FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"btn_side": "btn_side"},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.MOUSE,
            keyboard_uinput=fake_uinput,  # type: ignore[arg-type]
        )
        device._running = True

        action = dm.MappingAction(
            action_type=ActionType.KEYBOARD,
            target="key_a",
            rapidfire_enabled=True,
            rapidfire_hold_ms=50,
            rapidfire_wait_ms=50,
        )
        press_event = SimpleNamespace(value=1)
        release_event = SimpleNamespace(value=0)

        await _runtime_execute_grabbed_action(device, action, press_event, "btn_side")
        await _runtime_execute_grabbed_action(device, action, release_event, "btn_side")
        await _runtime_execute_grabbed_action(device, action, press_event, "btn_side")
        await asyncio.sleep(0)

        assert len(device.state.rapidfire_tasks) == 1

        await _runtime_execute_grabbed_action(device, action, release_event, "btn_side")
        await asyncio.sleep(0.01)

        assert device.state.rapidfire_tasks == {}
        assert device.state.rapidfire_outputs == {}
        assert device.state.held_output_keys["keyboard"] == set()
        assert fake_uinput.writes[-1] == (
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_A,
            0,
        )

    @pytest.mark.asyncio
    async def test_combo_passthrough_release_still_stops_active_rapidfire(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "mouse")

        decisions = [
            None,
            ComboDecision(passthrough_current_event=True),
        ]

        async def event_callback(*_args):
            return decisions.pop(0)

        fake_uinput = _FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"btn_side": "btn_side"},
            mapping_getter=lambda: {
                "btn_side": dm.MappingAction(
                    action_type=ActionType.KEYBOARD,
                    target="key_a",
                    rapidfire_enabled=True,
                    rapidfire_hold_ms=50,
                    rapidfire_wait_ms=50,
                )
            },
            event_callback=event_callback,
            device_type=DeviceType.MOUSE,
            keyboard_uinput=fake_uinput,  # type: ignore[arg-type]
        )
        device._running = True

        press_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.BTN_SIDE,
            value=1,
        )
        release_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.BTN_SIDE,
            value=0,
        )

        await _runtime_process_grabbed_event(device, press_event)
        await asyncio.sleep(0)
        assert "btn_side" in device.state.held_source_actions

        await _runtime_process_grabbed_event(device, release_event)
        await asyncio.sleep(0.01)

        assert device.state.rapidfire_tasks == {}
        assert device.state.rapidfire_outputs == {}
        assert device.state.held_output_keys["keyboard"] == set()
        assert "btn_side" not in device.state.held_source_actions
        assert fake_uinput.writes[-1] == (
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_A,
            0,
        )

    @pytest.mark.asyncio
    async def test_combo_passthrough_keydown_forces_matching_passthrough_keyup(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        decisions = [
            ComboDecision(passthrough_current_event=True, reset_candidates=True),
            None,
        ]

        async def event_callback(*_args):
            return decisions.pop(0)

        passthrough_uinput = _FakeUInput()
        mapped_uinput = _FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"key_1": "key_1"},
            mapping_getter=lambda: {},
            event_callback=event_callback,
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=mapped_uinput,  # type: ignore[arg-type]
        )
        device._running = True
        device.uinput = passthrough_uinput  # type: ignore[assignment]

        press_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_1,
            value=1,
        )
        release_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_1,
            value=0,
        )

        await _runtime_process_grabbed_event(device, press_event)
        await _runtime_process_grabbed_event(device, release_event)
        await asyncio.sleep(0)

        assert passthrough_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 0),
        ]
        assert mapped_uinput.writes == []
        assert device.state.combo_passthrough_held == set()

    @pytest.mark.asyncio
    async def test_combo_passthrough_does_not_bypass_unrelated_mapping_action(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        decisions = [
            ComboDecision(passthrough_current_event=True, reset_candidates=True),
            ComboDecision(passthrough_current_event=True, reset_candidates=True),
        ]

        async def event_callback(*_args):
            return decisions.pop(0)

        passthrough_uinput = _FakeUInput()
        mapped_uinput = _FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"key_1": "key_1"},
            mapping_getter=lambda: {
                "key_1": dm.MappingAction(
                    action_type=ActionType.KEYBOARD,
                    target="key_b",
                )
            },
            event_callback=event_callback,
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=mapped_uinput,  # type: ignore[arg-type]
        )
        device._running = True
        device.uinput = passthrough_uinput  # type: ignore[assignment]

        press_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_1,
            value=1,
        )
        release_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_1,
            value=0,
        )

        await _runtime_process_grabbed_event(device, press_event)
        await _runtime_process_grabbed_event(device, release_event)

        assert passthrough_uinput.writes == []
        assert mapped_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
        ]
        assert device.state.combo_passthrough_held == set()

    @pytest.mark.asyncio
    async def test_combo_consumed_modifier_release_still_passthroughs_when_held(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        decisions = [
            ComboDecision(passthrough_current_event=True),
            ComboDecision(consume_current_event=True),
        ]

        async def event_callback(*_args):
            return decisions.pop(0)

        passthrough_uinput = _FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"key_leftalt": "key_leftalt"},
            mapping_getter=lambda: {},
            event_callback=event_callback,
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=_FakeUInput(),  # type: ignore[arg-type]
        )
        device._running = True
        device.uinput = passthrough_uinput  # type: ignore[assignment]

        press_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_LEFTALT,
            value=1,
        )
        release_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_LEFTALT,
            value=0,
        )

        await _runtime_process_grabbed_event(device, press_event)
        await _runtime_process_grabbed_event(device, release_event)

        assert passthrough_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTALT, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTALT, 0),
        ]
        assert device.state.combo_passthrough_held == set()

    @pytest.mark.asyncio
    async def test_combo_consumed_release_still_stops_existing_rapidfire_action(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        decisions = [
            None,
            ComboDecision(consume_current_event=True),
        ]

        async def event_callback(*_args):
            return decisions.pop(0)

        fake_uinput = _FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"key_f5": "key_f5"},
            mapping_getter=lambda: {
                "key_f5": dm.MappingAction(
                    action_type=ActionType.KEYBOARD,
                    target="key_b",
                    rapidfire_enabled=True,
                    rapidfire_hold_ms=50,
                    rapidfire_wait_ms=50,
                )
            },
            event_callback=event_callback,
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=fake_uinput,  # type: ignore[arg-type]
        )
        device._running = True

        press_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_F5,
            value=1,
        )
        release_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_F5,
            value=0,
        )

        await _runtime_process_grabbed_event(device, press_event)
        await asyncio.sleep(0)
        assert "key_f5" in device.state.held_source_actions

        await _runtime_process_grabbed_event(device, release_event)
        await asyncio.sleep(0.01)

        assert device.state.rapidfire_tasks == {}
        assert device.state.rapidfire_outputs == {}
        assert device.state.held_output_keys["keyboard"] == set()
        assert "key_f5" not in device.state.held_source_actions
        assert fake_uinput.writes[-1] == (
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_B,
            0,
        )

    @pytest.mark.asyncio
    async def test_release_interface_skipped_when_path_still_desired(self):
        manager = DeviceManager(release_grace_s=0.001)
        fake_device = type("Device", (), {})()
        fake_device.release = AsyncMock()
        monkeypatch = pytest.MonkeyPatch()

        async def release_interface(_hardware_id: str, _path: str) -> None:
            await fake_device.release()

        manager.grabbed_devices = {"hw": []}
        manager.grab_state.desired_paths["hw"] = {"/dev/input/event0"}
        monkeypatch.setattr(ldm, "release_interface_unlocked", release_interface)

        await _runtime_delayed_interface_release(manager, "hw", "/dev/input/event0", 0.001)

        fake_device.release.assert_not_awaited()
        monkeypatch.undo()

    @pytest.mark.asyncio
    async def test_release_interface_clears_scoped_combo_runtime_before_device_teardown(
        self,
    ) -> None:
        manager = DeviceManager()
        fake_device = SimpleNamespace(
            path="/dev/input/event0",
            interface_id="mouse",
            release_tracked_outputs=Mock(),
            release=AsyncMock(),
        )
        manager.grabbed_devices = {"hw": [fake_device]}
        clear_combo_scope = AsyncMock()
        clear_combo_runtime = AsyncMock()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(cdm, "clear_combo_runtime_for_binding_scope", clear_combo_scope)
        monkeypatch.setattr(cdm, "clear_combo_runtime", clear_combo_runtime)

        await _runtime_release_interface_unlocked(manager, "hw", "/dev/input/event0")
        monkeypatch.undo()

        clear_combo_scope.assert_awaited_once()
        clear_combo_runtime.assert_not_awaited()
        fake_device.release_tracked_outputs.assert_called_once()
        fake_device.release.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_release_interface_preserves_desired_state_for_missing_managed_device(
        self,
    ) -> None:
        manager = DeviceManager()
        fake_device = SimpleNamespace(
            path="/dev/input/event0",
            interface_id="mouse",
            release_tracked_outputs=Mock(),
            release=AsyncMock(),
        )
        action = MappingAction(action_type=ActionType.KEYBOARD, target="key_a")
        manager.grabbed_devices = {"hw": [fake_device]}
        manager.active_mappings = {"hw": {"btn_side": action}}
        manager.grab_state.desired_paths["hw"] = {"/dev/input/event0"}
        manager.grab_state.desired_grabs["hw"] = DesiredGrabConfig(
            paths={"/dev/input/event0"},
            button_map={"btn_side": "btn_side"},
        )
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(cdm, "clear_combo_runtime_for_binding_scope", AsyncMock())

        await _runtime_release_interface_unlocked(manager, "hw", "/dev/input/event0")
        monkeypatch.undo()

        assert manager.grabbed_devices == {}
        assert manager.active_mappings["hw"] == {"btn_side": action}
        assert manager.grab_state.desired_paths["hw"] == {"/dev/input/event0"}
        assert manager.grab_state.desired_grabs["hw"].paths == {"/dev/input/event0"}

    @pytest.mark.asyncio
    async def test_release_device_clears_scoped_combo_runtime_before_releasing_hardware(
        self,
    ) -> None:
        manager = DeviceManager()
        fake_device = SimpleNamespace(release=AsyncMock())
        manager.grabbed_devices = {"hw": [fake_device]}
        clear_combo_scope = AsyncMock()
        clear_combo_runtime = AsyncMock()
        destroy_global_uinputs = Mock()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(cdm, "clear_combo_runtime_for_binding_scope", clear_combo_scope)
        monkeypatch.setattr(cdm, "clear_combo_runtime", clear_combo_runtime)
        monkeypatch.setattr(ldm.runtime_outputs, "destroy_global_uinputs", destroy_global_uinputs)

        result = await _runtime_release_device_unlocked(manager, "hw")
        monkeypatch.undo()

        assert result == {"released": True, "hardware_id": "hw"}
        clear_combo_scope.assert_awaited_once()
        clear_combo_runtime.assert_not_awaited()
        fake_device.release.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clear_combo_runtime_for_binding_scope_stops_only_affected_actions(
        self,
    ) -> None:
        manager = DeviceManager()
        manager.combo_state.engine.drop_candidates_for_binding_scope = Mock(  # type: ignore[method-assign]
            return_value={"combo-1"}
        )
        stop_combo_action = AsyncMock()
        refresh_combo_timeout_watchdog = Mock()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(cdm, "stop_combo_action", stop_combo_action)
        monkeypatch.setattr(cdm, "refresh_combo_timeout_watchdog", refresh_combo_timeout_watchdog)

        await _runtime_clear_combo_scope(manager, "1234:5678", "mouse")
        monkeypatch.undo()

        manager.combo_state.engine.drop_candidates_for_binding_scope.assert_called_once_with(
            "1234:5678",
            "mouse",
        )
        stop_combo_action.assert_awaited_once()
        refresh_combo_timeout_watchdog.assert_called_once()


class TestEventLoopRecovery:
    @pytest.mark.asyncio
    async def test_event_processing_error_releases_held_output_before_backoff(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        fake_uinput = _FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"key_f5": "key_f5"},
            mapping_getter=lambda: {
                "key_f5": dm.MappingAction(
                    action_type=ActionType.KEYBOARD,
                    target="key_a",
                )
            },
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=fake_uinput,  # type: ignore[arg-type]
        )

        class _FakeInputDevice:
            async def async_read_loop(self):
                yield SimpleNamespace(
                    type=evdev.ecodes.EV_KEY,
                    code=evdev.ecodes.KEY_F5,
                    value=1,
                )

        sleep_calls: list[float] = []
        original_execute_action = gda.execute_action

        async def fail_after_press(_device, action, event, event_name, **_kwargs):
            await original_execute_action(
                _device,
                action,
                event,
                event_name,
                asyncio_mod=gdm.ASYNCIO_RUNTIME,
                command_type=dm.CommandType,
                fire_and_observe_fn=lambda coro, _label: asyncio.create_task(coro),
                action_type_enum=ActionType,
                superkey_machine_cls=gda.SuperkeyMachine,
                evdev_mod=evdev,
                uinput_writer=lambda device: cast(gdt.WritableUInput | None, device),
            )
            raise RuntimeError("boom")

        async def fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        monkeypatch.setattr(gda, "execute_action", fail_after_press)
        monkeypatch.setattr(gdm.asyncio, "sleep", fake_sleep)

        device.device = _FakeInputDevice()  # type: ignore[assignment]
        device._running = True

        await gde.event_loop(device, asyncio_mod=gdm.ASYNCIO_RUNTIME, log=gdm.log)

        assert sleep_calls == [0.01]
        assert fake_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]
        assert device.state.held_output_keys["keyboard"] == set()
        assert device.state.held_source_actions == {}


class TestCombos:
    @pytest.mark.asyncio
    async def test_grabbed_device_combo_events_use_cached_identity_metadata(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Quick Save",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_f13",
                                }
                            ]
                        }
                    ],
                    "action": {"action": "suppress"},
                }
            ]
        )

        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: f"{path}-stable")
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"key_f13": "key_f13"},
            mapping_getter=lambda: {},
            event_callback=lambda *args, **kwargs: _runtime_on_device_event(
                manager, *args, **kwargs
            ),
            device_type=DeviceType.KEYBOARD,
        )

        def fail_resolve(_path: str) -> str:
            raise AssertionError("resolve_stable_path should not run in the hot event path")

        def fail_interface(_path: str) -> str:
            raise AssertionError("get_interface_id should not run in the hot event path")

        monkeypatch.setattr(gdm, "resolve_stable_path", fail_resolve)
        monkeypatch.setattr(gdm, "get_interface_id", fail_interface)

        decision = await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(
                type=evdev.ecodes.EV_KEY,
                code=evdev.ecodes.KEY_F13,
                value=1,
            ),
        )

        assert decision is None

    @pytest.mark.asyncio
    async def test_set_combos_parses_runtime_combo(self):
        manager = DeviceManager()

        result = await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Quick Toggle",
                    "profile_name": "Desktop",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "mouse",
                                    "evdev": "btn_side",
                                }
                            ]
                        }
                    ],
                    "action": {"action": "profile_toggle", "profile_name": "Gaming"},
                }
            ]
        )

        assert result == {"updated": True, "combo_count": 1}
        assert len(manager.active_combos) == 1
        assert manager.active_combos[0].action is not None
        assert manager.active_combos[0].action.profile_name == "Gaming"

    @pytest.mark.asyncio
    async def test_runtime_combo_match_consumes_events_and_broadcasts(self, monkeypatch):
        manager = DeviceManager()
        callback = AsyncMock()

        async def cb(command, data):
            await callback(command, data)

        manager.broadcast_callback = cb
        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Quick Toggle",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "mouse",
                                    "evdev": "btn_side",
                                }
                            ]
                        }
                    ],
                    "action": {"action": "profile_toggle", "profile_name": "Gaming"},
                }
            ]
        )

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "mouse")

        pressed = await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-mouse",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.BTN_SIDE,
            1,
        )
        released = await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-mouse",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.BTN_SIDE,
            0,
        )
        await asyncio.sleep(0)

        assert pressed is not None and pressed.consume_current_event is True
        assert released is not None and released.consume_current_event is True
        callback.assert_awaited_once()
        sent_command, sent_data = callback.await_args.args
        assert sent_command == CommandType.ACTION_TRIGGER
        assert sent_data["action_type"] == "profile_toggle"
        assert sent_data["profile_name"] == "Gaming"

    @pytest.mark.asyncio
    async def test_combo_recall_repeat_suppression_resumes_after_restore_via_event_loop(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        hardware_id = "1234:5678"
        passthrough = _FakeUInput()
        await manager.set_combos(
            [
                {
                    "id": "combo-recall-repeat",
                    "name": "combo-recall-repeat",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_x",
                                },
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_c",
                                },
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                    "recall_trigger_keys": True,
                    "restore_trigger_keys": ["key_x"],
                }
            ]
        )
        manager.output_state.keyboard_uinput = _FakeUInput()

        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id=hardware_id,
            button_map={"key_x": "key_x", "key_c": "key_c"},
            mapping_getter=lambda: {},
            event_callback=lambda *args, **kwargs: _runtime_on_device_event(
                manager, *args, **kwargs
            ),
            device_type=DeviceType.KEYBOARD,
        )
        device._running = True
        device.uinput = passthrough  # type: ignore[assignment]
        manager.grabbed_devices = {hardware_id: [device]}

        press_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=1)
        press_c = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_C, value=1)
        repeat_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=2)
        release_c = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_C, value=0)

        await _runtime_process_grabbed_event(device, press_x)
        await _runtime_process_grabbed_event(device, press_c)
        await asyncio.sleep(0)

        assert device.state.combo_recalled_bindings == {"key_x"}
        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 0),
        ]
        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
        ]

        await _runtime_process_grabbed_event(device, repeat_x)
        await asyncio.sleep(0)

        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 0),
        ]

        await _runtime_process_grabbed_event(device, release_c)
        await asyncio.sleep(0)

        assert device.state.combo_recalled_bindings == set()
        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 1),
        ]
        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0),
        ]

        await _runtime_process_grabbed_event(device, repeat_x)
        await asyncio.sleep(0)

        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 2),
        ]

    @pytest.mark.asyncio
    async def test_combo_restore_respects_suppress_mapping_for_trigger_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        hardware_id = "1234:5678"
        passthrough = _FakeUInput()
        keyboard = _FakeUInput()

        await manager.set_combos(
            [
                {
                    "id": "combo-restore-suppress",
                    "name": "combo-restore-suppress",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_capslock",
                                },
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_x",
                                },
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                    "recall_trigger_keys": True,
                    "restore_trigger_keys": ["key_capslock"],
                }
            ]
        )
        manager.output_state.keyboard_uinput = keyboard

        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        mapping = {
            "key_capslock": dm.MappingAction(action_type=ActionType.SUPPRESS),
        }
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id=hardware_id,
            button_map={"key_capslock": "key_capslock", "key_x": "key_x"},
            mapping_getter=lambda: mapping,
            event_callback=lambda *args, **kwargs: _runtime_on_device_event(
                manager, *args, **kwargs
            ),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=keyboard,  # type: ignore[arg-type]
        )
        device._running = True
        device.uinput = passthrough  # type: ignore[assignment]
        manager.grabbed_devices = {hardware_id: [device]}

        press_caps = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_CAPSLOCK,
            value=1,
        )
        press_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=1)
        release_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=0)

        await _runtime_process_grabbed_event(device, press_caps)
        await _runtime_process_grabbed_event(device, press_x)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(device, release_x)
        await asyncio.sleep(0)

        assert passthrough.writes == []
        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0),
        ]

    @pytest.mark.asyncio
    async def test_combo_restore_replays_simple_keyboard_remap_for_trigger_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        hardware_id = "1234:5678"
        passthrough = _FakeUInput()
        keyboard = _FakeUInput()

        await manager.set_combos(
            [
                {
                    "id": "combo-restore-remap",
                    "name": "combo-restore-remap",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_capslock",
                                },
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_x",
                                },
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                    "recall_trigger_keys": True,
                    "restore_trigger_keys": ["key_capslock"],
                }
            ]
        )
        manager.output_state.keyboard_uinput = keyboard

        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        mapping = {
            "key_capslock": dm.MappingAction(
                action_type=ActionType.KEYBOARD,
                target="key_leftmeta",
            ),
        }
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id=hardware_id,
            button_map={"key_capslock": "key_capslock", "key_x": "key_x"},
            mapping_getter=lambda: mapping,
            event_callback=lambda *args, **kwargs: _runtime_on_device_event(
                manager, *args, **kwargs
            ),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=keyboard,  # type: ignore[arg-type]
        )
        device._running = True
        device.uinput = passthrough  # type: ignore[assignment]
        manager.grabbed_devices = {hardware_id: [device]}

        press_caps = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_CAPSLOCK,
            value=1,
        )
        press_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=1)
        release_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=0)

        await _runtime_process_grabbed_event(device, press_caps)
        await _runtime_process_grabbed_event(device, press_x)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(device, release_x)
        await asyncio.sleep(0)

        assert passthrough.writes == []
        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTMETA, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTMETA, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTMETA, 1),
        ]

    @pytest.mark.asyncio
    async def test_combo_restore_recalls_remapped_modifier_trigger_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        hardware_id = "1234:5678"
        passthrough = _FakeUInput()
        keyboard = _FakeUInput()

        await manager.set_combos(
            [
                {
                    "id": "combo-restore-remapped-modifier",
                    "name": "combo-restore-remapped-modifier",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_leftctrl",
                                },
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_x",
                                },
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                    "recall_trigger_keys": True,
                    "restore_trigger_keys": ["ctrl"],
                }
            ]
        )
        manager.output_state.keyboard_uinput = keyboard

        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        mapping = {
            "key_leftctrl": dm.MappingAction(
                action_type=ActionType.KEYBOARD,
                target="key_leftmeta",
            ),
        }
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id=hardware_id,
            button_map={"key_leftctrl": "key_leftctrl", "key_x": "key_x"},
            mapping_getter=lambda: mapping,
            event_callback=lambda *args, **kwargs: _runtime_on_device_event(
                manager, *args, **kwargs
            ),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=keyboard,  # type: ignore[arg-type]
        )
        device._running = True
        device.uinput = passthrough  # type: ignore[assignment]
        manager.grabbed_devices = {hardware_id: [device]}

        press_ctrl = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_LEFTCTRL,
            value=1,
        )
        press_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=1)
        release_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=0)

        await _runtime_process_grabbed_event(device, press_ctrl)
        await _runtime_process_grabbed_event(device, press_x)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(device, release_x)
        await asyncio.sleep(0)

        assert passthrough.writes == []
        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTMETA, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTMETA, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTMETA, 1),
        ]

    @pytest.mark.asyncio
    async def test_combo_single_step_survives_unrelated_same_keyboard_actions(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        hardware_id = "1234:5678"
        passthrough = _FakeUInput()
        keyboard = _FakeUInput()

        await manager.set_combos(
            [
                {
                    "id": "combo-c",
                    "name": "combo-c",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_leftalt",
                                },
                                {"hardware_id": hardware_id, "source": "kbd", "evdev": "key_c"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                },
                {
                    "id": "combo-v",
                    "name": "combo-v",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_leftalt",
                                },
                                {"hardware_id": hardware_id, "source": "kbd", "evdev": "key_v"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f14"},
                },
            ]
        )
        manager.output_state.keyboard_uinput = keyboard

        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id=hardware_id,
            button_map={
                "key_leftalt": "key_leftalt",
                "key_c": "key_c",
                "key_v": "key_v",
                "key_h": "key_h",
            },
            mapping_getter=lambda: {},
            event_callback=lambda *args, **kwargs: _runtime_on_device_event(
                manager, *args, **kwargs
            ),
            device_type=DeviceType.KEYBOARD,
        )
        device._running = True
        device.uinput = passthrough  # type: ignore[assignment]
        manager.grabbed_devices = {hardware_id: [device]}

        press_alt = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_LEFTALT,
            value=1,
        )
        press_c = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_C, value=1)
        release_c = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_C, value=0)
        press_h = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_H, value=1)
        release_h = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_H, value=0)
        press_v = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_V, value=1)
        release_v = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_V, value=0)

        await _runtime_process_grabbed_event(device, press_alt)
        await _runtime_process_grabbed_event(device, press_c)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(device, release_c)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(device, press_h)
        await _runtime_process_grabbed_event(device, release_h)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(device, press_v)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(device, release_v)
        await asyncio.sleep(0)

        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 0),
        ]

    @pytest.mark.asyncio
    async def test_combo_single_step_survives_unrelated_mouse_click_between_combos(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        keyboard_hw = "1234:5678"
        mouse_hw = "1234:5678"
        keyboard = _FakeUInput()
        keyboard_passthrough = _FakeUInput()
        mouse_passthrough = _FakeUInput()

        await manager.set_combos(
            [
                {
                    "id": "combo-c",
                    "name": "combo-c",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": keyboard_hw,
                                    "source": "kbd",
                                    "evdev": "key_leftalt",
                                },
                                {"hardware_id": keyboard_hw, "source": "kbd", "evdev": "key_c"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                },
                {
                    "id": "combo-v",
                    "name": "combo-v",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": keyboard_hw,
                                    "source": "kbd",
                                    "evdev": "key_leftalt",
                                },
                                {"hardware_id": keyboard_hw, "source": "kbd", "evdev": "key_v"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f14"},
                },
            ]
        )
        manager.output_state.keyboard_uinput = keyboard

        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(
            gdm,
            "get_interface_id",
            lambda path: "mouse" if "mouse" in path else "kbd",
        )

        keyboard_device = GrabbedDevice(
            path="/dev/input/event-kbd",
            hardware_id=keyboard_hw,
            button_map={
                "key_leftalt": "key_leftalt",
                "key_c": "key_c",
                "key_v": "key_v",
            },
            mapping_getter=lambda: {},
            event_callback=lambda *args, **kwargs: _runtime_on_device_event(
                manager, *args, **kwargs
            ),
            device_type=DeviceType.KEYBOARD,
        )
        keyboard_device._running = True
        keyboard_device.uinput = keyboard_passthrough  # type: ignore[assignment]

        mouse_device = GrabbedDevice(
            path="/dev/input/event-mouse",
            hardware_id=mouse_hw,
            button_map={"btn_left": "btn_left"},
            mapping_getter=lambda: {},
            event_callback=lambda *args, **kwargs: _runtime_on_device_event(
                manager, *args, **kwargs
            ),
            device_type=DeviceType.MOUSE,
        )
        mouse_device._running = True
        mouse_device.uinput = mouse_passthrough  # type: ignore[assignment]
        manager.grabbed_devices = {keyboard_hw: [keyboard_device, mouse_device]}

        press_alt = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_LEFTALT,
            value=1,
        )
        press_c = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_C, value=1)
        release_c = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_C, value=0)
        press_mouse = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.BTN_LEFT, value=1)
        release_mouse = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.BTN_LEFT,
            value=0,
        )
        press_v = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_V, value=1)
        release_v = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_V, value=0)

        await _runtime_process_grabbed_event(keyboard_device, press_alt)
        await _runtime_process_grabbed_event(keyboard_device, press_c)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(keyboard_device, release_c)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(mouse_device, press_mouse)
        await _runtime_process_grabbed_event(mouse_device, release_mouse)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(keyboard_device, press_v)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(keyboard_device, release_v)
        await asyncio.sleep(0)

        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 0),
        ]

    @pytest.mark.asyncio
    async def test_combo_overlapping_first_step_combos_all_trigger(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        hardware_id = "1234:5678"
        passthrough = _FakeUInput()
        keyboard = _FakeUInput()

        await manager.set_combos(
            [
                {
                    "id": "combo-c",
                    "name": "combo-c",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_leftalt",
                                },
                                {"hardware_id": hardware_id, "source": "kbd", "evdev": "key_c"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                },
                {
                    "id": "combo-v",
                    "name": "combo-v",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_leftalt",
                                },
                                {"hardware_id": hardware_id, "source": "kbd", "evdev": "key_v"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f14"},
                },
                {
                    "id": "combo-c-v",
                    "name": "combo-c-v",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_leftalt",
                                },
                                {"hardware_id": hardware_id, "source": "kbd", "evdev": "key_c"},
                                {"hardware_id": hardware_id, "source": "kbd", "evdev": "key_v"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f15"},
                },
            ]
        )
        manager.output_state.keyboard_uinput = keyboard

        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id=hardware_id,
            button_map={
                "key_leftalt": "key_leftalt",
                "key_c": "key_c",
                "key_v": "key_v",
            },
            mapping_getter=lambda: {},
            event_callback=lambda *args, **kwargs: _runtime_on_device_event(
                manager, *args, **kwargs
            ),
            device_type=DeviceType.KEYBOARD,
        )
        device._running = True
        device.uinput = passthrough  # type: ignore[assignment]
        manager.grabbed_devices = {hardware_id: [device]}

        press_alt = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_LEFTALT,
            value=1,
        )
        press_c = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_C, value=1)
        press_v = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_V, value=1)

        await _runtime_process_grabbed_event(device, press_alt)
        await _runtime_process_grabbed_event(device, press_c)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(device, press_v)
        await asyncio.sleep(0)

        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F15, 1),
        ]

    @pytest.mark.asyncio
    async def test_runtime_combo_broadcast_does_not_block_hot_path(self, monkeypatch):
        manager = DeviceManager()
        blocker = asyncio.Event()

        async def stalled_callback(_command, _data):
            await blocker.wait()

        manager.broadcast_callback = stalled_callback
        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Quick Toggle",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "mouse",
                                    "evdev": "btn_side",
                                }
                            ]
                        }
                    ],
                    "action": {"action": "profile_toggle", "profile_name": "Gaming"},
                }
            ]
        )

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "mouse")

        result = await asyncio.wait_for(
            _runtime_on_device_event(
                manager,
                "1234:5678",
                "/dev/input/by-id/test-mouse",
                evdev.ecodes.EV_KEY,
                evdev.ecodes.BTN_SIDE,
                1,
            ),
            timeout=0.05,
        )

        assert result is not None and result.consume_current_event is True
        blocker.set()
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_runtime_combo_keyboard_action_mirrors_press_and_release(self, monkeypatch):
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = Mock()

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Quick Save",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_f13",
                                }
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f5"},
                }
            ]
        )

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

        pressed = await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            1,
        )
        released = await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            0,
        )

        assert pressed is not None and pressed.consume_current_event is True
        assert released is not None and released.consume_current_event is True
        assert manager.output_state.keyboard_uinput.write.call_args_list[0].args == (
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F5,
            1,
        )
        assert manager.output_state.keyboard_uinput.write.call_args_list[1].args == (
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F5,
            0,
        )

    @pytest.mark.asyncio
    async def test_runtime_combo_tap_key_releases_when_runtime_clears(self, monkeypatch):
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Quick Save",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_f13",
                                }
                            ]
                        }
                    ],
                    "action": {
                        "action": "keyboard",
                        "target": "key_f5",
                        "tap_enabled": True,
                        "tap_hold_ms": 1000,
                    },
                }
            ]
        )

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

        pressed = await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            1,
        )
        await asyncio.sleep(0)
        await _runtime_clear_combo_runtime(manager)

        assert pressed is not None and pressed.consume_current_event is True
        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F5, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F5, 0),
        ]

    @pytest.mark.asyncio
    async def test_runtime_combo_tap_trigger_releases_when_runtime_clears(self, monkeypatch):
        manager = DeviceManager()
        manager.output_state.gamepad_uinput = _FakeUInput()

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Quick Trigger",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "mouse",
                                    "evdev": "btn_side",
                                }
                            ]
                        }
                    ],
                    "action": {
                        "action": "gamepad",
                        "target": "btn_lt",
                        "tap_enabled": True,
                        "tap_hold_ms": 1000,
                    },
                }
            ]
        )

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "mouse")

        pressed = await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-mouse",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.BTN_SIDE,
            1,
        )
        await asyncio.sleep(0)
        await _runtime_clear_combo_runtime(manager)

        assert pressed is not None and pressed.consume_current_event is True
        assert manager.output_state.gamepad_uinput.writes == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 255),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0),
        ]

    @pytest.mark.asyncio
    async def test_runtime_combo_recall_uses_matching_grabbed_device(self, monkeypatch):
        manager = DeviceManager()
        recalled = Mock()
        fake_device = type(
            "FakeDevice",
            (),
            {"interface_id": "kbd", "emit_combo_release": recalled},
        )()
        manager.grabbed_devices = {"1234:5678": [fake_device]}

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Quick Save",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_a",
                                },
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_s",
                                },
                            ]
                        }
                    ],
                    "action": {"action": "suppress"},
                }
            ]
        )

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

        await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_A,
            1,
        )
        await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_S,
            1,
        )

        recalled.assert_called_once_with("key_a")

    @pytest.mark.asyncio
    async def test_runtime_combo_does_not_recall_modifier_keys(self, monkeypatch):
        manager = DeviceManager()
        recalled = Mock()
        fake_device = type(
            "FakeDevice",
            (),
            {"interface_id": "kbd", "emit_combo_release": recalled},
        )()
        manager.grabbed_devices = {"1234:5678": [fake_device]}

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Alt Combo",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_leftalt",
                                },
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_1",
                                },
                            ]
                        }
                    ],
                    "action": {"action": "suppress"},
                }
            ]
        )

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

        await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_LEFTALT,
            1,
        )
        await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_1,
            1,
        )

        recalled.assert_not_called()

    @pytest.mark.asyncio
    async def test_runtime_combo_rearms_from_held_modifier_after_wrong_key_release(
        self,
        monkeypatch,
    ):
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Meta One",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_leftmeta",
                                },
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_1",
                                },
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                }
            ]
        )

        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={
                "key_leftmeta": "key_leftmeta",
                "key_4": "key_4",
                "key_1": "key_1",
            },
            mapping_getter=lambda: {},
            event_callback=lambda *args, **kwargs: _runtime_on_device_event(
                manager, *args, **kwargs
            ),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=manager.output_state.keyboard_uinput,  # type: ignore[arg-type]
            mouse_uinput=_FakeUInput(),  # type: ignore[arg-type]
            gamepad_uinput=_FakeUInput(),  # type: ignore[arg-type]
        )
        passthrough = _FakeUInput()
        device.uinput = passthrough  # type: ignore[assignment]
        device._running = True
        manager.grabbed_devices = {"1234:5678": [device]}

        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_LEFTMETA, value=1)
        )
        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_4, value=1)
        )
        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_4, value=0)
        )
        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_1, value=1)
        )

        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTMETA, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_4, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_4, 0),
        ]
        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
        ]

    @pytest.mark.asyncio
    async def test_runtime_combo_hold_macro_stops_on_release(self, monkeypatch):
        manager = DeviceManager()
        manager.play_macro = AsyncMock(return_value={"status": "ok"})  # type: ignore[method-assign]

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Hold Macro",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_f13",
                                }
                            ]
                        }
                    ],
                    "action": {
                        "action": "macro",
                        "macro_name": "hold",
                        "macro_loop_mode": "hold",
                    },
                }
            ]
        )

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

        await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            1,
        )
        await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            0,
        )

        assert manager.play_macro.await_count == 2
        press_call = manager.play_macro.await_args_list[0].kwargs
        release_call = manager.play_macro.await_args_list[1].kwargs
        assert press_call["trigger_value"] == 1
        assert press_call["source_device"] == "combo"
        assert press_call["source_button"] == "combo:combo-1"
        assert release_call["trigger_value"] == 0
        assert release_call["source_device"] == "combo"
        assert release_call["source_button"] == "combo:combo-1"


class TestSuperkeys:
    @pytest.mark.asyncio
    async def test_mapping_reset_clears_combo_passthrough_hold_but_preserves_passthrough_release(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        decisions = [
            ComboDecision(passthrough_current_event=True, reset_candidates=True),
            None,
        ]
        mapping_state: dict[str, dm.MappingAction] = {}

        async def event_callback(*_args):
            return decisions.pop(0)

        passthrough_uinput = _FakeUInput()
        mapped_uinput = _FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"key_1": "key_1"},
            mapping_getter=lambda: mapping_state,
            event_callback=event_callback,
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=mapped_uinput,  # type: ignore[arg-type]
        )
        device._running = True
        device.uinput = passthrough_uinput  # type: ignore[assignment]

        press_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_1,
            value=1,
        )
        release_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_1,
            value=0,
        )

        await _runtime_process_grabbed_event(device, press_event)

        assert device.state.combo_passthrough_held == {"key_1"}
        assert "key_1" not in device.state.held_source_actions

        await device.reset_mapping_runtime_state()
        mapping_state["key_1"] = dm.MappingAction(
            action_type=ActionType.KEYBOARD,
            target="key_a",
        )

        assert device.state.combo_passthrough_held == set()
        assert device.state.held_source_actions["key_1"] is None

        await _runtime_process_grabbed_event(device, release_event)

        assert passthrough_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 0),
        ]
        assert mapped_uinput.writes == []
        assert "key_1" not in device.state.held_source_actions

    @pytest.mark.asyncio
    async def test_mapping_reset_clears_combo_recalled_suppression_state(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = _make_grabbed_device(monkeypatch)
        device.state.combo_passthrough_held.add("key_x")
        device.state.combo_recalled_bindings.add("key_x")

        await device.reset_mapping_runtime_state()

        assert device.state.combo_passthrough_held == set()
        assert device.state.combo_recalled_bindings == set()

    @pytest.mark.asyncio
    async def test_combo_recalled_repeat_is_suppressed_until_restore_or_new_press(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        passthrough = _FakeUInput()
        device = _make_grabbed_device(monkeypatch)
        device.uinput = passthrough  # type: ignore[assignment]
        device._running = True
        device.state.combo_passthrough_held.add("key_x")
        device.mark_combo_recalled_binding("key_x")

        repeat_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_X,
            value=2,
        )

        await _runtime_process_grabbed_event(device, repeat_event)

        assert passthrough.writes == []
        assert device.state.combo_passthrough_held == {"key_x"}
        assert device.state.combo_recalled_bindings == {"key_x"}

        device.clear_combo_recalled_binding("key_x")
        await _runtime_process_grabbed_event(device, repeat_event)

        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 2),
        ]

    @pytest.mark.asyncio
    async def test_combo_recalled_modifier_uses_normalized_name_for_suppression(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        passthrough = _FakeUInput()
        device = _make_grabbed_device(monkeypatch)
        device.uinput = passthrough  # type: ignore[assignment]
        device._running = True
        device.state.combo_passthrough_held.add("key_leftmeta")
        device.mark_combo_recalled_binding("key_leftmeta")

        repeat_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_LEFTMETA,
            value=2,
        )
        release_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_LEFTMETA,
            value=0,
        )

        await _runtime_process_grabbed_event(device, repeat_event)
        await _runtime_process_grabbed_event(device, release_event)

        assert passthrough.writes == []
        assert device.state.combo_recalled_bindings == set()
        assert device.state.combo_passthrough_held == set()

    @pytest.mark.asyncio
    async def test_combo_recalled_release_clears_suppression_without_passthrough(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        callback = AsyncMock(return_value=None)
        passthrough = _FakeUInput()
        device = _make_grabbed_device(monkeypatch)
        device.uinput = passthrough  # type: ignore[assignment]
        device._running = True
        device.event_callback = callback
        device.state.combo_passthrough_held.add("key_x")
        device.mark_combo_recalled_binding("key_x")
        device.state.held_source_actions["key_x"] = None

        release_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_X,
            value=0,
        )

        await _runtime_process_grabbed_event(device, release_event)

        assert callback.await_count == 1
        assert passthrough.writes == []
        assert device.state.combo_passthrough_held == set()
        assert device.state.combo_recalled_bindings == set()
        assert "key_x" not in device.state.held_source_actions

    @pytest.mark.asyncio
    async def test_combo_recalled_press_becomes_fresh_press_again(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        passthrough = _FakeUInput()
        device = _make_grabbed_device(monkeypatch)
        device.uinput = passthrough  # type: ignore[assignment]
        device._running = True
        device.state.combo_passthrough_held.add("key_x")
        device.mark_combo_recalled_binding("key_x")

        callback_calls = {"count": 0}

        async def event_callback(*_args):
            callback_calls["count"] += 1
            assert device.state.combo_recalled_bindings == set()
            assert device.state.combo_passthrough_held == set()
            return ComboDecision(passthrough_current_event=True)

        device.event_callback = event_callback
        press_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_X,
            value=1,
        )

        await _runtime_process_grabbed_event(device, press_event)

        assert callback_calls["count"] == 1
        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 1),
        ]
        assert device.state.combo_recalled_bindings == set()
        assert device.state.combo_passthrough_held == {"key_x"}

    @pytest.mark.asyncio
    async def test_vvv_logs_raw_hardware_events_but_skips_mouse_motion(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        passthrough = _FakeUInput()
        device = _make_grabbed_device(monkeypatch)
        device.uinput = passthrough  # type: ignore[assignment]
        device._running = True
        device.verbosity = 3

        key_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_X,
            value=2,
        )
        rel_event = SimpleNamespace(
            type=evdev.ecodes.EV_REL,
            code=evdev.ecodes.REL_X,
            value=12,
        )

        with caplog.at_level(logging.DEBUG, logger="keyforged.devices"):
            await _runtime_process_grabbed_event(device, key_event)
            await _runtime_process_grabbed_event(device, rel_event)

        assert "[hw 1234:5678 kbd] type=1 code=45 name=key_x value=2" in caplog.text
        assert "REL_X" not in caplog.text
        assert "type=2 code=0" not in caplog.text

    @pytest.mark.asyncio
    async def test_superkey_release_after_reset_does_not_recreate_stale_machine(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "mouse")

        mapping_state = {
            "btn_side": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=SuperkeyConfig(
                    name="test",
                    tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
                ),
            )
        }

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"btn_side": "btn_side"},
            mapping_getter=lambda: mapping_state,
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.MOUSE,
            keyboard_uinput=_FakeUInput(),  # type: ignore[arg-type]
        )
        device._running = True

        press_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.BTN_SIDE,
            value=1,
        )
        release_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.BTN_SIDE,
            value=0,
        )

        await _runtime_process_grabbed_event(device, press_event)
        assert "btn_side" in device.state.superkey_machines

        await device.reset_superkeys()
        assert device.state.superkey_machines == {}

        await _runtime_process_grabbed_event(device, release_event)

        assert device.state.superkey_machines == {}

    @pytest.mark.asyncio
    async def test_shared_superkey_config_on_two_inputs_holds_output_until_both_release(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "mouse")

        shared_config = SuperkeyConfig(
            name="shared",
            hold_threshold_ms=0,
            hold_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
        )
        mapping_state = {
            "btn_side": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=shared_config,
            ),
            "btn_extra": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=shared_config,
            ),
        }

        keyboard_uinput = _FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"btn_side": "btn_side", "btn_extra": "btn_extra"},
            mapping_getter=lambda: mapping_state,
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.MOUSE,
            keyboard_uinput=keyboard_uinput,  # type: ignore[arg-type]
        )
        device._running = True

        side_press = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.BTN_SIDE,
            value=1,
        )
        extra_press = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.BTN_EXTRA,
            value=1,
        )
        side_release = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.BTN_SIDE,
            value=0,
        )
        extra_release = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.BTN_EXTRA,
            value=0,
        )

        await _runtime_process_grabbed_event(device, side_press)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(device, extra_press)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert set(device.state.superkey_machines) == {"btn_side", "btn_extra"}
        assert device.state.superkey_machines["btn_side"].state.value == "holding"
        assert device.state.superkey_machines["btn_extra"].state.value == "holding"
        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
        ]

        await _runtime_process_grabbed_event(device, side_release)
        await asyncio.sleep(0)

        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
        ]
        assert device.state.held_output_keys["keyboard"] == {evdev.ecodes.KEY_A}

        await _runtime_process_grabbed_event(device, extra_release)
        await asyncio.sleep(0)

        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]
        assert device.state.held_output_keys["keyboard"] == set()

    @pytest.mark.asyncio
    async def test_overload_superkey_fans_out_press_repeat_and_release(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        mapping_state = {
            "key_f13": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=SuperkeyConfig(
                    name="overload",
                    mode=SuperkeyMode.OVERLOAD,
                    overload_actions=[
                        dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
                        dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_b"),
                    ],
                ),
            )
        }

        keyboard_uinput = _FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"key_f13": "key_f13"},
            mapping_getter=lambda: mapping_state,
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=keyboard_uinput,  # type: ignore[arg-type]
        )
        device._running = True

        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F13, value=1),
        )
        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F13, value=2),
        )
        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F13, value=0),
        )

        assert device.state.superkey_machines == {}
        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 2),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 2),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
        ]

    @pytest.mark.asyncio
    async def test_overload_superkey_refcounts_shared_outputs_across_two_inputs(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "mouse")

        shared_config = SuperkeyConfig(
            name="overload_shared",
            mode=SuperkeyMode.OVERLOAD,
            overload_actions=[
                dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
            ],
        )
        mapping_state = {
            "btn_side": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=shared_config,
            ),
            "btn_extra": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=shared_config,
            ),
        }

        keyboard_uinput = _FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"btn_side": "btn_side", "btn_extra": "btn_extra"},
            mapping_getter=lambda: mapping_state,
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.MOUSE,
            keyboard_uinput=keyboard_uinput,  # type: ignore[arg-type]
        )
        device._running = True

        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.BTN_SIDE, value=1),
        )
        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.BTN_EXTRA, value=1),
        )
        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.BTN_SIDE, value=0),
        )

        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
        ]
        assert device.state.held_output_keys["keyboard"] == {evdev.ecodes.KEY_A}

        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.BTN_EXTRA, value=0),
        )

        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]

    @pytest.mark.asyncio
    async def test_reset_mapping_runtime_state_seeds_startup_held_action_and_releases_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        mapping_state = {
            "key_f13": dm.MappingAction(
                action_type=ActionType.KEYBOARD,
                target="key_a",
            )
        }

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"key_f13": "key_f13"},
            mapping_getter=lambda: mapping_state,
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=_FakeUInput(),  # type: ignore[arg-type]
        )
        device._running = True
        device.device = SimpleNamespace(active_keys=lambda: [evdev.ecodes.KEY_F13])

        await device.reset_mapping_runtime_state()

        assert device.state.held_source_actions["key_f13"] == mapping_state["key_f13"]
        assert device.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]

    @pytest.mark.asyncio
    async def test_superkey_broadcast_does_not_block_hot_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "mouse")

        blocker = asyncio.Event()

        async def stalled_callback(_command, _data):
            await blocker.wait()

        mapping_state = {
            "btn_side": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=SuperkeyConfig(
                    name="test",
                    tap_actions=[SuperkeyActionData(action_type="exec", exec_ref=7)],
                ),
            )
        }

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"btn_side": "btn_side"},
            mapping_getter=lambda: mapping_state,
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.MOUSE,
            keyboard_uinput=_FakeUInput(),  # type: ignore[arg-type]
            broadcast_callback=stalled_callback,
        )
        device._running = True

        press_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.BTN_SIDE,
            value=1,
        )
        release_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.BTN_SIDE,
            value=0,
        )

        await asyncio.wait_for(_runtime_process_grabbed_event(device, press_event), timeout=0.05)
        await asyncio.wait_for(
            _runtime_process_grabbed_event(device, release_event), timeout=0.05
        )

        blocker.set()
        await asyncio.sleep(0)


class TestRuntimeFailureCleanup:
    @pytest.mark.asyncio
    async def test_event_processing_error_clears_scoped_runtime_and_releases_outputs(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        cleanup = AsyncMock()
        keyboard_uinput = _FakeUInput()
        gamepad_uinput = _FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=keyboard_uinput,  # type: ignore[arg-type]
            gamepad_uinput=gamepad_uinput,  # type: ignore[arg-type]
            runtime_cleanup_callback=cleanup,
        )
        device._running = True
        _runtime_write_grabbed_key(
            device, keyboard_uinput, evdev.ecodes.KEY_A, 1
        )  # type: ignore[arg-type]

        await _runtime_recover_grabbed_event_processing_error(device)

        cleanup.assert_awaited_once_with("1234:5678", "kbd")
        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]
        assert gamepad_uinput.writes[-2:] == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RZ, 0),
        ]
        assert device.state.held_output_keys["keyboard"] == set()


class TestDeviceManagerHelpers:
    def test_create_global_uinputs_uses_explicit_test_identities(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KEYFORGE_TEST_UINPUT", "1")
        manager = SimpleNamespace(
            output_state=SimpleNamespace(
                device_count=0,
                keyboard_uinput=None,
                mouse_uinput=None,
                gamepad_uinput=None,
            )
        )
        created: list[_FakeUInput] = []

        def fake_uinput(**kwargs) -> _FakeUInput:
            device = _FakeUInput(**kwargs)
            created.append(device)
            return device

        ldm.runtime_outputs.create_global_uinputs(
            manager,
            evdev_mod=SimpleNamespace(
                ecodes=evdev.ecodes,
                UInput=fake_uinput,
                AbsInfo=evdev.AbsInfo,
            ),
            log=logging.getLogger("test"),
            uinput_writer=lambda device: device,
        )

        assert manager.output_state.device_count == 1
        assert len(created) == 3
        assert created[0].kwargs["name"] == "keyforge-test-keyboard"
        assert created[0].kwargs["vendor"] == 0x4B46
        assert created[0].kwargs["product"] == 0x1001
        assert created[1].kwargs["name"] == "keyforge-test-mouse"
        assert created[1].kwargs["vendor"] == 0x4B46
        assert created[1].kwargs["product"] == 0x1002
        assert created[2].kwargs["name"] == "keyforge-test-gamepad"
        assert created[2].kwargs["vendor"] == 0x4B46
        assert created[2].kwargs["product"] == 0x1003

    @pytest.mark.asyncio
    async def test_grab_and_release_device_orchestrates_existing_and_removed_paths(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _RawInputDevice:
            def __init__(self, path: str) -> None:
                self.path = path

            def capabilities(self) -> dict[int, list[int]]:
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SIDE],
                    evdev.ecodes.EV_REL: [evdev.ecodes.REL_X, evdev.ecodes.REL_Y],
                }

        created: dict[str, object] = {}

        class _FakeManagedDevice:
            def __init__(self, **kwargs) -> None:
                self.path = kwargs["path"]
                self.interface_id = "mouse"
                self.button_map_updates: list[dict[str, str]] = []
                self.button_code_updates: list[dict[str, int]] = []
                self.grab = AsyncMock()
                self.release = AsyncMock()
                self.reset_mapping_runtime_state = AsyncMock()
                created[self.path] = self

            def release_tracked_outputs(self) -> None:
                return

            def has_held_source_inputs(self) -> bool:
                return False

            def update_button_map(
                self,
                button_map: dict[str, str],
                button_codes: dict[str, int] | None = None,
                button_values: dict[str, int] | None = None,
            ) -> None:
                self.button_map_updates.append(dict(button_map))
                self.button_code_updates.append(dict(button_codes or {}))
                assert button_values is None or isinstance(button_values, dict)

        manager = DeviceManager()
        create_global_uinputs = Mock()
        destroy_global_uinputs = Mock()
        schedule_interface_release = Mock()
        cancel_pending_interface_release = Mock()

        monkeypatch.setattr(dm.evdev, "InputDevice", _RawInputDevice)
        monkeypatch.setattr(dm, "GrabbedDevice", _FakeManagedDevice)
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(ldm.runtime_outputs, "create_global_uinputs", create_global_uinputs)
        monkeypatch.setattr(ldm.runtime_outputs, "destroy_global_uinputs", destroy_global_uinputs)
        monkeypatch.setattr(ldm, "schedule_interface_release", schedule_interface_release)
        monkeypatch.setattr(
            ldm,
            "cancel_pending_interface_release",
            cancel_pending_interface_release,
        )

        first = await manager.grab_device(
            "1234:5678",
            ["/dev/input/event0", "/dev/input/event1"],
            {"left": "btn_side"},
        )
        second = await manager.grab_device(
            "1234:5678",
            ["/dev/input/event1"],
            {"right": "btn_side"},
        )
        released = await manager.release_device("1234:5678", immediate=True)

        assert first == {
            "grabbed": True,
            "hardware_id": "1234:5678",
            "grabbed_count": 2,
            "skipped_count": 0,
            "waiting_for_device": False,
        }
        assert second["grabbed_count"] == 2
        create_global_uinputs.assert_called_once()
        cancel_pending_interface_release.assert_called_once_with(
            manager, "1234:5678", "/dev/input/event1"
        )
        schedule_interface_release.assert_called_once_with(
            manager,
            "1234:5678",
            "/dev/input/event0",
            asyncio_mod=ldm.ASYNCIO_RUNTIME,
            log=ldm.log,
        )
        assert created["/dev/input/event1"].button_map_updates == [{"right": "btn_side"}]
        assert created["/dev/input/event1"].button_code_updates == [{}]
        assert released == {"released": True, "hardware_id": "1234:5678"}
        assert created["/dev/input/event0"].release.await_count == 1
        assert created["/dev/input/event1"].release.await_count == 1
        assert manager.grabbed_devices == {}
        assert manager.active_mappings == {}
        assert manager.grab_state.desired_paths == {}
        destroy_global_uinputs.assert_called_once()

    def test_parse_action_supports_string_and_hyprland_dispatch_alias(self) -> None:
        manager = DeviceManager()

        string_action = _runtime_parse_action(manager, "key_a")
        dispatch_action = _runtime_parse_action(
            manager,
            {"action": "hyprland_dispatch", "dispatcher": "workspace", "args": "2"},
        )

        assert string_action.action_type == ActionType.KEYBOARD
        assert string_action.target == "key_a"
        assert dispatch_action.action_type == ActionType.COMPOSITOR_DISPATCH
        assert dispatch_action.compositor_id == "hyprland"
        assert dispatch_action.compositor_dispatcher == "workspace"
        assert dispatch_action.compositor_args == "2"

    @pytest.mark.asyncio
    async def test_set_combos_skips_malformed_entries_and_parses_timeout(self) -> None:
        manager = DeviceManager()
        clear_combo_runtime = AsyncMock()
        refresh_combo_timeout_watchdog = Mock()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(cdm, "clear_combo_runtime", clear_combo_runtime)
        monkeypatch.setattr(cdm, "refresh_combo_timeout_watchdog", refresh_combo_timeout_watchdog)

        result = await manager.set_combos(
            [
                "bad",
                {"id": "missing-action", "steps": []},
                {
                    "id": "valid",
                    "name": "Valid",
                    "steps": [
                        "bad-step",
                        {
                            "events": [
                                "bad-event",
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_a",
                                },
                            ],
                            "timeout_ms": "250",
                        },
                    ],
                    "action": {"action": "suppress"},
                },
            ]
        )

        assert result == {"updated": True, "combo_count": 1}
        assert len(manager.active_combos) == 1
        assert manager.active_combos[0].steps[0].timeout_ms == 250
        clear_combo_runtime.assert_awaited_once()
        refresh_combo_timeout_watchdog.assert_called_once()
        monkeypatch.undo()

    @pytest.mark.asyncio
    async def test_set_combos_parses_superkey_combo_action(self) -> None:
        manager = DeviceManager()

        result = await manager.set_combos(
            [
                {
                    "id": "superkey-combo",
                    "name": "Superkey Combo",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_a",
                                }
                            ]
                        }
                    ],
                    "action": {
                        "action": "superkey",
                        "superkey": {
                            "name": "combo-pattern",
                            "mode": "pattern",
                            "tap_actions": [{"action": "keyboard", "target": "key_b"}],
                            "double_tap_actions": [
                                {"action": "keyboard", "target": "key_c"}
                            ],
                        },
                    },
                }
            ]
        )

        assert result == {"updated": True, "combo_count": 1}
        assert len(manager.active_combos) == 1
        assert manager.active_combos[0].action is not None
        assert manager.active_combos[0].action.action_type == ActionType.SUPERKEY
        assert manager.active_combos[0].action.superkey_config is not None
        assert manager.active_combos[0].action.superkey_config.mode == SuperkeyMode.PATTERN
        assert manager.active_combos[0].action.superkey_config.tap_actions[0].target == "key_b"

    @pytest.mark.asyncio
    async def test_set_combos_parses_trigger_recall_settings(self) -> None:
        manager = DeviceManager()

        result = await manager.set_combos(
            [
                {
                    "id": "recall-combo",
                    "name": "Recall Combo",
                    "recall_trigger_keys": True,
                    "restore_trigger_keys": ["meta", "key_c", "meta"],
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "meta",
                                },
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_c",
                                },
                            ]
                        }
                    ],
                    "action": {"action": "suppress"},
                }
            ]
        )

        assert result == {"updated": True, "combo_count": 1}
        assert len(manager.active_combos) == 1
        assert manager.active_combos[0].recall_trigger_keys is True
        assert manager.active_combos[0].restore_trigger_keys == ["meta", "key_c"]

    @pytest.mark.asyncio
    async def test_schedule_topology_reconcile_logs_failures_and_clears_task(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        manager = DeviceManager(topology_debounce_s=0.01)
        snapshot: dict[str, dm.LiveInterfaceInfo] = {}

        async def fake_sleep(_delay: float) -> None:
            return

        reconcile_topology = AsyncMock(side_effect=RuntimeError("reconcile boom"))
        monkeypatch.setattr(dm.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(tdm, "reconcile_topology", reconcile_topology)

        with caplog.at_level(logging.WARNING, logger="keyforged.devices"):
            _runtime_schedule_topology_reconcile(manager, snapshot)
            task = manager.topology_state.reconcile_task
            assert task is not None
            await task

        assert "Topology reconcile failed: reconcile boom" in caplog.text
        assert manager.topology_state.reconcile_task is None

    def test_combo_capture_queue_round_trip(self) -> None:
        manager = DeviceManager()
        ready = asyncio.Event()
        manager.grabbed_devices = {"hw": [object(), object()], "other": [object()]}

        started = manager.begin_combo_capture("token", {"1234:5678"}, ready)
        capture_queue, hardware_ids, notify_event = manager.combo_state.capture_queues["token"]
        capture_queue.put({"evdev": "key_a"})

        assert started == {"token": "token", "grabbed_devices": 3}
        assert hardware_ids == {"1234:5678"}
        assert notify_event is ready
        assert manager.read_combo_capture("token") == {"event": {"evdev": "key_a"}}
        assert manager.read_combo_capture("token") == {"event": None}
        assert manager.end_combo_capture("token") == {"status": "ok", "ended": True}
        assert manager.end_combo_capture("token") == {"status": "ok", "ended": False}

    @pytest.mark.asyncio
    async def test_refresh_combo_timeout_watchdog_cancels_or_replaces_existing_task(self) -> None:
        manager = DeviceManager()
        previous = asyncio.create_task(asyncio.sleep(60))
        manager.combo_state.timeout_task = previous
        manager.combo_state.engine.next_deadline = Mock(return_value=None)  # type: ignore[method-assign]

        _runtime_refresh_combo_watchdog(manager)
        await asyncio.sleep(0)

        assert previous.cancelled() is True
        assert manager.combo_state.timeout_task is None

        replacement = asyncio.create_task(asyncio.sleep(60))
        manager.combo_state.timeout_task = replacement
        manager.combo_state.engine.next_deadline = Mock(return_value=42.0)  # type: ignore[method-assign]
        combo_timeout_watchdog = AsyncMock()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(cdm, "combo_timeout_watchdog", combo_timeout_watchdog)

        _runtime_refresh_combo_watchdog(manager)
        await asyncio.sleep(0)

        assert replacement.cancelled() is True
        combo_timeout_watchdog.assert_awaited_once()
        manager.combo_state.timeout_task.cancel()
        await asyncio.sleep(0)
        assert manager.combo_state.timeout_task.done() is True
        monkeypatch.undo()

    @pytest.mark.asyncio
    async def test_combo_timeout_watchdog_expires_and_clears_current_task(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        manager.combo_state.engine.expire_timeouts = Mock()  # type: ignore[method-assign]
        refreshes: list[str] = []
        monkeypatch.setattr(
            cdm,
            "refresh_combo_timeout_watchdog",
            Mock(side_effect=lambda *args, **kwargs: refreshes.append("refresh")),
        )

        monkeypatch.setattr(dm.time, "monotonic", lambda: 10.0)
        monkeypatch.setattr(dm.asyncio, "sleep", AsyncMock())

        task = asyncio.create_task(_runtime_combo_timeout_watchdog(manager, 10.5))
        manager.combo_state.timeout_task = task
        await task

        manager.combo_state.engine.expire_timeouts.assert_called_once_with(10.0)
        assert manager.combo_state.timeout_task is None
        assert refreshes == ["refresh"]


class TestGrabbedDeviceHelpers:
    def test_find_action_for_event_prefers_evdev_code_over_alias_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = _make_grabbed_device(
            monkeypatch,
            button_map={"south": "btn_south"},
            button_codes={"south": evdev.ecodes.BTN_SOUTH},
        )
        mapping = {"south": MappingAction(action_type=ActionType.KEYBOARD, target="key_a")}
        event = evdev.InputEvent(
            0,
            0,
            evdev.ecodes.EV_KEY,
            evdev.ecodes.BTN_SOUTH,
            1,
        )

        assert _runtime_find_grabbed_action_for_event(device, event, mapping) == mapping["south"]

    def test_device_has_mapped_buttons_matches_by_code_when_names_differ(self) -> None:
        caps = {
            evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SOUTH],
        }

        assert _runtime_device_has_mapped_buttons(
            caps,
            {"btn_south"},
            {(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH)},
        )

    def test_device_has_mapped_buttons_ignores_cross_type_code_collision(self) -> None:
        caps = {
            evdev.ecodes.EV_REL: [evdev.ecodes.REL_WHEEL],
        }

        assert not _runtime_device_has_mapped_buttons(
            caps,
            {"key_7"},
            {(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_7)},
        )

    def test_find_grabbed_action_for_event_ignores_cross_type_code_collision(
        self,
        monkeypatch,
    ) -> None:
        device = _make_grabbed_device(
            monkeypatch,
            button_map={
                "extra_13": "key_7",
                "wheel_up": "rel_wheel",
                "wheel_down": "rel_wheel",
            },
            button_codes={
                "extra_13": evdev.ecodes.KEY_7,
                "wheel_up": evdev.ecodes.REL_WHEEL,
                "wheel_down": evdev.ecodes.REL_WHEEL,
            },
            button_values={"wheel_up": 1, "wheel_down": -1},
        )
        mapping = {"extra_13": MappingAction(action_type=ActionType.KEYBOARD, target="key_a")}
        event = evdev.InputEvent(
            0,
            0,
            evdev.ecodes.EV_REL,
            evdev.ecodes.REL_WHEEL,
            -1,
        )

        assert _runtime_find_grabbed_action_for_event(device, event, mapping) is None

    def test_find_grabbed_action_for_event_distinguishes_wheel_direction(self, monkeypatch) -> None:
        device = _make_grabbed_device(
            monkeypatch,
            button_map={
                "wheel_up": "rel_wheel",
                "wheel_down": "rel_wheel",
            },
            button_codes={
                "wheel_up": evdev.ecodes.REL_WHEEL,
                "wheel_down": evdev.ecodes.REL_WHEEL,
            },
            button_values={"wheel_up": 1, "wheel_down": -1},
        )
        mapping = {
            "wheel_down": MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
        }
        down_event = evdev.InputEvent(
            0,
            0,
            evdev.ecodes.EV_REL,
            evdev.ecodes.REL_WHEEL,
            -1,
        )
        up_event = evdev.InputEvent(
            0,
            0,
            evdev.ecodes.EV_REL,
            evdev.ecodes.REL_WHEEL,
            1,
        )

        assert _runtime_find_grabbed_action_for_event(
            device,
            down_event,
            mapping,
        ) == mapping["wheel_down"]
        assert _runtime_find_grabbed_action_for_event(device, up_event, mapping) is None

    def test_bucket_tracking_and_release_all_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        device = _make_grabbed_device(monkeypatch)
        passthrough = _FakeUInput()
        keyboard = _FakeUInput()
        mouse = _FakeUInput()
        gamepad = _FakeUInput()
        canceled = Mock()
        task = SimpleNamespace(done=lambda: False, cancel=canceled)

        device.uinput = passthrough  # type: ignore[assignment]
        device.keyboard_uinput = keyboard  # type: ignore[assignment]
        device.mouse_uinput = mouse  # type: ignore[assignment]
        device.gamepad_uinput = gamepad  # type: ignore[assignment]
        gdo.track_key_state(device, device.uinput, evdev.ecodes.KEY_A, 1)
        gdo.track_key_state(device, device.keyboard_uinput, evdev.ecodes.KEY_B, 1)
        gdo.track_key_state(device, device.mouse_uinput, evdev.ecodes.BTN_LEFT, 1)
        gdo.track_superkey_output(device, "gamepad", evdev.ecodes.BTN_SOUTH, 1)
        device.state.rapidfire_tasks["btn_side"] = task  # type: ignore[assignment]
        device.state.rapidfire_outputs["btn_side"] = gdt.RapidfireOutputState(kind="key")
        device.state.rapidfire_active["btn_side"] = True
        device.state.tap_active["btn_side"] = True
        device.state.combo_passthrough_held.add("btn_side")
        device.state.combo_recalled_bindings.add("btn_side")
        device.state.held_source_actions["btn_side"] = None

        assert gdo.bucket_for_uinput(device, device.keyboard_uinput) == "keyboard"

        gdo.release_all_keys(
            device,
            evdev_mod=evdev,
            uinput_writer=lambda device: cast(gdt.WritableUInput | None, device),
        )

        assert passthrough.writes == [(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0)]
        assert keyboard.writes == [(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0)]
        assert mouse.writes == [(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 0)]
        assert gamepad.writes[-2:] == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RZ, 0),
        ]
        canceled.assert_called_once()
        assert device.state.rapidfire_tasks == {}
        assert device.state.tap_active == {}
        assert device.state.combo_recalled_bindings == set()
        assert device.state.held_source_actions == {}

    @pytest.mark.asyncio
    async def test_wait_for_active_key_activity_handles_timeouts_and_drain_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        device = _make_grabbed_device(monkeypatch)

        class _FakeLoop:
            def add_reader(self, _fd: int, _callback) -> None:
                return

            def remove_reader(self, _fd: int) -> None:
                return

        class _FakeInputDevice:
            def __init__(self) -> None:
                self.calls = 0

            def fileno(self) -> int:
                return 4

            def read_one(self):
                self.calls += 1
                if self.calls == 1:
                    raise OSError(errno.EIO, "drain failed")
                return None

        fake_input = _FakeInputDevice()
        device.device = fake_input  # type: ignore[assignment]

        monkeypatch.setattr(gdm.asyncio, "get_running_loop", lambda: _FakeLoop())

        outcomes = iter([TimeoutError(), None])

        async def fake_wait_for(awaitable, _timeout: float):
            close = getattr(awaitable, "close", None)
            if close is not None:
                close()
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(gdm.asyncio, "wait_for", fake_wait_for)

        assert await _runtime_wait_for_grabbed_active_key_activity(device, 0.1) is False

        with caplog.at_level(logging.WARNING, logger="keyforged.devices"):
            assert await _runtime_wait_for_grabbed_active_key_activity(device, 0.1) is True

        assert "failed to drain pending events before grab" in caplog.text

    @pytest.mark.asyncio
    async def test_broadcast_grab_status_and_startup_held_actions(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        callback = AsyncMock(side_effect=RuntimeError("boom"))
        keyboard = _FakeUInput()
        mouse = _FakeUInput()
        gamepad = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            button_map={"left": "key_a", "right": "key_b"},
            broadcast_callback=callback,
            keyboard_uinput=keyboard,  # type: ignore[arg-type]
            mouse_uinput=mouse,  # type: ignore[arg-type]
            gamepad_uinput=gamepad,  # type: ignore[arg-type]
        )

        class _FakeInputDevice:
            def active_keys(self) -> list[int]:
                return [evdev.ecodes.KEY_A, evdev.ecodes.KEY_B]

        device.device = _FakeInputDevice()  # type: ignore[assignment]
        mapping_state = {
            "left": dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_z"),
            "right": dm.MappingAction(action_type=ActionType.MOUSE, target="btn_left"),
        }
        device.mapping_getter = lambda: mapping_state

        with caplog.at_level(logging.WARNING, logger="keyforged.devices"):
            await gdg.broadcast_grab_status(
                device,
                "waiting",
                ["key_a"],
                waited_s=1.5,
                command_type=CommandType,
                log=gdm.log,
            )

        gdg.seed_startup_held_actions(device)

        callback.assert_awaited_once()
        assert "Failed to broadcast grab status" in caplog.text
        assert device.state.held_source_actions["key_a"] == mapping_state["left"]
        assert device.state.held_source_actions["key_b"] == mapping_state["right"]
        assert keyboard.writes == [(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_Z, 0)]
        assert mouse.writes == [(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 0)]

    def test_seed_startup_held_actions_matches_gamepad_alias_by_code(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = _make_grabbed_device(
            monkeypatch,
            button_map={"south": "btn_south"},
            button_codes={"south": evdev.ecodes.BTN_SOUTH},
        )

        class _FakeInputDevice:
            def active_keys(self) -> list[int]:
                return [evdev.ecodes.BTN_SOUTH]

        device.device = _FakeInputDevice()  # type: ignore[assignment]
        mapping_state = {
            "south": dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_z"),
        }
        device.mapping_getter = lambda: mapping_state

        gdg.seed_startup_held_actions(device)

        assert device.state.held_source_actions["btn_a"] == mapping_state["south"]

    def test_reconcile_startup_held_action_releases_gamepad_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = _make_grabbed_device(monkeypatch)
        gamepad = _FakeUInput()
        device.gamepad_uinput = gamepad  # type: ignore[assignment]

        gdg.reconcile_startup_held_action(
            device,
            dm.MappingAction(action_type=ActionType.GAMEPAD, target="btn_lt")
            ,
            action_type_enum=ActionType,
        )
        gdg.reconcile_startup_held_action(
            device,
            dm.MappingAction(action_type=ActionType.GAMEPAD, target="btn_south")
            ,
            action_type_enum=ActionType,
        )

        assert gamepad.writes == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 0),
        ]

    @pytest.mark.asyncio
    async def test_tap_helpers_and_emit_combo_release_cover_cleanup_paths(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = _make_grabbed_device(monkeypatch)
        passthrough = _FakeUInput()
        gamepad = _FakeUInput()
        device.uinput = passthrough  # type: ignore[assignment]
        device.gamepad_uinput = gamepad  # type: ignore[assignment]

        monkeypatch.setattr(gdm.asyncio, "sleep", AsyncMock())

        await _runtime_tap_grabbed_key(
            device,
            evdev.ecodes.KEY_A,
            25,
            "tap",
            device.keyboard_uinput,  # type: ignore[arg-type]
        )
        device.state.tap_active["trigger"] = True
        await _runtime_tap_grabbed_trigger(device, evdev.ecodes.ABS_Z, 25, "trigger")
        move_action = dm.MappingAction(
            action_type=ActionType.MOUSE_MOVE_REL,
            move_x=4,
            move_y=-3,
        )
        device.state.tap_active["move"] = True
        await _runtime_tap_grabbed_move(device, move_action, "move", 25)
        device.emit_combo_release("key_b")
        device.emit_combo_release("missing")

        assert device.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]
        assert gamepad.writes == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 255),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0),
        ]
        assert passthrough.writes == [(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0)]
        assert device.state.tap_active == {}

    @pytest.mark.asyncio
    async def test_execute_action_covers_synthetic_non_keyboard_branches(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        callback = AsyncMock()
        macro_player = AsyncMock(return_value={"status": "ok"})
        mouse = _FakeUInput()
        gamepad = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            broadcast_callback=callback,
            macro_player=macro_player,
            mouse_uinput=mouse,  # type: ignore[arg-type]
            gamepad_uinput=gamepad,  # type: ignore[arg-type]
        )
        passthrough_calls: list[tuple[int, int, int]] = []
        emitted_moves: list[tuple[ActionType, int, int]] = []
        fire_tasks: list[asyncio.Task] = []
        monkeypatch.setattr(
            gda,
            "emit_configured_mouse_move",
            lambda _device, action: emitted_moves.append(
                (action.action_type, action.move_x, action.move_y)
            ),
        )
        monkeypatch.setattr(
            gda,
            "passthrough",
            lambda _device, event, **_kwargs: passthrough_calls.append(
                (event.type, event.code, event.value)
            ),
        )
        monkeypatch.setattr(
            gde,
            "_fire_and_observe",
            lambda coro, _label: fire_tasks.append(asyncio.create_task(coro)) or fire_tasks[-1],
        )

        press = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=1, value=1)
        release = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=1, value=0)

        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.PASSTHROUGH),
            press,
            "btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.MOUSE, target="btn_left"),
            press,
            "mouse_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.MOUSE, target="btn_left"),
            release,
            "mouse_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.GAMEPAD, target="btn_lt"),
            press,
            "trigger_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.GAMEPAD, target="btn_lt"),
            release,
            "trigger_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.GAMEPAD, target="btn_south"),
            press,
            "pad_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.GAMEPAD, target="btn_south"),
            release,
            "pad_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.EXEC, exec_ref=7),
            press,
            "exec_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(
                action_type=ActionType.COMPOSITOR_DISPATCH,
                compositor_dispatcher="workspace",
                compositor_args="2",
            ),
            press,
            "dispatch_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.START_MACRO_RECORDING),
            press,
            "start_rec",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.STOP_MACRO_RECORDING),
            press,
            "stop_rec",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.CANCEL_MACRO_PLAYBACK),
            press,
            "cancel_macro",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.PROFILE_TOGGLE, profile_name="Gaming"),
            press,
            "toggle_profile",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.MACRO, macro_name="demo"),
            press,
            "macro_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.MACRO, macro_name="demo"),
            release,
            "macro_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.MOUSE_MOVE_REL, move_x=5, move_y=-2),
            press,
            "move_rel",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.MOUSE_MOVE_ABS, move_x=10, move_y=20),
            press,
            "move_abs",
        )

        if fire_tasks:
            await asyncio.gather(*fire_tasks)

        assert passthrough_calls == [(press.type, press.code, press.value)]
        assert mouse.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 0),
        ]
        assert gamepad.writes == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 255),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 0),
        ]
        assert emitted_moves == [
            (ActionType.MOUSE_MOVE_REL, 5, -2),
            (ActionType.MOUSE_MOVE_ABS, 10, 20),
        ]
        assert callback.await_count == 6
        assert macro_player.await_count == 2
        assert macro_player.await_args_list[0].kwargs["trigger_value"] == 1
        assert macro_player.await_args_list[1].kwargs["trigger_value"] == 0

    @pytest.mark.asyncio
    async def test_execute_action_covers_superkey_and_tap_move_branches(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = _make_grabbed_device(monkeypatch)
        move_calls: list[tuple[str, int]] = []
        fake_machine = SimpleNamespace(on_down=AsyncMock(), on_up=AsyncMock())
        created_configs: list[SuperkeyConfig] = []

        monkeypatch.setattr(
            gda,
            "SuperkeyMachine",
            lambda **kwargs: created_configs.append(kwargs["config"]) or fake_machine,
        )
        monkeypatch.setattr(
            gde,
            "_fire_and_observe",
            lambda coro, _label: asyncio.create_task(coro),
        )
        monkeypatch.setattr(
            gda,
            "tap_move",
            AsyncMock(
                side_effect=lambda _device, action, event_name, hold_ms, **_kwargs: (
                    move_calls.append((event_name, hold_ms))
                )
            ),
        )

        superkey_action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="super",
                tap_actions=[SuperkeyActionData(action_type="exec", exec_ref=4)],
            ),
        )
        tap_move_action = dm.MappingAction(
            action_type=ActionType.MOUSE_MOVE_REL,
            move_x=1,
            move_y=2,
            tap_enabled=True,
            tap_hold_ms=33,
        )

        await _runtime_execute_grabbed_action(
            device, superkey_action, SimpleNamespace(value=1), "super_btn"
        )
        await _runtime_execute_grabbed_action(
            device, superkey_action, SimpleNamespace(value=0), "super_btn"
        )
        await _runtime_execute_grabbed_action(
            device, tap_move_action, SimpleNamespace(value=1), "move_btn"
        )
        await asyncio.sleep(0)

        assert created_configs and created_configs[0].name == "super"
        fake_machine.on_down.assert_awaited_once()
        fake_machine.on_up.assert_awaited_once()
        assert move_calls == [("move_btn", 33)]


class TestComboActionDispatch:
    @pytest.mark.asyncio
    async def test_combo_overload_superkey_starts_children_and_releases_in_reverse_order(
        self,
    ) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()
        binding = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-overload",
                mode=SuperkeyMode.OVERLOAD,
                overload_actions=[
                    dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
                    dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_b"),
                ],
            ),
        )

        await _runtime_start_combo_action(manager, "combo-overload", action, binding)
        await _runtime_stop_combo_action(manager, "combo-overload")

        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]

    @pytest.mark.asyncio
    async def test_combo_overload_superkey_logs_nested_superkey_children(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        manager = DeviceManager()
        binding = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-overload",
                mode=SuperkeyMode.OVERLOAD,
                overload_actions=[
                    dm.MappingAction(action_type=ActionType.SUPERKEY, superkey_name="nested"),
                ],
            ),
        )

        with caplog.at_level("WARNING", logger="keyforged.runtime.combos"):
            await _runtime_start_combo_action(manager, "combo-overload", action, binding)

        assert (
            "Skipping nested superkey child nested in combo overload combo-overload "
            "(combo-overload)" in caplog.text
        )

    @pytest.mark.asyncio
    async def test_combo_overload_restore_runs_after_child_release_for_overlapping_key(
        self,
    ) -> None:
        class FakeComboDevice:
            def __init__(self) -> None:
                self.hardware_id = "1234:5678"
                self.interface_id = "kbd"
                self.active = {"key_leftmeta", "key_c"}
                self.held = {"key_leftmeta", "key_c"}
                self.recalled: set[str] = set()
                self.releases: list[str] = []
                self.presses: list[str] = []

            def emit_combo_release(self, evdev_name: str) -> None:
                self.releases.append(evdev_name)
                self.active.discard(evdev_name)

            def emit_combo_press(self, evdev_name: str) -> None:
                self.presses.append(evdev_name)
                self.active.add(evdev_name)

            def combo_passthrough_binding_active(self, evdev_name: str) -> bool:
                return evdev_name in self.active

            def combo_source_binding_held(self, evdev_name: str) -> bool:
                return evdev_name in self.held

            def mark_combo_recalled_binding(self, evdev_name: str) -> None:
                self.recalled.add(evdev_name)

            def clear_combo_recalled_binding(self, evdev_name: str) -> None:
                self.recalled.discard(evdev_name)

            def combo_passthrough_held_modifiers(self) -> set[str]:
                return set()

        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()
        fake_device = FakeComboDevice()
        manager.grabbed_devices = {"1234:5678": [fake_device]}
        trigger_meta = dm.RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_leftmeta",
        )
        trigger_c = dm.RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_c",
        )
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-overload-overlap",
                mode=SuperkeyMode.OVERLOAD,
                overload_actions=[
                    dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_leftmeta"),
                ],
            ),
        )
        manager.active_combos = [
            dm.RuntimeCombo(
                id="combo-overload-overlap",
                name="combo-overload-overlap",
                steps=[dm.RuntimeComboStep(bindings=(trigger_meta, trigger_c))],
                action=action,
                recall_trigger_keys=True,
                restore_trigger_keys=["meta"],
            )
        ]

        await _runtime_start_combo_action(
            manager,
            "combo-overload-overlap",
            action,
            trigger_c,
            trigger_bindings=(trigger_meta, trigger_c),
        )

        fake_device.held = {"key_leftmeta"}
        await _runtime_stop_combo_action(manager, "combo-overload-overlap")

        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTMETA, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTMETA, 0),
        ]
        assert fake_device.releases == ["key_c", "key_leftmeta"]
        assert fake_device.presses == ["key_leftmeta"]
        assert fake_device.active == {"key_leftmeta"}
        assert fake_device.recalled == {"key_c"}

    @pytest.mark.asyncio
    async def test_combo_recall_trigger_keys_restores_selected_keys_on_immediate_action_completion(
        self,
    ) -> None:
        class FakeComboDevice:
            def __init__(self) -> None:
                self.hardware_id = "1234:5678"
                self.interface_id = "kbd"
                self.active = {"key_leftmeta", "key_c"}
                self.held = {"key_leftmeta", "key_c"}
                self.recalled: set[str] = set()
                self.releases: list[str] = []
                self.presses: list[str] = []

            def emit_combo_release(self, evdev_name: str) -> None:
                self.releases.append(evdev_name)
                self.active.discard(evdev_name)

            def emit_combo_press(self, evdev_name: str) -> None:
                self.presses.append(evdev_name)
                self.active.add(evdev_name)

            def combo_passthrough_binding_active(self, evdev_name: str) -> bool:
                return evdev_name in self.active

            def combo_source_binding_held(self, evdev_name: str) -> bool:
                return evdev_name in self.held

            def mark_combo_recalled_binding(self, evdev_name: str) -> None:
                self.recalled.add(evdev_name)

            def clear_combo_recalled_binding(self, evdev_name: str) -> None:
                self.recalled.discard(evdev_name)

            def combo_passthrough_held_modifiers(self) -> set[str]:
                return set()

        manager = DeviceManager()
        fake_device = FakeComboDevice()
        manager.grabbed_devices = {"1234:5678": [fake_device]}
        trigger_meta = dm.RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_leftmeta",
        )
        trigger_c = dm.RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_c",
        )
        action = dm.MappingAction(action_type=ActionType.SUPPRESS)
        manager.active_combos = [
            dm.RuntimeCombo(
                id="combo-recall-immediate",
                name="combo-recall-immediate",
                steps=[dm.RuntimeComboStep(bindings=(trigger_meta, trigger_c))],
                action=action,
                recall_trigger_keys=True,
                restore_trigger_keys=["meta"],
            )
        ]

        await _runtime_start_combo_action(
            manager,
            "combo-recall-immediate",
            action,
            trigger_c,
            trigger_bindings=(trigger_meta, trigger_c),
        )

        assert fake_device.releases == ["key_c", "key_leftmeta"]
        assert fake_device.presses == ["key_leftmeta"]
        assert fake_device.active == {"key_leftmeta"}
        assert fake_device.recalled == {"key_c"}

    @pytest.mark.asyncio
    async def test_combo_recall_trigger_keys_restores_selected_keys_after_action_stop(
        self,
    ) -> None:
        class FakeComboDevice:
            def __init__(self) -> None:
                self.hardware_id = "1234:5678"
                self.interface_id = "kbd"
                self.active = {"key_leftmeta", "key_c"}
                self.held = {"key_leftmeta", "key_c"}
                self.recalled: set[str] = set()
                self.releases: list[str] = []
                self.presses: list[str] = []

            def emit_combo_release(self, evdev_name: str) -> None:
                self.releases.append(evdev_name)
                self.active.discard(evdev_name)

            def emit_combo_press(self, evdev_name: str) -> None:
                self.presses.append(evdev_name)
                self.active.add(evdev_name)

            def combo_passthrough_binding_active(self, evdev_name: str) -> bool:
                return evdev_name in self.active

            def combo_source_binding_held(self, evdev_name: str) -> bool:
                return evdev_name in self.held

            def mark_combo_recalled_binding(self, evdev_name: str) -> None:
                self.recalled.add(evdev_name)

            def clear_combo_recalled_binding(self, evdev_name: str) -> None:
                self.recalled.discard(evdev_name)

            def combo_passthrough_held_modifiers(self) -> set[str]:
                return set()

        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()
        fake_device = FakeComboDevice()
        manager.grabbed_devices = {"1234:5678": [fake_device]}
        trigger_meta = dm.RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_leftmeta",
        )
        trigger_c = dm.RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_c",
        )
        action = dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_f5")
        manager.active_combos = [
            dm.RuntimeCombo(
                id="combo-recall-hold",
                name="combo-recall-hold",
                steps=[dm.RuntimeComboStep(bindings=(trigger_meta, trigger_c))],
                action=action,
                recall_trigger_keys=True,
                restore_trigger_keys=["meta"],
            )
        ]

        await _runtime_start_combo_action(
            manager,
            "combo-recall-hold",
            action,
            trigger_c,
            trigger_bindings=(trigger_meta, trigger_c),
        )

        assert fake_device.releases == ["key_c", "key_leftmeta"]
        assert fake_device.presses == []
        assert fake_device.recalled == {"key_c", "key_leftmeta"}
        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F5, 1)
        ]

        fake_device.held = {"key_leftmeta"}
        await _runtime_stop_combo_action(manager, "combo-recall-hold")

        assert fake_device.presses == ["key_leftmeta"]
        assert fake_device.recalled == {"key_c"}
        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F5, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F5, 0),
        ]

    @pytest.mark.asyncio
    async def test_combo_pattern_superkey_single_step_supports_double_tap(self) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()
        binding = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern",
                mode=SuperkeyMode.PATTERN,
                tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
                double_tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_b")],
            ),
        )
        manager.active_combos = [
            dm.RuntimeCombo(
                id="combo-pattern",
                name="combo-pattern",
                steps=[dm.RuntimeComboStep(bindings=(binding,))],
                action=action,
            )
        ]

        await _runtime_start_combo_action(manager, "combo-pattern", action, binding)
        await _runtime_stop_combo_action(manager, "combo-pattern")
        await _runtime_start_combo_action(manager, "combo-pattern", action, binding)
        await _runtime_stop_combo_action(manager, "combo-pattern")
        await asyncio.sleep(0.02)

        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
        ]

    @pytest.mark.asyncio
    async def test_combo_pattern_superkey_single_step_supports_tap_hold(self) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()
        binding = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern-tap-hold",
                mode=SuperkeyMode.PATTERN,
                hold_threshold_ms=0,
                tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
                double_tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_c")],
                tap_hold_actions=[SuperkeyActionData(action_type="keyboard", target="key_b")],
            ),
        )
        manager.active_combos = [
            dm.RuntimeCombo(
                id="combo-pattern-tap-hold",
                name="combo-pattern-tap-hold",
                steps=[dm.RuntimeComboStep(bindings=(binding,))],
                action=action,
            )
        ]

        await _runtime_start_combo_action(manager, "combo-pattern-tap-hold", action, binding)
        await _runtime_stop_combo_action(manager, "combo-pattern-tap-hold")
        await _runtime_start_combo_action(manager, "combo-pattern-tap-hold", action, binding)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await _runtime_stop_combo_action(manager, "combo-pattern-tap-hold")
        await asyncio.sleep(0.02)

        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
        ]

    @pytest.mark.asyncio
    async def test_combo_pattern_superkey_multistep_ignores_double_tap_slots(self) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()
        first = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        second = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_b")
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern-multi",
                mode=SuperkeyMode.PATTERN,
                tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_c")],
                double_tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_d")],
            ),
        )
        manager.active_combos = [
            dm.RuntimeCombo(
                id="combo-pattern-multi",
                name="combo-pattern-multi",
                steps=[
                    dm.RuntimeComboStep(bindings=(first,)),
                    dm.RuntimeComboStep(bindings=(second,)),
                ],
                action=action,
            )
        ]

        await _runtime_start_combo_action(manager, "combo-pattern-multi", action, second)
        await _runtime_stop_combo_action(manager, "combo-pattern-multi")
        await asyncio.sleep(0.02)

        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_C, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_C, 0),
        ]

    @pytest.mark.asyncio
    async def test_combo_pattern_superkey_multistep_supports_hold_only(self) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()
        first = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        second = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_b")
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern-hold",
                mode=SuperkeyMode.PATTERN,
                hold_threshold_ms=0,
                hold_actions=[SuperkeyActionData(action_type="keyboard", target="key_e")],
                tap_hold_actions=[SuperkeyActionData(action_type="keyboard", target="key_f")],
            ),
        )
        manager.active_combos = [
            dm.RuntimeCombo(
                id="combo-pattern-hold",
                name="combo-pattern-hold",
                steps=[
                    dm.RuntimeComboStep(bindings=(first,)),
                    dm.RuntimeComboStep(bindings=(second,)),
                ],
                action=action,
            )
        ]

        await _runtime_start_combo_action(manager, "combo-pattern-hold", action, second)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await _runtime_stop_combo_action(manager, "combo-pattern-hold")
        await asyncio.sleep(0.02)

        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_E, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_E, 0),
        ]

    @pytest.mark.asyncio
    async def test_clear_combo_runtime_releases_active_pattern_superkey_hold(self) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()
        binding = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern-clear",
                mode=SuperkeyMode.PATTERN,
                hold_threshold_ms=0,
                hold_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
            ),
        )
        manager.active_combos = [
            dm.RuntimeCombo(
                id="combo-pattern-clear",
                name="combo-pattern-clear",
                steps=[dm.RuntimeComboStep(bindings=(binding,))],
                action=action,
            )
        ]

        await _runtime_start_combo_action(manager, "combo-pattern-clear", action, binding)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await _runtime_clear_combo_runtime(manager)

        assert manager.combo_state.superkey_machines == {}
        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]

    @pytest.mark.asyncio
    async def test_clear_combo_scope_stops_pending_pattern_superkey_machine(self) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()
        binding = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern-scope",
                mode=SuperkeyMode.PATTERN,
                tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
                double_tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_b")],
            ),
        )
        manager.active_combos = [
            dm.RuntimeCombo(
                id="combo-pattern-scope",
                name="combo-pattern-scope",
                steps=[dm.RuntimeComboStep(bindings=(binding,))],
                action=action,
            )
        ]

        await _runtime_start_combo_action(manager, "combo-pattern-scope", action, binding)
        await _runtime_stop_combo_action(manager, "combo-pattern-scope")

        assert "combo-pattern-scope" in manager.combo_state.superkey_machines

        await _runtime_clear_combo_scope(manager, "1234:5678", "kbd")

        assert manager.combo_state.superkey_machines == {}

    @pytest.mark.asyncio
    async def test_combo_pattern_superkey_replaces_stale_cached_machine_when_config_changes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        binding = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        old_config = SuperkeyConfig(
            name="combo-pattern-stale",
            mode=SuperkeyMode.PATTERN,
            tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
        )
        new_config = SuperkeyConfig(
            name="combo-pattern-stale",
            mode=SuperkeyMode.PATTERN,
            tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_b")],
        )
        old_machine = SimpleNamespace(
            config=old_config,
            event_name="combo:combo-pattern-stale",
            stop=AsyncMock(),
        )
        created: list[object] = []

        class _FakeMachine:
            def __init__(self, **kwargs) -> None:
                self.config = kwargs["config"]
                self.event_name = kwargs["event_name"]
                self.stop = AsyncMock()
                self.on_down = AsyncMock()
                self.on_up = AsyncMock()
                created.append(self)

        monkeypatch.setattr(cdm, "SuperkeyMachine", _FakeMachine)
        manager.combo_state.superkey_machines["combo-pattern-stale"] = old_machine  # type: ignore[assignment]
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=new_config,
        )
        manager.active_combos = [
            dm.RuntimeCombo(
                id="combo-pattern-stale",
                name="combo-pattern-stale",
                steps=[dm.RuntimeComboStep(bindings=(binding,))],
                action=action,
            )
        ]

        await _runtime_start_combo_action(manager, "combo-pattern-stale", action, binding)

        old_machine.stop.assert_awaited_once()
        assert len(created) == 1
        assert manager.combo_state.superkey_machines["combo-pattern-stale"] is created[0]

    @pytest.mark.asyncio
    async def test_start_and_stop_combo_action_cover_additional_synthetic_paths(self) -> None:
        manager = DeviceManager()
        manager.output_state.mouse_uinput = _FakeUInput()
        manager.output_state.gamepad_uinput = _FakeUInput()
        manager.play_macro = AsyncMock(return_value={"status": "ok"})  # type: ignore[method-assign]
        broadcast_combo_action = AsyncMock()
        emit_combo_mouse_move = Mock()
        resolve_code = Mock(return_value=evdev.ecodes.BTN_SOUTH)
        combo_tap_trigger = AsyncMock()
        combo_rapidfire_trigger = AsyncMock()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(cdm, "broadcast_combo_action", broadcast_combo_action)
        monkeypatch.setattr(cdm, "emit_combo_mouse_move", emit_combo_mouse_move)
        monkeypatch.setattr(cdm, "combo_tap_trigger", combo_tap_trigger)
        monkeypatch.setattr(cdm, "combo_rapidfire_trigger", combo_rapidfire_trigger)

        binding = dm.RuntimeComboBinding(hardware_id="1234:5678", source="mouse", evdev="btn_side")

        await _runtime_start_combo_action(
            manager,
            "mouse-move",
            dm.MappingAction(action_type=ActionType.MOUSE_MOVE_REL, move_x=3, move_y=-1),
            binding,
        )
        await _runtime_start_combo_action(
            manager,
            "macro",
            dm.MappingAction(
                action_type=ActionType.MACRO,
                macro_name="demo",
                macro_loop_mode="hold",
            ),
            binding,
        )
        await _runtime_start_combo_action(
            manager,
            "exec",
            dm.MappingAction(action_type=ActionType.EXEC, exec_ref=9),
            binding,
        )
        await _runtime_start_combo_action(
            manager,
            "dispatch",
            dm.MappingAction(
                action_type=ActionType.COMPOSITOR_DISPATCH,
                compositor_dispatcher="workspace",
                compositor_args="2",
            ),
            binding,
        )
        await _runtime_start_combo_action(
            manager,
            "record",
            dm.MappingAction(action_type=ActionType.START_MACRO_RECORDING),
            binding,
        )
        await _runtime_start_combo_action(
            manager,
            "profile",
            dm.MappingAction(action_type=ActionType.PROFILE_ENABLE, profile_name="Gaming"),
            binding,
        )
        await _runtime_start_combo_action(
            manager,
            "trigger",
            dm.MappingAction(action_type=ActionType.GAMEPAD, target="btn_lt"),
            binding,
            resolve_code_fn=resolve_code,
        )
        await _runtime_stop_combo_action(manager, "trigger")
        await _runtime_start_combo_action(
            manager,
            "tap-trigger",
            dm.MappingAction(
                action_type=ActionType.GAMEPAD,
                target="btn_lt",
                tap_enabled=True,
                tap_hold_ms=25,
            ),
            binding,
            resolve_code_fn=resolve_code,
        )
        await _runtime_stop_combo_action(manager, "tap-trigger")
        await _runtime_start_combo_action(
            manager,
            "rapid-trigger",
            dm.MappingAction(
                action_type=ActionType.GAMEPAD,
                target="btn_lt",
                rapidfire_enabled=True,
                rapidfire_hold_ms=10,
                rapidfire_wait_ms=10,
            ),
            binding,
            resolve_code_fn=resolve_code,
        )
        await _runtime_stop_combo_action(manager, "rapid-trigger")
        await _runtime_stop_combo_action(manager, "macro")

        emit_combo_mouse_move.assert_called_once()
        assert manager.play_macro.await_count == 2
        assert manager.play_macro.await_args_list[0].kwargs["trigger_value"] == 1
        assert manager.play_macro.await_args_list[1].kwargs["trigger_value"] == 0
        assert broadcast_combo_action.await_count == 4
        assert manager.output_state.gamepad_uinput.writes == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 255),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0),
        ]
        monkeypatch.undo()
