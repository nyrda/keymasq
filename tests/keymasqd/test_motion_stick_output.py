"""Exercise stick/gyro sharing through the real device pipeline and output router."""

import logging
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import evdev
import pytest

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.analog import (
    AnalogActionThreshold,
    AnalogControlConfig,
    AnalogGamepadOutputConfig,
)
from keymasq.common.model.core import ActionType, DeviceType
from keymasq.common.model.hardware import EvdevDevice, HardwareConfig
from keymasq.common.model.motion import (
    MotionAnalogConfig,
    MotionControlConfig,
    MotionGamepadConfig,
    MotionSensorDefinition,
    MotionTiltConfig,
)
from keymasq.keymasqd.runtime.grabbed_device import device as device_module
from keymasq.keymasqd.runtime.grabbed_device.event import pipeline
from keymasq.keymasqd.runtime.grabbed_device.event.pipeline import process_event
from keymasq.keymasqd.runtime.grabbed_device.types import InputAccessMode
from keymasq.keymasqd.runtime.virtual_gamepads import GamepadOutputRouter
from keymasq.session.manager.profile import grab_plan
from keymasq.session.profile.types import ResolvedDeviceProfile
from tests.keymasqd.device_manager_support import (
    FakeUInput,
    grabbed_event_processing_deps,
    make_grabbed_device,
)

E = evdev.ecodes


