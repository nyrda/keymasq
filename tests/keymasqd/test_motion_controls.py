import logging
import math
from types import SimpleNamespace

import evdev
import pytest

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.analog import (
    SAME_DEVICE_OUTPUT_ID,
    AnalogActionThreshold,
    AnalogControlConfig,
    AnalogGamepadOutputConfig,
)
from keymasq.common.model.core import ActionType, DeviceType
from keymasq.common.model.hardware import EvdevDevice, HardwareConfig
from keymasq.common.model.motion import (
    MotionAnalogConfig,
    MotionAxisRoutingConfig,
    MotionControlConfig,
    MotionGamepadConfig,
    MotionMouseConfig,
    MotionSensorDefinition,
    MotionTiltConfig,
)
from keymasq.common.model.profiles import DeviceProfileLayer, ProfileConfig
from keymasq.keymasqd import device_inventory
from keymasq.keymasqd.runtime.action_parser import parse_action
from keymasq.keymasqd.runtime.analog.reset import reset_analog_controls
from keymasq.keymasqd.runtime.grabbed_device.event.pipeline import build_action_execution_deps
from keymasq.keymasqd.runtime.grabbed_device.types import GrabbedDeviceState
from keymasq.keymasqd.runtime.motion_controls import dispatch_motion_event
from keymasq.keymasqd.runtime.virtual_gamepads import GamepadOutputRouter
from keymasq.session.manager.core import SessionManager
from keymasq.session.manager.payload.action import mapping_action_payload
from keymasq.session.manager.profile import grab_plan
from keymasq.session.motion_controls import MotionControlManager
from keymasq.session.profile.codec import ProfileCodec
from keymasq.session.profile.types import ResolvedDeviceProfile


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
        self.analog_reset_prefixes: list[str | None] = []
        self.device = None
        self.event_time_us = 1_000_000

    def next_syn(self) -> evdev.InputEvent:
        sec, usec = divmod(self.event_time_us, 1_000_000)
        self.event_time_us += 10_000
        return evdev.InputEvent(sec, usec, evdev.ecodes.EV_SYN, evdev.ecodes.SYN_REPORT, 0)

    async def reset_analog_controls(
        self,
        preserve_state_keys: set[str] | None = None,
        *,
        state_key_prefix: str | None = None,
    ) -> None:
        del preserve_state_keys
        self.analog_reset_prefixes.append(state_key_prefix)

    def reset_motion_controls(self) -> None:
        self.state.motion_frame_values.clear()
        self.state.motion_smoothed_values.clear()
        self.state.motion_last_frame_ns.clear()
        self.state.motion_mouse_accumulators.clear()
        self.state.motion_tilt_centers.clear()

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
        runtime.next_syn(),
        mapping,
        deps=deps,
    )


async def _send_motion_frame(runtime, mapping, timestamp_us, values) -> None:
    sec, usec = divmod(timestamp_us, 1_000_000)
    deps = build_action_execution_deps()
    for code, value in values.items():
        await dispatch_motion_event(
            runtime,
            evdev.InputEvent(sec, usec, evdev.ecodes.EV_ABS, code, value),
            mapping,
            deps=deps,
        )
    await dispatch_motion_event(
        runtime,
        evdev.InputEvent(sec, usec, evdev.ecodes.EV_SYN, evdev.ecodes.SYN_REPORT, 0),
        mapping,
        deps=deps,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["mouse", "tilt_mouse"])
