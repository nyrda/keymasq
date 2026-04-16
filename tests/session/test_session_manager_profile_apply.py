# ruff: noqa: F403, F405, I001
from tests.session.profile_support import *

@pytest.mark.asyncio
async def test_reevaluate_profiles_skips_unchanged_mapping_and_combos() -> None:
    manager = SessionManager()
    hardware_id = "1234:5678"
    profile = ProfileConfig(
        name="Desktop",
        enabled=True,
        is_permanent=True,
        device_layers={hardware_id: DeviceProfileLayer(hardware_id=hardware_id)},
    )

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
                mappings={
                    "btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_f13")
                },
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
    await session_profiles_module.reevaluate_profiles(manager)

    sent = manager.client.send_command.await_args_list
    assert [call.args[0].command for call in sent] == [
        CommandType.GRAB_DEVICE,
        CommandType.SET_MAPPING,
        CommandType.SET_COMBOS,
    ]


@pytest.mark.asyncio
async def test_apply_resolved_device_profile_uses_extended_grab_timeout() -> None:
    manager = SessionManager()
    hardware_id = "1234:5678"
    resolved = ResolvedDeviceProfile(
        hardware_id=hardware_id,
        active_profile_names=["Desktop"],
        mappings={"btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_f13")},
    )
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        hardware_id=hardware_id,
        name="Test Mouse",
        evdev_devices=[SimpleNamespace(id="mouse", path="/dev/input/event10")],
        buttons=[SimpleNamespace(id="btn_side", evdev="btn_side", source="mouse")],
    )
    manager.client.send_command = AsyncMock(
        side_effect=[
            Response(status="ok", data={"grabbed_count": 1}),
            Response(status="ok", data={"updated": True}),
        ]
    )

    await session_profiles_module.apply_resolved_device_profile(manager, hardware_id, resolved)

    sent = manager.client.send_command.await_args_list
    assert sent[0].args[0].command == CommandType.GRAB_DEVICE
    assert sent[0].kwargs["timeout"] == session_manager_module.GRAB_DEVICE_TIMEOUT_S


@pytest.mark.asyncio
async def test_apply_resolved_device_profile_retries_after_grab_timeout() -> None:
    manager = SessionManager()
    hardware_id = "1234:5678"
    resolved = ResolvedDeviceProfile(
        hardware_id=hardware_id,
        active_profile_names=["Desktop"],
        mappings={"btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_f13")},
    )
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        hardware_id=hardware_id,
        name="Test Mouse",
        evdev_devices=[SimpleNamespace(id="mouse", path="/dev/input/event10")],
        buttons=[SimpleNamespace(id="btn_side", evdev="btn_side", source="mouse")],
    )
    manager.client.send_command = AsyncMock(side_effect=TimeoutError())
    manager.send_notification = Mock()  # type: ignore[method-assign]
    schedule_grab_retry = Mock()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(session_profiles_module, "schedule_grab_retry", schedule_grab_retry)

    try:
        await session_profiles_module.apply_resolved_device_profile(manager, hardware_id, resolved)
    finally:
        monkeypatch.undo()

    manager.send_notification.assert_called_once_with(  # type: ignore[attr-defined]
        "Keymasq: Grab Timed Out",
        (
            "Test Mouse: grab timed out while waiting for keys to be released. "
            "Retrying automatically."
        ),
    )
    schedule_grab_retry.assert_called_once_with(
        manager,
        hardware_id,
        delay_s=session_profiles_module.GRAB_RETRY_DELAY_S,
    )


@pytest.mark.asyncio
async def test_apply_resolved_device_profile_waits_when_daemon_reports_no_live_interfaces() -> None:
    manager = SessionManager()
    hardware_id = "1234:5678"
    resolved = ResolvedDeviceProfile(
        hardware_id=hardware_id,
        active_profile_names=["Desktop"],
        mappings={"btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_f13")},
    )
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        hardware_id=hardware_id,
        name="Test Mouse",
        evdev_devices=[SimpleNamespace(id="mouse", path="/dev/input/by-id/test-mouse")],
        buttons=[SimpleNamespace(id="btn_side", evdev="btn_side", source="mouse")],
    )
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"grabbed_count": 0, "waiting_for_device": True})
    )

    await session_profiles_module.apply_resolved_device_profile(manager, hardware_id, resolved)

    manager.client.send_command.assert_awaited_once()
    assert hardware_id not in manager.profile_state.grabbed_devices
    assert hardware_id not in manager.profile_state.grabbed_interfaces
    assert manager.profile_state.last_sent_mapping_signatures == {}


