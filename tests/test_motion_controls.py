import math
from types import SimpleNamespace

import evdev
import pytest

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.analog import SAME_DEVICE_OUTPUT_ID
from keymasq.common.model.core import ActionType
from keymasq.common.model.motion import (
    MotionAxisRoutingConfig,
    MotionControlConfig,
    MotionGamepadConfig,
    MotionMouseConfig,
    MotionTiltConfig,
)
from keymasq.common.model.profiles import DeviceProfileLayer, ProfileConfig
from keymasq.keymasqd import device_inventory
from keymasq.keymasqd.runtime.grabbed_device.event.pipeline import build_action_execution_deps
from keymasq.keymasqd.runtime.grabbed_device.types import GrabbedDeviceState
from keymasq.keymasqd.runtime.motion_controls import dispatch_motion_event
from keymasq.session.manager.payload.action import mapping_action_payload
from keymasq.session.motion_controls import MotionControlManager
from keymasq.session.profile.codec import ProfileCodec


class _Writer:
    def __init__(self) -> None:
        self.events: list[tuple[int, int, int]] = []

    def write(self, event_type: int, code: int, value: int) -> None:
        self.events.append((event_type, code, value))

    def syn(self) -> None:
        return


class _Runtime:
    def __init__(self) -> None:
        self.motion_axis_bindings = {
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RZ): (
                "motion_1",
                "gyro",
                "yaw",
                0.0,
                math.radians(1.0),
                False,
                0.0,
            ),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RY): (
                "motion_1",
                "gyro",
                "pitch",
                0.0,
                math.radians(1.0),
                True,
                0.0,
            ),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RX): (
                "motion_1",
                "gyro",
                "roll",
                0.0,
                math.radians(1.0),
                False,
                0.0,
            ),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X): (
                "motion_1",
                "accelerometer",
                "x",
                0.0,
                1.0,
                False,
                0.0,
            ),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Y): (
                "motion_1",
                "accelerometer",
                "y",
                0.0,
                1.0,
                False,
                0.0,
            ),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z): (
                "motion_1",
                "accelerometer",
                "z",
                0.0,
                1.0,
                False,
                0.0,
            ),
        }
        self.motion_sensors = {"motion_1": {}}
        self.state = GrabbedDeviceState()
        self.mouse_uinput = _Writer()
        self.hardware_id = "054c:0ce6"
        self.analog_inputs = {}
        self.analog_axis_output_codes = {}

    async def reset_analog_controls(self) -> None:
        return

    def resolve_gamepad_output(self, output_id, context):
        return None


async def _send_accelerometer_frame(runtime, mapping, deps, x, y, z) -> None:
    for code, value in (
        (evdev.ecodes.ABS_X, x),
        (evdev.ecodes.ABS_Y, y),
        (evdev.ecodes.ABS_Z, z),
    ):
        assert await dispatch_motion_event(
            runtime,
            SimpleNamespace(type=evdev.ecodes.EV_ABS, code=code, value=value),
            mapping,
            deps=deps,
        )
    assert await dispatch_motion_event(
        runtime,
        SimpleNamespace(type=evdev.ecodes.EV_SYN, code=evdev.ecodes.SYN_REPORT, value=0),
        mapping,
        deps=deps,
    )


def test_motion_control_manager_round_trip(temp_config_dir) -> None:
    manager = MotionControlManager()
    config = MotionControlConfig(
        name="Gyro Aim",
        mode="mouse",
        axis_routing=MotionAxisRoutingConfig(
            yaw="none",
            pitch="horizontal",
            roll="vertical",
        ),
        mouse=MotionMouseConfig(
            sensitivity_x=4.5,
            sensitivity_y=3.5,
            deadzone_dps=1.25,
        ),
    )

    manager.save_motion_control(config)

    assert MotionControlManager().get_motion_control("Gyro Aim") == config
    assert (temp_config_dir / "motion_controls" / "gyro_aim.toml").exists()
    content = (temp_config_dir / "motion_controls" / "gyro_aim.toml").read_text()
    assert "[axis_routing]" in content
    assert 'yaw = "none"' in content
    assert "horizontal_axis" not in content


