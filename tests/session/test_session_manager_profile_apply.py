import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import keymasq.session.manager as session_manager_module
import keymasq.session.manager.payloads as session_payloads_module
import keymasq.session.manager.profiles as session_profiles_module
from keymasq.common.ipc import CommandType, Response
from keymasq.common.models import (
    ActionType,
    ComboEvent,
    ComboStep,
    DeviceProfileLayer,
    MappingAction,
    ProfileConfig,
)
from keymasq.session.manager import SessionManager
from keymasq.session.profiles import ResolvedCombo, ResolvedDeviceProfile, ResolvedProfiles


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
    assert [combo.id for combo in manager.profile_state.resolved_combos] == ["combo-1"]


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
async def test_apply_resolved_device_profile_force_grabs_all_interfaces_for_inspector() -> None:
    from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig

    manager = SessionManager()
    hardware_id = "1234:5678"
    resolved = ResolvedDeviceProfile(hardware_id=hardware_id)
    manager.device_inspector_state.active_hardware_ids.add(hardware_id)
    manager.hardware.get_hardware = lambda _hardware_id: HardwareConfig(  # type: ignore[assignment]
        vendor_id="1234",
        product_id="5678",
        name="Split Pad",
        evdev_devices=[
            EvdevDevice(
                path="/dev/input/event10",
                device_type=DeviceType.GAMEPAD,
                id="buttons",
            ),
            EvdevDevice(
                path="/dev/input/event11",
                device_type=DeviceType.GAMEPAD,
                id="axes",
            ),
        ],
        buttons=[
            ButtonDefinition(
                id="btn_south",
                label="A",
                evdev="btn_south",
                evdev_code=304,
                source="buttons",
            )
        ],
    )
    manager.client.send_command = AsyncMock(
        side_effect=[
            Response(status="ok", data={"grabbed_count": 2}),
            Response(status="ok", data={"updated": True}),
        ]
    )

    await session_profiles_module.apply_resolved_device_profile(manager, hardware_id, resolved)

    sent = manager.client.send_command.await_args_list
    assert [call.args[0].command for call in sent] == [
        CommandType.GRAB_DEVICE,
        CommandType.SET_MAPPING,
    ]
    grab_data = sent[0].args[0].data
    assert grab_data["evdev_paths"] == ["/dev/input/event10", "/dev/input/event11"]
    assert grab_data["evdev_interfaces"] == [
        {
            "id": "buttons",
            "path": "/dev/input/event10",
            "type": "gamepad",
            "phys": "",
            "capabilities": [],
        },
        {
            "id": "axes",
            "path": "/dev/input/event11",
            "type": "gamepad",
            "phys": "",
            "capabilities": [],
        },
    ]
    assert grab_data["force_grab_unmapped"] is True
    assert manager.profile_state.grabbed_interfaces[hardware_id] == {
        "buttons": "/dev/input/event10",
        "axes": "/dev/input/event11",
    }


def test_grab_device_payload_signature_includes_interface_descriptors() -> None:
    payload = {
        "evdev_paths": ["keymasq:2dc8:3106"],
        "evdev_interfaces": [
            {
                "id": "gamepad",
                "path": "keymasq:2dc8:3106",
                "type": "gamepad",
                "capabilities": ["btn_south"],
            }
        ],
    }
    changed_payload = {
        **payload,
        "evdev_interfaces": [
            {
                "id": "gamepad_2",
                "path": "keymasq:2dc8:3106",
                "type": "gamepad",
                "capabilities": ["btn_south"],
            }
        ],
    }

    assert session_profiles_module.grab_device_payload_signature(
        payload
    ) != session_profiles_module.grab_device_payload_signature(changed_payload)


def test_grab_device_payload_signature_normalizes_interface_order() -> None:
    first = {
        "evdev_paths": ["keymasq:2dc8:3106"],
        "evdev_interfaces": [
            {"id": "buttons", "path": "keymasq:2dc8:3106", "type": "gamepad"},
            {"id": "axes", "path": "keymasq:2dc8:3106", "type": "gamepad"},
        ],
    }
    second = {
        **first,
        "evdev_interfaces": list(reversed(first["evdev_interfaces"])),
    }

    assert session_profiles_module.grab_device_payload_signature(
        first
    ) == session_profiles_module.grab_device_payload_signature(second)


