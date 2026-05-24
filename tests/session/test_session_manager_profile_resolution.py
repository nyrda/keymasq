# ruff: noqa: F403, F405, I001
from tests.session.profile_support import *

@pytest.mark.asyncio
async def test_window_churn_conflict_then_fallback_keeps_deterministic_active_profile() -> None:
    manager = SessionManager()
    hardware_id = "1234:5678"

    profile_game = ProfileConfig(
        name="Game",
        enabled=True,
        device_layers={hardware_id: DeviceProfileLayer(hardware_id=hardware_id)},
    )
    profile_base = ProfileConfig(
        name="Base",
        enabled=True,
        is_permanent=True,
        device_layers={hardware_id: DeviceProfileLayer(hardware_id=hardware_id)},
    )

    manager.hardware.list_hardware_ids = lambda: [hardware_id]  # type: ignore[assignment]

    def resolve_active_profiles(
        window_info: dict | None,
        _caps: list[str],
        hardware_ids: list[str],
        *,
        runtime_profile_names: list[str] | None = None,
    ) -> ResolvedProfiles:
        assert runtime_profile_names in (None, [])
        assert hardware_ids == [hardware_id]
        title = str((window_info or {}).get("title", ""))
        if title == "game":
            return ResolvedProfiles(
                active_profiles=[profile_game],
                devices={
                    hardware_id: ResolvedDeviceProfile(
                        hardware_id=hardware_id,
                        active_profile_names=["Game"],
                        mappings={},
                    )
                },
            )
        return ResolvedProfiles(
            active_profiles=[profile_base],
            devices={
                hardware_id: ResolvedDeviceProfile(
                    hardware_id=hardware_id,
                    active_profile_names=["Base"],
                    mappings={},
                )
            },
        )

    manager.profiles.resolve_active_profiles = resolve_active_profiles  # type: ignore[assignment]

    actions: list[tuple[str, str]] = []

    async def apply_resolved_device_profile(
        _manager: SessionManager,
        hwid: str,
        resolved: ResolvedDeviceProfile,
        **_kwargs: object,
    ) -> None:
        actions.append(
            (
                "activate",
                resolved.active_profile_names[-1] if resolved.active_profile_names else "",
            )
        )
        manager.profile_state.resolved_devices[hwid] = resolved

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        session_profiles_module,
        "apply_resolved_device_profile",
        apply_resolved_device_profile,
    )
    monkeypatch.setattr(session_profiles_module, "update_combos", AsyncMock())
    manager.send_notification = lambda _title, _message: None  # type: ignore[method-assign]

    try:
        await manager.on_window_change("app", "game", [])
        await manager.on_window_change("app", "browser", [])
    finally:
        monkeypatch.undo()

    assert actions == [
        ("activate", "Game"),
        ("activate", "Base"),
    ]
    assert manager.profile_state.resolved_devices[hardware_id].active_profile_names == ["Base"]


@pytest.mark.asyncio
async def test_profile_reevaluation_interrupts_stale_apply() -> None:
    manager = SessionManager()
    hardware_id = "1234:5678"
    profile_game = ProfileConfig(
        name="Game",
        enabled=True,
        device_layers={hardware_id: DeviceProfileLayer(hardware_id=hardware_id)},
    )
    profile_base = ProfileConfig(
        name="Base",
        enabled=True,
        is_permanent=True,
        device_layers={hardware_id: DeviceProfileLayer(hardware_id=hardware_id)},
    )
    manager.hardware.list_hardware_ids = lambda: [hardware_id]  # type: ignore[assignment]
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]

    def resolve_active_profiles(
        window_info: dict | None,
        _caps: list[str],
        hardware_ids: list[str],
        *,
        runtime_profile_names: list[str] | None = None,
    ) -> ResolvedProfiles:
        assert runtime_profile_names in (None, [])
        assert hardware_ids == [hardware_id]
        title = str((window_info or {}).get("title", ""))
        profile = profile_game if title == "game" else profile_base
        return ResolvedProfiles(
            active_profiles=[profile],
            devices={
                hardware_id: ResolvedDeviceProfile(
                    hardware_id=hardware_id,
                    active_profile_names=[profile.name],
                    mappings={},
                )
            },
        )

    manager.profiles.resolve_active_profiles = resolve_active_profiles  # type: ignore[assignment]
    game_started = asyncio.Event()
    release_game = asyncio.Event()

    async def apply_resolved_device_profile(
        _manager: SessionManager,
        hwid: str,
        resolved: ResolvedDeviceProfile,
        **_kwargs: object,
    ) -> None:
        if resolved.active_profile_names == ["Game"]:
            game_started.set()
            await release_game.wait()
        manager.profile_state.resolved_devices[hwid] = resolved

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        session_profiles_module,
        "apply_resolved_device_profile",
        apply_resolved_device_profile,
    )
    monkeypatch.setattr(session_profiles_module, "update_combos", AsyncMock())

    try:
        manager.compositor_state.current_window = {"title": "game"}
        stale_task = asyncio.create_task(session_profiles_module.reevaluate_profiles(manager))
        await asyncio.wait_for(game_started.wait(), timeout=1.0)

        manager.compositor_state.current_window = {"title": "browser"}
        await session_profiles_module.reevaluate_profiles(manager)
        release_game.set()
        await stale_task
    finally:
        monkeypatch.undo()

    assert manager.profile_state.resolved_devices[hardware_id].active_profile_names == ["Base"]