def test_tilt_motion_control_manager_round_trip(temp_config_dir) -> None:
    manager = MotionControlManager()
    config = MotionControlConfig(
        name="Area Mouse",
        mode="area_mouse",
        tilt=MotionTiltConfig(
            reference="gravity",
            pitch="none",
            roll="horizontal",
            deadzone_deg=3.0,
            full_scale_deg=40.0,
            area_radius_x=640.0,
            area_radius_y=360.0,
            drag_center=False,
        ),
    )

    manager.save_motion_control(config)

    assert MotionControlManager().get_motion_control("Area Mouse") == config
    content = (temp_config_dir / "motion_controls" / "area_mouse.toml").read_text()
    assert 'mode = "area_mouse"' in content
    assert "[tilt]" in content
    assert 'reference = "gravity"' in content
    assert "drag_center = false" in content


def test_motion_axis_routing_normalizes_invalid_assignments() -> None:
    routing = MotionAxisRoutingConfig(yaw="bad", pitch="none", roll="vertical")

    assert routing == MotionAxisRoutingConfig(
        yaw="horizontal",
        pitch="none",
        roll="vertical",
    )


def test_motion_control_profile_reference_round_trip_and_runtime_resolution() -> None:
    config = MotionControlConfig(name="Gyro Aim")
    profile = ProfileConfig(
        name="Motion Profile",
        device_layers={
            "controller": DeviceProfileLayer(
                hardware_id="controller",
                mappings={
                    "motion_1": MappingAction(
                        action_type=ActionType.MOTION_CONTROL,
                        motion_control_name=config.name,
                    )
                },
            )
        },
    )
    codec = ProfileCodec(motion_control_exists=lambda name: name == config.name)

    decoded = codec.decode(codec.encode(profile), default_name="fallback").config
    action = decoded.device_layers["controller"].mappings["motion_1"]
    manager = SimpleNamespace(
        motion_controls=SimpleNamespace(
            get_motion_control=lambda name: config if name == config.name else None
        )
    )
    payload = mapping_action_payload(manager, action, "controller")

    assert action.motion_control_name == "Gyro Aim"
    assert payload["action"] == "motion_control"
    assert payload["motion_control"]["name"] == "Gyro Aim"
    assert payload["motion_control"]["axis_routing"] == {
        "yaw": "horizontal",
        "pitch": "vertical",
        "roll": "horizontal",
    }
    assert payload["motion_control"]["tilt"]["reference"] == "activation"


def test_privileged_inventory_serializes_motion_axis_resolution() -> None:
    class Device:
        def capabilities(self, *, absinfo: bool = False):
            if not absinfo:
                return {evdev.ecodes.EV_ABS: [evdev.ecodes.ABS_RX]}
            return {
                evdev.ecodes.EV_ABS: [
                    (
                        evdev.ecodes.ABS_RX,
                        evdev.AbsInfo(0, -32767000, 32767000, 10, 0, 14247),
                    )
                ]
            }

    result = device_inventory._abs_axis_info(Device(), evdev)

    assert result[str(evdev.ecodes.ABS_RX)]["resolution"] == 14247