async def test_motion_mouse_uses_event_time_independent_of_processing_schedule(monkeypatch, mode):
    config = MotionControlConfig(
        name="Aim",
        mode=mode,
        mouse=MotionMouseConfig(deadzone_dps=0.0, smoothing=0.0),
        tilt=MotionTiltConfig(
            reference="gravity",
            smoothing=0.0,
            deadzone_deg=0.0,
            speed_x=800.0,
        ),
    )
    mapping = {
        "motion_1": MappingAction(
            action_type=ActionType.MOTION_CONTROL,
            motion_control_config=config,
        )
    }
    outputs = []
    for processing_times in (
        [1_000_000_000, 1_010_000_000, 1_020_000_000],
        [9_000_000_000, 9_000_010_000, 9_000_020_000],
    ):
        runtime = _Runtime()
        clock = iter(processing_times)
        monkeypatch.setattr(
            "keymasq.keymasqd.runtime.motion_controls.time.monotonic_ns",
            clock.__next__,
        )
        values = (
            {evdev.ecodes.ABS_RZ: 100}
            if mode == "mouse"
            else {evdev.ecodes.ABS_X: -1000, evdev.ecodes.ABS_Y: 0, evdev.ecodes.ABS_Z: 0}
        )
        for timestamp in (1_000_000, 1_010_000, 1_020_000):
            await _send_motion_frame(runtime, mapping, timestamp, values)
        outputs.append(runtime.mouse_uinput.events)
    assert outputs[0] == outputs[1]
    assert sum(
        value
        for kind, code, value in outputs[0]
        if kind == evdev.ecodes.EV_REL and code == evdev.ecodes.REL_X
    ) == (-16 if mode == "mouse" else 16)


@pytest.mark.asyncio
async def test_motion_timestamp_discontinuities_do_not_create_mouse_jumps(monkeypatch):
    monkeypatch.setattr("keymasq.keymasqd.runtime.motion_controls.time.monotonic_ns", lambda: 0)
    runtime = _Runtime()
    config = MotionControlConfig(name="Aim", mouse=MotionMouseConfig(deadzone_dps=0, smoothing=0))
    mapping = {
        "motion_1": MappingAction(
            action_type=ActionType.MOTION_CONTROL,
            motion_control_config=config,
        )
    }
    for timestamp in (1_000_000, 1_000_000, 990_000):
        await _send_motion_frame(runtime, mapping, timestamp, {evdev.ecodes.ABS_RZ: 100})
    assert runtime.mouse_uinput.events == []
    await _send_motion_frame(runtime, mapping, 1_000_000, {evdev.ecodes.ABS_RZ: 100})
    assert runtime.mouse_uinput.events == [
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, -8),
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, 0),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["gamepad", "tilt_gamepad"])
async def test_dropped_motion_frame_releases_output_and_resyncs_unchanged_axes(mode):
    runtime = _Runtime()
    writer = _Writer()
    runtime.resolve_gamepad_output = lambda *_: SimpleNamespace(  # type: ignore[method-assign]
        uinput=writer,
        bucket="gamepad:test",
        is_virtual=True,
    )
    deps = build_action_execution_deps()
    runtime.reset_analog_controls = lambda **kwargs: reset_analog_controls(  # type: ignore[method-assign]
        runtime,
        deps=deps,
        **kwargs,
    )
    config = MotionControlConfig(
        name="Tilt",
        mode=mode,
        gamepad=MotionGamepadConfig(smoothing=0, deadzone_dps=0),
        tilt=MotionTiltConfig(reference="gravity", deadzone_deg=0, smoothing=0),
    )
    mapping = {
        "motion_1": MappingAction(
            action_type=ActionType.MOTION_CONTROL,
            motion_control_config=config,
        )
    }
    runtime.state.analog_axis_values["other"] = {"x": 0.25}
    await _send_motion_frame(
        runtime,
        mapping,
        1_000_000,
        {
            evdev.ecodes.ABS_X: 500,
            evdev.ecodes.ABS_Y: 0,
            evdev.ecodes.ABS_Z: 866,
            evdev.ecodes.ABS_RZ: 90,
        },
    )
    assert writer.events[0][2] < 0
    writer.events.clear()
    await dispatch_motion_event(
        runtime,
        evdev.InputEvent(
            1,
            10000,
            evdev.ecodes.EV_SYN,
            evdev.ecodes.SYN_DROPPED,
            0,
        ),
        mapping,
        deps=deps,
    )
    assert writer.events == [
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RX, 0),
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RY, 0),
    ]
    writer.events.clear()
    values = {
        evdev.ecodes.ABS_X: -500,
        evdev.ecodes.ABS_Y: 0,
        evdev.ecodes.ABS_Z: 866,
        evdev.ecodes.ABS_RZ: -90,
    }
    runtime.device = SimpleNamespace(
        absinfo=lambda code: SimpleNamespace(value=values.get(code, 0))
    )
    await _send_motion_frame(
        runtime,
        mapping,
        1_020_000,
        {
            evdev.ecodes.ABS_X: 900,
            evdev.ecodes.ABS_RZ: 180,
        },
    )
    assert writer.events == []
    # Only Y changes after resync; X and Z must come from the kernel snapshot.
    await _send_motion_frame(runtime, mapping, 1_030_000, {evdev.ecodes.ABS_Y: 1})
    assert writer.events[0][2] > 0
    assert runtime.state.analog_axis_values["other"] == {"x": 0.25}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["gamepad", "tilt_gamepad", "analog", "mouse"])