@pytest.mark.asyncio
async def test_topology_refresh_retries_after_reevaluate_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SessionManager()
    sleep_calls: list[float] = []
    reevaluate_profiles = AsyncMock(side_effect=[RuntimeError("refresh boom"), None])
    monkeypatch.setattr(session_profiles_module, "reevaluate_profiles", reevaluate_profiles)

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        return

    monkeypatch.setattr(session_profiles_module.asyncio, "sleep", fake_sleep)

    with caplog.at_level("WARNING", logger="keymasq-session"):
        session_profiles_module.schedule_topology_refresh(
            manager,
            session_manager_module.TOPOLOGY_REFRESH_DEBOUNCE_S,
            session_manager_module.TOPOLOGY_REFRESH_RETRY_S,
        )
        task = manager.profile_state.topology_refresh_task
        assert task is not None
        await task

    assert sleep_calls == [
        session_manager_module.TOPOLOGY_REFRESH_DEBOUNCE_S,
        session_manager_module.TOPOLOGY_REFRESH_RETRY_S,
    ]
    assert "Topology refresh failed: refresh boom" in caplog.text
    assert reevaluate_profiles.await_count == 2
    assert manager.profile_state.topology_refresh_task is None


@pytest.mark.asyncio
async def test_reevaluate_profiles_sends_combo_payload_and_forces_combo_grab() -> None:
    manager = SessionManager()
    hardware_id = "1234:5678"
    profile = ProfileConfig(name="Desktop", enabled=True, is_permanent=True)

    manager.hardware.list_hardware_ids = lambda: [hardware_id]  # type: ignore[assignment]
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        hardware_id=hardware_id,
        name="Test Mouse",
        evdev_devices=[SimpleNamespace(id="mouse", path="/dev/input/event10")],
        buttons=[SimpleNamespace(id="btn_side", evdev="btn_side", source="mouse")],
    )
    manager.profiles.resolve_active_profiles = lambda *_args, **_kwargs: ResolvedProfiles(  # type: ignore[assignment]
        active_profiles=[profile],
        devices={
            hardware_id: ResolvedDeviceProfile(
                hardware_id=hardware_id,
                active_profile_names=["Desktop"],
                combo_event_count=1,
                combo_sources={"mouse"},
            )
        },
        combos=[
            ResolvedCombo(
                id="combo-1",
                name="Quick Toggle",
                steps=[
                    ComboStep(
                        events=[
                            ComboEvent(
                                hardware_id=hardware_id,
                                source="mouse",
                                evdev="btn_side",
                            )
                        ],
                        timeout_ms=750,
                    )
                ],
                action=MappingAction(
                    action_type=ActionType.PROFILE_TOGGLE,
                    profile_name="Gaming",
                ),
                profile_name="Desktop",
            )
        ],
    )
    manager.client.send_command = AsyncMock(
        side_effect=[
            Response(status="ok", data={"grabbed_count": 1}),
            Response(status="ok", data={"updated": True}),
            Response(status="ok", data={"updated": True, "combo_count": 1}),
        ]
    )

    await session_profiles_module.reevaluate_profiles(manager)

    sent = manager.client.send_command.await_args_list
    assert [call.args[0].command for call in sent] == [
        CommandType.GRAB_DEVICE,
        CommandType.SET_MAPPING,
        CommandType.SET_COMBOS,
    ]
    assert sent[0].args[0].data["force_grab_unmapped"] is True
    assert sent[2].args[0].data["combos"][0]["action"]["profile_name"] == "Gaming"
    assert sent[2].args[0].data["combos"][0]["steps"][0]["timeout_ms"] == 750