class _Rig:
    def __init__(self, monkeypatch, *, analog=False, virtual=False, unsigned=False, target="right"):
        self.output = FakeUInput()
        self.minimum, self.maximum, self.center = (0, 255, 128) if unsigned else (-32768, 32767, 0)
        axes = [
            {
                "role": role,
                "evdev_code": code,
                "minimum": self.minimum,
                "maximum": self.maximum,
                "center": self.center,
            }
            for role, code in (("x", E.ABS_RX), ("y", E.ABS_RY))
        ]
        analog_inputs = {"right_stick": {"type": "stick", "axes": axes}}
        self.router = GamepadOutputRouter(logging.getLogger(__name__))
        self.outputs = SimpleNamespace(virtual_gamepad_uinputs={"virtual-gamepad-1": self.output})
        self.devices = {}
        self.output_id = "virtual-gamepad-1" if virtual else "1234:5678"
        self.mapping = {}
        if analog:
            self.mapping["right_stick"] = MappingAction(
                action_type=ActionType.ANALOG_CONTROL,
                analog_control_config=AnalogControlConfig(
                    name="Stick",
                    input_type="stick",
                    gamepad_output=AnalogGamepadOutputConfig(
                        enabled=True,
                        output_id=self.output_id,
                        target="right",
                        deadzone=0,
                    ),
                ),
            )
        self.stick = make_grabbed_device(
            monkeypatch,
            device_type=DeviceType.GAMEPAD,
            interface_id="gamepad",
            passthrough_uinput=FakeUInput() if virtual else self.output,
            analog_inputs=analog_inputs,
            mapping=self.mapping,
            gamepad_output_resolver=self.resolve,
        )
        self.config = MotionControlConfig(
            name="Gyro Stick",
            mode="gamepad",
            gamepad=MotionGamepadConfig(
                output_id=self.output_id,
                target=target,
                target_analog_id="right_stick",
                smoothing=0,
                deadzone_dps=0,
                max_rate_dps=180,
                minimum_output=0.0,
            ),
        )
        self.motion_mapping = {
            "imu": MappingAction(
                action_type=ActionType.MOTION_CONTROL,
                motion_control_config=self.config,
            )
        }
        self.motion = make_grabbed_device(
            monkeypatch,
            device_type=DeviceType.MOTION,
            interface_id="motion",
            mapping=self.motion_mapping,
            gamepad_output_resolver=self.resolve,
            motion_sensors={
                "imu": {
                    "source": "motion",
                    "gyro_axes": [
                        {"role": "yaw", "evdev_code": E.ABS_RZ, "scale": math.radians(1)}
                    ],
                    "accelerometer_axes": [
                        {"role": role, "evdev_code": code, "scale": 1}
                        for role, code in (("x", E.ABS_X), ("y", E.ABS_Y), ("z", E.ABS_Z))
                    ],
                }
            },
        )
        self.devices["1234:5678"] = [self.stick, self.motion]
        self.time_us = 1_000_000

    def resolve(self, output_id, context):
        return self.router.resolve(self.outputs, self.devices, output_id, context=context)

    async def frame(self, device, *axes):
        sec, usec = divmod(self.time_us, 1_000_000)
        self.time_us += 10_000
        deps = grabbed_event_processing_deps()
        for code, value in axes:
            await process_event(
                device, evdev.InputEvent(sec, usec, E.EV_ABS, code, value), deps=deps
            )
        await process_event(
            device, evdev.InputEvent(sec, usec, E.EV_SYN, E.SYN_REPORT, 0), deps=deps
        )

    async def gyro(self, rate):
        await self.frame(self.motion, (E.ABS_RZ, -rate))

    def axis(self, code=E.ABS_RX):
        return next(
            value
            for kind, axis, value in reversed(self.output.writes)
            if kind == E.EV_ABS and axis == code
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("base", [0, 12000])
@pytest.mark.parametrize("smoothing", [0.15, 0.99])
async def test_default_minimum_output_does_not_amplify_smoothing_tail(monkeypatch, base, smoothing):
    rig = _Rig(monkeypatch)
    rig.config.gamepad = MotionGamepadConfig(output_id=rig.output_id, smoothing=smoothing)
    await rig.frame(rig.stick, (E.ABS_RX, base))
    await rig.gyro(90)
    assert rig.axis() > base

    await rig.gyro(0)
    assert rig.axis() == base
    await rig.frame(rig.motion)
    assert rig.axis() == base
    # A tiny real input must still receive minimum-output compensation.
    await rig.gyro(0.00001)
    assert rig.axis() > base + 1000


@pytest.mark.asyncio
async def test_gyro_axis_settles_independently_inside_configured_deadzone(monkeypatch):
    rig = _Rig(monkeypatch)
    rig.config.gamepad = MotionGamepadConfig(output_id=rig.output_id, deadzone_dps=1)
    rig.motion.motion_axis_bindings[(E.EV_ABS, E.ABS_RY)] = (
        "imu",
        "gyro",
        "pitch",
        0,
        math.radians(1),
        False,
        0,
    )
    await rig.frame(rig.motion, (E.ABS_RZ, -90), (E.ABS_RY, -90))
    await rig.frame(rig.motion, (E.ABS_RZ, 0), (E.ABS_RY, -45))
    assert rig.axis() == 0
    assert rig.axis(E.ABS_RY) > 8192
    await rig.frame(rig.motion, (E.ABS_RY, -1))
    assert rig.axis(E.ABS_RY) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,smoothing_mode", [("tilt_gamepad", "fixed"), ("analog", "fixed"), ("analog", "adaptive")]
)
async def test_motion_startup_and_profile_reset_seed_unchanged_axes(
    monkeypatch, mode, smoothing_mode
):
    rig = _Rig(monkeypatch)
    rig.config.mode = mode
    rig.config.tilt = MotionTiltConfig(reference="activation", smoothing=0, deadzone_deg=0)
    rig.config.analog = MotionAnalogConfig(
        smoothing=0,
        smoothing_mode=smoothing_mode,
        analog_control_config=AnalogControlConfig(
            name="Tilt stick",
            gamepad_output=AnalogGamepadOutputConfig(
                enabled=True, output_id=rig.output_id, target="right"
            ),
        ),
    )
    values = {E.ABS_RZ: 0, E.ABS_X: 0, E.ABS_Y: 0, E.ABS_Z: 1000}
    absinfo = Mock(side_effect=lambda code: SimpleNamespace(value=values[code]))
    monkeypatch.setattr(
        device_module, "_device_input", lambda path: SimpleNamespace(absinfo=absinfo)
    )
    monkeypatch.setattr(pipeline, "event_loop", AsyncMock())
    rig.motion.access_mode = InputAccessMode.OBSERVE
    await rig.motion.grab()
    assert rig.motion.task is not None
    await rig.motion.task
    assert rig.motion.state.motion_tilt_centers["motion:imu"] == (0, 0)
    assert rig.output.writes == []

    values.update({E.ABS_X: 500, E.ABS_Z: 866})
    await rig.frame(rig.motion, (E.ABS_X, 500), (E.ABS_Z, 866))
    assert rig.axis() < -32000
    await rig.motion.reset_mapping_runtime_state()
    assert rig.axis() == 0
    assert rig.motion.state.motion_adaptive_filters == {}
    center = rig.motion.state.motion_tilt_centers["motion:imu"]
    assert center[0] == pytest.approx(-30, abs=0.01)
    await rig.frame(rig.motion, (E.ABS_X, 866), (E.ABS_Z, 500))
    assert rig.axis() < -32000
    assert rig.motion.state.motion_tilt_centers["motion:imu"] == center
    assert absinfo.call_count == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("virtual", [False, True])
