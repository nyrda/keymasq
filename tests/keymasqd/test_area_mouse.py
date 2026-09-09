from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import evdev
import pytest

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.analog import AnalogControlConfig, AnalogMouseMotionConfig
from keymasq.common.model.core import ActionType
from keymasq.keymasqd.runtime.grabbed_device.event.pipeline import process_event
from tests.keymasqd.device_manager_support import (
    FakeUInput,
    grabbed_event_processing_deps,
    make_grabbed_device,
)


@pytest.fixture
def area_mouse(monkeypatch):
    mouse = FakeUInput()
    config = AnalogControlConfig(
        name="Touchpad Mouse",
        mouse_motion=AnalogMouseMotionConfig(
            enabled=True,
            mode="area",
            area_input_style="touchpad",
            area_radius_x=400,
            area_radius_y=200,
            # Stick shaping and anchoring must not affect touchpad movement.
            deadzone=0.9,
            sensitivity=2,
            response_curve=4,
            area_start_enabled=True,
        ),
    )
    action = MappingAction(
        action_type=ActionType.ANALOG_CONTROL,
        analog_control_config=config,
    )
    device = make_grabbed_device(
        monkeypatch,
        mapping={"pad": action},
        mouse_uinput=mouse,
        analog_inputs={
            "pad": {
                "type": "stick",
                "axes": [
                    {
                        "role": role,
                        "evdev_code": code,
                        "minimum": -32768,
                        "maximum": 32768,
                        "center": 0,
                    }
                    for role, code in (("x", evdev.ecodes.ABS_HAT1X), ("y", evdev.ecodes.ABS_HAT1Y))
                ],
            }
        },
    )
    axis_values = {evdev.ecodes.ABS_HAT1X: 0, evdev.ecodes.ABS_HAT1Y: 0}
    device.device = SimpleNamespace(
        axis_values=axis_values,
        absinfo=Mock(
            side_effect=lambda code: evdev.AbsInfo(axis_values[code], -32768, 32768, 0, 0, 0)
        ),
        active_keys=lambda: [],
    )
    device.update_analog_inputs(device.analog_inputs)
    device.device.absinfo.reset_mock()
    device.cursor_position_setter = AsyncMock()
    return device, mouse, config


async def _event(device, event_type, code, value=0):
    if event_type == evdev.ecodes.EV_ABS:
        device.device.axis_values[code] = value
    await process_event(
        device,
        evdev.InputEvent(0, 0, event_type, code, value),
        deps=grabbed_event_processing_deps(),
    )


async def _report(device, mouse, x=None, y=None):
    before = list(mouse.writes)
    for code, value in ((evdev.ecodes.ABS_HAT1X, x), (evdev.ecodes.ABS_HAT1Y, y)):
        if value is not None:
            await _event(device, evdev.ecodes.EV_ABS, code, value)
    assert mouse.writes == before, "Area motion must wait for the complete report"
    await _event(device, evdev.ecodes.EV_SYN, evdev.ecodes.SYN_REPORT)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_motion_sensor", [False, True])
async def test_touch_slide_hold_release_and_retouch_without_jumps(area_mouse, with_motion_sensor):
    device, mouse, _ = area_mouse
    if with_motion_sensor:
        # Motion dispatch has its own early return for synchronization events.
        device.motion_axis_bindings = {(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RX): ("imu", "yaw")}

    await _report(device, mouse, 8192, -16384)
    assert mouse.writes == []
    await _report(device, mouse, 16384, -8192)
    assert mouse.writes == [
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 100),
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, 50),
    ]
    # No axis events while holding still, then release both axes in one report.
    await _report(device, mouse)
    await _report(device, mouse, 0, 0)
    assert len(mouse.writes) == 2
    await _report(device, mouse, -24576, 8192)
    assert len(mouse.writes) == 2
    await _report(device, mouse, -16384)
    assert mouse.writes[-1] == (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 100)
    assert device.state.analog_mouse_tasks == {}
    device.cursor_position_setter.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_zero_axis_is_still_touching_and_tiny_nonzero_is_not_release(area_mouse):
    device, mouse, config = area_mouse
    config.mouse_motion.area_radius_x = 32768
    config.mouse_motion.area_radius_y = 32768
    await _report(device, mouse, y=1)
    await _report(device, mouse, x=1, y=0)
    assert mouse.writes == [
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 1),
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, -1),
    ]
    await _report(device, mouse, x=0)
    assert len(mouse.writes) == 2