@pytest.mark.asyncio
async def test_motion_mouse_combines_yaw_and_roll_with_natural_pitch(monkeypatch) -> None:
    runtime = _Runtime()
    config = MotionControlConfig(
        name="Gyro Aim",
        mouse=MotionMouseConfig(
            sensitivity_x=10.0,
            sensitivity_y=10.0,
            deadzone_dps=0.0,
        ),
    )
    mapping = {
        "motion_1": MappingAction(
            action_type=ActionType.MOTION_CONTROL,
            motion_control_config=config,
        )
    }
    timestamps = iter([1_000_000_000, 1_010_000_000])
    monkeypatch.setattr(
        "keymasq.keymasqd.runtime.motion_controls.time.monotonic_ns",
        lambda: next(timestamps),
    )
    deps = build_action_execution_deps()
    yaw = SimpleNamespace(
        type=evdev.ecodes.EV_ABS,
        code=evdev.ecodes.ABS_RZ,
        value=45,
    )
    roll = SimpleNamespace(
        type=evdev.ecodes.EV_ABS,
        code=evdev.ecodes.ABS_RX,
        value=45,
    )
    pitch = SimpleNamespace(
        type=evdev.ecodes.EV_ABS,
        code=evdev.ecodes.ABS_RY,
        value=90,
    )
    syn = SimpleNamespace(type=evdev.ecodes.EV_SYN, code=evdev.ecodes.SYN_REPORT, value=0)

    assert await dispatch_motion_event(runtime, yaw, mapping, deps=deps)
    assert await dispatch_motion_event(runtime, roll, mapping, deps=deps)
    assert await dispatch_motion_event(runtime, pitch, mapping, deps=deps)
    assert await dispatch_motion_event(runtime, syn, mapping, deps=deps)
    assert await dispatch_motion_event(runtime, yaw, mapping, deps=deps)
    assert await dispatch_motion_event(runtime, roll, mapping, deps=deps)
    assert await dispatch_motion_event(runtime, pitch, mapping, deps=deps)
    assert await dispatch_motion_event(runtime, syn, mapping, deps=deps)

    assert (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, -9) in runtime.mouse_uinput.events
    assert (evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, 9) in runtime.mouse_uinput.events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output_id", "expected_output"),
    [
        (SAME_DEVICE_OUTPUT_ID, "origin"),
        (None, "virtual-gamepad-1"),
    ],
)
async def test_motion_stick_honors_its_selected_output(
    monkeypatch,
    output_id,
    expected_output,
) -> None:
    runtime = _Runtime()
    gamepad = _Writer()
    resolved: list[str | None] = []

    def resolve_gamepad_output(output_id, _context):
        resolved.append(output_id)
        return SimpleNamespace(
            output_id=output_id,
            uinput=gamepad,
            bucket=f"gamepad:{output_id}",
            is_virtual=True,
        )

    runtime.resolve_gamepad_output = resolve_gamepad_output  # type: ignore[method-assign]
    config = MotionControlConfig(
        name="Right Stick",
        mode="gamepad",
        gamepad=MotionGamepadConfig(
            output_id=output_id,
            deadzone_dps=0.0,
            smoothing=0.0,
        ),
    )
    mapping = {
        "motion_1": MappingAction(
            action_type=ActionType.MOTION_CONTROL,
            motion_control_config=config,
        )
    }
    timestamps = iter([1_000_000_000, 1_010_000_000])
    monkeypatch.setattr(
        "keymasq.keymasqd.runtime.motion_controls.time.monotonic_ns",
        lambda: next(timestamps),
    )
    deps = build_action_execution_deps()
    yaw = SimpleNamespace(
        type=evdev.ecodes.EV_ABS,
        code=evdev.ecodes.ABS_RZ,
        value=90,
    )
    syn = SimpleNamespace(type=evdev.ecodes.EV_SYN, code=evdev.ecodes.SYN_REPORT, value=0)

    assert await dispatch_motion_event(runtime, yaw, mapping, deps=deps)
    assert await dispatch_motion_event(runtime, syn, mapping, deps=deps)
    assert await dispatch_motion_event(runtime, yaw, mapping, deps=deps)
    assert await dispatch_motion_event(runtime, syn, mapping, deps=deps)

    expected = runtime.hardware_id if expected_output == "origin" else expected_output
    assert resolved == [expected, expected]
    assert any(event_type == evdev.ecodes.EV_ABS for event_type, _code, _value in gamepad.events)


@pytest.mark.asyncio
async def test_tilt_mouse_keeps_moving_while_tilt_is_held(monkeypatch) -> None:
    runtime = _Runtime()
    config = MotionControlConfig(
        name="Tilt Mouse",
        mode="tilt_mouse",
        tilt=MotionTiltConfig(
            deadzone_deg=0.0,
            full_scale_deg=30.0,
            smoothing=0.0,
            speed_x=100.0,
            speed_y=100.0,
        ),
    )
    mapping = {
        "motion_1": MappingAction(
            action_type=ActionType.MOTION_CONTROL,
            motion_control_config=config,
        )
    }
    timestamps = iter([1_000_000_000, 1_010_000_000, 1_020_000_000])
    monkeypatch.setattr(
        "keymasq.keymasqd.runtime.motion_controls.time.monotonic_ns",
        lambda: next(timestamps),
    )
    deps = build_action_execution_deps()

    await _send_accelerometer_frame(runtime, mapping, deps, 0, 0, 1000)
    await _send_accelerometer_frame(runtime, mapping, deps, -1000, 0, 0)
    await _send_accelerometer_frame(runtime, mapping, deps, -1000, 0, 0)

    horizontal = [
        value
        for event_type, code, value in runtime.mouse_uinput.events
        if event_type == evdev.ecodes.EV_REL and code == evdev.ecodes.REL_X
    ]
    assert horizontal == [1, 1]