def test_grab_device_payload_signature_normalizes_interface_capability_order() -> None:
    interface = {
        "id": "gamepad",
        "path": "keymasq:2dc8:3106",
        "type": "gamepad",
        "capabilities": ["btn_south", "btn_east"],
    }
    first = {
        "evdev_paths": ["keymasq:2dc8:3106"],
        "evdev_interfaces": [interface],
    }
    second = {
        **first,
        "evdev_interfaces": [
            {
                **interface,
                "capabilities": ["btn_east", "btn_south"],
            }
        ],
    }

    assert session_profiles_module.grab_device_payload_signature(
        first
    ) == session_profiles_module.grab_device_payload_signature(second)


@pytest.mark.asyncio
async def test_apply_resolved_device_profile_retries_after_grab_timeout(
    monkeypatch: pytest.MonkeyPatch,
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
    manager.client.send_command = AsyncMock(side_effect=TimeoutError())
    manager.send_notification = Mock()  # type: ignore[method-assign]
    schedule_grab_retry = Mock()
    monkeypatch.setattr(session_profiles_module, "schedule_grab_retry", schedule_grab_retry)

    await session_profiles_module.apply_resolved_device_profile(manager, hardware_id, resolved)

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
    assert hardware_id in manager.profile_state.grab_waiting_devices
    assert hardware_id in manager.profile_state.last_sent_grab_signatures
    assert manager.profile_state.last_sent_mapping_signatures == {}


@pytest.mark.asyncio
async def test_apply_resolved_device_profile_skips_unchanged_waiting_device() -> None:
    manager = SessionManager()
    hardware_id = "1234:5678@2"
    resolved = ResolvedDeviceProfile(
        hardware_id=hardware_id,
        active_profile_names=["Desktop"],
        mappings={"btn_south": MappingAction(action_type=ActionType.KEYBOARD, target="key_f13")},
    )
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        hardware_id=hardware_id,
        name="Test Pad 2",
        evdev_devices=[
            SimpleNamespace(
                id="gamepad",
                path="keymasq:1234:5678",
                device_type=SimpleNamespace(value="gamepad"),
                phys="",
                capabilities=[],
            )
        ],
        buttons=[
            SimpleNamespace(
                id="btn_south",
                evdev="btn_south",
                source="gamepad",
                evdev_value=None,
            )
        ],
        analog_inputs=[],
    )
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"grabbed_count": 0, "waiting_for_device": True})
    )

    await session_profiles_module.apply_resolved_device_profile(manager, hardware_id, resolved)
    await session_profiles_module.apply_resolved_device_profile(manager, hardware_id, resolved)

    manager.client.send_command.assert_awaited_once()
    assert hardware_id in manager.profile_state.grab_waiting_devices


@pytest.mark.asyncio
async def test_apply_resolved_device_profile_keeps_waiting_cache_across_inactive_window() -> None:
    manager = SessionManager()
    hardware_id = "1234:5678@2"
    mapped = ResolvedDeviceProfile(
        hardware_id=hardware_id,
        active_profile_names=["Desktop"],
        mappings={"btn_south": MappingAction(action_type=ActionType.KEYBOARD, target="key_f13")},
    )
    inactive = ResolvedDeviceProfile(
        hardware_id=hardware_id,
        active_profile_names=[],
    )
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        hardware_id=hardware_id,
        name="Test Pad 2",
        evdev_devices=[
            SimpleNamespace(
                id="gamepad",
                path="keymasq:1234:5678",
                device_type=SimpleNamespace(value="gamepad"),
                phys="",
                capabilities=[],
            )
        ],
        buttons=[
            SimpleNamespace(
                id="btn_south",
                evdev="btn_south",
                source="gamepad",
                evdev_value=None,
            )
        ],
        analog_inputs=[],
    )
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"grabbed_count": 0, "waiting_for_device": True})
    )

    await session_profiles_module.apply_resolved_device_profile(manager, hardware_id, mapped)
    await session_profiles_module.apply_resolved_device_profile(manager, hardware_id, inactive)
    await session_profiles_module.apply_resolved_device_profile(manager, hardware_id, mapped)

    manager.client.send_command.assert_awaited_once()
    assert hardware_id in manager.profile_state.grab_waiting_devices


