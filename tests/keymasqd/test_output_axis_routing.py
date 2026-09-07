"""Hardware capability discovery through calibration, routing, emission, and reset."""

import asyncio
import copy
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import evdev
import pytest

from keymasq.common.model.analog import AnalogControlConfig, AnalogGamepadOutputConfig
from keymasq.common.model.core import DeviceType
from keymasq.keymasqd.runtime.adapters import identity_uinput_writer
from keymasq.keymasqd.runtime.analog.gamepad import emit_gamepad_output, reset_gamepad_output
from keymasq.keymasqd.runtime.grabbed_device.device import GrabbedDevice
from keymasq.keymasqd.runtime.grabbed_device.types import ActionExecutionDeps
from keymasq.keymasqd.runtime.virtual_gamepads import GamepadOutputRouter


class OutputWriter:
    def __init__(self) -> None:
        self.events: list[tuple[int, int, int]] = []

    def write(self, event_type: int, code: int, value: int) -> None:
        self.events.append((event_type, code, value))

    def syn(self) -> None:
        pass


def _hardware(axis, *, absinfo):
    device = GrabbedDevice(
        path="/dev/input/test-output-axis",
        hardware_id="hardware",
        button_map={},
        mapping_getter=lambda: {},
        event_callback=AsyncMock(),
        device_type=DeviceType.GAMEPAD,
    )
    device.device = SimpleNamespace(absinfo=lambda _code: absinfo)
    device.uinput = OutputWriter()
    device.update_analog_inputs({"left_trigger": {"type": "axis", "axes": [axis]}})
    return device


def _router_target(device):
    router = GamepadOutputRouter(logging.getLogger(__name__))
    return router.resolve(SimpleNamespace(), {"hardware": [device]}, "hardware")


@pytest.mark.parametrize("identity", [{"evdev": "ABS_Z"}, {"evdev_code": 2}])
@pytest.mark.parametrize("saved_range", [False, True])
@pytest.mark.parametrize("target_kind", ["same", "left", "axis", "analog"])
def test_hardware_axis_routes_with_saved_or_discovered_metadata(identity, saved_range, target_kind):
    axis = {"role": "x", **identity}
    if saved_range:
        axis.update(minimum=100, maximum=900, rest=200)
    original = copy.deepcopy(axis)
    device = _hardware(axis, absinfo=evdev.AbsInfo(200, 100, 900, 0, 0, 0))
    original_inputs = copy.deepcopy(device.analog_inputs)
    target = _router_target(device)
    assert target is not None and target.output_axes
    spec = target.output_axes[0]
    assert (spec.evdev, spec.minimum, spec.maximum, spec.neutral) == ("ABS_Z", 100, 900, 200)
    assert device.analog_inputs == original_inputs
    assert axis == original
    device._gamepad_output_resolver = lambda _id, _context: target
    device.state.analog_axis_values["left_trigger"] = {"x": 1.0, "x_signed": 1.0}
    config = AnalogControlConfig(
        name="Route",
        input_type="axis",
        gamepad_output=AnalogGamepadOutputConfig(
            enabled=True,
            output_id="hardware",
            target=target_kind,
            target_axis="abs_z" if target_kind == "axis" else None,
            target_analog_id="left_trigger" if target_kind == "analog" else None,
        ),
    )
    deps = ActionExecutionDeps(
        asyncio_mod=asyncio,
        evdev_mod=evdev,
        uinput_writer=identity_uinput_writer,
        fire_and_observe_fn=lambda coro, _label: asyncio.create_task(coro),
    )
    emit_gamepad_output(device, "left_trigger", "left_trigger", config, deps=deps)
    reset_gamepad_output(device, "left_trigger", "left_trigger", config, deps=deps)
    assert device.uinput.events == [
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 900),
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 200),
    ]


def test_hardware_axis_preserves_saved_calibration_over_absinfo() -> None:
    device = _hardware(
        {"role": "x", "evdev": "ABS_Z", "minimum": 10, "maximum": 500, "rest": 50},
        absinfo=evdev.AbsInfo(0, 0, 1023, 0, 0, 0),
    )
    target = _router_target(device)
    assert target is not None and target.output_axes
    axis = target.output_axes[0]
    assert (axis.minimum, axis.maximum, axis.neutral) == (10, 500, 50)


def test_hardware_axis_without_saved_or_runtime_range_is_not_advertised() -> None:
    device = _hardware({"role": "x", "evdev": "ABS_Z"}, absinfo=None)
    target = _router_target(device)
    assert target is not None
    assert target.output_axes == ()