@pytest.mark.parametrize("output_id", [SAME_DEVICE_OUTPUT_ID, "virtual-gamepad-1"])
async def test_motion_only_profile_plans_and_routes_its_gamepad_output(mode, output_id):
    manager = SessionManager()
    config = MotionControlConfig(
        name="Motion",
        mode=mode,
        gamepad=MotionGamepadConfig(output_id=output_id),
        tilt=MotionTiltConfig(reference="gravity", smoothing=0, deadzone_deg=0),
    )
    if mode == "analog":
        manager.analog_controls.save_analog_control(
            AnalogControlConfig(
                name="Stick",
                input_type="stick",
                gamepad_output=AnalogGamepadOutputConfig(
                    enabled=True, output_id=output_id, target="right"
                ),
            )
        )
        config.analog = MotionAnalogConfig(
            analog_control_name="Stick",
            reference="gravity",
            smoothing=0,
        )
    manager.motion_controls.save_motion_control(config)
    hardware = HardwareConfig(
        "054c",
        "0ce6",
        "Controller",
        [
            EvdevDevice("/dev/input/event1", DeviceType.GAMEPAD, id="gamepad"),
            EvdevDevice("/dev/input/event2", DeviceType.MOTION, id="motion"),
        ],
        [],
        motion_sensors=[MotionSensorDefinition("motion_1", "Sensor", source="motion")],
    )
    action = MappingAction(action_type=ActionType.MOTION_CONTROL, motion_control_name="Motion")
    resolved = ResolvedDeviceProfile(hardware.hardware_id, mappings={"motion_1": action})
    interfaces = grab_plan.get_interfaces_to_grab(hardware, resolved, manager=manager)
    needs_gamepad = mode != "mouse" and output_id == SAME_DEVICE_OUTPUT_ID
    assert set(interfaces) == ({"motion", "gamepad"} if needs_gamepad else {"motion"})
    payload = grab_plan.build_grab_device_payload(
        manager,
        hardware.hardware_id,
        hardware,
        resolved,
        interfaces,
    )
    assert payload["force_grab_unmapped"] is needs_gamepad
    writer = _Writer()
    devices = [SimpleNamespace(device_type=DeviceType.MOTION, device_types=["motion"], uinput=None)]
    if "gamepad" in interfaces:
        devices.append(SimpleNamespace(device_type=DeviceType.GAMEPAD, uinput=writer))
    router = GamepadOutputRouter(logging.getLogger("test"))
    runtime = _Runtime()
    runtime.resolve_gamepad_output = lambda output, context: router.resolve(  # type: ignore[method-assign]
        SimpleNamespace(virtual_gamepad_uinputs={"virtual-gamepad-1": writer}),
        {hardware.hardware_id: devices},
        output,
        context=context,
    )
    parsed = parse_action(manager, mapping_action_payload(manager, action, hardware.hardware_id))
    await _send_motion_frame(
        runtime,
        {"motion_1": parsed},
        1_000_000,
        {
            evdev.ecodes.ABS_RZ: 90,
            evdev.ecodes.ABS_X: -500,
            evdev.ecodes.ABS_Y: 0,
            evdev.ecodes.ABS_Z: 866,
        },
    )
    if mode != "mouse":
        assert any(value != 0 for _kind, _code, value in writer.events)
    else:
        assert writer.events == []