@pytest.mark.asyncio
async def test_fractional_motion_accumulates_and_inversion_preserves_release(area_mouse):
    device, mouse, config = area_mouse
    config.mouse_motion.area_radius_x = 1
    config.mouse_motion.area_radius_y = 1
    config.mouse_motion.invert_x = True
    config.mouse_motion.invert_y = True
    await _report(device, mouse, -16384, -16384)
    for position in (-8192, 0, 8192, 16384):
        await _report(device, mouse, position, -16384)
    assert mouse.writes == [(evdev.ecodes.EV_REL, evdev.ecodes.REL_X, -1)]
    await _report(device, mouse, y=16384)
    assert mouse.writes[-1] == (evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, -1)
    await _report(device, mouse, x=24576)
    await _report(device, mouse, 0, 0)
    assert len(mouse.writes) == 2
    assert "pad" not in device.state.analog_mouse_accumulators


@pytest.mark.asyncio
async def test_reset_discards_pending_motion_and_preserves_unchanged_controls(area_mouse):
    device, mouse, _ = area_mouse
    await _report(device, mouse, 8192, 8192)
    await _event(device, evdev.ecodes.EV_ABS, evdev.ecodes.ABS_HAT1X, 16384)
    await device.reset_analog_controls(preserve_state_keys={"pad"})
    await _report(device, mouse)
    assert mouse.writes == [(evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 100)]
    await _event(device, evdev.ecodes.EV_ABS, evdev.ecodes.ABS_HAT1X, 24576)
    await device.reset_analog_controls()
    await _report(device, mouse)
    await _report(device, mouse, -16384, -8192)
    assert len(mouse.writes) == 1
    await _report(device, mouse, -8192)
    assert mouse.writes[-1] == (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 100)


@pytest.mark.asyncio
async def test_dropped_report_waits_for_release_before_resuming(area_mouse):
    device, mouse, _ = area_mouse
    await _report(device, mouse, 8192, 8192)
    await _event(device, evdev.ecodes.EV_ABS, evdev.ecodes.ABS_HAT1X, 16384)
    await _event(device, evdev.ecodes.EV_SYN, evdev.ecodes.SYN_DROPPED)
    await _report(device, mouse, 16384, 8192)  # Snapshot still shows a held touch.
    await _report(device, mouse, -8192, -8192)
    await _report(device, mouse, 8192, 8192)
    assert mouse.writes == []
    await _report(device, mouse, 0, 0)
    await _report(device, mouse, -16384, -16384)
    await _report(device, mouse, -8192, -8192)
    assert mouse.writes == [
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 100),
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, 50),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("released_at_snapshot", [False, True])
async def test_dropped_release_recovers_unchanged_zero_axis(area_mouse, released_at_snapshot):
    device, mouse, _ = area_mouse
    await _report(device, mouse, 8192, 8192)
    await _event(device, evdev.ecodes.EV_SYN, evdev.ecodes.SYN_DROPPED)
    # Y changes to zero in the discarded report and is never sent again.
    await _report(device, mouse, x=0 if released_at_snapshot else None, y=0)
    assert device.device.absinfo.call_count == 2
    assert mouse.writes == []
    if not released_at_snapshot:
        await _report(device, mouse, x=0)
    await _report(device, mouse, x=-16384)
    assert mouse.writes == []
    await _report(device, mouse, x=-8192)
    assert mouse.writes == [(evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 100)]


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_mapping", [False, True])
async def test_mapping_reset_retains_unchanged_physical_axis(area_mouse, changed_mapping):
    device, mouse, config = area_mouse
    await _report(device, mouse, 8192, 16384)
    mapping = device.mapping_getter()
    previous_mapping = dict(mapping)
    if changed_mapping:
        mapping["pad"] = MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=replace(config, name="Changed touchpad"),
        )
    await device.reset_mapping_runtime_state(previous_mapping=previous_mapping)
    await _report(device, mouse, x=16384)
    assert mouse.writes == (
        [] if changed_mapping else [(evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 100)]
    )
    await _report(device, mouse, y=24576)
    assert mouse.writes[-1] == (evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, 50)
    device.device.absinfo.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("action_type", [ActionType.SUPPRESS, ActionType.PASSTHROUGH])