@pytest.mark.asyncio
async def test_tilt_stick_uses_the_selected_gamepad_output(monkeypatch) -> None:
    runtime = _Runtime()
    gamepad = _Writer()
    resolved: list[str | None] = []

    def resolve_gamepad_output(output_id, _context):
        resolved.append(output_id)
        return SimpleNamespace(
            output_id=output_id,
            uinput=gamepad,
            bucket=f"gamepad:{output_id}",
            is_virtual=True,
        )

    runtime.resolve_gamepad_output = resolve_gamepad_output  # type: ignore[method-assign]
    config = MotionControlConfig(
        name="Tilt Right Stick",
        mode="tilt_gamepad",
        gamepad=MotionGamepadConfig(output_id="virtual-gamepad-1"),
        tilt=MotionTiltConfig(deadzone_deg=0.0, full_scale_deg=30.0, smoothing=0.0),
    )
    mapping = {
        "motion_1": MappingAction(
            action_type=ActionType.MOTION_CONTROL,
            motion_control_config=config,
        )
    }
    timestamps = iter([1_000_000_000, 1_010_000_000])
    monkeypatch.setattr(
        "keymasq.keymasqd.runtime.motion_controls.time.monotonic_ns",
        lambda: next(timestamps),
    )
    deps = build_action_execution_deps()

    await _send_accelerometer_frame(runtime, mapping, deps, 0, 0, 1000)
    await _send_accelerometer_frame(runtime, mapping, deps, -1000, 0, 0)

    assert resolved == ["virtual-gamepad-1", "virtual-gamepad-1"]
    assert any(event_type == evdev.ecodes.EV_ABS for event_type, _code, _value in gamepad.events)


@pytest.mark.asyncio
async def test_tilt_stick_pitch_outputs_both_axis_directions(monkeypatch) -> None:
    runtime = _Runtime()
    gamepad = _Writer()

    def resolve_gamepad_output(output_id, _context):
        return SimpleNamespace(
            output_id=output_id,
            uinput=gamepad,
            bucket=f"gamepad:{output_id}",
            is_virtual=True,
        )

    runtime.resolve_gamepad_output = resolve_gamepad_output  # type: ignore[method-assign]
    config = MotionControlConfig(
        name="Tilt Right Stick",
        mode="tilt_gamepad",
        gamepad=MotionGamepadConfig(output_id="virtual-gamepad-1"),
        tilt=MotionTiltConfig(deadzone_deg=0.0, full_scale_deg=45.0, smoothing=0.0),
    )
    mapping = {
        "motion_1": MappingAction(
            action_type=ActionType.MOTION_CONTROL,
            motion_control_config=config,
        )
    }
    timestamps = iter([1_000_000_000, 1_010_000_000, 1_020_000_000])
    monkeypatch.setattr(
        "keymasq.keymasqd.runtime.motion_controls.time.monotonic_ns",
        lambda: next(timestamps),
    )
    deps = build_action_execution_deps()

    await _send_accelerometer_frame(runtime, mapping, deps, 0, 0, 1000)
    await _send_accelerometer_frame(runtime, mapping, deps, 0, 500, 866)
    await _send_accelerometer_frame(runtime, mapping, deps, 0, -500, 866)

    pitch_values = [
        value
        for event_type, code, value in gamepad.events
        if event_type == evdev.ecodes.EV_ABS and code == evdev.ecodes.ABS_RY
    ]
    assert pitch_values[0] == 0
    assert pitch_values[1] < 0
    assert pitch_values[2] > 0


@pytest.mark.asyncio
async def test_area_mouse_maps_tilt_to_an_area_and_drags_its_center(monkeypatch) -> None:
    runtime = _Runtime()
    config = MotionControlConfig(
        name="Area Mouse",
        mode="area_mouse",
        tilt=MotionTiltConfig(
            deadzone_deg=0.0,
            full_scale_deg=30.0,
            smoothing=0.0,
            area_radius_x=100.0,
            area_radius_y=100.0,
            drag_center=True,
        ),
    )
    mapping = {
        "motion_1": MappingAction(
            action_type=ActionType.MOTION_CONTROL,
            motion_control_config=config,
        )
    }
    timestamps = iter([1_000_000_000, 1_010_000_000, 1_020_000_000])
    monkeypatch.setattr(
        "keymasq.keymasqd.runtime.motion_controls.time.monotonic_ns",
        lambda: next(timestamps),
    )
    deps = build_action_execution_deps()

    await _send_accelerometer_frame(runtime, mapping, deps, 0, 0, 1000)
    await _send_accelerometer_frame(runtime, mapping, deps, -1000, 0, 0)
    await _send_accelerometer_frame(runtime, mapping, deps, -985, 0, 174)

    horizontal = [
        value
        for event_type, code, value in runtime.mouse_uinput.events
        if event_type == evdev.ecodes.EV_REL and code == evdev.ecodes.REL_X
    ]
    assert horizontal[0] == 100
    assert horizontal[1] < 0
