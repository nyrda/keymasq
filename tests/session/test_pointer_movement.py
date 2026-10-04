from unittest.mock import Mock

import pytest

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType, DeviceType
from keymasq.common.model.hardware import EvdevDevice, HardwareConfig
from keymasq.common.model.pointer import (
    MAX_POINTER_COUNTS,
    MAX_POINTER_FACTOR,
    MAX_POINTER_RECENTER_MS,
    MAX_POINTER_WINDOW_MS,
    MIN_POINTER_WINDOW_MS,
    PointerMovementConfig,
)
from keymasq.common.model.profiles import DeviceProfileLayer, ProfileConfig
from keymasq.common.virtual_devices import SAME_DEVICE_OUTPUT_ID
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


@pytest.mark.parametrize("output_id", ["cafe:0004", SAME_DEVICE_OUTPUT_ID])
def test_pointer_axes_on_own_controller_grab_the_controller_interface(output_id):
    hardware = HardwareConfig(
        "cafe",
        "0004",
        "Hybrid",
        [
            EvdevDevice("/dev/input/event4", DeviceType.MOUSE, "mouse"),
            EvdevDevice("/dev/input/event6", DeviceType.GAMEPAD, "pad"),
        ],
        [],
    )
    resolved = ResolvedDeviceProfile(hardware.hardware_id)
    resolved.mappings["pointer"] = MappingAction(
        action_type=ActionType.POINTER_MOVEMENT,
        pointer_movement=PointerMovementConfig(mode="axes", output_id=output_id),
    )

    interfaces = get_interfaces_to_grab(hardware, resolved, manager=Mock())

    assert interfaces == {"mouse": "/dev/input/event4", "pad": "/dev/input/event6"}


def test_pointer_axes_on_another_output_leave_the_controller_interface_alone():
    hardware = HardwareConfig(
        "cafe",
        "0004",
        "Hybrid",
        [
            EvdevDevice("/dev/input/event4", DeviceType.MOUSE, "mouse"),
            EvdevDevice("/dev/input/event6", DeviceType.GAMEPAD, "pad"),
        ],
        [],
    )
    resolved = ResolvedDeviceProfile(hardware.hardware_id)
    resolved.mappings["pointer"] = MappingAction(
        action_type=ActionType.POINTER_MOVEMENT,
        pointer_movement=PointerMovementConfig(mode="axes"),
    )

    interfaces = get_interfaces_to_grab(hardware, resolved, manager=Mock())

    assert interfaces == {"mouse": "/dev/input/event4"}


def _pointer_profile(**fields: object) -> dict[str, object]:
    return {
        "profile": {"name": "Hand edited", "created_at": "2026-01-01T00:00:00"},
        "devices": {
            "cafe:0004": {"mapping": {"pointer": {"action": "pointer_movement", **fields}}}
        },
    }


@pytest.mark.parametrize(
    ("field", "value", "clamped"),
    [
        ("mode", "axis", "mouse"),
        ("behavior", "pos", "velocity"),
        ("overshoot", 1, "drag"),
        ("x_direction", "up", "both"),
        ("x_axis", "abs_bogus", None),
        ("y_axis", 3, None),
        ("factor_x", -3, 0.0),
        ("factor_y", 51.0, 50.0),
        ("factor_x", "2", 2.0),
        ("full_speed", 0, 1.0),
        ("window_ms", 20.5, 20),
        ("window_ms", 1000, 250),
        ("radius", float("nan"), 1500.0),
        ("recenter_ms", -1, 0),
        ("deadzone", 0.96, 0.95),
        ("minimum_output", True, 0.0),
        ("response_curve", 5, 4.0),
        ("invert_x", "yes", True),
        ("output_id", 2, "2"),
    ],
)
def test_malformed_pointer_mapping_fails_profile_load_but_not_daemon_ipc(field, value, clamped):
    with pytest.raises(ValueError, match=rf"mapping cafe:0004 pointer: pointer movement {field} "):
        ProfileCodec().decode(_pointer_profile(**{field: value}), default_name="fallback")

    runtime = parse_action(
        Mock(),
        {"action": "pointer_movement", "pointer_movement": {"mode": "axes", field: value}},
    )

    assert runtime.pointer_movement is not None
    assert getattr(runtime.pointer_movement, field) == clamped


def test_pointer_values_at_the_editor_limits_load_unchanged():
    lowest = PointerMovementConfig(
        mode="axes",
        factor_x=0.0,
        factor_y=0.0,
        full_speed=1.0,
        window_ms=MIN_POINTER_WINDOW_MS,
        radius=1.0,
        recenter_ms=0,
        x_axis=None,
        y_axis="abs_hat0x",
        deadzone=0.0,
        minimum_output=0.0,
        response_curve=0.25,
    )
    highest = PointerMovementConfig(
        mode="axes",
        factor_x=MAX_POINTER_FACTOR,
        factor_y=MAX_POINTER_FACTOR,
        full_speed=MAX_POINTER_COUNTS,
        window_ms=MAX_POINTER_WINDOW_MS,
        radius=MAX_POINTER_COUNTS,
        recenter_ms=MAX_POINTER_RECENTER_MS,
        deadzone=95.0 / 100.0,
        minimum_output=95.0 / 100.0,
        response_curve=4.0,
    )
    codec = ProfileCodec()

    for config in (lowest, highest):
        action = MappingAction(action_type=ActionType.POINTER_MOVEMENT, pointer_movement=config)
        layer = DeviceProfileLayer("cafe:0004", mappings={"pointer": action})
        data = codec.encode(ProfileConfig(name="Limits", device_layers={"cafe:0004": layer}))
        loaded = codec.decode(data, default_name="fallback").config

        assert loaded.device_layers["cafe:0004"].mappings["pointer"].pointer_movement == config
