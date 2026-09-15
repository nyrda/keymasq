from unittest.mock import AsyncMock, Mock

import pytest

from keymasq.common.ipc import CommandType, Response
from keymasq.common.model.core import DeviceType
from keymasq.common.model.hardware import EvdevDevice, HardwareConfig
from keymasq.session.manager import events
from keymasq.session.manager.core import SessionManager
from keymasq.session.manager.profile import coordinator
from keymasq.session.profile.types import ResolvedProfiles


@pytest.mark.asyncio
async def test_mask_ready_reapplies_saved_output_without_profiles():
    manager = SessionManager()
    hardware = HardwareConfig(
        "28de",
        "1205",
        "Steam Deck",
        [EvdevDevice("/dev/input/by-id/deck-gamepad", DeviceType.GAMEPAD, "gamepad")],
        [],
        default_output="virtual-gamepad-1",
    )
    manager.hardware.list_hardware_ids = lambda: [hardware.hardware_id]
    manager.hardware.get_hardware = lambda _: hardware
    manager.profiles.resolve_active_profiles = lambda *args, **kwargs: ResolvedProfiles()
    manager.broadcast_to_session_clients = Mock()
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"grabbed_count": 1, "updated": True})
    )
    await coordinator.reevaluate_profiles(manager)
    assert hardware.hardware_id in manager.profile_state.grabbed_devices
    manager.client.send_command.reset_mock()

    # Takeover replaces physical nodes. A cached successful grab must not
    # suppress the request that adopts the new reservation and restores routing.
    await events.handle_runtime_reset_event(manager, {"reason": "hardware_mask_ready"})
    commands = [call.args[0] for call in manager.client.send_command.await_args_list]
    grabs = [command for command in commands if command.command == CommandType.GRAB_DEVICE]
    assert len(grabs) == 1
    assert grabs[0].data["hardware_id"] == hardware.hardware_id
    assert grabs[0].data["default_output"] == "virtual-gamepad-1"
    assert grabs[0].data["force_grab_unmapped"] is True
    assert hardware.hardware_id in manager.profile_state.grabbed_devices


@pytest.mark.asyncio
async def test_automatic_mask_recovery_reports_retry_without_clearing_profile_activations(
    monkeypatch,
):
    manager = SessionManager()
    manager.broadcast_to_session_clients = Mock()
    manager.send_notification = Mock()
    manager.profile_state.runtime_profile_activations = {"manual-profile": Mock()}
    clear_inspectors = Mock()
    monkeypatch.setattr(
        events.device_inspector, "clear_all_device_inspector_state", clear_inspectors
    )
    await events.handle_runtime_reset_event(
        manager, {"reason": "hardware_mask_recovery", "retrying": True}
    )
    assert "manual-profile" in manager.profile_state.runtime_profile_activations
    clear_inspectors.assert_not_called()
    assert "retry automatically" in manager.send_notification.call_args.args[1]
