# ruff: noqa: F403, F405, I001
from tests.keymasqd.device_manager_support import *


class _FakeGrabbedRecorder:
    def __init__(self, *, should_record: bool = True) -> None:
        self.is_recording = True
        self.should_record = should_record
        self.calls: list[tuple[str, evdev.InputEvent]] = []
        self.should_record_calls: list[tuple[str, list[str]]] = []

    def should_record_grabbed_event(
        self,
        device_path: str,
        device_types: list[str],
    ) -> bool:
        self.should_record_calls.append((device_path, list(device_types)))
        return self.should_record

    def record_event(self, device_type: str, event: evdev.InputEvent) -> None:
        self.calls.append((device_type, event))


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

    @pytest.mark.asyncio
    async def test_process_grabbed_wheel_event_executes_mapped_action_as_pulse(
        self,
        monkeypatch,
    ) -> None:
        keyboard = _FakeUInput()
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
            keyboard_uinput=keyboard,  # type: ignore[arg-type]
        )
        mapping = {
            "wheel_down": MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
            "wheel_up": MappingAction(action_type=ActionType.KEYBOARD, target="key_b"),
        }
        device.mapping_getter = lambda: mapping  # type: ignore[method-assign]

        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(
                0,
                0,
                evdev.ecodes.EV_REL,
                evdev.ecodes.REL_WHEEL,
                -1,
            ),
        )
        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(
                0,
                0,
                evdev.ecodes.EV_REL,
                evdev.ecodes.REL_WHEEL,
                1,
            ),
        )

        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
        ]

    @pytest.mark.asyncio
    async def test_process_grabbed_wheel_passthrough_and_suppress(
        self,
        monkeypatch,
    ) -> None:
        passthrough = _FakeUInput()
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
        device.uinput = passthrough  # type: ignore[assignment]
        device.mapping_getter = lambda: {  # type: ignore[method-assign]
            "wheel_up": MappingAction(action_type=ActionType.PASSTHROUGH),
            "wheel_down": MappingAction(action_type=ActionType.SUPPRESS),
        }

        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(
                0,
                0,
                evdev.ecodes.EV_REL,
                evdev.ecodes.REL_WHEEL,
                1,
            ),
        )
        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(
                0,
                0,
                evdev.ecodes.EV_REL,
                evdev.ecodes.REL_WHEEL,
                -1,
            ),
        )

        assert passthrough.writes == [(evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL, 1)]

    @pytest.mark.asyncio
    async def test_process_grabbed_wheel_mapping_suppresses_only_matching_high_res_direction(
        self,
        monkeypatch,
    ) -> None:
        passthrough = _FakeUInput()
        keyboard = _FakeUInput()
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
            keyboard_uinput=keyboard,  # type: ignore[arg-type]
        )
        device.uinput = passthrough  # type: ignore[assignment]
        device.mapping_getter = lambda: {  # type: ignore[method-assign]
            "wheel_down": MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
        }

        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(
                0,
                0,
                evdev.ecodes.EV_REL,
                evdev.ecodes.REL_WHEEL_HI_RES,
                -120,
            ),
        )

        assert keyboard.writes == []
        assert passthrough.writes == []

        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(
                0,
                0,
                evdev.ecodes.EV_REL,
                evdev.ecodes.REL_WHEEL_HI_RES,
                120,
            ),
        )

        assert keyboard.writes == []
        assert passthrough.writes == [
            (evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL_HI_RES, 120),
        ]

    @pytest.mark.asyncio
    async def test_process_grabbed_wheel_mapping_records_original_event(
        self,
        monkeypatch,
    ) -> None:
        recorder = _FakeGrabbedRecorder()
        keyboard = _FakeUInput()
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
            device_type=DeviceType.MOUSE,
            keyboard_uinput=keyboard,  # type: ignore[arg-type]
            recording_manager=recorder,
        )
        device.mapping_getter = lambda: {  # type: ignore[method-assign]
            "wheel_up": MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
            "wheel_down": MappingAction(action_type=ActionType.SUPPRESS),
        }

        up_event = evdev.InputEvent(
            0,
            0,
            evdev.ecodes.EV_REL,
            evdev.ecodes.REL_WHEEL,
            1,
        )
        down_event = evdev.InputEvent(
            0,
            1,
            evdev.ecodes.EV_REL,
            evdev.ecodes.REL_WHEEL,
            -1,
        )
        await _runtime_process_grabbed_event(device, up_event)
        await _runtime_process_grabbed_event(device, down_event)

        recorded_events = [
            (device_type, event.code, event.value)
            for device_type, event in recorder.calls
        ]
        assert recorded_events == [
            ("mouse", evdev.ecodes.REL_WHEEL, 1),
            ("mouse", evdev.ecodes.REL_WHEEL, -1),
        ]
        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]

    @pytest.mark.asyncio
    async def test_process_grabbed_mapped_wheel_records_suppressed_high_res_event(
        self,
        monkeypatch,
    ) -> None:
        recorder = _FakeGrabbedRecorder()
        passthrough = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            button_map={"wheel_down": "rel_wheel"},
            button_codes={"wheel_down": evdev.ecodes.REL_WHEEL},
            button_values={"wheel_down": -1},
            device_type=DeviceType.MOUSE,
            recording_manager=recorder,
        )
        device.uinput = passthrough  # type: ignore[assignment]
        device.mapping_getter = lambda: {  # type: ignore[method-assign]
            "wheel_down": MappingAction(action_type=ActionType.SUPPRESS),
        }

        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(
                0,
                0,
                evdev.ecodes.EV_REL,
                evdev.ecodes.REL_WHEEL_HI_RES,
                -120,
            ),
        )

        assert [
            (device_type, event.code, event.value)
            for device_type, event in recorder.calls
        ] == [
            ("mouse", evdev.ecodes.REL_WHEEL_HI_RES, -120),
        ]
        assert passthrough.writes == []

    @pytest.mark.asyncio
    async def test_process_grabbed_wheel_passthrough_records_once(
        self,
        monkeypatch,
    ) -> None:
        recorder = _FakeGrabbedRecorder()
        passthrough = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            button_map={"wheel_up": "rel_wheel"},
            button_codes={"wheel_up": evdev.ecodes.REL_WHEEL},
            button_values={"wheel_up": 1},
            device_type=DeviceType.MOUSE,
            recording_manager=recorder,
        )
        device.uinput = passthrough  # type: ignore[assignment]
        device.mapping_getter = lambda: {  # type: ignore[method-assign]
            "wheel_up": MappingAction(action_type=ActionType.PASSTHROUGH),
        }

        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(
                0,
                0,
                evdev.ecodes.EV_REL,
                evdev.ecodes.REL_WHEEL,
                1,
            ),
        )

        recorded_events = [
            (device_type, event.code, event.value)
            for device_type, event in recorder.calls
        ]
        assert recorded_events == [
            ("mouse", evdev.ecodes.REL_WHEEL, 1),
        ]
        assert passthrough.writes == [(evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL, 1)]

    @pytest.mark.asyncio
    async def test_process_grabbed_high_res_wheel_passthrough_when_unmapped_or_explicit(
        self,
        monkeypatch,
    ) -> None:
        passthrough = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            button_map={"wheel_down": "rel_wheel"},
            button_codes={"wheel_down": evdev.ecodes.REL_WHEEL},
            button_values={"wheel_down": -1},
        )
        device.uinput = passthrough  # type: ignore[assignment]

        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(
                0,
                0,
                evdev.ecodes.EV_REL,
                evdev.ecodes.REL_WHEEL_HI_RES,
                -120,
            ),
        )
        device.mapping_getter = lambda: {  # type: ignore[method-assign]
            "wheel_down": MappingAction(action_type=ActionType.PASSTHROUGH),
        }
        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(
                0,
                0,
                evdev.ecodes.EV_REL,
                evdev.ecodes.REL_WHEEL_HI_RES,
                -120,
            ),
        )

        assert passthrough.writes == [
            (evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL_HI_RES, -120),
            (evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL_HI_RES, -120),
        ]
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

        with caplog.at_level(logging.WARNING, logger="keymasqd.devices"):
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

        with caplog.at_level(logging.WARNING, logger="keymasqd.devices"):
            await gdg.broadcast_grab_status(
                device,
                "waiting",
                ["key_a"],
                waited_s=1.5,
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
        )
        gdg.reconcile_startup_held_action(
            device,
            dm.MappingAction(action_type=ActionType.GAMEPAD, target="btn_south")
            ,
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
    def test_emit_combo_press_reestablishes_passthrough_hold_tracking(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = _make_grabbed_device(monkeypatch)
        passthrough = _FakeUInput()
        device.uinput = passthrough  # type: ignore[assignment]

        device.emit_combo_press("key_b")

        assert passthrough.writes == [(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1)]
        assert device.state.combo_passthrough_held == {"key_b"}
        assert device.state.held_output_keys["passthrough"] == {evdev.ecodes.KEY_B}
    @pytest.mark.asyncio
    async def test_execute_action_covers_synthetic_non_keyboard_branches(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        callback = AsyncMock()
        macro_player = AsyncMock(return_value={"status": "ok"})
        emergency_resetter = AsyncMock(return_value={"status": "ok", "reset": True})
        mouse = _FakeUInput()
        gamepad = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            broadcast_callback=callback,
            macro_player=macro_player,
            emergency_resetter=emergency_resetter,
            mouse_uinput=mouse,  # type: ignore[arg-type]
            gamepad_uinput=gamepad,  # type: ignore[arg-type]
        )
        passthrough_calls: list[tuple[int, int, int]] = []
        emitted_moves: list[tuple[ActionType, int, int]] = []
        fire_tasks: list[asyncio.Task] = []
        monkeypatch.setattr(
            gdr,
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
            dm.MappingAction(action_type=ActionType.MOUSE, target="rel_wheel:1"),
            press,
            "mouse_wheel",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.MOUSE, target="rel_wheel:1"),
            release,
            "mouse_wheel",
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
            dm.MappingAction(action_type=ActionType.EMERGENCY_RESET),
            press,
            "emergency_reset",
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
        rel_wheel_hi_res = evdev.ecodes.REL_WHEEL_HI_RES  # type: ignore[attr-defined]
        assert mouse.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 0),
            (evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL, 1),
            (evdev.ecodes.EV_REL, rel_wheel_hi_res, 120),
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
        emergency_resetter.assert_awaited_once_with()
        assert macro_player.await_args_list[0].kwargs["trigger_value"] == 1
        assert macro_player.await_args_list[1].kwargs["trigger_value"] == 0

    @pytest.mark.asyncio
    async def test_execute_action_mouse_move_abs_uses_cursor_position_setter(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cursor_position_setter = AsyncMock(return_value={"status": "ok"})
        mouse = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            cursor_position_setter=cursor_position_setter,
            mouse_uinput=mouse,  # type: ignore[arg-type]
        )

        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.MOUSE_MOVE_ABS, move_x=10, move_y=20),
            SimpleNamespace(value=1),
            "move_abs",
        )

        cursor_position_setter.assert_awaited_once_with(10, 20)
        assert mouse.writes == []

    @pytest.mark.asyncio
    async def test_rapidfire_mouse_move_abs_uses_cursor_position_setter(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cursor_position_setter = AsyncMock(return_value={"status": "ok"})
        mouse = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            cursor_position_setter=cursor_position_setter,
            mouse_uinput=mouse,  # type: ignore[arg-type]
        )
        device._running = True
        device.state.rapidfire_active["move_abs"] = True
        action = dm.MappingAction(
            action_type=ActionType.MOUSE_MOVE_ABS,
            move_x=10,
            move_y=20,
        )

        task = asyncio.create_task(
            gdr.rapidfire_move(
                device,
                action,
                "move_abs",
                1,
                100,
                asyncio_mod=gdm.ASYNCIO_RUNTIME,
            )
        )
        await asyncio.sleep(0.02)
        device.state.rapidfire_active["move_abs"] = False
        await task

        cursor_position_setter.assert_awaited()
        cursor_position_setter.assert_any_await(10, 20)
        assert mouse.writes == []

    @pytest.mark.asyncio
    async def test_tap_mouse_move_abs_uses_cursor_position_setter(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cursor_position_setter = AsyncMock(return_value={"status": "ok"})
        mouse = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            cursor_position_setter=cursor_position_setter,
            mouse_uinput=mouse,  # type: ignore[arg-type]
        )
        action = dm.MappingAction(
            action_type=ActionType.MOUSE_MOVE_ABS,
            move_x=10,
            move_y=20,
        )

        await _runtime_tap_grabbed_move(device, action, "move_abs", 1)

        cursor_position_setter.assert_awaited_once_with(10, 20)
        assert mouse.writes == []

    @pytest.mark.asyncio
    async def test_execute_action_covers_superkey_and_tap_move_branches(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cursor_position_setter = AsyncMock(return_value={"status": "ok"})
        device = _make_grabbed_device(
            monkeypatch,
            cursor_position_setter=cursor_position_setter,
        )
        move_calls: list[tuple[str, int]] = []
        fake_machine = SimpleNamespace(on_down=AsyncMock(), on_up=AsyncMock())
        created_configs: list[SuperkeyConfig] = []
        created_setters: list[object] = []

        monkeypatch.setattr(
            gda,
            "SuperkeyMachine",
            lambda **kwargs: (
                created_configs.append(kwargs["config"]),
                created_setters.append(kwargs.get("cursor_position_setter")),
                fake_machine,
            )[-1],
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
        assert created_setters == [cursor_position_setter]
        fake_machine.on_down.assert_awaited_once()
        fake_machine.on_up.assert_awaited_once()
        assert move_calls == [("move_btn", 33)]

    @pytest.mark.asyncio
    async def test_execute_action_mouse_wheel_rapidfire_emits_relative_events(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mouse = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            mouse_uinput=mouse,  # type: ignore[arg-type]
        )
        device._running = True

        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(
                action_type=ActionType.MOUSE,
                target="rel_hwheel:-1",
                rapidfire_enabled=True,
                rapidfire_hold_ms=1,
                rapidfire_wait_ms=1,
            ),
            SimpleNamespace(value=1),
            "wheel_rf",
        )
        await asyncio.sleep(0.01)
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(
                action_type=ActionType.MOUSE,
                target="rel_hwheel:-1",
                rapidfire_enabled=True,
                rapidfire_hold_ms=1,
                rapidfire_wait_ms=1,
            ),
            SimpleNamespace(value=0),
            "wheel_rf",
        )

        assert any(
            write == (evdev.ecodes.EV_REL, evdev.ecodes.REL_HWHEEL, -1)
            for write in mouse.writes
        )

    @pytest.mark.asyncio
    async def test_execute_action_mouse_wheel_ignores_repeat_without_rapidfire(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mouse = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            mouse_uinput=mouse,  # type: ignore[arg-type]
        )

        action = dm.MappingAction(
            action_type=ActionType.MOUSE,
            target="rel_wheel:1",
        )

        await _runtime_execute_grabbed_action(
            device,
            action,
            SimpleNamespace(value=1),
            "wheel_plain",
        )
        await _runtime_execute_grabbed_action(
            device,
            action,
            SimpleNamespace(value=2),
            "wheel_plain",
        )
        await _runtime_execute_grabbed_action(
            device,
            action,
            SimpleNamespace(value=0),
            "wheel_plain",
        )

        rel_wheel_hi_res = evdev.ecodes.REL_WHEEL_HI_RES  # type: ignore[attr-defined]
        assert mouse.writes == [
            (evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL, 1),
            (evdev.ecodes.EV_REL, rel_wheel_hi_res, 120),
        ]

    @pytest.mark.asyncio
    async def test_execute_action_mouse_wheel_tap_ignores_repeat(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mouse = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            mouse_uinput=mouse,  # type: ignore[arg-type]
        )

        action = dm.MappingAction(
            action_type=ActionType.MOUSE,
            target="rel_wheel:-1",
            tap_enabled=True,
            tap_hold_ms=1,
        )

        await _runtime_execute_grabbed_action(
            device,
            action,
            SimpleNamespace(value=1),
            "wheel_tap",
        )
        await asyncio.sleep(0)
        await _runtime_execute_grabbed_action(
            device,
            action,
            SimpleNamespace(value=2),
            "wheel_tap",
        )
        await asyncio.sleep(0.01)

        rel_wheel_hi_res = evdev.ecodes.REL_WHEEL_HI_RES  # type: ignore[attr-defined]
        assert mouse.writes == [
            (evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL, -1),
            (evdev.ecodes.EV_REL, rel_wheel_hi_res, -120),
        ]