@pytest.mark.parametrize(
    "action_type,target", [(ActionType.GAMEPAD, "btn_south"), (ActionType.GAMEPAD_AXIS, "abs_rx")]
)
async def test_motion_threshold_plans_and_routes_gamepad_output(
    monkeypatch, virtual, action_type, target
):
    rig = _Rig(monkeypatch, virtual=virtual)
    rig.config.mode = "analog"
    rig.config.analog = MotionAnalogConfig(
        source="gyro",
        x_axis="yaw",
        y_axis="none",
        smoothing=0,
        full_scale_dps=90,
        analog_control_config=AnalogControlConfig(
            name="Threshold",
            thresholds=[
                AnalogActionThreshold(
                    axis="x",
                    trigger_min=0.5,
                    trigger_max=1,
                    release_min=0.3,
                    release_max=1,
                    actions=[
                        MappingAction(
                            action_type=action_type,
                            target=target,
                            output_id=rig.output_id,
                            axis_value=20000,
                        )
                    ],
                )
            ],
        ),
    )
    hardware = HardwareConfig(
        "1234",
        "5678",
        "Controller",
        [
            EvdevDevice("/dev/input/event1", DeviceType.GAMEPAD, id="gamepad"),
            EvdevDevice("/dev/input/event2", DeviceType.MOTION, id="motion"),
        ],
        [],
        motion_sensors=[MotionSensorDefinition("imu", "Sensor", source="motion")],
    )
    resolved = ResolvedDeviceProfile(hardware.hardware_id, mappings=rig.motion_mapping)
    manager = SimpleNamespace(resolved_button_codes=lambda buttons: {})
    interfaces = grab_plan.get_interfaces_to_grab(hardware, resolved, manager=manager)
    payload = grab_plan.build_grab_device_payload(
        manager, hardware.hardware_id, hardware, resolved, interfaces
    )
    assert set(interfaces) == ({"motion"} if virtual else {"motion", "gamepad"})
    assert payload["force_grab_unmapped"] is (not virtual)
    rig.devices[hardware.hardware_id] = [rig.motion]
    if "gamepad" in interfaces:
        rig.devices[hardware.hardware_id].append(rig.stick)
    await rig.gyro(90)
    expected = (
        (E.EV_KEY, E.BTN_SOUTH, 1)
        if action_type == ActionType.GAMEPAD
        else (E.EV_ABS, E.ABS_RX, 20000)
    )
    assert expected in rig.output.writes
    await rig.gyro(0)
    assert rig.output.writes[-1][2] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("analog,virtual", [(False, False), (True, False), (True, True)])