@pytest.mark.asyncio
async def test_apply_resolved_device_profile_resends_waiting_device_when_payload_changes() -> None:
    manager = SessionManager()
    hardware_id = "1234:5678@2"
    mapped = ResolvedDeviceProfile(
        hardware_id=hardware_id,
        active_profile_names=["Desktop"],
        mappings={"btn_south": MappingAction(action_type=ActionType.KEYBOARD, target="key_f13")},
    )
    changed = ResolvedDeviceProfile(
        hardware_id=hardware_id,
        active_profile_names=["Gaming"],
        mappings={"btn_south": MappingAction(action_type=ActionType.MOUSE, target="btn_left")},
        combo_event_count=1,
        combo_sources={"gamepad"},
    )
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        hardware_id=hardware_id,
        name="Test Pad 2",
        evdev_devices=[
            SimpleNamespace(
                id="gamepad",
                path="keymasq:1234:5678",
                device_type=SimpleNamespace(value="gamepad"),
                phys="",
                capabilities=[],
            )
        ],
        buttons=[
            SimpleNamespace(
                id="btn_south",
                evdev="btn_south",
                source="gamepad",
                evdev_value=None,
            )
        ],
        analog_inputs=[],
    )
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"grabbed_count": 0, "waiting_for_device": True})
    )

    await session_profiles_module.apply_resolved_device_profile(manager, hardware_id, mapped)
    await session_profiles_module.apply_resolved_device_profile(manager, hardware_id, changed)

    assert manager.client.send_command.await_count == 2
    assert hardware_id in manager.profile_state.grab_waiting_devices


@pytest.mark.asyncio
async def test_apply_resolved_device_profile_does_not_grab_empty_interface_selection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SessionManager()
    hardware_id = "1234:5678@2"
    resolved = ResolvedDeviceProfile(
        hardware_id=hardware_id,
        active_profile_names=["Desktop"],
        combo_event_count=1,
        combo_sources={"if02_joystick"},
    )
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        hardware_id=hardware_id,
        name="Test Pad 2",
        evdev_devices=[
            SimpleNamespace(
                id="joystick",
                path="keymasq:1234:5678",
                device_type=SimpleNamespace(value="gamepad"),
                phys="",
                capabilities=[],
            )
        ],
        buttons=[],
        analog_inputs=[],
    )
    manager.client.send_command = AsyncMock()

    with caplog.at_level(logging.WARNING, logger="keymasq-session"):
        await session_profiles_module.apply_resolved_device_profile(manager, hardware_id, resolved)
        await session_profiles_module.apply_resolved_device_profile(manager, hardware_id, resolved)

    manager.client.send_command.assert_not_awaited()
    assert "No configured interfaces selected for 1234:5678@2" in caplog.text
    assert caplog.text.count("No configured interfaces selected for 1234:5678@2") == 1