@pytest.mark.asyncio
async def test_syn_dropped_clears_motion_state_with_scoped_analog_reset() -> None:
    runtime = _Runtime()
    runtime.state.motion_frame_values["motion_1"] = {"gyro": {"yaw": 1.0}}
    runtime.state.motion_smoothed_values["motion:motion_1"] = {"x": 1.0, "y": 0.0}
    runtime.state.motion_last_frame_ns["motion:motion_1"] = 123
    runtime.state.motion_mouse_accumulators["motion:motion_1"] = (0.5, 0.0)
    runtime.state.motion_tilt_centers["motion:motion_1"] = (1.0, 2.0)
    dropped = SimpleNamespace(
        type=evdev.ecodes.EV_SYN,
        code=evdev.ecodes.SYN_DROPPED,
        value=0,
    )

    assert await dispatch_motion_event(runtime, dropped, {}, deps=build_action_execution_deps())

    assert runtime.state.motion_frame_values == {}
    assert runtime.state.motion_smoothed_values == {}
    assert runtime.state.motion_last_frame_ns == {}
    assert runtime.state.motion_mouse_accumulators == {}
    assert runtime.state.motion_tilt_centers == {}
    assert runtime.analog_reset_prefixes == ["motion:"]


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


def test_gyro_stick_minimum_output_survives_storage_and_runtime_payload(temp_config_dir):
    manager = SessionManager()
    config = MotionControlConfig(
        name="Stick Aim",
        mode="gamepad",
        gamepad=MotionGamepadConfig(minimum_output=0.12, max_rate_dps=180, deadzone_dps=0.5),
    )
    manager.motion_controls.save_motion_control(config)
    loaded = MotionControlManager().get_motion_control(config.name)
    assert loaded == config
    action = MappingAction(action_type=ActionType.MOTION_CONTROL, motion_control_name=config.name)
    parsed = parse_action(manager, mapping_action_payload(manager, action, "controller"))
    assert parsed.motion_control_config == config


@pytest.mark.parametrize(
    "saved_settings,expected",
    [
        ({}, (90.0, 0.0, 0.25)),
        ({"max_rate_dps": 360.0, "deadzone_dps": 1.0}, (360.0, 1.0, 0.25)),
        ({"minimum_output": 0.0}, (90.0, 0.0, 0.0)),
    ],
)
def test_gyro_stick_defaults_and_existing_settings(temp_config_dir, saved_settings, expected):
    directory = temp_config_dir / "motion_controls"
    directory.mkdir(exist_ok=True)
    settings = "\n".join(f"{key} = {value}" for key, value in saved_settings.items())
    (directory / "aim.toml").write_text(f'name = "Aim"\nmode = "gamepad"\n[gamepad]\n{settings}\n')
    loaded = MotionControlManager().get_motion_control("Aim")
    assert loaded is not None
    parsed = parse_action(
        SimpleNamespace(),
        {
            "action": "motion_control",
            "motion_control": {"name": "Aim", "mode": "gamepad", "gamepad": saved_settings},
        },
    )
    assert parsed.motion_control_config is not None
    for config in (loaded.gamepad, parsed.motion_control_config.gamepad):
        assert (config.max_rate_dps, config.deadzone_dps, config.minimum_output) == expected
    defaults = MotionGamepadConfig()
    assert (defaults.max_rate_dps, defaults.deadzone_dps, defaults.minimum_output) == (
        90.0,
        0.0,
        0.25,
    )


def test_tilt_motion_control_manager_round_trip(temp_config_dir) -> None:
    manager = MotionControlManager()
    config = MotionControlConfig(
        name="Tilt Mouse",
        mode="tilt_mouse",
        tilt=MotionTiltConfig(
            reference="gravity",
            pitch="none",
            roll="horizontal",
            deadzone_deg=3.0,
            full_scale_deg=40.0,
            speed_x=640.0,
            speed_y=360.0,
        ),
    )

    manager.save_motion_control(config)

    assert MotionControlManager().get_motion_control("Tilt Mouse") == config
    content = (temp_config_dir / "motion_controls" / "tilt_mouse.toml").read_text()
    assert 'mode = "tilt_mouse"' in content
    assert "[tilt]" in content
    assert 'reference = "gravity"' in content
    assert "speed_x = 640.0" in content
    assert "speed_y = 360.0" in content


def test_motion_to_analog_manager_round_trip(temp_config_dir) -> None:
    manager = MotionControlManager()
    config = MotionControlConfig(
        name="Tilt Directions",
        mode="analog",
        analog=MotionAnalogConfig(
            analog_control_name="Direction Actions",
            source="tilt",
            x_axis="roll",
            y_axis="pitch",
            reference="gravity",
            full_scale_deg=40.0,
            smoothing=0.4,
            invert_y=True,
        ),
    )

    manager.save_motion_control(config)

    assert MotionControlManager().get_motion_control("Tilt Directions") == config
    content = (temp_config_dir / "motion_controls" / "tilt_directions.toml").read_text()
    assert 'mode = "analog"' in content
    assert "[analog]" in content
    assert 'analog_control_name = "Direction Actions"' in content