@pytest.mark.asyncio
async def test_reevaluate_profiles_keeps_numbered_duplicate_hardware_separate() -> None:
    manager = SessionManager()
    left_id = "cafe:00dd"
    right_id = "cafe:00dd@2"
    profile = ProfileConfig(name="Desktop", enabled=True, is_permanent=True)
    hardware_by_id = {
        left_id: SimpleNamespace(
            hardware_id=left_id,
            name="Duplicate Keyboard 1",
            evdev_devices=[SimpleNamespace(id="kbd", path="/dev/input/event-left")],
            buttons=[SimpleNamespace(id="key_l", evdev="key_l", source="kbd")],
        ),
        right_id: SimpleNamespace(
            hardware_id=right_id,
            name="Duplicate Keyboard 2",
            evdev_devices=[SimpleNamespace(id="kbd", path="/dev/input/event-right")],
            buttons=[SimpleNamespace(id="key_l", evdev="key_l", source="kbd")],
        ),
    }

    manager.hardware.list_hardware_ids = lambda: [left_id, right_id]  # type: ignore[assignment]
    manager.hardware.get_hardware = lambda hardware_id: hardware_by_id.get(hardware_id)  # type: ignore[assignment]
    manager.profiles.resolve_active_profiles = lambda *_args, **_kwargs: ResolvedProfiles(  # type: ignore[assignment]
        active_profiles=[profile],
        devices={
            left_id: ResolvedDeviceProfile(
                hardware_id=left_id,
                active_profile_names=["Desktop"],
                mappings={
                    "key_l": MappingAction(action_type=ActionType.KEYBOARD, target="key_1")
                },
            ),
            right_id: ResolvedDeviceProfile(
                hardware_id=right_id,
                active_profile_names=["Desktop"],
                mappings={
                    "key_l": MappingAction(action_type=ActionType.KEYBOARD, target="key_2")
                },
            ),
        },
        combos=[],
    )
    manager.client.send_command = AsyncMock(
        side_effect=[
            Response(status="ok", data={"grabbed_count": 1}),
            Response(status="ok", data={"updated": True}),
            Response(status="ok", data={"grabbed_count": 1}),
            Response(status="ok", data={"updated": True}),
            Response(status="ok", data={"updated": True, "combo_count": 0}),
        ]
    )

    await session_profiles_module.reevaluate_profiles(manager)

    sent = manager.client.send_command.await_args_list
    assert [call.args[0].command for call in sent[:4]] == [
        CommandType.GRAB_DEVICE,
        CommandType.SET_MAPPING,
        CommandType.GRAB_DEVICE,
        CommandType.SET_MAPPING,
    ]
    left_grab = sent[0].args[0].data
    right_grab = sent[2].args[0].data
    assert left_grab["hardware_id"] == left_id
    assert left_grab["evdev_paths"] == ["/dev/input/event-left"]
    assert right_grab["hardware_id"] == right_id
    assert right_grab["evdev_paths"] == ["/dev/input/event-right"]
    assert sent[1].args[0].data["hardware_id"] == left_id
    assert sent[1].args[0].data["mapping"]["key_l"]["target"] == "key_1"
    assert sent[3].args[0].data["hardware_id"] == right_id
    assert sent[3].args[0].data["mapping"]["key_l"]["target"] == "key_2"


