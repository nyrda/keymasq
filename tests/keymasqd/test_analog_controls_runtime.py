import asyncio
from types import SimpleNamespace

import evdev
import pytest

from keymasq.common.models import (
    ActionType,
    AnalogActionThreshold,
    AnalogControlConfig,
    MappingAction,
)
from keymasq.keymasqd.runtime.analog_controls import normalize_axis_value, process_analog_event
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
