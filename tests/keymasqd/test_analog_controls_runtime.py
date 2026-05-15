import asyncio
from types import SimpleNamespace

import evdev
import pytest

from keymasq.common.models import (
    ActionType,
    AnalogActionThreshold,
    AnalogControlConfig,
    AnalogGamepadOutputConfig,
    MappingAction,
)
from keymasq.keymasqd.runtime.analog_controls import (
    normalize_axis_value,
    normalize_trigger_value,
    process_analog_event,
    reset_analog_controls,
)
from keymasq.keymasqd.runtime.grabbed_device_types import (
    ActionExecutionDeps,
    GrabbedDeviceState,
    identity_uinput_writer,
)


class FakeUInput:
    def __init__(self) -> None:
        self.events: list[tuple[int, int, int]] = []

    def write(self, event_type: int, code: int, value: int) -> None:
        self.events.append((event_type, code, value))

    def syn(self) -> None:
        pass


class FakeEvent:
    type = evdev.ecodes.EV_ABS
    code = evdev.ecodes.ABS_X

    def __init__(self, value: int) -> None:
        self.value = value


def _deps() -> ActionExecutionDeps:
    return ActionExecutionDeps(
        asyncio_mod=asyncio,
        fire_and_observe_fn=lambda coro, _label: asyncio.create_task(coro),
        evdev_mod=evdev,
        uinput_writer=identity_uinput_writer,
    )


def _runtime(mapping: dict[str, MappingAction], keyboard: FakeUInput) -> SimpleNamespace:
    return SimpleNamespace(
        hardware_id="1234:5678",
        keyboard_uinput=keyboard,
        mouse_uinput=FakeUInput(),
        gamepad_uinput=None,
        uinput=None,
        broadcast_callback=None,
        cursor_position_setter=None,
        recording_manager=None,
        macro_player=None,
        emergency_resetter=None,
        suppress_rel_getter=None,
        diagnostics_recorder=None,
        runtime_cleanup_callback=None,
        mapping_getter=lambda: mapping,
        state=GrabbedDeviceState(),
        analog_axis_bindings={(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X): ("left_stick", "x")},
        analog_axis_ranges={("left_stick", "x"): (-32768, 32767)},
        resolve_gamepad_output=lambda _output_id, _context: None,
    )


def test_normalize_axis_value_maps_sides_independently() -> None:
    assert normalize_axis_value(-32768, -32768, 32767) == pytest.approx(-1.0)
    assert normalize_axis_value(32767, -32768, 32767) == pytest.approx(1.0)
    assert abs(normalize_axis_value(0, -32768, 32767)) < 0.001


def test_normalize_trigger_value_maps_min_to_zero_and_max_to_one() -> None:
    assert normalize_trigger_value(0, 0, 255) == pytest.approx(0.0)
    assert normalize_trigger_value(255, 0, 255) == pytest.approx(1.0)
    assert normalize_trigger_value(128, 0, 255) == pytest.approx(0.5019, abs=0.0001)


@pytest.mark.asyncio
async def test_threshold_enter_and_release_emit_child_actions() -> None:
    keyboard = FakeUInput()
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Test",
                thresholds=[
                    AnalogActionThreshold(
                        axis="x",
                        trigger_min=0.65,
                        trigger_max=1.0,
                        release_min=0.55,
                        release_max=1.0,
                        actions=[MappingAction(action_type=ActionType.KEYBOARD, target="key_a")],
                    )
                ],
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)

    assert await process_analog_event(runtime, FakeEvent(32767), "abs_x", mapping, deps=_deps())
    assert keyboard.events[-1] == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)

    assert await process_analog_event(runtime, FakeEvent(0), "abs_x", mapping, deps=_deps())
    assert keyboard.events[-1] == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0)


@pytest.mark.asyncio
async def test_trigger_threshold_uses_positive_normalized_range() -> None:
    keyboard = FakeUInput()
    mapping = {
        "left_trigger": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Trigger",
                input_type="trigger",
                thresholds=[
                    AnalogActionThreshold(
                        axis="x",
                        trigger_min=0.5,
                        trigger_max=1.0,
                        release_min=0.45,
                        release_max=1.0,
                        actions=[MappingAction(action_type=ActionType.KEYBOARD, target="key_a")],
                    )
                ],
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.analog_axis_bindings = {
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z): ("left_trigger", "x")
    }
    runtime.analog_axis_ranges = {("left_trigger", "x"): (0, 255)}
    event = FakeEvent(200)
    event.code = evdev.ecodes.ABS_Z

    assert await process_analog_event(runtime, event, "abs_z", mapping, deps=_deps())
    assert keyboard.events[-1] == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)

    event = FakeEvent(0)
    event.code = evdev.ecodes.ABS_Z
    assert await process_analog_event(runtime, event, "abs_z", mapping, deps=_deps())
    assert keyboard.events[-1] == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0)