def test_motion_manager_updates_attached_analog_control_references(temp_config_dir) -> None:
    manager = MotionControlManager()
    manager.save_motion_control(
        MotionControlConfig(
            name="Tilt Directions",
            mode="analog",
            analog=MotionAnalogConfig(analog_control_name="Old Actions"),
        )
    )

    assert manager.rename_analog_control_references("Old Actions", "New Actions") == 1
    renamed = MotionControlManager().get_motion_control("Tilt Directions")
    assert renamed is not None
    assert renamed.analog.analog_control_name == "New Actions"

    assert manager.clear_analog_control_references("New Actions") == 1
    cleared = MotionControlManager().get_motion_control("Tilt Directions")
    assert cleared is not None
    assert cleared.analog.analog_control_name is None


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


def test_motion_control_profile_fan_out_round_trip_and_runtime_resolution() -> None:
    attached_analog = AnalogControlConfig(name="Direction Actions", input_type="stick")
    configs = {
        "Gyro Aim": MotionControlConfig(name="Gyro Aim"),
        "Tilt Directions": MotionControlConfig(
            name="Tilt Directions",
            mode="analog",
            analog=MotionAnalogConfig(analog_control_name=attached_analog.name),
        ),
    }
    profile = ProfileConfig(
        name="Motion Profile",
        device_layers={
            "controller": DeviceProfileLayer(
                hardware_id="controller",
                mappings={
                    "motion_1": MappingAction(
                        action_type=ActionType.MOTION_CONTROL,
                        motion_control_names=list(configs),
                    )
                },
            )
        },
    )
    codec = ProfileCodec(motion_control_exists=lambda name: name in configs)

    decoded = codec.decode(codec.encode(profile), default_name="fallback").config
    action = decoded.device_layers["controller"].mappings["motion_1"]
    manager = SimpleNamespace(
        motion_controls=SimpleNamespace(get_motion_control=configs.get),
        analog_controls=SimpleNamespace(
            get_analog_control=lambda name: (
                attached_analog if name == attached_analog.name else None
            )
        ),
    )
    payload = mapping_action_payload(manager, action, "controller")

    assert action.motion_control_names == ["Gyro Aim", "Tilt Directions"]
    assert [config["name"] for config in payload["motion_controls"]] == [
        "Gyro Aim",
        "Tilt Directions",
    ]
    assert payload["motion_controls"][1]["analog"]["analog_control"]["name"] == (
        "Direction Actions"
    )
    runtime_action = parse_action(SimpleNamespace(), payload)
    assert len(runtime_action.motion_control_configs) == 2
    assert runtime_action.motion_control_configs[1].analog.analog_control_config == attached_analog


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
async def test_motion_mouse_combines_yaw_and_roll_with_natural_pitch() -> None:
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

    assert await dispatch_motion_event(runtime, yaw, mapping, deps=deps)
    assert await dispatch_motion_event(runtime, roll, mapping, deps=deps)
    assert await dispatch_motion_event(runtime, pitch, mapping, deps=deps)
    assert await dispatch_motion_event(runtime, runtime.next_syn(), mapping, deps=deps)
    assert await dispatch_motion_event(runtime, yaw, mapping, deps=deps)
    assert await dispatch_motion_event(runtime, roll, mapping, deps=deps)
    assert await dispatch_motion_event(runtime, pitch, mapping, deps=deps)
    assert await dispatch_motion_event(runtime, runtime.next_syn(), mapping, deps=deps)

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
    deps = build_action_execution_deps()
    yaw = SimpleNamespace(
        type=evdev.ecodes.EV_ABS,
        code=evdev.ecodes.ABS_RZ,
        value=90,
    )

    assert await dispatch_motion_event(runtime, yaw, mapping, deps=deps)
    assert await dispatch_motion_event(runtime, runtime.next_syn(), mapping, deps=deps)
    assert await dispatch_motion_event(runtime, yaw, mapping, deps=deps)
    assert await dispatch_motion_event(runtime, runtime.next_syn(), mapping, deps=deps)

    expected = runtime.hardware_id if expected_output == "origin" else expected_output
    assert resolved == [expected, expected]
    assert any(event_type == evdev.ecodes.EV_ABS for event_type, _code, _value in gamepad.events)