def test_resolved_combo_signature_changes_when_superkey_definition_changes() -> None:
    manager = SessionManager()
    combo_action = MappingAction(action_type=ActionType.SUPERKEY, superkey_name="combo-superkey")
    combos = [
        ResolvedCombo(
            id="combo-1",
            name="Combo Superkey",
            steps=[
                ComboStep(events=[ComboEvent(hardware_id="1234:5678", source="kbd", evdev="key_a")])
            ],
            action=combo_action,
            profile_name="Desktop",
        )
    ]
    base_superkey = SuperkeyConfig(
        name="combo-superkey",
        tap_actions=[SuperkeyAction(action_type=ActionType.KEYBOARD, target="key_a")],
    )
    updated_superkey = SuperkeyConfig(
        name="combo-superkey",
        tap_actions=[SuperkeyAction(action_type=ActionType.KEYBOARD, target="key_b")],
    )

    manager.superkeys = SimpleNamespace(get_superkey=lambda _name: base_superkey)  # type: ignore[assignment]
    base_signature = session_payloads_module.resolved_combos_signature(manager, combos)

    manager.superkeys = SimpleNamespace(get_superkey=lambda _name: updated_superkey)  # type: ignore[assignment]
    updated_signature = session_payloads_module.resolved_combos_signature(manager, combos)

    assert base_signature != updated_signature


def test_resolved_combo_signature_and_payload_include_trigger_recall_settings() -> None:
    manager = SessionManager()
    combos = [
        ResolvedCombo(
            id="combo-1",
            name="Recall Combo",
            steps=[
                ComboStep(
                    events=[
                        ComboEvent(hardware_id="1234:5678", source="kbd", evdev="meta"),
                        ComboEvent(hardware_id="1234:5678", source="kbd", evdev="key_c"),
                    ]
                )
            ],
            action=MappingAction(action_type=ActionType.SUPPRESS),
            profile_name="Desktop",
            recall_trigger_keys=True,
            restore_trigger_keys=["meta"],
        )
    ]

    signature = session_payloads_module.resolved_combos_signature(manager, combos)
    payload = session_payloads_module.resolved_combos_payload(manager, combos)

    assert '"recall_trigger_keys":true' in signature
    assert '"restore_trigger_keys":["meta"]' in signature
    assert payload[0]["recall_trigger_keys"] is True
    assert payload[0]["restore_trigger_keys"] == ["meta"]


@pytest.mark.asyncio
async def test_reevaluate_profiles_broadcast_omits_window_state() -> None:
    manager = SessionManager()
    hardware_id = "1234:5678"
    profile = ProfileConfig(
        name="Desktop",
        enabled=True,
        is_permanent=True,
        device_layers={hardware_id: DeviceProfileLayer(hardware_id=hardware_id)},
    )
    manager.compositor_state.current_window = {
        "class": "steam",
        "title": "Counter-Strike 2",
        "tags": ["game"],
    }
    manager.hardware.list_hardware_ids = lambda: []  # type: ignore[assignment]
    manager.profiles.resolve_active_profiles = lambda *_args, **_kwargs: ResolvedProfiles(  # type: ignore[assignment]
        active_profiles=[profile],
        devices={},
        combos=[],
    )
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]

    await session_profiles_module.reevaluate_profiles(manager)

    manager.broadcast_to_session_clients.assert_called_once()  # type: ignore[attr-defined]
    payload = manager.broadcast_to_session_clients.call_args.args[0]  # type: ignore[attr-defined]
    assert payload["event"] == "profiles_changed"
    assert payload["active_profiles"] == ["Desktop"]
    assert "devices" in payload
    assert "window" not in payload


@pytest.mark.asyncio
async def test_reevaluate_profiles_releases_removed_hardware_config() -> None:
    manager = SessionManager()
    hardware_id = "045e:02a1"
    manager.hardware.list_hardware_ids = lambda: []  # type: ignore[assignment]
    manager.profiles.resolve_active_profiles = lambda *_args, **_kwargs: ResolvedProfiles(  # type: ignore[assignment]
        active_profiles=[],
        devices={},
        combos=[],
    )
    manager.profile_state.resolved_devices[hardware_id] = ResolvedDeviceProfile(
        hardware_id=hardware_id
    )
    manager.profile_state.grabbed_devices.add(hardware_id)
    manager.profile_state.grabbed_interfaces[hardware_id] = {
        "gamepad": "/dev/input/event20"
    }
    manager.profile_state.last_sent_grab_signatures[hardware_id] = "grab"
    manager.profile_state.last_sent_mapping_signatures[hardware_id] = "mapping"
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"released": True})
    )

    await session_profiles_module.reevaluate_profiles(manager)

    sent = next(
        call.args[0]
        for call in manager.client.send_command.await_args_list
        if call.args[0].command == CommandType.RELEASE_DEVICE
    )
    assert sent.command == CommandType.RELEASE_DEVICE
    assert sent.data == {"hardware_id": hardware_id, "immediate": True}
    assert hardware_id not in manager.profile_state.resolved_devices
    assert hardware_id not in manager.profile_state.grabbed_devices
    assert hardware_id not in manager.profile_state.grabbed_interfaces
    assert hardware_id not in manager.profile_state.last_sent_grab_signatures
    assert hardware_id not in manager.profile_state.last_sent_mapping_signatures


