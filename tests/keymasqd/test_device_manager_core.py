# ruff: noqa: F403, F405, I001
from tests.keymasqd.device_manager_support import *


@pytest.mark.asyncio
async def test_set_cursor_position_emits_absolute_mouse_move() -> None:
    manager = DeviceManager()
    mouse = _FakeUInput()
    manager.output_state.mouse_uinput = mouse  # type: ignore[assignment]

    result = await manager.set_cursor_position(123, 456)

    assert result == {"status": "ok", "x": 123, "y": 456}
    assert mouse.writes == [
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, -2147483648),
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, -2147483648),
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 123),
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, 456),
    ]


@pytest.mark.asyncio
async def test_set_cursor_position_reports_missing_mouse_uinput() -> None:
    manager = DeviceManager()

    assert await manager.set_cursor_position(123, 456) == {
        "status": "error",
        "message": "No mouse uinput device available",
    }


@pytest.mark.asyncio
async def test_set_cursor_position_uses_session_backend_when_enabled() -> None:
    manager = DeviceManager()
    sent: list[tuple[CommandType, dict[str, object]]] = []

    async def callback(command: CommandType, data: dict[str, object]) -> None:
        sent.append((command, data))
        manager.complete_cursor_position_request(
            str(data["request_id"]),
            ok=True,
            message="ok",
        )

    manager.broadcast_callback = callback
    manager.set_cursor_position_backend(True)

    result = await manager.set_cursor_position(123, 456)

    assert result == {"status": "ok", "backend": "session", "x": 123, "y": 456}
    assert sent == [
        (
            CommandType.SET_CURSOR_POSITION,
            {"request_id": "1", "x": 123, "y": 456},
        )
    ]
    assert manager.cursor_position_state.request_seq == 0


@pytest.mark.asyncio
async def test_set_cursor_position_falls_back_when_session_backend_fails() -> None:
    manager = DeviceManager()
    mouse = _FakeUInput()
    manager.output_state.mouse_uinput = mouse  # type: ignore[assignment]
    manager.set_cursor_position_backend(True)

    async def callback(command: CommandType, data: dict[str, object]) -> None:
        assert command == CommandType.SET_CURSOR_POSITION
        manager.complete_cursor_position_request(
            str(data["request_id"]),
            ok=False,
            message="unsupported",
        )

    manager.broadcast_callback = callback

    result = await manager.set_cursor_position(123, 456)

    assert result == {"status": "ok", "x": 123, "y": 456}
    assert any(write == (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 123) for write in mouse.writes)


def test_set_cursor_position_backend_disable_resets_request_sequence() -> None:
    manager = DeviceManager()
    manager.cursor_position_state.request_seq = 42

    assert manager.set_cursor_position_backend(False) == {"status": "ok", "enabled": False}
    assert manager.cursor_position_state.request_seq == 0


@pytest.mark.asyncio
async def test_complete_cursor_position_request_resets_sequence_when_pending_empty() -> None:
    manager = DeviceManager()
    future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
    manager.cursor_position_state.request_seq = 42
    manager.cursor_position_state.pending["42"] = future

    assert manager.complete_cursor_position_request("42", ok=True, message="ok") == {
        "status": "ok",
        "completed": True,
    }
    assert manager.cursor_position_state.request_seq == 0


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

        with caplog.at_level(logging.WARNING, logger="keymasqd.devices"):
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
