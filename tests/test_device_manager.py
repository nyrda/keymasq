import asyncio
import errno
import logging
import os
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import evdev
import pytest

from keyforge.common.ipc import CommandType
from keyforge.common.models import ActionType, DeviceType, MappingAction
from keyforge.keyforged import device_manager as dm
from keyforge.keyforged.combo_engine import ComboDecision
from keyforge.keyforged.device_manager import DesiredGrabConfig, DeviceManager, GrabbedDevice


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
        manager._create_global_uinputs = Mock()
        manager._destroy_global_uinputs = Mock()

        def _missing_input_device(_path: str):
            raise FileNotFoundError(errno.ENOENT, "missing")

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm.evdev, "InputDevice", _missing_input_device)

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
        assert manager._desired_paths["1234:5678"] == {"/dev/input/event404"}
        assert manager._desired_grabs["1234:5678"] == DesiredGrabConfig(
            paths={"/dev/input/event404"},
            button_map={"btn_side": "btn_side"},
            force_grab_unmapped=False,
        )
        manager._create_global_uinputs.assert_not_called()

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
        manager._diagnostics_enabled = True
        manager._diag_samples = {"event": deque([1.0, 3.0])}
        snapshots: list[dict[str, list[float]]] = []
        calls: list[tuple[object, tuple[object, ...]]] = []

        async def fake_sleep(_delay: float) -> None:
            manager._diagnostics_enabled = False

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
        manager._live_topology_snapshot = dict(snapshot)
        manager._reconciled_topology_snapshot = {}
        manager._schedule_topology_reconcile = Mock()  # type: ignore[assignment]
        sleep_calls = 0

        async def fake_sleep(_delay: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                raise asyncio.CancelledError()

        async def fake_to_thread(func, /, *args, **kwargs):
            assert kwargs == {}
            return snapshot

        monkeypatch.setattr(dm.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(dm.asyncio, "to_thread", fake_to_thread)

        with pytest.raises(asyncio.CancelledError):
            await manager._topology_watch_loop()

        manager._schedule_topology_reconcile.assert_called_once_with(snapshot)

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
        manager._schedule_topology_reconcile = Mock()  # type: ignore[assignment]
        sleep_calls = 0
        scan_calls = 0

        async def fake_sleep(_delay: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 3:
                raise asyncio.CancelledError()

        async def fake_to_thread(func, /, *args, **kwargs):
            nonlocal scan_calls
            assert kwargs == {}
            scan_calls += 1
            if scan_calls == 1:
                raise RuntimeError("scan boom")
            return snapshot

        monkeypatch.setattr(dm.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(dm.asyncio, "to_thread", fake_to_thread)

        with caplog.at_level(logging.WARNING, logger="keyforged.devices"):
            with pytest.raises(asyncio.CancelledError):
                await manager._topology_watch_loop()

        assert "Topology scan failed: scan boom" in caplog.text
        manager._schedule_topology_reconcile.assert_called_once_with(snapshot)


class TestMacroControlActions:
    @pytest.mark.asyncio
    async def test_run_macro_control_action_wait_fixed_uses_speed(self, monkeypatch):
        manager = DeviceManager()

        sleep_calls: list[float] = []

        async def fake_sleep(duration: float) -> None:
            sleep_calls.append(duration)

        monkeypatch.setattr(dm.asyncio, "sleep", fake_sleep)

        await manager._run_macro_control_action(
            {"macro_action": "wait_fixed", "duration_ms": 20},
            speed=2.0,
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

        await manager._run_macro_control_action(
            {"macro_action": "wait_random", "min_ms": 10, "max_ms": 80},
            speed=10.0,
        )

        assert sleep_calls == [0.005]

    @pytest.mark.asyncio
    async def test_run_macro_control_action_exec_async_broadcasts(self):
        manager = DeviceManager()

        callback = AsyncMock()

        async def cb(command, data):
            await callback(command, data)

        manager.broadcast_callback = cb

        await manager._run_macro_control_action(
            {
                "macro_action": "exec_async",
                "command": "echo hi",
            },
            speed=1.0,
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
        manager.begin_mouse_rel_suppression = Mock()
        manager.end_mouse_rel_suppression = Mock()

        async def fake_sleep(duration: float) -> None:
            return None

        async def fake_wait_for(awaitable, timeout):
            raise TimeoutError

        monkeypatch.setattr(dm.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(dm.asyncio, "wait_for", fake_wait_for)

        await manager._run_macro_control_action(
            {
                "macro_action": "exec_sync",
                "command": "echo hi",
                "inhibit_mouse": True,
                "timeout_ms": 100,
            },
            speed=1.0,
        )

        assert manager.begin_mouse_rel_suppression.called is True
        assert manager.end_mouse_rel_suppression.called is True
        assert manager._macro_exec_waiters == {}
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

        async def release_device(_hardware_id: str) -> None:
            await fake_device.release()

        manager._release_device_unlocked = release_device

        manager._schedule_hardware_release_unlocked("hw", 0.001)
        task = manager._pending_hardware_release["hw"]
        await task

        assert fake_device.release.await_count == 1
        assert holds["count"] >= 2


class _FakeUInput:
    def __init__(self, *args, **kwargs) -> None:
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
    monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
    monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")
    button_map = kwargs.pop("button_map", {})
    button_codes = kwargs.pop("button_codes", None)
    keyboard_uinput = kwargs.pop("keyboard_uinput", _FakeUInput())
    mouse_uinput = kwargs.pop("mouse_uinput", _FakeUInput())
    gamepad_uinput = kwargs.pop("gamepad_uinput", _FakeUInput())
    return GrabbedDevice(
        path="/dev/input/event-test",
        hardware_id="1234:5678",
        button_map=button_map,
        button_codes=button_codes,
        mapping_getter=lambda: {},
        event_callback=AsyncMock(return_value=None),
        keyboard_uinput=keyboard_uinput,  # type: ignore[arg-type]
        mouse_uinput=mouse_uinput,  # type: ignore[arg-type]
        gamepad_uinput=gamepad_uinput,  # type: ignore[arg-type]
        **kwargs,
    )


class TestRapidfireRelease:
    @pytest.mark.asyncio
    async def test_grab_waits_until_active_keys_clear_before_grabbing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")
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

        monkeypatch.setattr(dm.evdev, "InputDevice", lambda _path: fake_input)
        monkeypatch.setattr(
            dm.evdev,
            "UInput",
            lambda *args, **kwargs: call_order.append("uinput") or _FakeUInput(*args, **kwargs),
        )
        monkeypatch.setattr(dm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(dm.asyncio, "create_task", fake_create_task)
        monkeypatch.setattr(
            GrabbedDevice,
            "_wait_for_active_key_activity",
            lambda _self, timeout_s: fake_wait_for_active_key_activity(timeout_s),
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

        assert wait_timeouts == [dm.ACTIVE_KEY_IDLE_LOG_INTERVAL_S]
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
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

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

        monkeypatch.setattr(dm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(dm.time, "monotonic", fake_monotonic)
        monkeypatch.setattr(
            GrabbedDevice,
            "_wait_for_active_key_activity",
            lambda _self, timeout_s: fake_wait_for_active_key_activity(timeout_s),
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
            await device._wait_for_active_keys_to_clear()

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
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

        class _FakeInputDevice:
            def active_keys(self) -> list[int]:
                raise RuntimeError("broken active_keys")

        to_thread_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

        async def fake_to_thread(func, /, *args, **kwargs):
            to_thread_calls.append((func, args, kwargs))
            return func(*args, **kwargs)

        monkeypatch.setattr(dm.asyncio, "to_thread", fake_to_thread)

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
            await device._wait_for_active_keys_to_clear()

        assert [call[0] for call in to_thread_calls] == [device.device.active_keys]
        assert "failed to read active keys before grab: broken active_keys" in caplog.text
        assert "proceeding with grab" in caplog.text

    @pytest.mark.asyncio
    async def test_wait_for_active_keys_times_out_with_clear_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")
        monkeypatch.setattr(dm, "ACTIVE_KEY_IDLE_MAX_WAIT_S", 60.0)

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

        monkeypatch.setattr(dm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(dm.time, "monotonic", fake_monotonic)
        monkeypatch.setattr(
            GrabbedDevice,
            "_wait_for_active_key_activity",
            lambda _self, timeout_s: fake_wait_for_active_key_activity(timeout_s),
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
                await device._wait_for_active_keys_to_clear()

        assert wait_timeouts == []
        assert "timed out waiting 60.0s for active keys to clear before grab" in caplog.text

    @pytest.mark.asyncio
    async def test_grab_closes_precreated_uinput_when_wait_times_out(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")
        monkeypatch.setattr(dm, "ACTIVE_KEY_IDLE_MAX_WAIT_S", 60.0)

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

        monkeypatch.setattr(dm.evdev, "InputDevice", lambda _path: _FakeInputDevice())
        monkeypatch.setattr(dm.evdev, "UInput", fake_uinput)
        monkeypatch.setattr(dm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(dm.time, "monotonic", fake_monotonic)

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
    async def test_rapidfire_key_releases_before_exiting_when_stopped_during_hold(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

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
        device._rapidfire_active["btn_side"] = True

        async def fake_sleep(_delay: float) -> None:
            device._rapidfire_active["btn_side"] = False

        monkeypatch.setattr(dm.asyncio, "sleep", fake_sleep)

        await device._rapidfire_key(
            evdev.ecodes.KEY_A,
            50,
            50,
            "btn_side",
            fake_uinput,  # type: ignore[arg-type]
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
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

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
        original_stop = device._stop_rapidfire

        def wrapped_stop(event_name: str) -> None:
            calls.append(f"stop:{event_name}")
            original_stop(event_name)

        monkeypatch.setattr(device, "_stop_rapidfire", wrapped_stop)

        def task_factory() -> asyncio.Task:
            calls.append("factory")
            return asyncio.create_task(asyncio.sleep(0))

        device._start_rapidfire_task(
            "btn_side",
            "key",
            task_factory,
            code=evdev.ecodes.KEY_A,
            uinput=fake_uinput,  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)
        device._stop_rapidfire("btn_side")

        assert calls[:2] == ["stop:btn_side", "factory"]

    @pytest.mark.asyncio
    async def test_rapidfire_quick_release_and_repress_does_not_leave_task_or_key_stuck(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

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

        await device._execute_action(action, press_event, "btn_side")
        await device._execute_action(action, release_event, "btn_side")
        await device._execute_action(action, press_event, "btn_side")
        await asyncio.sleep(0)

        assert len(device._rapidfire_tasks) == 1

        await device._execute_action(action, release_event, "btn_side")
        await asyncio.sleep(0.01)

        assert device._rapidfire_tasks == {}
        assert device._rapidfire_outputs == {}
        assert device._held_output_keys["keyboard"] == set()
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
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "mouse")

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

        await device._process_event(press_event)
        await asyncio.sleep(0)
        assert "btn_side" in device._held_source_actions

        await device._process_event(release_event)
        await asyncio.sleep(0.01)

        assert device._rapidfire_tasks == {}
        assert device._rapidfire_outputs == {}
        assert device._held_output_keys["keyboard"] == set()
        assert "btn_side" not in device._held_source_actions
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
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

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

        await device._process_event(press_event)
        await device._process_event(release_event)
        await asyncio.sleep(0)

        assert passthrough_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 0),
        ]
        assert mapped_uinput.writes == []
        assert device._combo_passthrough_held == set()

    @pytest.mark.asyncio
    async def test_combo_passthrough_does_not_bypass_unrelated_mapping_action(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

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

        await device._process_event(press_event)
        await device._process_event(release_event)

        assert passthrough_uinput.writes == []
        assert mapped_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
        ]
        assert device._combo_passthrough_held == set()

    @pytest.mark.asyncio
    async def test_combo_consumed_modifier_release_still_passthroughs_when_held(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

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

        await device._process_event(press_event)
        await device._process_event(release_event)

        assert passthrough_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTALT, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTALT, 0),
        ]
        assert device._combo_passthrough_held == set()

    @pytest.mark.asyncio
    async def test_combo_consumed_release_still_stops_existing_rapidfire_action(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

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

        await device._process_event(press_event)
        await asyncio.sleep(0)
        assert "key_f5" in device._held_source_actions

        await device._process_event(release_event)
        await asyncio.sleep(0.01)

        assert device._rapidfire_tasks == {}
        assert device._rapidfire_outputs == {}
        assert device._held_output_keys["keyboard"] == set()
        assert "key_f5" not in device._held_source_actions
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

        async def release_interface(_hardware_id: str, _path: str) -> None:
            await fake_device.release()

        manager.grabbed_devices = {"hw": []}
        manager._desired_paths["hw"] = {"/dev/input/event0"}
        manager._release_interface_unlocked = release_interface

        await manager._delayed_interface_release("hw", "/dev/input/event0", 0.001)

        fake_device.release.assert_not_awaited()

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
        manager._clear_combo_runtime_for_binding_scope = AsyncMock()  # type: ignore[method-assign]
        manager._clear_combo_runtime = AsyncMock()  # type: ignore[method-assign]

        await manager._release_interface_unlocked("hw", "/dev/input/event0")

        manager._clear_combo_runtime_for_binding_scope.assert_awaited_once_with("hw", "mouse")
        manager._clear_combo_runtime.assert_not_awaited()
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
        manager._desired_paths["hw"] = {"/dev/input/event0"}
        manager._desired_grabs["hw"] = DesiredGrabConfig(
            paths={"/dev/input/event0"},
            button_map={"btn_side": "btn_side"},
        )
        manager._clear_combo_runtime_for_binding_scope = AsyncMock()  # type: ignore[method-assign]

        await manager._release_interface_unlocked("hw", "/dev/input/event0")

        assert manager.grabbed_devices == {}
        assert manager.active_mappings["hw"] == {"btn_side": action}
        assert manager._desired_paths["hw"] == {"/dev/input/event0"}
        assert manager._desired_grabs["hw"].paths == {"/dev/input/event0"}

    @pytest.mark.asyncio
    async def test_release_device_clears_scoped_combo_runtime_before_releasing_hardware(
        self,
    ) -> None:
        manager = DeviceManager()
        fake_device = SimpleNamespace(release=AsyncMock())
        manager.grabbed_devices = {"hw": [fake_device]}
        manager._clear_combo_runtime_for_binding_scope = AsyncMock()  # type: ignore[method-assign]
        manager._clear_combo_runtime = AsyncMock()  # type: ignore[method-assign]
        manager._destroy_global_uinputs = Mock()

        result = await manager._release_device_unlocked("hw")

        assert result == {"released": True, "hardware_id": "hw"}
        manager._clear_combo_runtime_for_binding_scope.assert_awaited_once_with("hw")
        manager._clear_combo_runtime.assert_not_awaited()
        fake_device.release.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clear_combo_runtime_for_binding_scope_stops_only_affected_actions(
        self,
    ) -> None:
        manager = DeviceManager()
        manager._combo_engine.drop_candidates_for_binding_scope = Mock(  # type: ignore[method-assign]
            return_value={"combo-1"}
        )
        manager.stop_combo_action = AsyncMock()  # type: ignore[method-assign]
        manager._refresh_combo_timeout_watchdog = Mock()  # type: ignore[method-assign]

        await manager._clear_combo_runtime_for_binding_scope("1234:5678", "mouse")

        manager._combo_engine.drop_candidates_for_binding_scope.assert_called_once_with(
            "1234:5678",
            "mouse",
        )
        manager.stop_combo_action.assert_awaited_once_with("combo-1")
        manager._refresh_combo_timeout_watchdog.assert_called_once()


class TestEventLoopRecovery:
    @pytest.mark.asyncio
    async def test_event_processing_error_releases_held_output_before_backoff(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

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
        original_execute_action = device._execute_action

        async def fail_after_press(action, event, event_name):
            await original_execute_action(action, event, event_name)
            raise RuntimeError("boom")

        async def fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        monkeypatch.setattr(device, "_execute_action", fail_after_press)
        monkeypatch.setattr(dm.asyncio, "sleep", fake_sleep)

        device.device = _FakeInputDevice()  # type: ignore[assignment]
        device._running = True

        await device._event_loop()

        assert sleep_calls == [0.01]
        assert fake_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]
        assert device._held_output_keys["keyboard"] == set()
        assert device._held_source_actions == {}


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

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: f"{path}-stable")
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"key_f13": "key_f13"},
            mapping_getter=lambda: {},
            event_callback=manager._on_device_event,
            device_type=DeviceType.KEYBOARD,
        )

        def fail_resolve(_path: str) -> str:
            raise AssertionError("resolve_stable_path should not run in the hot event path")

        def fail_interface(_path: str) -> str:
            raise AssertionError("get_interface_id should not run in the hot event path")

        monkeypatch.setattr(dm, "resolve_stable_path", fail_resolve)
        monkeypatch.setattr(dm, "get_interface_id", fail_interface)

        decision = await device._process_event(
            SimpleNamespace(
                type=evdev.ecodes.EV_KEY,
                code=evdev.ecodes.KEY_F13,
                value=1,
            )
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

        pressed = await manager._on_device_event(
            "1234:5678",
            "/dev/input/by-id/test-mouse",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.BTN_SIDE,
            1,
        )
        released = await manager._on_device_event(
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
            manager._on_device_event(
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
        manager._keyboard_uinput = Mock()

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

        pressed = await manager._on_device_event(
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            1,
        )
        released = await manager._on_device_event(
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            0,
        )

        assert pressed is not None and pressed.consume_current_event is True
        assert released is not None and released.consume_current_event is True
        assert manager._keyboard_uinput.write.call_args_list[0].args == (
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F5,
            1,
        )
        assert manager._keyboard_uinput.write.call_args_list[1].args == (
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F5,
            0,
        )

    @pytest.mark.asyncio
    async def test_runtime_combo_tap_key_releases_when_runtime_clears(self, monkeypatch):
        manager = DeviceManager()
        manager._keyboard_uinput = _FakeUInput()

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

        pressed = await manager._on_device_event(
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            1,
        )
        await asyncio.sleep(0)
        await manager._clear_combo_runtime()

        assert pressed is not None and pressed.consume_current_event is True
        assert manager._keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F5, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F5, 0),
        ]

    @pytest.mark.asyncio
    async def test_runtime_combo_tap_trigger_releases_when_runtime_clears(self, monkeypatch):
        manager = DeviceManager()
        manager._gamepad_uinput = _FakeUInput()

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

        pressed = await manager._on_device_event(
            "1234:5678",
            "/dev/input/by-id/test-mouse",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.BTN_SIDE,
            1,
        )
        await asyncio.sleep(0)
        await manager._clear_combo_runtime()

        assert pressed is not None and pressed.consume_current_event is True
        assert manager._gamepad_uinput.writes == [
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

        await manager._on_device_event(
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_A,
            1,
        )
        await manager._on_device_event(
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

        await manager._on_device_event(
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_LEFTALT,
            1,
        )
        await manager._on_device_event(
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
        manager._keyboard_uinput = _FakeUInput()

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

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={
                "key_leftmeta": "key_leftmeta",
                "key_4": "key_4",
                "key_1": "key_1",
            },
            mapping_getter=lambda: {},
            event_callback=manager._on_device_event,
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=manager._keyboard_uinput,  # type: ignore[arg-type]
            mouse_uinput=_FakeUInput(),  # type: ignore[arg-type]
            gamepad_uinput=_FakeUInput(),  # type: ignore[arg-type]
        )
        passthrough = _FakeUInput()
        device.uinput = passthrough  # type: ignore[assignment]
        device._running = True
        manager.grabbed_devices = {"1234:5678": [device]}

        await device._process_event(
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_LEFTMETA, value=1)
        )
        await device._process_event(
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_4, value=1)
        )
        await device._process_event(
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_4, value=0)
        )
        await device._process_event(
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_1, value=1)
        )

        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTMETA, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_4, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_4, 0),
        ]
        assert manager._keyboard_uinput.writes == [
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

        await manager._on_device_event(
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            1,
        )
        await manager._on_device_event(
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
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

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

        await device._process_event(press_event)

        assert device._combo_passthrough_held == {"key_1"}
        assert "key_1" not in device._held_source_actions

        await device.reset_mapping_runtime_state()
        mapping_state["key_1"] = dm.MappingAction(
            action_type=ActionType.KEYBOARD,
            target="key_a",
        )

        assert device._combo_passthrough_held == set()
        assert device._held_source_actions["key_1"] is None

        await device._process_event(release_event)

        assert passthrough_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 0),
        ]
        assert mapped_uinput.writes == []
        assert "key_1" not in device._held_source_actions

    @pytest.mark.asyncio
    async def test_superkey_release_after_reset_does_not_recreate_stale_machine(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "mouse")

        mapping_state = {
            "btn_side": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=dm.SuperkeyConfig(
                    name="test",
                    tap_action=dm.SuperkeyActionData(action_type="keyboard", target="key_a"),
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

        await device._process_event(press_event)
        assert "btn_side" in device._superkey_machines

        await device.reset_superkeys()
        assert device._superkey_machines == {}

        await device._process_event(release_event)

        assert device._superkey_machines == {}

    @pytest.mark.asyncio
    async def test_shared_superkey_config_on_two_inputs_holds_output_until_both_release(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "mouse")

        shared_config = dm.SuperkeyConfig(
            name="shared",
            hold_threshold_ms=0,
            hold_action=dm.SuperkeyActionData(action_type="keyboard", target="key_a"),
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

        await device._process_event(side_press)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await device._process_event(extra_press)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert set(device._superkey_machines) == {"btn_side", "btn_extra"}
        assert device._superkey_machines["btn_side"].state.value == "holding"
        assert device._superkey_machines["btn_extra"].state.value == "holding"
        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
        ]

        await device._process_event(side_release)
        await asyncio.sleep(0)

        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
        ]
        assert device._held_output_keys["keyboard"] == {evdev.ecodes.KEY_A}

        await device._process_event(extra_release)
        await asyncio.sleep(0)

        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]
        assert device._held_output_keys["keyboard"] == set()

    @pytest.mark.asyncio
    async def test_reset_mapping_runtime_state_seeds_startup_held_action_and_releases_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

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

        assert device._held_source_actions["key_f13"] == mapping_state["key_f13"]
        assert device.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]

    @pytest.mark.asyncio
    async def test_superkey_broadcast_does_not_block_hot_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "mouse")

        blocker = asyncio.Event()

        async def stalled_callback(_command, _data):
            await blocker.wait()

        mapping_state = {
            "btn_side": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=dm.SuperkeyConfig(
                    name="test",
                    tap_action=dm.SuperkeyActionData(action_type="exec", exec_ref=7),
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

        await asyncio.wait_for(device._process_event(press_event), timeout=0.05)
        await asyncio.wait_for(device._process_event(release_event), timeout=0.05)

        blocker.set()
        await asyncio.sleep(0)


class TestRuntimeFailureCleanup:
    @pytest.mark.asyncio
    async def test_event_processing_error_clears_scoped_runtime_and_releases_outputs(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

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
        device._write_key(keyboard_uinput, evdev.ecodes.KEY_A, 1)  # type: ignore[arg-type]

        await device._recover_from_event_processing_error()

        cleanup.assert_awaited_once_with("1234:5678", "kbd")
        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]
        assert gamepad_uinput.writes[-2:] == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RZ, 0),
        ]
        assert device._held_output_keys["keyboard"] == set()


class TestDeviceManagerHelpers:
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

            def update_button_map(
                self,
                button_map: dict[str, str],
                button_codes: dict[str, int] | None = None,
            ) -> None:
                self.button_map_updates.append(dict(button_map))
                self.button_code_updates.append(dict(button_codes or {}))

            def release_tracked_outputs(self) -> None:
                return

            def has_held_source_inputs(self) -> bool:
                return False

        manager = DeviceManager()
        manager._create_global_uinputs = Mock()
        manager._destroy_global_uinputs = Mock()
        manager._schedule_interface_release = Mock()
        manager._cancel_pending_interface_release = Mock()
        manager._clear_combo_runtime_for_binding_scope = AsyncMock()  # type: ignore[method-assign]

        monkeypatch.setattr(dm.evdev, "InputDevice", _RawInputDevice)
        monkeypatch.setattr(dm, "GrabbedDevice", _FakeManagedDevice)
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)

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
        manager._create_global_uinputs.assert_called_once()
        manager._cancel_pending_interface_release.assert_called_once_with(
            "1234:5678",
            "/dev/input/event1",
        )
        manager._schedule_interface_release.assert_called_once_with(
            "1234:5678",
            "/dev/input/event0",
        )
        assert created["/dev/input/event1"].button_map_updates == [{"right": "btn_side"}]
        assert created["/dev/input/event1"].button_code_updates == [{}]
        assert released == {"released": True, "hardware_id": "1234:5678"}
        assert created["/dev/input/event0"].release.await_count == 1
        assert created["/dev/input/event1"].release.await_count == 1
        assert manager.grabbed_devices == {}
        assert manager.active_mappings == {}
        assert manager._desired_paths == {}
        manager._destroy_global_uinputs.assert_called_once()

    def test_parse_action_supports_string_and_hyprland_dispatch_alias(self) -> None:
        manager = DeviceManager()

        string_action = manager._parse_action("key_a")
        dispatch_action = manager._parse_action(
            {"action": "hyprland_dispatch", "dispatcher": "workspace", "args": "2"}
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
        manager._clear_combo_runtime = AsyncMock()  # type: ignore[method-assign]
        manager._refresh_combo_timeout_watchdog = Mock()  # type: ignore[method-assign]

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
        manager._clear_combo_runtime.assert_awaited_once()
        manager._refresh_combo_timeout_watchdog.assert_called_once()

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

        manager._reconcile_topology = AsyncMock(side_effect=RuntimeError("reconcile boom"))  # type: ignore[assignment]
        monkeypatch.setattr(dm.asyncio, "sleep", fake_sleep)

        with caplog.at_level(logging.WARNING, logger="keyforged.devices"):
            manager._schedule_topology_reconcile(snapshot)
            task = manager._topology_reconcile_task
            assert task is not None
            await task

        assert "Topology reconcile failed: reconcile boom" in caplog.text
        assert manager._topology_reconcile_task is None

    def test_combo_capture_queue_round_trip(self) -> None:
        manager = DeviceManager()
        ready = asyncio.Event()
        manager.grabbed_devices = {"hw": [object(), object()], "other": [object()]}

        started = manager.begin_combo_capture("token", {"1234:5678"}, ready)
        capture_queue, hardware_ids, notify_event = manager._combo_capture_queues["token"]
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
        manager._combo_timeout_task = previous
        manager._combo_engine.next_deadline = Mock(return_value=None)  # type: ignore[method-assign]

        manager._refresh_combo_timeout_watchdog()
        await asyncio.sleep(0)

        assert previous.cancelled() is True
        assert manager._combo_timeout_task is None

        replacement = asyncio.create_task(asyncio.sleep(60))
        manager._combo_timeout_task = replacement
        manager._combo_engine.next_deadline = Mock(return_value=42.0)  # type: ignore[method-assign]
        manager._combo_timeout_watchdog = AsyncMock()  # type: ignore[method-assign]

        manager._refresh_combo_timeout_watchdog()
        await asyncio.sleep(0)

        assert replacement.cancelled() is True
        manager._combo_timeout_watchdog.assert_awaited_once_with(42.0)
        manager._combo_timeout_task.cancel()
        await asyncio.sleep(0)
        assert manager._combo_timeout_task.done() is True

    @pytest.mark.asyncio
    async def test_combo_timeout_watchdog_expires_and_clears_current_task(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        manager._combo_engine.expire_timeouts = Mock()  # type: ignore[method-assign]
        refreshes: list[str] = []
        manager._refresh_combo_timeout_watchdog = Mock(  # type: ignore[method-assign]
            side_effect=lambda: refreshes.append("refresh")
        )

        monkeypatch.setattr(dm.time, "monotonic", lambda: 10.0)
        monkeypatch.setattr(dm.asyncio, "sleep", AsyncMock())

        task = asyncio.create_task(manager._combo_timeout_watchdog(10.5))
        manager._combo_timeout_task = task
        await task

        manager._combo_engine.expire_timeouts.assert_called_once_with(10.0)
        assert manager._combo_timeout_task is None
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

        assert device._find_action_for_event(event, mapping) == mapping["south"]

    def test_device_has_mapped_buttons_matches_by_code_when_names_differ(self) -> None:
        manager = DeviceManager()
        caps = {
            evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SOUTH],
        }

        assert manager._device_has_mapped_buttons(
            caps,
            {"btn_south"},
            {evdev.ecodes.BTN_SOUTH},
        )

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
        device._track_key_state(device.uinput, evdev.ecodes.KEY_A, 1)
        device._track_key_state(device.keyboard_uinput, evdev.ecodes.KEY_B, 1)
        device._track_key_state(device.mouse_uinput, evdev.ecodes.BTN_LEFT, 1)
        device._track_superkey_output("gamepad", evdev.ecodes.BTN_SOUTH, 1)
        device._rapidfire_tasks["btn_side"] = task  # type: ignore[assignment]
        device._rapidfire_outputs["btn_side"] = {"kind": "key"}
        device._rapidfire_active["btn_side"] = True
        device._tap_active["btn_side"] = True
        device._combo_passthrough_held.add("btn_side")
        device._held_source_actions["btn_side"] = None

        assert device._bucket_for_uinput(device.keyboard_uinput) == "keyboard"

        device._release_all_keys()

        assert passthrough.writes == [(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0)]
        assert keyboard.writes == [(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0)]
        assert mouse.writes == [(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 0)]
        assert gamepad.writes[-2:] == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RZ, 0),
        ]
        canceled.assert_called_once()
        assert device._rapidfire_tasks == {}
        assert device._tap_active == {}
        assert device._held_source_actions == {}

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

        monkeypatch.setattr(dm.asyncio, "get_running_loop", lambda: _FakeLoop())

        outcomes = iter([TimeoutError(), None])

        async def fake_wait_for(awaitable, _timeout: float):
            close = getattr(awaitable, "close", None)
            if close is not None:
                close()
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(dm.asyncio, "wait_for", fake_wait_for)

        assert await device._wait_for_active_key_activity(0.1) is False

        with caplog.at_level(logging.WARNING, logger="keyforged.devices"):
            assert await device._wait_for_active_key_activity(0.1) is True

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
            await device._broadcast_grab_status("waiting", ["key_a"], waited_s=1.5)

        device._seed_startup_held_actions()

        callback.assert_awaited_once()
        assert "Failed to broadcast grab status" in caplog.text
        assert device._held_source_actions["key_a"] == mapping_state["left"]
        assert device._held_source_actions["key_b"] == mapping_state["right"]
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

        device._seed_startup_held_actions()

        assert device._held_source_actions["btn_a"] == mapping_state["south"]

    def test_reconcile_startup_held_action_releases_gamepad_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = _make_grabbed_device(monkeypatch)
        gamepad = _FakeUInput()
        device.gamepad_uinput = gamepad  # type: ignore[assignment]

        device._reconcile_startup_held_action(
            dm.MappingAction(action_type=ActionType.GAMEPAD, target="btn_lt")
        )
        device._reconcile_startup_held_action(
            dm.MappingAction(action_type=ActionType.GAMEPAD, target="btn_south")
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

        monkeypatch.setattr(dm.asyncio, "sleep", AsyncMock())

        await device._tap_key(evdev.ecodes.KEY_A, 25, "tap", device.keyboard_uinput)  # type: ignore[arg-type]
        device._tap_active["trigger"] = True
        await device._tap_trigger(evdev.ecodes.ABS_Z, 25, "trigger")
        move_action = dm.MappingAction(
            action_type=ActionType.MOUSE_MOVE_REL,
            move_x=4,
            move_y=-3,
        )
        device._tap_active["move"] = True
        await device._tap_move(move_action, "move", 25)
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
        assert device._tap_active == {}

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
        passthrough = Mock()
        emitted_moves: list[tuple[ActionType, int, int]] = []
        fire_tasks: list[asyncio.Task] = []

        device._passthrough = passthrough
        monkeypatch.setattr(
            device,
            "_emit_mouse_move",
            lambda action: emitted_moves.append((action.action_type, action.move_x, action.move_y)),
        )

        def fire_and_observe(coro, _label: str) -> asyncio.Task:
            task = asyncio.create_task(coro)
            fire_tasks.append(task)
            return task

        monkeypatch.setattr(dm, "_fire_and_observe", fire_and_observe)

        press = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=1, value=1)
        release = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=1, value=0)

        await device._execute_action(
            dm.MappingAction(action_type=ActionType.PASSTHROUGH),
            press,
            "btn",
        )
        await device._execute_action(
            dm.MappingAction(action_type=ActionType.MOUSE, target="btn_left"),
            press,
            "mouse_btn",
        )
        await device._execute_action(
            dm.MappingAction(action_type=ActionType.MOUSE, target="btn_left"),
            release,
            "mouse_btn",
        )
        await device._execute_action(
            dm.MappingAction(action_type=ActionType.GAMEPAD, target="btn_lt"),
            press,
            "trigger_btn",
        )
        await device._execute_action(
            dm.MappingAction(action_type=ActionType.GAMEPAD, target="btn_lt"),
            release,
            "trigger_btn",
        )
        await device._execute_action(
            dm.MappingAction(action_type=ActionType.GAMEPAD, target="btn_south"),
            press,
            "pad_btn",
        )
        await device._execute_action(
            dm.MappingAction(action_type=ActionType.GAMEPAD, target="btn_south"),
            release,
            "pad_btn",
        )
        await device._execute_action(
            dm.MappingAction(action_type=ActionType.EXEC, exec_ref=7),
            press,
            "exec_btn",
        )
        await device._execute_action(
            dm.MappingAction(
                action_type=ActionType.COMPOSITOR_DISPATCH,
                compositor_dispatcher="workspace",
                compositor_args="2",
            ),
            press,
            "dispatch_btn",
        )
        await device._execute_action(
            dm.MappingAction(action_type=ActionType.START_MACRO_RECORDING),
            press,
            "start_rec",
        )
        await device._execute_action(
            dm.MappingAction(action_type=ActionType.STOP_MACRO_RECORDING),
            press,
            "stop_rec",
        )
        await device._execute_action(
            dm.MappingAction(action_type=ActionType.CANCEL_MACRO_PLAYBACK),
            press,
            "cancel_macro",
        )
        await device._execute_action(
            dm.MappingAction(action_type=ActionType.PROFILE_TOGGLE, profile_name="Gaming"),
            press,
            "toggle_profile",
        )
        await device._execute_action(
            dm.MappingAction(action_type=ActionType.MACRO, macro_name="demo"),
            press,
            "macro_btn",
        )
        await device._execute_action(
            dm.MappingAction(action_type=ActionType.MACRO, macro_name="demo"),
            release,
            "macro_btn",
        )
        await device._execute_action(
            dm.MappingAction(action_type=ActionType.MOUSE_MOVE_REL, move_x=5, move_y=-2),
            press,
            "move_rel",
        )
        await device._execute_action(
            dm.MappingAction(action_type=ActionType.MOUSE_MOVE_ABS, move_x=10, move_y=20),
            press,
            "move_abs",
        )

        if fire_tasks:
            await asyncio.gather(*fire_tasks)

        passthrough.assert_called_once_with(press)
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
        created_configs: list[dm.SuperkeyConfig] = []

        monkeypatch.setattr(
            dm,
            "SuperkeyMachine",
            lambda **kwargs: created_configs.append(kwargs["config"]) or fake_machine,
        )

        def fire_and_observe(coro, _label: str) -> asyncio.Task:
            return asyncio.create_task(coro)

        monkeypatch.setattr(dm, "_fire_and_observe", fire_and_observe)
        monkeypatch.setattr(
            device,
            "_tap_move",
            AsyncMock(
                side_effect=lambda action, event_name, hold_ms: move_calls.append(
                    (event_name, hold_ms)
                )
            ),
        )

        superkey_action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=dm.SuperkeyConfig(
                name="super",
                tap_action=dm.SuperkeyActionData(action_type="exec", exec_ref=4),
            ),
        )
        tap_move_action = dm.MappingAction(
            action_type=ActionType.MOUSE_MOVE_REL,
            move_x=1,
            move_y=2,
            tap_enabled=True,
            tap_hold_ms=33,
        )

        await device._execute_action(superkey_action, SimpleNamespace(value=1), "super_btn")
        await device._execute_action(superkey_action, SimpleNamespace(value=0), "super_btn")
        await device._execute_action(tap_move_action, SimpleNamespace(value=1), "move_btn")
        await asyncio.sleep(0)

        assert created_configs and created_configs[0].name == "super"
        fake_machine.on_down.assert_awaited_once()
        fake_machine.on_up.assert_awaited_once()
        assert move_calls == [("move_btn", 33)]


class TestComboActionDispatch:
    @pytest.mark.asyncio
    async def test_start_and_stop_combo_action_cover_additional_synthetic_paths(self) -> None:
        manager = DeviceManager()
        manager._mouse_uinput = _FakeUInput()
        manager._gamepad_uinput = _FakeUInput()
        manager.play_macro = AsyncMock(return_value={"status": "ok"})  # type: ignore[method-assign]
        manager._broadcast_combo_action = AsyncMock()  # type: ignore[method-assign]
        manager._emit_combo_mouse_move = Mock()  # type: ignore[method-assign]
        manager._resolve_code = Mock(return_value=evdev.ecodes.BTN_SOUTH)  # type: ignore[method-assign]
        manager._combo_tap_trigger = AsyncMock()  # type: ignore[method-assign]
        manager._combo_rapidfire_trigger = AsyncMock()  # type: ignore[method-assign]

        binding = dm.RuntimeComboBinding(hardware_id="1234:5678", source="mouse", evdev="btn_side")

        await manager.start_combo_action(
            "mouse-move",
            dm.MappingAction(action_type=ActionType.MOUSE_MOVE_REL, move_x=3, move_y=-1),
            binding,
        )
        await manager.start_combo_action(
            "macro",
            dm.MappingAction(
                action_type=ActionType.MACRO,
                macro_name="demo",
                macro_loop_mode="hold",
            ),
            binding,
        )
        await manager.start_combo_action(
            "exec",
            dm.MappingAction(action_type=ActionType.EXEC, exec_ref=9),
            binding,
        )
        await manager.start_combo_action(
            "dispatch",
            dm.MappingAction(
                action_type=ActionType.COMPOSITOR_DISPATCH,
                compositor_dispatcher="workspace",
                compositor_args="2",
            ),
            binding,
        )
        await manager.start_combo_action(
            "record",
            dm.MappingAction(action_type=ActionType.START_MACRO_RECORDING),
            binding,
        )
        await manager.start_combo_action(
            "profile",
            dm.MappingAction(action_type=ActionType.PROFILE_ENABLE, profile_name="Gaming"),
            binding,
        )
        await manager.start_combo_action(
            "trigger",
            dm.MappingAction(action_type=ActionType.GAMEPAD, target="btn_lt"),
            binding,
        )
        await manager.stop_combo_action("trigger")
        await manager.start_combo_action(
            "tap-trigger",
            dm.MappingAction(
                action_type=ActionType.GAMEPAD,
                target="btn_lt",
                tap_enabled=True,
                tap_hold_ms=25,
            ),
            binding,
        )
        await manager.stop_combo_action("tap-trigger")
        await manager.start_combo_action(
            "rapid-trigger",
            dm.MappingAction(
                action_type=ActionType.GAMEPAD,
                target="btn_lt",
                rapidfire_enabled=True,
                rapidfire_hold_ms=10,
                rapidfire_wait_ms=10,
            ),
            binding,
        )
        await manager.stop_combo_action("rapid-trigger")
        await manager.stop_combo_action("macro")

        manager._emit_combo_mouse_move.assert_called_once()
        assert manager.play_macro.await_count == 2
        assert manager.play_macro.await_args_list[0].kwargs["trigger_value"] == 1
        assert manager.play_macro.await_args_list[1].kwargs["trigger_value"] == 0
        assert manager._broadcast_combo_action.await_count == 4
        assert manager._gamepad_uinput.writes == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 255),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0),
        ]