@pytest.mark.asyncio
async def test_reevaluate_profiles_releases_removed_hardware_without_resolved_cache() -> None:
    manager = SessionManager()
    hardware_id = "045e:02a1"
    manager.hardware.list_hardware_ids = lambda: []  # type: ignore[assignment]
    manager.profiles.resolve_active_profiles = lambda *_args, **_kwargs: ResolvedProfiles(  # type: ignore[assignment]
        active_profiles=[],
        devices={},
        combos=[],
    )
    manager.profile_state.grabbed_devices.add(hardware_id)
    manager.profile_state.grabbed_interfaces[hardware_id] = {
        "gamepad": "/dev/input/event20"
    }
    manager.profile_state.grab_waiting_devices.add(hardware_id)
    manager.profile_state.last_sent_grab_signatures[hardware_id] = "grab"
    manager.profile_state.last_sent_mapping_signatures[hardware_id] = "mapping"
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"released": True})
    )

    await session_profiles_module.reevaluate_profiles(manager)

    sent = next(
        call.args[0]
        for call in manager.client.send_command.await_args_list
        if call.args[0].command == CommandType.RELEASE_DEVICE
    )
    assert sent.data == {"hardware_id": hardware_id, "immediate": True}
    assert hardware_id not in manager.profile_state.grabbed_devices
    assert hardware_id not in manager.profile_state.grabbed_interfaces
    assert hardware_id not in manager.profile_state.grab_waiting_devices
    assert hardware_id not in manager.profile_state.last_sent_grab_signatures
    assert hardware_id not in manager.profile_state.last_sent_mapping_signatures


@pytest.mark.asyncio
async def test_reevaluate_profiles_plays_lifecycle_macros_once_per_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    desktop = ProfileConfig(
        name="Desktop",
        enabled=True,
        is_permanent=True,
        activation_macro_name="desktop_enter",
        deactivation_macro_name="desktop_leave",
    )
    gaming = ProfileConfig(
        name="Gaming",
        enabled=True,
        activation_macro_name="gaming_enter",
        deactivation_macro_name="gaming_leave",
    )
    profiles_by_name = {
        desktop.name: SimpleNamespace(config=desktop),
        gaming.name: SimpleNamespace(config=gaming),
    }
    resolved_profiles = [
        ResolvedProfiles(active_profiles=[desktop], devices={}, combos=[]),
        ResolvedProfiles(active_profiles=[desktop], devices={}, combos=[]),
        ResolvedProfiles(active_profiles=[gaming], devices={}, combos=[]),
        ResolvedProfiles(active_profiles=[], devices={}, combos=[]),
    ]

    manager.hardware.list_hardware_ids = lambda: []  # type: ignore[assignment]
    manager.profiles.get_profile = lambda name: profiles_by_name.get(name)  # type: ignore[assignment]
    manager.profiles.resolve_active_profiles = Mock(  # type: ignore[assignment]
        side_effect=resolved_profiles
    )
    manager.client.send_command = AsyncMock(return_value=Response(status="ok", data={}))
    monkeypatch.setattr(session_profiles_module, "update_combos", AsyncMock())

    for _ in resolved_profiles:
        await session_profiles_module.reevaluate_profiles(manager)

    sent = manager.client.send_command.await_args_list
    assert [call.args[0].command for call in sent] == [
        CommandType.MACRO_PLAY_BY_NAME,
        CommandType.MACRO_PLAY_BY_NAME,
        CommandType.MACRO_PLAY_BY_NAME,
        CommandType.MACRO_PLAY_BY_NAME,
    ]
    assert [call.args[0].data["name"] for call in sent] == [
        "desktop_enter",
        "desktop_leave",
        "gaming_enter",
        "gaming_leave",
    ]
