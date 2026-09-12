from pathlib import Path
from unittest.mock import Mock

import pytest

from keymasq.common.model.core import DeviceType
from keymasq.common.model.hardware import EvdevDevice, HardwareConfig
from keymasq.session.manager.profile.grab_plan import (
    build_grab_device_payload,
    get_interfaces_to_grab,
    grab_device_payload_signature,
)
from keymasq.session.profile.codec import ProfileCodec
from keymasq.session.profile.resolution import ProfileResolver
from keymasq.session.profile.types import ProfileInfo, ResolvedDeviceProfile


def test_route_alone_selects_controller_interfaces_and_changes_grab_signature():
    hardware = HardwareConfig(
        "1234",
        "5678",
        "Composite controller",
        [
            EvdevDevice("/dev/input/event0", DeviceType.GAMEPAD, "pad"),
            EvdevDevice("/dev/input/event1", DeviceType.MOTION, "imu"),
            EvdevDevice("/dev/input/event2", DeviceType.KEYBOARD, "keyboard"),
            EvdevDevice(
                "/dev/input/event3",
                DeviceType.OTHER,
                "extra",
                capabilities=[
                    "abs_x",
                    "abs_y",
                    "btn_trigger",
                ],
            ),
        ],
        [],
    )
    hardware.default_output = "virtual-gamepad-1"
    resolved = ResolvedDeviceProfile(hardware.hardware_id)
    manager = Mock()
    manager.resolved_button_codes.return_value = {}
    interfaces = get_interfaces_to_grab(hardware, resolved, manager=manager)
    assert interfaces == {"pad": "/dev/input/event0", "extra": "/dev/input/event3"}
    payload = build_grab_device_payload(
        manager,
        hardware.hardware_id,
        hardware,
        resolved,
        interfaces,
    )
    assert payload["force_grab_unmapped"] is True
    assert payload["default_output"] == "virtual-gamepad-1"
    before = grab_device_payload_signature(payload)
    payload["default_output"] = "flight"
    assert grab_device_payload_signature(payload) != before


@pytest.mark.parametrize("output", ["passthrough", "virtual-gamepad-1", "flight", "missing"])
def test_hardware_route_roundtrip(tmp_path, monkeypatch, output):
    from keymasq.session.hardware import HardwareManager

    monkeypatch.setattr("keymasq.common.paths.HARDWARE_DIR", tmp_path)
    manager = HardwareManager()
    hardware = HardwareConfig("1234", "5678", "Controller", [], [], default_output=output)
    manager.save_hardware(hardware)
    loaded = HardwareManager().get_hardware(hardware.hardware_id)
    assert loaded.default_output == output


def test_profile_does_not_store_or_resolve_output():
    codec = ProfileCodec()
    profile = codec.decode(
        {
            "profile": {"name": "Test", "is_permanent": True},
            "devices": {"pad": {"default_output": "flight"}},
        },
        default_name="Test",
    ).config
    devices = codec.encode(profile)["devices"]
    assert isinstance(devices, dict)
    assert "default_output" not in devices["pad"]
    result = ProfileResolver({"Test": ProfileInfo(Path("test"), profile)}).resolve()
    assert not result.devices["pad"].has_effective_mapping


@pytest.mark.asyncio
async def test_hardware_route_without_profiles_and_after_mapping_changes():
    from unittest.mock import AsyncMock

    from keymasq.common.ipc import CommandType, Response
    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.core import ActionType
    from keymasq.common.model.hardware import ButtonDefinition
    from keymasq.session.manager.core import SessionManager
    from keymasq.session.manager.profile import coordinator
    from keymasq.session.profile.types import ResolvedProfiles

    manager = SessionManager()
    hardware = HardwareConfig(
        "1234",
        "5678",
        "Pad",
        [
            EvdevDevice("/dev/input/event0", DeviceType.GAMEPAD, "pad"),
        ],
        [ButtonDefinition("south", "South", "btn_south", source="pad")],
        default_output="virtual-gamepad-1",
    )
    manager.hardware.list_hardware_ids = lambda: [hardware.hardware_id]
    manager.hardware.get_hardware = lambda _: hardware
    resolved = ResolvedProfiles()
    manager.profiles.resolve_active_profiles = lambda *args, **kwargs: resolved
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"grabbed_count": 1, "updated": True})
    )

    await coordinator.reevaluate_profiles(manager)
    commands = [call.args[0] for call in manager.client.send_command.await_args_list]
    assert commands[0].command == CommandType.GRAB_DEVICE
    assert commands[0].data["default_output"] == "virtual-gamepad-1"
    assert hardware.hardware_id in manager.profile_state.grabbed_devices
    manager.client.send_command.reset_mock()
    resolved.devices[hardware.hardware_id] = ResolvedDeviceProfile(
        hardware.hardware_id, mappings={"south": MappingAction(ActionType.SUPPRESS)}
    )
    await coordinator.reevaluate_profiles(manager)
    resolved.devices.clear()
    await coordinator.reevaluate_profiles(manager)
    commands = [call.args[0] for call in manager.client.send_command.await_args_list]
    assert commands
    assert all(c.command == CommandType.SET_MAPPING for c in commands)
    assert hardware.hardware_id in manager.profile_state.grabbed_devices

    manager.client.send_command.reset_mock()
    hardware.default_output = "passthrough"
    await coordinator.reevaluate_profiles(manager)
    commands = [call.args[0] for call in manager.client.send_command.await_args_list]
    assert commands[0].command == CommandType.RELEASE_DEVICE