@pytest.mark.asyncio
async def test_overlapping_thresholds_activate_independently() -> None:
    keyboard = FakeUInput()
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Overlap",
                thresholds=[
                    AnalogActionThreshold(
                        "x",
                        0.4,
                        0.9,
                        0.3,
                        1.0,
                        [MappingAction(action_type=ActionType.KEYBOARD, target="key_a")],
                    ),
                    AnalogActionThreshold(
                        "x",
                        0.6,
                        1.0,
                        0.5,
                        1.0,
                        [MappingAction(action_type=ActionType.KEYBOARD, target="key_b")],
                    ),
                ],
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)

    assert await process_analog_event(runtime, FakeEvent(26000), "abs_x", mapping, deps=_deps())

    assert (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1) in keyboard.events
    assert (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1) in keyboard.events


@pytest.mark.asyncio
async def test_stick_gamepad_output_routes_axes_with_deadzone() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    resolved: list[str | None] = []
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Route Stick",
                gamepad_output=AnalogGamepadOutputConfig(
                    enabled=True,
                    output_id="virtual-gamepad-2",
                    deadzone=0.2,
                ),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.resolve_gamepad_output = lambda output_id, _context: (  # noqa: E731
        resolved.append(output_id)
        or SimpleNamespace(uinput=gamepad, bucket=f"gamepad:{output_id or 'default'}")
    )

    assert await process_analog_event(runtime, FakeEvent(32767), "abs_x", mapping, deps=_deps())
    assert resolved[-1] == "virtual-gamepad-2"
    assert (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 32767) in gamepad.events
    assert (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Y, 0) in gamepad.events

    assert await process_analog_event(runtime, FakeEvent(2000), "abs_x", mapping, deps=_deps())
    assert gamepad.events[-2] == (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 0)


@pytest.mark.asyncio
async def test_trigger_gamepad_output_routes_axis_with_deadzone() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "left_trigger": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Route Trigger",
                input_type="trigger",
                gamepad_output=AnalogGamepadOutputConfig(enabled=True, deadzone=0.2),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.analog_axis_bindings = {
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z): ("left_trigger", "x")
    }
    runtime.analog_axis_ranges = {("left_trigger", "x"): (0, 255)}
    runtime.resolve_gamepad_output = lambda _output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket="gamepad",
    )

    event = FakeEvent(128)
    event.code = evdev.ecodes.ABS_Z
    assert await process_analog_event(runtime, event, "abs_z", mapping, deps=_deps())
    assert gamepad.events[-1] == (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 96)

    event = FakeEvent(20)
    event.code = evdev.ecodes.ABS_Z
    assert await process_analog_event(runtime, event, "abs_z", mapping, deps=_deps())
    assert gamepad.events[-1] == (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0)


@pytest.mark.asyncio
async def test_trigger_gamepad_output_can_route_to_opposite_trigger() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "right_trigger": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Swap Trigger",
                input_type="trigger",
                gamepad_output=AnalogGamepadOutputConfig(enabled=True, target="left"),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.analog_axis_bindings = {
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RZ): ("right_trigger", "x")
    }
    runtime.analog_axis_ranges = {("right_trigger", "x"): (0, 255)}
    runtime.resolve_gamepad_output = lambda _output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket="gamepad",
    )
    event = FakeEvent(255)
    event.code = evdev.ecodes.ABS_RZ

    assert await process_analog_event(runtime, event, "abs_rz", mapping, deps=_deps())

    assert gamepad.events[-1] == (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 255)


@pytest.mark.asyncio
async def test_stick_gamepad_output_can_route_to_opposite_stick() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "right_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Swap Stick",
                gamepad_output=AnalogGamepadOutputConfig(enabled=True, target="left"),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.analog_axis_bindings = {
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RX): ("right_stick", "x")
    }
    runtime.analog_axis_ranges = {("right_stick", "x"): (-32768, 32767)}
    runtime.resolve_gamepad_output = lambda _output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket="gamepad",
    )
    event = FakeEvent(32767)
    event.code = evdev.ecodes.ABS_RX

    assert await process_analog_event(runtime, event, "abs_rx", mapping, deps=_deps())

    assert (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 32767) in gamepad.events
    assert (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Y, 0) in gamepad.events


@pytest.mark.asyncio
async def test_reset_analog_controls_centers_gamepad_output() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "right_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Route Right Stick",
                gamepad_output=AnalogGamepadOutputConfig(enabled=True),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.resolve_gamepad_output = lambda _output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket="gamepad",
    )

    await reset_analog_controls(runtime, deps=_deps())

    assert (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RX, 0) in gamepad.events
    assert (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RY, 0) in gamepad.events


@pytest.mark.asyncio
async def test_reset_analog_controls_centers_previous_gamepad_output_after_mapping_change() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Route Stick",
                gamepad_output=AnalogGamepadOutputConfig(
                    enabled=True,
                    output_id="virtual-gamepad-2",
                ),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.resolve_gamepad_output = lambda output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket=f"gamepad:{output_id or 'default'}",
    )

    assert await process_analog_event(runtime, FakeEvent(32767), "abs_x", mapping, deps=_deps())
    mapping.clear()
    await reset_analog_controls(runtime, deps=_deps())

    assert gamepad.events[-2:] == [
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 0),
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Y, 0),
    ]