async def test_held_stick_survives_idle_gyro_and_mapping_removal(monkeypatch, analog, virtual):
    rig = _Rig(monkeypatch, analog=analog, virtual=virtual)
    await rig.frame(rig.stick, (E.ABS_RX, 12000), (E.ABS_RY, 0))
    base = rig.axis()
    assert base == 12000
    await rig.gyro(0)
    assert rig.axis() == base
    await rig.gyro(45)
    assert rig.axis() == base + 8192
    # A physical update must also reapply the gyro offset.
    await rig.frame(rig.stick, (E.ABS_RX, 10000))
    assert rig.axis() == 18192
    await rig.gyro(0)
    assert rig.axis() == 10000
    await rig.gyro(45)
    rig.motion_mapping.clear()
    await rig.motion.reset_mapping_runtime_state()
    rig.motion.release_tracked_outputs()
    assert rig.axis() == 10000
    await rig.frame(rig.stick, (E.ABS_RX, 0))
    assert rig.axis() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("unsigned,target", [(False, "right"), (True, "right"), (True, "analog")])
async def test_clamp_and_opposite_rotation_keep_unclamped_base(monkeypatch, unsigned, target):
    rig = _Rig(monkeypatch, unsigned=unsigned, target=target)
    await rig.frame(rig.stick, (E.ABS_RX, rig.maximum - 10))
    await rig.gyro(180)
    assert rig.axis() == rig.maximum
    await rig.gyro(180)
    assert rig.axis() == rig.maximum
    await rig.gyro(0)
    assert rig.axis() == rig.maximum - 10
    await rig.gyro(-180)
    assert rig.axis() == rig.maximum - 10 + rig.minimum - rig.center
    await rig.frame(rig.stick, (E.ABS_RX, rig.minimum + 10))
    assert rig.axis() == rig.minimum
    await rig.motion.reset_analog_controls(state_key_prefix="motion:")
    assert rig.axis() == rig.minimum + 10


@pytest.mark.asyncio
async def test_gyro_first_and_base_release(monkeypatch):
    rig = _Rig(monkeypatch, analog=True, virtual=True)
    await rig.gyro(45)
    assert rig.axis() == 8192
    await rig.frame(rig.stick, (E.ABS_RX, 12000))
    assert rig.axis() == 20192
    await rig.stick.reset_analog_controls()
    assert rig.axis() == 8192
    await rig.motion.reset_analog_controls()
    assert rig.axis() == 0


@pytest.mark.asyncio
async def test_other_controller_sticks_are_not_added(monkeypatch):
    rig = _Rig(monkeypatch, analog=True, virtual=True)
    other = make_grabbed_device(
        monkeypatch,
        hardware_id="aaaa:bbbb",
        device_type=DeviceType.GAMEPAD,
        analog_inputs=rig.stick.analog_inputs,
        mapping=rig.mapping,
        gamepad_output_resolver=rig.resolve,
    )
    await rig.frame(rig.stick, (E.ABS_RX, 12000))
    await rig.frame(other, (E.ABS_RX, 4000))
    assert rig.axis() == 4000
    await rig.gyro(45)
    assert rig.axis() == 8192  # No pairing with the other controller's base.
    await rig.frame(other, (E.ABS_RX, 5000))
    assert rig.axis() == 5000
    await rig.frame(rig.stick, (E.ABS_RX, 10000))
    assert rig.axis() == 18192


@pytest.mark.asyncio
async def test_tilt_remains_an_ordinary_stick_writer(monkeypatch):
    rig = _Rig(monkeypatch)
    rig.config.mode = "tilt_gamepad"
    rig.config.tilt = MotionTiltConfig(reference="absolute", smoothing=0, deadzone_deg=0)
    await rig.frame(rig.stick, (E.ABS_RX, 12000))
    await rig.frame(rig.motion, (E.ABS_X, 0), (E.ABS_Y, 0), (E.ABS_Z, 1))
    assert rig.axis() == 0
    await rig.frame(rig.stick, (E.ABS_RX, 10000))
    assert rig.axis() == 10000


