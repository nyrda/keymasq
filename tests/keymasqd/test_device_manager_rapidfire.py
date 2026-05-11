# ruff: noqa: F403, F405, I001
from tests.keymasqd.device_manager_support import *

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

        assert wait_timeouts == [pytest.approx(gdm.ACTIVE_KEY_IDLE_LOG_INTERVAL_S)]
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

        with caplog.at_level(logging.INFO, logger="keymasqd.devices"):
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

        with caplog.at_level(logging.WARNING, logger="keymasqd.devices"):
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

        with caplog.at_level(logging.ERROR, logger="keymasqd.devices"):
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
        monkeypatch.setenv("KEYMASQ_TEST_UINPUT", "1")
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
        assert device.uinput.kwargs["name"] == "keymasq-test-passthrough-1234:5678"
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
