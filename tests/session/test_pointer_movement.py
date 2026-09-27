from unittest.mock import Mock

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType, DeviceType
from keymasq.common.model.hardware import EvdevDevice, HardwareConfig
from keymasq.common.model.pointer import PointerMovementConfig
from keymasq.common.model.profiles import DeviceProfileLayer, ProfileConfig
from keymasq.keymasqd.runtime.action_parser import parse_action
from keymasq.session.manager.payload.action import mapping_action_payload
from keymasq.session.manager.profile.grab_plan import (
    build_grab_device_payload,
    get_interfaces_to_grab,
)
from keymasq.session.profile.codec import ProfileCodec
from keymasq.session.profile.repository import ProfileRepository
from keymasq.session.profile.types import ResolvedDeviceProfile


def test_saved_pointer_mapping_reaches_daemon_unchanged(tmp_path):
    config = PointerMovementConfig(
        mode="axes",
        factor_x=0.25,
        factor_y=3.0,
        invert_y=True,
        swap_axes=True,
        output_id="virtual-gamepad-2",
        behavior="position",
        full_speed=2500,
        window_ms=40,
        radius=750,
        overshoot="keep",
        recenter_ms=300,
        x_axis="abs_throttle",
        y_axis=None,
        x_direction="min",
        y_direction="max",
        deadzone=0.1,
        minimum_output=0.2,
        response_curve=1.5,
    )
    action = MappingAction(action_type=ActionType.POINTER_MOVEMENT, pointer_movement=config)
    profile = ProfileConfig(
        name="Pointer",
        device_layers={"cafe:0004": DeviceProfileLayer("cafe:0004", mappings={"pointer": action})},
    )
    codec = ProfileCodec()
    path = tmp_path / "pointer.toml"
    ProfileRepository(codec).write(profile, path)

    loaded = codec.load(path).config.device_layers["cafe:0004"].mappings["pointer"]
    runtime = parse_action(Mock(), mapping_action_payload(Mock(), loaded, "cafe:0004"))

    assert loaded.pointer_movement == config
    assert runtime.action_type == ActionType.POINTER_MOVEMENT
    assert runtime.pointer_movement == config


def test_pointer_only_mouse_grabs_its_pointer_interface():
    hardware = HardwareConfig(
        "cafe",
        "0004",
        "Mouse",
        [
            EvdevDevice("/dev/input/event4", DeviceType.MOUSE, "mouse"),
            EvdevDevice("/dev/input/event5", DeviceType.KEYBOARD, "keys"),
        ],
        [],
    )
    resolved = ResolvedDeviceProfile(hardware.hardware_id)
    resolved.mappings["pointer"] = MappingAction(action_type=ActionType.POINTER_MOVEMENT)
    manager = Mock()
    manager.resolved_button_codes.return_value = {}

    interfaces = get_interfaces_to_grab(hardware, resolved, manager=manager)
    payload = build_grab_device_payload(
        manager,
        hardware.hardware_id,
        hardware,
        resolved,
        interfaces,
    )

    assert interfaces == {"mouse": "/dev/input/event4"}
    assert payload["force_grab_unmapped"] is True
