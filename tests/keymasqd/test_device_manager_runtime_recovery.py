# ruff: noqa: F403, F405, I001
from tests.keymasqd.device_manager_support import *

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
                deps=gde.build_action_execution_deps(
                    fire_and_observe_fn=lambda coro, _label: asyncio.create_task(coro)
                ),
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

    @pytest.mark.asyncio
    async def test_event_processing_error_resets_analog_controls(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=_FakeUInput(),  # type: ignore[arg-type]
        )
        reset_analog_controls = AsyncMock()
        monkeypatch.setattr(device, "reset_analog_controls", reset_analog_controls)

        await _runtime_recover_grabbed_event_processing_error(device)

        reset_analog_controls.assert_awaited_once()

class TestDeviceManagerHelpers:
    def test_create_global_uinputs_uses_explicit_test_identities(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KEYMASQ_TEST_UINPUT", "1")
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
        assert created[0].kwargs["name"] == "keymasq-test-keyboard"
        assert created[0].kwargs["vendor"] == 0x4B46
        assert created[0].kwargs["product"] == 0x1001
        assert created[1].kwargs["name"] == "keymasq-test-mouse"
        assert created[1].kwargs["vendor"] == 0x4B46
        assert created[1].kwargs["product"] == 0x1002
        mouse_rel_caps = created[1].kwargs["events"][evdev.ecodes.EV_REL]
        assert evdev.ecodes.REL_WHEEL in mouse_rel_caps
        rel_wheel_hi_res = getattr(evdev.ecodes, "REL_WHEEL_HI_RES", None)
        if rel_wheel_hi_res is not None:
            assert int(rel_wheel_hi_res) in mouse_rel_caps
        assert created[2].kwargs["name"] == "keymasq-test-gamepad"
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
    def test_parse_action_supports_string_and_compositor_dispatch(self) -> None:
        manager = DeviceManager()

        string_action = _runtime_parse_action(manager, "key_a")
        dispatch_action = _runtime_parse_action(
            manager,
            {
                "action": "compositor_dispatch",
                "compositor": "hyprland",
                "dispatcher": "workspace",
                "args": "2",
            },
        )

        assert string_action.action_type == ActionType.KEYBOARD
        assert string_action.target == "key_a"
        assert dispatch_action.action_type == ActionType.COMPOSITOR_DISPATCH
        assert dispatch_action.compositor_id == "hyprland"
        assert dispatch_action.compositor_dispatcher == "workspace"
        assert dispatch_action.compositor_args == "2"

    def test_parse_action_warns_and_strips_unsupported_rapidfire(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        manager = DeviceManager()

        with caplog.at_level("WARNING", logger="keymasqd.runtime.actions"):
            action = _runtime_parse_action(
                manager,
                {
                    "action": "exec",
                    "cmd": "echo hi",
                    "rapidfire_enabled": True,
                    "rapidfire_hold_ms": 40,
                    "rapidfire_wait_ms": 60,
                },
            )

        assert action.action_type == ActionType.EXEC
        assert action.rapidfire_enabled is False
        assert action.rapidfire_hold_ms == 20
        assert action.rapidfire_wait_ms == 20
        assert "Ignoring rapidfire for unsupported exec action in runtime payload" in caplog.text
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

        with caplog.at_level(logging.WARNING, logger="keymasqd.devices"):
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

        monkeypatch.setattr(cdm.time, "monotonic", lambda: 10.0)
        monkeypatch.setattr(dm.asyncio, "sleep", AsyncMock())

        task = asyncio.create_task(_runtime_combo_timeout_watchdog(manager, 10.5))
        manager.combo_state.timeout_task = task
        await task

        manager.combo_state.engine.expire_timeouts.assert_called_once_with(10.0)
        assert manager.combo_state.timeout_task is None
        assert refreshes == ["refresh"]