async def test_touchpad_enabled_after_sparse_unmapped_input(area_mouse, action_type):
    device, mouse, _ = area_mouse
    mapping = device.mapping_getter()
    touchpad_action = mapping["pad"]
    mapping["pad"] = MappingAction(action_type=action_type)
    await _report(device, mouse, 8192, 16384)
    mapping["pad"] = touchpad_action
    await device.reset_mapping_runtime_state()
    await _report(device, mouse, x=16384)
    assert mouse.writes == []
    await _report(device, mouse, y=24576)
    assert mouse.writes == [(evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, 50)]


@pytest.mark.asyncio
async def test_initial_sparse_report_queries_held_axis(area_mouse):
    device, mouse, _ = area_mouse
    device.device.axis_values[evdev.ecodes.ABS_HAT1Y] = 16384
    await _report(device, mouse, x=8192)
    assert mouse.writes == []
    device.device.absinfo.assert_called_once_with(evdev.ecodes.ABS_HAT1Y)
    await _report(device, mouse, y=24576)
    assert mouse.writes == [(evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, 50)]


@pytest.mark.asyncio
async def test_dropped_release_is_recovered_while_input_is_paused(area_mouse):
    device, mouse, _ = area_mouse
    await _report(device, mouse, 8192, 8192)
    device.input_paused_getter = lambda: True
    await _event(device, evdev.ecodes.EV_SYN, evdev.ecodes.SYN_DROPPED)
    await _report(device, mouse, 0, 0)
    assert mouse.writes == []
    device.input_paused_getter = lambda: False
    await _report(device, mouse, x=8192)
    assert mouse.writes == []
    await _report(device, mouse, x=16384)
    assert mouse.writes == [(evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 100)]


@pytest.mark.asyncio
async def test_failed_resync_does_not_treat_unknown_axes_as_release(area_mouse):
    device, mouse, _ = area_mouse
    await _report(device, mouse, 8192, 8192)
    read_axis = device.device.absinfo.side_effect
    device.device.absinfo.side_effect = OSError("Device state unavailable")
    await _event(device, evdev.ecodes.EV_SYN, evdev.ecodes.SYN_DROPPED)
    await _report(device, mouse, 0, 0)
    await _report(device, mouse, 8192, 8192)
    assert mouse.writes == []
    assert device.state.analog_mouse_area_resyncing
    device.device.absinfo.side_effect = read_axis
    await _report(device, mouse)
    await _report(device, mouse, x=16384)
    assert mouse.writes == []
    await _report(device, mouse, 0, 0)
    await _report(device, mouse, x=8192)
    await _report(device, mouse, x=16384)
    assert mouse.writes == [(evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 100)]