@pytest.mark.asyncio
async def test_tilt_mouse_keeps_moving_while_tilt_is_held() -> None:
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
async def test_tilt_stick_uses_the_selected_gamepad_output() -> None:
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
    deps = build_action_execution_deps()

    await _send_accelerometer_frame(runtime, mapping, deps, 0, 0, 1000)
    await _send_accelerometer_frame(runtime, mapping, deps, -1000, 0, 0)

    assert resolved == ["virtual-gamepad-1", "virtual-gamepad-1"]
    assert any(event_type == evdev.ecodes.EV_ABS for event_type, _code, _value in gamepad.events)


@pytest.mark.asyncio
async def test_tilt_stick_pitch_outputs_both_axis_directions() -> None:
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
async def test_motion_fan_out_runs_tilt_mouse_and_attached_analog_control() -> None:
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
    tilt_mouse = MotionControlConfig(
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
    analog_control = AnalogControlConfig(
        name="Stick Output",
        input_type="stick",
        gamepad_output=AnalogGamepadOutputConfig(
            enabled=True,
            output_id="virtual-gamepad-1",
            target="right",
        ),
    )
    motion_to_analog = MotionControlConfig(
        name="Tilt Analog",
        mode="analog",
        analog=MotionAnalogConfig(
            analog_control_name=analog_control.name,
            analog_control_config=analog_control,
            source="tilt",
            x_axis="roll",
            y_axis="pitch",
            full_scale_deg=30.0,
            smoothing=0.0,
        ),
    )
    mapping = {
        "motion_1": MappingAction(
            action_type=ActionType.MOTION_CONTROL,
            motion_control_configs=[tilt_mouse, motion_to_analog],
        )
    }
    deps = build_action_execution_deps()

    await _send_accelerometer_frame(runtime, mapping, deps, 0, 0, 1000)
    await _send_accelerometer_frame(runtime, mapping, deps, -500, 0, 866)

    assert (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 1) in runtime.mouse_uinput.events
    right_stick_x = [
        value
        for event_type, code, value in gamepad.events
        if event_type == evdev.ecodes.EV_ABS and code == evdev.ecodes.ABS_RX
    ]
    assert right_stick_x[-1] > 0
    assert "motion:motion_1:control:0" in runtime.state.motion_tilt_centers
    assert "motion:motion_1:control:1" in runtime.state.analog_axis_values


@pytest.mark.asyncio
async def test_motion_to_analog_uses_signed_axis_thresholds() -> None:
    runtime = _Runtime()
    analog_control = AnalogControlConfig(
        name="Yaw Actions",
        input_type="axis",
        thresholds=[
            AnalogActionThreshold(
                axis="x",
                trigger_min=0.5,
                trigger_max=1.0,
                release_min=0.4,
                release_max=1.0,
            )
        ],
    )
    config = MotionControlConfig(
        name="Yaw Analog",
        mode="analog",
        analog=MotionAnalogConfig(
            analog_control_name=analog_control.name,
            analog_control_config=analog_control,
            source="gyro",
            x_axis="yaw",
            full_scale_dps=180.0,
            smoothing=0.0,
        ),
    )
    mapping = {
        "motion_1": MappingAction(
            action_type=ActionType.MOTION_CONTROL,
            motion_control_config=config,
        )
    }
    deps = build_action_execution_deps()

    yaw = SimpleNamespace(
        type=evdev.ecodes.EV_ABS,
        code=evdev.ecodes.ABS_RZ,
        value=-180,
    )
    assert await dispatch_motion_event(runtime, yaw, mapping, deps=deps)
    assert await dispatch_motion_event(runtime, runtime.next_syn(), mapping, deps=deps)

    state_key = "motion:motion_1"
    assert runtime.state.analog_axis_values[state_key]["x_signed"] == 1.0
    assert runtime.state.analog_active_thresholds[state_key] == {f"{state_key}:0"}
