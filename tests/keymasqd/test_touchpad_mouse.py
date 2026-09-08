from dataclasses import replace
from unittest.mock import AsyncMock

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
def touchpad(monkeypatch):
    mouse = FakeUInput()
    config = AnalogControlConfig(
        name="Touchpad Mouse",
        mouse_motion=AnalogMouseMotionConfig(
            enabled=True,
            mode="touchpad",
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
    device.cursor_position_setter = AsyncMock()
    return device, mouse, config


async def _event(device, event_type, code, value=0):
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
    assert mouse.writes == before, "Touchpad motion must wait for the complete report"
    await _event(device, evdev.ecodes.EV_SYN, evdev.ecodes.SYN_REPORT)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_motion_sensor", [False, True])
async def test_touch_slide_hold_release_and_retouch_without_jumps(touchpad, with_motion_sensor):
    device, mouse, _ = touchpad
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
async def test_single_zero_axis_is_still_touching_and_tiny_nonzero_is_not_release(touchpad):
    device, mouse, config = touchpad
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
async def test_fractional_motion_accumulates_and_inversion_preserves_release(touchpad):
    device, mouse, config = touchpad
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
async def test_reset_discards_pending_motion_and_preserves_unchanged_controls(touchpad):
    device, mouse, _ = touchpad
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
async def test_dropped_report_waits_for_release_before_resuming(touchpad):
    device, mouse, _ = touchpad
    await _report(device, mouse, 8192, 8192)
    await _event(device, evdev.ecodes.EV_ABS, evdev.ecodes.ABS_HAT1X, 16384)
    await _event(device, evdev.ecodes.EV_SYN, evdev.ecodes.SYN_DROPPED)
    await _report(device, mouse, 0, 0)  # Incomplete report is ignored.
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
async def test_multiple_controls_on_one_pad_keep_independent_motion_state(touchpad):
    device, mouse, config = touchpad
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