@pytest.mark.asyncio
async def test_dropped_motion_frame_restores_held_stick(monkeypatch):
    rig = _Rig(monkeypatch)
    await rig.frame(rig.stick, (E.ABS_RX, 12000))
    await rig.gyro(45)
    await process_event(
        rig.motion,
        evdev.InputEvent(1, 100000, E.EV_SYN, E.SYN_DROPPED, 0),
        deps=grabbed_event_processing_deps(),
    )
    assert rig.axis() == 12000


@pytest.mark.asyncio
async def test_replaced_virtual_output_does_not_inherit_base_or_gyro(monkeypatch):
    rig = _Rig(monkeypatch, analog=True, virtual=True)
    await rig.frame(rig.stick, (E.ABS_RX, 12000))
    await rig.gyro(45)
    rig.output = FakeUInput()
    rig.outputs.virtual_gamepad_uinputs[rig.output_id] = rig.output
    await rig.motion.reset_analog_controls()
    assert rig.output.writes == []
    await rig.gyro(0)
    assert rig.axis() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("unsigned,target", [(False, "right"), (True, "right"), (True, "analog")])
@pytest.mark.parametrize("invert", [False, True])
async def test_minimum_output_preserves_small_motion_and_neutral(
    monkeypatch, unsigned, target, invert
):
    rig = _Rig(monkeypatch, unsigned=unsigned, target=target)
    rig.config.gamepad.minimum_output = 0.1
    rig.config.gamepad.max_rate_dps = 1000
    rig.config.gamepad.invert_x = invert
    await rig.gyro(0)
    assert rig.axis() == rig.center
    # 0.1% gyro would round to neutral on the unsigned output without compensation.
    positive, negative = (141, 115) if unsigned else (3306, -3306)
    await rig.gyro(1)
    assert rig.axis() == (negative if invert else positive)
    assert rig.axis(E.ABS_RY) == rig.center
    await rig.gyro(-1)
    assert rig.axis() == (positive if invert else negative)
    await rig.gyro(0)
    assert rig.axis() == rig.center
    await rig.gyro(1)
    await rig.motion.reset_analog_controls()
    rig.motion.release_tracked_outputs()
    assert rig.axis() == rig.center


@pytest.mark.asyncio
@pytest.mark.parametrize("analog,virtual", [(False, False), (True, True)])
async def test_minimum_output_compensates_combined_axis_only_while_gyro_active(
    monkeypatch, analog, virtual
):
    rig = _Rig(monkeypatch, analog=analog, virtual=virtual)
    rig.config.gamepad.minimum_output = 0.1
    await rig.frame(rig.stick, (E.ABS_RX, 10000))
    assert rig.axis() == 10000
    await rig.gyro(45)
    # Combined 18191.75 is remapped once, rather than boosting gyro independently.
    assert rig.axis() == 19649
    await rig.frame(rig.stick, (E.ABS_RX, 12000))
    assert rig.axis() == 21449
    await rig.gyro(180)
    assert rig.axis() == rig.maximum
    await rig.gyro(0)
    assert rig.axis() == 12000
    await rig.gyro(45)
    await rig.motion.reset_analog_controls()
    assert rig.axis() == 12000


@pytest.mark.asyncio
async def test_minimum_output_does_not_boost_cancelled_combined_axis(monkeypatch):
    rig = _Rig(monkeypatch, unsigned=True)
    rig.config.gamepad.minimum_output = 0.2
    rig.config.gamepad.max_rate_dps = 128
    await rig.frame(rig.stick, (E.ABS_RX, 200))
    await rig.gyro(-72)
    assert rig.axis() == 128