@pytest.mark.asyncio
async def test_multiple_controls_on_one_pad_keep_independent_motion_state(area_mouse):
    device, mouse, config = area_mouse
    device.mapping_getter()["pad"] = MappingAction(
        action_type=ActionType.ANALOG_CONTROL,
        analog_control_configs=[
            config,
            replace(config, mouse_motion=replace(config.mouse_motion, invert_x=True)),
        ],
    )
    await _report(device, mouse, 8192, 8192)
    await _report(device, mouse, 16384)
    await _report(device, mouse, 0, 0)
    await _report(device, mouse, -16384, -8192)
    assert mouse.writes == [
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 100),
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, -100),
    ]


@pytest.fixture
def stick_area(area_mouse):
    device, mouse, config = area_mouse
    config.mouse_motion.area_input_style = "stick"
    config.mouse_motion.deadzone = 0
    config.mouse_motion.sensitivity = 1
    config.mouse_motion.response_curve = 1
    config.mouse_motion.area_start_enabled = False
    return device, mouse, config


@pytest.mark.asyncio
@pytest.mark.parametrize("positions", [(8192, 24576, 16384, 0), (32768, 0)])
async def test_stick_counts_first_and_last_samples_and_returns_to_origin(stick_area, positions):
    device, mouse, _ = stick_area
    previous = 0
    for position in positions:
        before = len(mouse.writes)
        await _report(device, mouse, x=position)
        assert mouse.writes[before:] == [
            (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, (position - previous) * 400 // 32768),
        ]
        previous = position
    assert sum(value for _, _, value in mouse.writes) == 0
    assert device.state.analog_mouse_tasks == {}


@pytest.mark.asyncio
async def test_stick_deadzone_sensitivity_and_curve_apply_to_position(stick_area):
    device, mouse, config = stick_area
    config.mouse_motion.deadzone = 0.25
    config.mouse_motion.sensitivity = 0.5
    config.mouse_motion.response_curve = 2
    for x, y in [(100, -200), (8192, -8192), (-400, 300)]:
        await _report(device, mouse, x, y)
    assert mouse.writes == []
    await _report(device, mouse, 16384, 0)
    assert mouse.writes == [(evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 22)]
    await _report(device, mouse, 8192, 0)
    assert mouse.writes[-1] == (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, -22)
    await _report(device, mouse, 0, 0)
    assert len(mouse.writes) == 2


@pytest.mark.asyncio
async def test_stick_start_position_uses_complete_reports_and_deadzone(stick_area):
    device, mouse, config = stick_area
    config.mouse_motion.deadzone = 0.25
    config.mouse_motion.area_start_enabled = True
    config.mouse_motion.area_start_x = 300
    config.mouse_motion.area_start_y = 400
    await _report(device, mouse, 100, -200)
    device.cursor_position_setter.assert_not_awaited()
    await _report(device, mouse, -32768, 0)
    await _report(device, mouse, 0, -32768)
    device.cursor_position_setter.assert_awaited_once_with(300, 400)
    assert mouse.writes == [
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, -400),
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 400),
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, -200),
    ]
    await _report(device, mouse, 0, 0)
    await _report(device, mouse, 32768, 0)
    assert device.cursor_position_setter.await_count == 2


@pytest.mark.asyncio
async def test_stick_sparse_start_reads_other_axis(stick_area):
    device, mouse, _ = stick_area
    device.device.axis_values[evdev.ecodes.ABS_HAT1Y] = 16384
    await _report(device, mouse, x=8192)
    assert mouse.writes == [
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 100),
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, 100),
    ]


@pytest.mark.asyncio
async def test_stick_recovers_dropped_report_without_touch_release_gate(stick_area):
    device, mouse, _ = stick_area
    await _report(device, mouse, 8192, 8192)
    before = len(mouse.writes)
    await _event(device, evdev.ecodes.EV_SYN, evdev.ecodes.SYN_DROPPED)
    await _report(device, mouse, 16384, 0)
    assert len(mouse.writes) == before
    await _report(device, mouse, x=24576)
    assert mouse.writes[-1] == (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 100)
    await _report(device, mouse, x=0)
    assert mouse.writes[-1] == (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, -300)