def test_hardware_axis_can_use_cached_range_without_calibration() -> None:
    device = _hardware({"role": "x", "evdev": "ABS_Z"}, absinfo=None)
    device.analog_axis_ranges[("left_trigger", "x")] = (100, 900)
    target = _router_target(device)
    assert target is not None and target.output_axes
    axis = target.output_axes[0]
    assert (axis.minimum, axis.maximum, axis.neutral) == (100, 900, 100)


@pytest.mark.parametrize("mode", ["release", "tap", "rapidfire", "cleanup"])
def test_physical_axis_action_returns_to_calibrated_neutral(mode):
    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.core import ActionType
    from keymasq.keymasqd.runtime.action.outputs import execute_gamepad_axis_action

    device = _hardware(
        {"role": "x", "evdev": "ABS_RZ", "minimum": 0, "maximum": 255, "rest": 127},
        absinfo=evdev.AbsInfo(127, 0, 255, 0, 0, 0),
    )
    target = _router_target(device)
    device._gamepad_output_resolver = lambda *_args: target
    device.running = True
    action = MappingAction(
        action_type=ActionType.GAMEPAD_AXIS,
        target="abs_rz",
        axis_value=255,
        output_id="hardware",
        tap_enabled=mode == "tap",
        rapidfire_enabled=mode == "rapidfire",
        tap_hold_ms=1,
        rapidfire_hold_ms=1,
        rapidfire_wait_ms=1,
    )
    tasks = []

    def start(coro, _label):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    deps = ActionExecutionDeps(
        asyncio_mod=asyncio,
        evdev_mod=evdev,
        uinput_writer=identity_uinput_writer,
        fire_and_observe_fn=start,
    )

    async def run():
        await execute_gamepad_axis_action(
            device,
            action,
            SimpleNamespace(value=1),
            "key_a",
            deps=deps,
        )
        if mode == "tap":
            await asyncio.gather(*tasks)
        elif mode == "rapidfire":
            # Wait for an actual pulse before releasing, without relying on sleep timing.
            async with asyncio.timeout(1):
                while len(device.uinput.events) < 2:
                    await asyncio.sleep(0)
            await execute_gamepad_axis_action(
                device,
                action,
                SimpleNamespace(value=0),
                "key_a",
                deps=deps,
            )
        elif mode == "cleanup":
            device.release_tracked_outputs()
        else:
            await execute_gamepad_axis_action(
                device,
                action,
                SimpleNamespace(value=0),
                "key_a",
                deps=deps,
            )

    asyncio.run(run())
    values = [value for _type, code, value in device.uinput.events if code == evdev.ecodes.ABS_RZ]
    assert values[0] == 255
    assert values[-1] == 127
    assert set(values) == {127, 255}


@pytest.mark.parametrize("primary_type", [DeviceType.GAMEPAD, DeviceType.KEYBOARD])
def test_physical_output_inventory_matches_router_interface_and_calibration(primary_type):
    from keymasq.keymasqd.device_inventory import recording_virtual_device_metadata

    first = _hardware(
        {"role": "x", "evdev": "ABS_THROTTLE", "minimum": 0, "maximum": 255},
        absinfo=evdev.AbsInfo(255, 0, 255, 0, 0, 0),
    )
    second = _hardware(
        {"role": "x", "evdev": "ABS_RZ"}, absinfo=evdev.AbsInfo(127, 0, 255, 0, 0, 0)
    )
    first.device_type = primary_type
    first.device_types = [primary_type.value, "gamepad"]
    first.interface_id, second.interface_id = "one", "two"
    first.uinput.device = SimpleNamespace(path="/dev/input/first-output")
    second.uinput.device = SimpleNamespace(path="/dev/input/second-output")
    first.analog_inputs["other"] = {
        "source": "two",
        "type": "axis",
        "axes": [{"role": "x", "evdev": "ABS_RZ", "minimum": 0, "maximum": 255, "rest": 127}],
    }
    grabbed = {"hardware": [first, second]}
    target = GamepadOutputRouter(logging.getLogger(__name__)).resolve(
        SimpleNamespace(), grabbed, "hardware"
    )
    metadata = recording_virtual_device_metadata(SimpleNamespace(), grabbed)
    assert target.uinput is first.uinput
    assert "gamepad_output" not in metadata["/dev/input/second-output"]
    analogs = metadata["/dev/input/first-output"]["gamepad_output"]["analog_inputs"]
    assert "other" not in analogs
    assert analogs["left_trigger"]["axes"][0]["rest"] == 255
    assert target.axis_rest_values[evdev.ecodes.ABS_THROTTLE] == 255
