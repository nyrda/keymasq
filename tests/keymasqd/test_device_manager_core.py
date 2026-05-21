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
async def test_device_inspector_suppression_status_broadcasts() -> None:
    manager = DeviceManager()
    broadcasts: list[tuple[CommandType, dict[str, object]]] = []
    manager._broadcast_runtime_event = lambda event_type, data: broadcasts.append(  # type: ignore[method-assign]
        (event_type, data)
    )

    started = await manager.start_device_inspector("1234:5678")
    enabled = await manager.enable_device_inspector_suppression("1234:5678")
    disabled = await manager.disable_device_inspector_suppression("1234:5678", "key_esc")

    assert started["active"] is True
    assert started["suppressed"] is False
    assert enabled["suppressed"] is True
    assert disabled == {
        "status": "ok",
        "hardware_id": "1234:5678",
        "active": True,
        "suppressed": False,
        "reason": "key_esc",
    }
    assert broadcasts == [
        (
            CommandType.DEVICE_INSPECTOR_STATUS,
            {
                "hardware_id": "1234:5678",
                "active": True,
                "suppressed": False,
                "reason": "start",
            },
        ),
        (
            CommandType.DEVICE_INSPECTOR_STATUS,
            {
                "hardware_id": "1234:5678",
                "active": True,
                "suppressed": True,
                "reason": "enable_suppression",
            },
        ),
        (
            CommandType.DEVICE_INSPECTOR_STATUS,
            {
                "hardware_id": "1234:5678",
                "active": True,
                "suppressed": False,
                "reason": "key_esc",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_device_inspector_disable_does_not_activate_inactive_inspector() -> None:
    manager = DeviceManager()
    broadcasts: list[tuple[CommandType, dict[str, object]]] = []
    manager._broadcast_runtime_event = lambda event_type, data: broadcasts.append(  # type: ignore[method-assign]
        (event_type, data)
    )

    disabled = await manager.disable_device_inspector_suppression("1234:5678", "manual")

    assert disabled == {
        "status": "ok",
        "hardware_id": "1234:5678",
        "active": False,
        "suppressed": False,
        "reason": "manual",
    }
    assert "1234:5678" not in manager.device_inspector_active_hardware_ids
    assert broadcasts == [
        (
            CommandType.DEVICE_INSPECTOR_STATUS,
            {
                "hardware_id": "1234:5678",
                "active": False,
                "suppressed": False,
                "reason": "manual",
            },
        )
    ]


@pytest.mark.asyncio
async def test_release_all_devices_clears_device_inspector_state() -> None:
    manager = DeviceManager()
    manager.device_inspector_active_hardware_ids.add("1234:5678")
    manager.device_inspector_suppressed_hardware_ids.add("1234:5678")

    await manager.release_all_devices()

    assert manager.device_inspector_active_hardware_ids == set()
    assert manager.device_inspector_suppressed_hardware_ids == set()


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
    def test_list_devices_closes_devices_after_metadata_scan(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        closed_paths: list[str] = []

        class FakeDevice:
            name = "Raw Keyboard"
            phys = "usb-test"
            uniq = ""
            info = SimpleNamespace(vendor=0x1234, product=0x5678)

            def __init__(self, path: str) -> None:
                self.path = path

            def capabilities(self):
                return {evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_A]}

            def input_props(self):
                return []

            def close(self) -> None:
                closed_paths.append(self.path)

        monkeypatch.setattr(dm, "_device_paths", lambda: ["/dev/input/event0"])
        monkeypatch.setattr(dm.evdev, "InputDevice", FakeDevice)
        monkeypatch.setattr(dm, "resolve_stable_path", lambda _path: "/dev/input/by-id/raw-kbd")
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

        result = manager._list_devices_sync()

        assert len(cast(list[dict[str, object]], result["devices"])) == 1
        assert closed_paths == ["/dev/input/event0"]

    def test_list_devices_closes_devices_when_metadata_scan_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        closed_paths: list[str] = []

        class FakeDevice:
            name = "Broken Keyboard"
            phys = "usb-test"
            uniq = ""
            info = SimpleNamespace(vendor=0x1234, product=0x5678)

            def __init__(self, path: str) -> None:
                self.path = path

            def capabilities(self):
                raise RuntimeError("unreadable capabilities")

            def close(self) -> None:
                closed_paths.append(self.path)

        monkeypatch.setattr(dm, "_device_paths", lambda: ["/dev/input/event0"])
        monkeypatch.setattr(dm.evdev, "InputDevice", FakeDevice)

        result = manager._list_devices_sync()

        assert result == {"devices": []}
        assert closed_paths == ["/dev/input/event0"]

    def test_list_devices_marks_physical_recording_identity(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()

        class FakeDevice:
            path = "/dev/input/event0"
            name = "Raw Keyboard"
            phys = "usb-test"
            uniq = ""
            info = SimpleNamespace(vendor=0x1234, product=0x5678)

            def capabilities(self):
                return {evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_A]}

            def input_props(self):
                return []

        monkeypatch.setattr(dm, "_device_paths", lambda: ["/dev/input/event0"])
        monkeypatch.setattr(dm.evdev, "InputDevice", lambda _path: FakeDevice())
        monkeypatch.setattr(dm, "resolve_stable_path", lambda _path: "/dev/input/by-id/raw-kbd")
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

        result = manager._list_devices_sync()
        result_devices = cast(list[dict[str, object]], result["devices"])
        device = result_devices[0]

        assert device["path"] == "/dev/input/event0"
        assert device["open_path"] == "/dev/input/event0"
        assert device["stable_path"] == "/dev/input/by-id/raw-kbd"
        assert device["recording_id"] == "physical:/dev/input/by-id/raw-kbd"
        assert device["recording_kind"] == "physical"
        assert device["grabbed_by_keymasq"] is False

    def test_list_devices_marks_keymasq_outputs_and_passthrough_sources(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = SimpleNamespace(
            device=SimpleNamespace(path="/dev/input/event10")
        )  # type: ignore[assignment]
        manager.grabbed_devices = {
            "1234:5678": [
                SimpleNamespace(
                    path="/dev/input/event0",
                    stable_path="/dev/input/by-id/raw-kbd",
                    hardware_id="1234:5678",
                    interface_id="kbd",
                    uinput=SimpleNamespace(device=SimpleNamespace(path="/dev/input/event20")),
                )
            ]
        }

        class FakeDevice:
            def __init__(self, path: str) -> None:
                self.path = path
                self.name = {
                    "/dev/input/event0": "Raw Keyboard",
                    "/dev/input/event10": "keymasq-keyboard",
                    "/dev/input/event20": "keymasq-1234:5678",
                }[path]
                self.phys = "py-evdev-uinput" if path != "/dev/input/event0" else "usb-test"
                self.uniq = ""
                self.info = SimpleNamespace(vendor=0x1234, product=0x5678)

            def capabilities(self):
                return {evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_A]}

            def input_props(self):
                return []

        stable_paths = {
            "/dev/input/event0": "/dev/input/by-id/raw-kbd",
            "/dev/input/event10": "/dev/input/event10",
            "/dev/input/event20": "/dev/input/event20",
        }
        monkeypatch.setattr(
            dm,
            "_device_paths",
            lambda: ["/dev/input/event0", "/dev/input/event10", "/dev/input/event20"],
        )
        monkeypatch.setattr(dm.evdev, "InputDevice", FakeDevice)
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: stable_paths[path])
        monkeypatch.setattr(dm, "get_interface_id", lambda path: "kbd" if "raw" in path else path)

        result = manager._list_devices_sync()
        result_devices = cast(list[dict[str, object]], result["devices"])
        devices = {device["path"]: device for device in result_devices}

        raw = devices["/dev/input/event0"]
        assert raw["recording_id"] == "physical:/dev/input/by-id/raw-kbd"
        assert raw["recording_kind"] == "physical"
        assert raw["grabbed_by_keymasq"] is True
        assert raw["source_hardware_id"] == "1234:5678"
        assert raw["source_interface_id"] == "kbd"

        output = devices["/dev/input/event10"]
        assert output["recording_id"] == "keymasq:output:keyboard"
        assert output["recording_kind"] == "keymasq_output"
        assert output["keymasq_output"] == "keyboard"

        passthrough = devices["/dev/input/event20"]
        assert passthrough["recording_id"] == "keymasq:passthrough:1234:5678:kbd"
        assert passthrough["recording_kind"] == "keymasq_passthrough"
        assert passthrough["source_stable_path"] == "/dev/input/by-id/raw-kbd"
        assert passthrough["source_path"] == "/dev/input/event0"

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
        manager.broadcast_callback = AsyncMock()
        manager.diagnostics_state.samples = {"passthrough_mapped": deque([1.0, 3.0])}
        summaries: list[dict[str, dict[str, object]]] = []
        broadcasts: list[tuple[CommandType, dict[str, object]]] = []
        calls: list[tuple[object, tuple[object, ...]]] = []

        async def fake_sleep(_delay: float) -> None:
            manager.diagnostics_state.enabled = False

        async def fake_to_thread(func, /, *args, **kwargs):
            assert kwargs == {}
            calls.append((func, args))
            return func(*args)

        monkeypatch.setattr(dm.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(dm.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(manager, "_log_diagnostics_summary", summaries.append)
        monkeypatch.setattr(
            manager,
            "_broadcast_runtime_event",
            lambda *args: broadcasts.append(args),
        )

        await manager._diagnostics_loop()

        expected_summary = {
            "passthrough_mapped": {"n": 2, "p50": 1.0, "p95": 1.0, "p99": 1.0, "max": 3.0}
        }
        assert summaries == [expected_summary]
        assert calls == [
            (
                manager._summarize_diagnostics_snapshot,
                ({"passthrough_mapped": [1.0, 3.0]},),
            ),
            (summaries.append, (expected_summary,)),
        ]
        assert broadcasts == [
            (
                CommandType.DIAGNOSTICS_SNAPSHOT,
                {
                    "enabled": True,
                    "interval": 5.0,
                    "categories": ["mainline"],
                    "samples": expected_summary,
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_diagnostics_filters_to_mainline_by_default(self) -> None:
        manager = DeviceManager()
        manager.diagnostics_state.enabled = True

        manager._record_diagnostic("passthrough_mapped", 10.0)
        manager._record_diagnostic("passthrough_syn", 15.0)
        manager._record_diagnostic("action_key", 20.0)
        manager._record_diagnostic("combo_passthrough", 30.0)
        manager._record_diagnostic("syn", 40.0)

        assert set(manager.diagnostics_state.samples) == {
            "passthrough_mapped",
            "passthrough_syn",
            "action_key",
        }

    @pytest.mark.asyncio
    async def test_diagnostics_can_include_combo_and_internal_categories(self) -> None:
        manager = DeviceManager()
        manager.diagnostics_state.enabled = True
        manager.diagnostics_state.categories = {"combo", "internal"}

        manager._record_diagnostic("passthrough_mapped", 10.0)
        manager._record_diagnostic("combo_passthrough", 20.0)
        manager._record_diagnostic("combo_passthrough_held", 30.0)
        manager._record_diagnostic("syn", 40.0)
        manager._record_diagnostic("combo_recalled_release_suppressed", 50.0)

        assert set(manager.diagnostics_state.samples) == {
            "combo_passthrough",
            "combo_passthrough_held",
            "syn",
            "combo_recalled_release_suppressed",
        }

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
            log=dm.log,
            deps=dm._topology_runtime_deps(),
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
            log=dm.log,
            deps=dm._topology_runtime_deps(),
        )


class TestMacroControlActions:
    @pytest.mark.asyncio
    async def test_run_macro_control_action_wait_uses_wall_clock_duration(self, monkeypatch):
        manager = DeviceManager()
        clock = {"now": 10.0}
        sleep_calls: list[float] = []

        class _FakeLoop:
            def time(self) -> float:
                return clock["now"]

        async def fake_sleep(duration: float) -> None:
            sleep_calls.append(duration)
            clock["now"] += duration

        monkeypatch.setattr(dm.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(dm.asyncio, "get_running_loop", lambda: _FakeLoop())

        result = await _runtime_run_macro_control_action(
            manager,
            {"macro_action": "wait", "duration_us": 20_000},
        )

        assert sleep_calls == [0.02]
        assert result == pytest.approx(0.02)

    @pytest.mark.asyncio
    async def test_run_macro_control_action_wait_renews_mouse_suppression(
        self, monkeypatch
    ):
        manager = DeviceManager()
        clock = {"now": 10.0}
        begin_mouse_rel_suppression = Mock()

        class _FakeLoop:
            def time(self) -> float:
                return clock["now"]

        async def fake_sleep(duration: float) -> None:
            clock["now"] += duration

        monkeypatch.setattr(dm.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(dm.asyncio, "get_running_loop", lambda: _FakeLoop())
        monkeypatch.setattr(mdm, "begin_mouse_rel_suppression", begin_mouse_rel_suppression)

        result = await _runtime_run_macro_control_action(
            manager,
            {"macro_action": "wait", "duration_us": 10_000_000},
            renew_mouse_suppression=True,
        )

        begin_mouse_rel_suppression.assert_called_once()
        assert begin_mouse_rel_suppression.call_args.kwargs["timeout_s"] == pytest.approx(11.0)
        assert result == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_mouse_suppression_watchdog_keeps_active_inhibit_count(
        self, monkeypatch
    ):
        manager = DeviceManager()

        async def fake_sleep(_duration: float) -> None:
            return None

        monkeypatch.setattr(dm.asyncio, "sleep", fake_sleep)

        manager.macro_state.mouse_rel_suppressed = True
        manager.macro_state.mouse_inhibit_count = 1
        await mdm.mouse_rel_suppression_watchdog(
            manager,
            1.0,
            deps=dm._macro_runtime_deps(),
        )

        assert manager.macro_state.mouse_rel_suppressed is True

        manager.macro_state.mouse_inhibit_count = 0
        await mdm.mouse_rel_suppression_watchdog(
            manager,
            1.0,
            deps=dm._macro_runtime_deps(),
        )

        assert manager.macro_state.mouse_rel_suppressed is False

    @pytest.mark.asyncio
    async def test_run_macro_control_action_wait_random_uses_random_range(self, monkeypatch):
        manager = DeviceManager()
        clock = {"now": 20.0}
        sleep_calls: list[float] = []

        class _FakeLoop:
            def time(self) -> float:
                return clock["now"]

        async def fake_sleep(duration: float) -> None:
            sleep_calls.append(duration)
            clock["now"] += duration

        monkeypatch.setattr(dm.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(dm.asyncio, "get_running_loop", lambda: _FakeLoop())
        monkeypatch.setattr(mdm.random, "randint", lambda _minimum, _maximum: 50_000)

        result = await _runtime_run_macro_control_action(
            manager,
            {"macro_action": "wait_random", "min_us": 10_000, "max_us": 80_000},
        )

        assert sleep_calls == [0.05]
        assert result == pytest.approx(0.05)

    @pytest.mark.asyncio
    async def test_run_macro_control_action_exec_async_broadcasts(self):
        manager = DeviceManager()

        callback = AsyncMock()

        async def cb(command, data):
            await callback(command, data)

        manager.broadcast_callback = cb

        result = await _runtime_run_macro_control_action(
            manager,
            {
                "macro_action": "exec_async",
                "command": "echo hi",
            },
        )

        callback.assert_awaited_once()
        called_command, called_data = callback.await_args.args
        assert called_command == CommandType.ACTION_TRIGGER
        assert called_data["action_type"] == "exec"
        assert called_data["macro_exec_async"] is True
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_run_macro_control_action_compositor_dispatch_broadcasts(self):
        manager = DeviceManager()

        callback = AsyncMock()

        async def cb(command, data):
            await callback(command, data)

        manager.broadcast_callback = cb

        result = await _runtime_run_macro_control_action(
            manager,
            {
                "macro_action": "compositor_dispatch",
                "compositor": "hyprland",
                "dispatcher": "workspace",
                "args": "e+1",
            },
        )

        callback.assert_awaited_once()
        called_command, called_data = callback.await_args.args
        assert called_command == CommandType.ACTION_TRIGGER
        assert called_data == {
            "action_type": "compositor_dispatch",
            "compositor": "hyprland",
            "dispatcher": "workspace",
            "args": "e+1",
        }
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_run_macro_control_action_exec_sync_wait_id_and_cleanup(self, monkeypatch):
        manager = DeviceManager()
        clock = {"now": 30.0}
        callback = AsyncMock()

        async def cb(command, data):
            await callback(command, data)

        manager.broadcast_callback = cb
        begin_mouse_rel_suppression = Mock()
        end_mouse_rel_suppression = Mock()
        real_loop = asyncio.get_running_loop()

        class _FakeLoop:
            def create_future(self) -> asyncio.Future[int]:
                return real_loop.create_future()

            def time(self) -> float:
                return clock["now"]

        async def fake_sleep(duration: float) -> None:
            return None

        async def fake_wait_for(awaitable, timeout):
            clock["now"] += 0.025
            raise TimeoutError

        monkeypatch.setattr(dm.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(dm.asyncio, "get_running_loop", lambda: _FakeLoop())
        monkeypatch.setattr(dm.asyncio, "wait_for", fake_wait_for)
        monkeypatch.setattr(mdm, "begin_mouse_rel_suppression", begin_mouse_rel_suppression)
        monkeypatch.setattr(mdm, "end_mouse_rel_suppression", end_mouse_rel_suppression)

        result = await _runtime_run_macro_control_action(
            manager,
            {
                "macro_action": "exec_sync",
                "command": "echo hi",
                "inhibit_mouse": True,
                "timeout_ms": 100,
            },
        )

        assert begin_mouse_rel_suppression.called is True
        assert end_mouse_rel_suppression.called is True
        assert manager.macro_state.exec_waiters == {}
        callback.assert_awaited_once()
        assert callback.await_args.args[0] == CommandType.ACTION_TRIGGER
        called_data = callback.await_args.args[1]
        assert called_data["action_type"] == "exec"
        assert called_data["cmd"] == "echo hi"
        assert called_data["macro_exec_timeout_ms"] == 100
        assert called_data["macro_exec_wait_id"]
        assert result == pytest.approx(0.025)


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