@pytest.mark.asyncio
async def test_apply_resolved_device_profile_skips_same_interface_noop_without_mapping_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SessionManager()
    hardware_id = "1234:5678"
    resolved = ResolvedDeviceProfile(
        hardware_id=hardware_id,
        active_profile_names=["Desktop"],
        mappings={"btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_f13")},
    )
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        hardware_id=hardware_id,
        name="Test Mouse",
        evdev_devices=[SimpleNamespace(id="mouse", path="/dev/input/event10")],
        buttons=[SimpleNamespace(id="btn_side", evdev="btn_side", source="mouse")],
    )
    manager.profile_state.resolved_devices[hardware_id] = ResolvedDeviceProfile(
        hardware_id=hardware_id,
        active_profile_names=["Desktop"],
        mappings=dict(resolved.mappings),
    )
    manager.profile_state.grabbed_devices.add(hardware_id)
    manager.profile_state.grabbed_interfaces[hardware_id] = {"mouse": "/dev/input/event10"}
    manager.profile_state.last_sent_mapping_signatures[
        hardware_id
    ] = session_payloads_module.resolved_mapping_signature(manager, resolved, hardware_id)
    update_mapping = AsyncMock(return_value=True)
    maybe_notify = Mock()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(session_profiles_module, "update_mapping", update_mapping)
    monkeypatch.setattr(session_profiles_module, "maybe_notify_profile_activation", maybe_notify)

    try:
        with caplog.at_level("INFO", logger="keymasq-session"):
            await session_profiles_module.apply_resolved_device_profile(
                manager,
                hardware_id,
                resolved,
            )
    finally:
        monkeypatch.undo()

    update_mapping.assert_not_awaited()
    maybe_notify.assert_called_once_with(manager, "Test Mouse", ["Desktop"], resolved)
    assert "Same interfaces for 1234:5678, updating mapping only" not in caplog.text


@pytest.mark.asyncio
async def test_apply_resolved_device_profile_skips_profile_only_change_without_mapping_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SessionManager()
    hardware_id = "1234:5678"
    resolved = ResolvedDeviceProfile(
        hardware_id=hardware_id,
        active_profile_names=["Desktop", "Games"],
        mappings={"btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_f13")},
        notify_profiles=["Games"],
    )
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        hardware_id=hardware_id,
        name="Test Mouse",
        evdev_devices=[SimpleNamespace(id="mouse", path="/dev/input/event10")],
        buttons=[SimpleNamespace(id="btn_side", evdev="btn_side", source="mouse")],
    )
    manager.profile_state.resolved_devices[hardware_id] = ResolvedDeviceProfile(
        hardware_id=hardware_id,
        active_profile_names=["Desktop"],
        mappings=dict(resolved.mappings),
    )
    manager.profile_state.grabbed_devices.add(hardware_id)
    manager.profile_state.grabbed_interfaces[hardware_id] = {"mouse": "/dev/input/event10"}
    manager.profile_state.last_sent_mapping_signatures[
        hardware_id
    ] = session_payloads_module.resolved_mapping_signature(manager, resolved, hardware_id)
    update_mapping = AsyncMock(return_value=True)
    maybe_notify = Mock()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(session_profiles_module, "update_mapping", update_mapping)
    monkeypatch.setattr(session_profiles_module, "maybe_notify_profile_activation", maybe_notify)

    try:
        with caplog.at_level("INFO", logger="keymasq-session"):
            await session_profiles_module.apply_resolved_device_profile(
                manager,
                hardware_id,
                resolved,
            )
    finally:
        monkeypatch.undo()

    update_mapping.assert_not_awaited()
    maybe_notify.assert_called_once_with(manager, "Test Mouse", ["Desktop"], resolved)
    assert "Same interfaces for 1234:5678, updating mapping only" not in caplog.text