@pytest.mark.asyncio
async def test_apply_resolved_device_profile_skips_same_interface_noop_without_mapping_log(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
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
    grab_payload = session_profiles_module.build_grab_device_payload(
        manager,
        hardware_id,
        manager.hardware.get_hardware(hardware_id),
        resolved,
        {"mouse": "/dev/input/event10"},
    )
    manager.profile_state.last_sent_grab_signatures[
        hardware_id
    ] = session_profiles_module.grab_device_payload_signature(grab_payload)
    manager.profile_state.last_sent_mapping_signatures[
        hardware_id
    ] = session_payloads_module.resolved_mapping_signature(manager, resolved, hardware_id)
    update_mapping = AsyncMock(return_value=True)
    maybe_notify = Mock()
    monkeypatch.setattr(session_profiles_module, "update_mapping", update_mapping)
    monkeypatch.setattr(session_profiles_module, "maybe_notify_profile_activation", maybe_notify)

    with caplog.at_level("INFO", logger="keymasq-session"):
        await session_profiles_module.apply_resolved_device_profile(
            manager,
            hardware_id,
            resolved,
        )

    update_mapping.assert_not_awaited()
    maybe_notify.assert_called_once_with(manager, "Test Mouse", ["Desktop"], resolved)
    assert "Same interfaces for 1234:5678, updating mapping only" not in caplog.text


@pytest.mark.asyncio
async def test_apply_resolved_device_profile_skips_profile_only_change_without_mapping_log(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
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
    grab_payload = session_profiles_module.build_grab_device_payload(
        manager,
        hardware_id,
        manager.hardware.get_hardware(hardware_id),
        resolved,
        {"mouse": "/dev/input/event10"},
    )
    manager.profile_state.last_sent_grab_signatures[
        hardware_id
    ] = session_profiles_module.grab_device_payload_signature(grab_payload)
    manager.profile_state.last_sent_mapping_signatures[
        hardware_id
    ] = session_payloads_module.resolved_mapping_signature(manager, resolved, hardware_id)
    update_mapping = AsyncMock(return_value=True)
    maybe_notify = Mock()
    monkeypatch.setattr(session_profiles_module, "update_mapping", update_mapping)
    monkeypatch.setattr(session_profiles_module, "maybe_notify_profile_activation", maybe_notify)

    with caplog.at_level("INFO", logger="keymasq-session"):
        await session_profiles_module.apply_resolved_device_profile(
            manager,
            hardware_id,
            resolved,
        )

    update_mapping.assert_not_awaited()
    maybe_notify.assert_called_once_with(manager, "Test Mouse", ["Desktop"], resolved)
    assert "Same interfaces for 1234:5678, updating mapping only" not in caplog.text


@pytest.mark.asyncio
async def test_apply_resolved_device_profile_refreshes_same_interface_grab_config() -> None:
    manager = SessionManager()
    hardware_id = "1234:5678"
    old_hardware = SimpleNamespace(
        hardware_id=hardware_id,
        name="Test Mouse",
        evdev_devices=[SimpleNamespace(id="mouse", path="/dev/input/event10")],
        buttons=[SimpleNamespace(id="btn_side", evdev="btn_side", source="mouse")],
    )
    new_hardware = SimpleNamespace(
        hardware_id=hardware_id,
        name="Test Mouse",
        evdev_devices=[SimpleNamespace(id="mouse", path="/dev/input/event10")],
        buttons=[
            SimpleNamespace(id="btn_side", evdev="btn_side", source="mouse"),
            SimpleNamespace(
                id="wheel_up",
                evdev="rel_wheel",
                evdev_code=8,
                evdev_value=1,
                source="mouse",
            ),
        ],
    )
    resolved = ResolvedDeviceProfile(
        hardware_id=hardware_id,
        active_profile_names=["Desktop"],
        mappings={
            "btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_f13"),
            "wheel_up": MappingAction(action_type=ActionType.KEYBOARD, target="key_f14"),
        },
    )
    old_resolved = ResolvedDeviceProfile(
        hardware_id=hardware_id,
        active_profile_names=["Desktop"],
        mappings={"btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_f13")},
    )
    manager.hardware.get_hardware = lambda _hardware_id: new_hardware  # type: ignore[assignment]
    manager.profile_state.resolved_devices[hardware_id] = old_resolved
    manager.profile_state.grabbed_devices.add(hardware_id)
    manager.profile_state.grabbed_interfaces[hardware_id] = {"mouse": "/dev/input/event10"}
    old_grab_payload = session_profiles_module.build_grab_device_payload(
        manager,
        hardware_id,
        old_hardware,
        old_resolved,
        {"mouse": "/dev/input/event10"},
    )
    manager.profile_state.last_sent_grab_signatures[
        hardware_id
    ] = session_profiles_module.grab_device_payload_signature(old_grab_payload)
    manager.profile_state.last_sent_mapping_signatures[
        hardware_id
    ] = session_payloads_module.resolved_mapping_signature(manager, old_resolved, hardware_id)
    manager.client.send_command = AsyncMock(
        side_effect=[
            Response(status="ok", data={"grabbed_count": 1}),
            Response(status="ok", data={"updated": True}),
        ]
    )

    await session_profiles_module.apply_resolved_device_profile(manager, hardware_id, resolved)

    sent = manager.client.send_command.await_args_list
    assert [call.args[0].command for call in sent] == [
        CommandType.GRAB_DEVICE,
        CommandType.SET_MAPPING,
    ]
    grab_data = sent[0].args[0].data
    assert grab_data["button_map"]["wheel_up"] == "rel_wheel"
    assert grab_data["button_codes"]["wheel_up"] == 8
    assert grab_data["button_values"]["wheel_up"] == 1
    assert manager.profile_state.last_sent_grab_signatures[hardware_id] == (
        session_profiles_module.grab_device_payload_signature(grab_data)
    )
