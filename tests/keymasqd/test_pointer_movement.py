import asyncio
import time

import evdev
import pytest

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.common.model.pointer import PointerMovementConfig
from keymasq.keymasqd.runtime.grabbed_device.event.pipeline import process_event
from tests.keymasqd.device_manager_support import (
    FakeUInput,
    grabbed_event_processing_deps,
    make_grabbed_device,
)

ec = evdev.ecodes


def _position_action() -> MappingAction:
    return MappingAction(
        action_type=ActionType.POINTER_MOVEMENT,
        pointer_movement=PointerMovementConfig(
            mode="axes",
            behavior="position",
            radius=1000,
            x_axis="abs_x",
            y_axis="none",
        ),
    )


async def _send(device, *events: tuple[int, int, int]) -> None:
    for event_type, code, value in events:
        await process_event(
            device,
            evdev.InputEvent(0, 0, event_type, code, value),
            deps=grabbed_event_processing_deps(),
        )


def _abs_x(gamepad: FakeUInput) -> list[int]:
    return [value for kind, code, value in gamepad.writes if (kind, code) == (ec.EV_ABS, ec.ABS_X)]


@pytest.mark.asyncio
async def test_dropped_report_discards_partial_pointer_movement(monkeypatch):
    gamepad = FakeUInput()
    device = make_grabbed_device(
        monkeypatch,
        mapping={"pointer": _position_action()},
        gamepad_uinput=gamepad,
        passthrough_uinput=FakeUInput(),
    )

    await _send(device, (ec.EV_REL, ec.REL_X, 300), (ec.EV_SYN, ec.SYN_REPORT, 0))
    await _send(device, (ec.EV_REL, ec.REL_X, 500), (ec.EV_SYN, ec.SYN_DROPPED, 0))
    await _send(device, (ec.EV_REL, ec.REL_X, 100), (ec.EV_SYN, ec.SYN_REPORT, 0))

    assert _abs_x(gamepad) == [round(0.3 * 32767), round(0.4 * 32767)]


@pytest.mark.asyncio
async def test_unchanged_pointer_mapping_keeps_held_position_across_mapping_update(monkeypatch):
    gamepad = FakeUInput()
    mapping = {"pointer": _position_action()}
    device = make_grabbed_device(
        monkeypatch,
        mapping_getter=lambda: mapping,
        gamepad_uinput=gamepad,
        passthrough_uinput=FakeUInput(),
    )
    await _send(device, (ec.EV_REL, ec.REL_X, 500), (ec.EV_SYN, ec.SYN_REPORT, 0))

    previous = dict(mapping)
    mapping["key_a"] = MappingAction(action_type=ActionType.KEYBOARD, target="key_b")
    await device.reset_mapping_runtime_state(previous_mapping=previous)
    await _send(device, (ec.EV_REL, ec.REL_X, 100), (ec.EV_SYN, ec.SYN_REPORT, 0))

    assert _abs_x(gamepad) == [16384, round(0.6 * 32767)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "macro_blocks_movement"),
    [
        (MappingAction(action_type=ActionType.SUPPRESS), False),
        (
            MappingAction(
                action_type=ActionType.POINTER_MOVEMENT,
                pointer_movement=PointerMovementConfig(factor_x=2.0),
            ),
            True,
        ),
    ],
    ids=["suppress-mapping", "macro-blocks-mouse-movement"],
)
async def test_blocked_pointer_emits_no_movement_but_keeps_buttons(
    monkeypatch,
    action,
    macro_blocks_movement,
):
    passthrough = FakeUInput()
    device = make_grabbed_device(
        monkeypatch,
        mapping={"pointer": action},
        passthrough_uinput=passthrough,
        suppress_rel_getter=lambda: macro_blocks_movement,
    )

    await _send(
        device,
        (ec.EV_REL, ec.REL_X, 5),
        (ec.EV_KEY, ec.BTN_LEFT, 1),
        (ec.EV_SYN, ec.SYN_REPORT, 0),
    )

    assert [(kind, code) for kind, code, _value in passthrough.writes] == [(ec.EV_KEY, ec.BTN_LEFT)]


@pytest.mark.asyncio
async def test_velocity_returns_to_rest_after_an_unchanged_mapping_is_resent(monkeypatch):
    gamepad = FakeUInput()

    def velocity_mapping() -> dict[str, MappingAction]:
        return {
            "pointer": MappingAction(
                action_type=ActionType.POINTER_MOVEMENT,
                pointer_movement=PointerMovementConfig(
                    mode="axes", full_speed=1000, window_ms=10, x_axis="abs_x", y_axis="none"
                ),
            )
        }

    mapping = velocity_mapping()
    device = make_grabbed_device(
        monkeypatch,
        mapping_getter=lambda: mapping,
        gamepad_uinput=gamepad,
        passthrough_uinput=FakeUInput(),
    )
    await _send(device, (ec.EV_REL, ec.REL_X, 5), (ec.EV_SYN, ec.SYN_REPORT, 0))
    await asyncio.sleep(0.05)

    previous = mapping
    mapping = velocity_mapping()
    await device.reset_mapping_runtime_state(previous_mapping=previous)
    await _send(device, (ec.EV_REL, ec.REL_X, 5), (ec.EV_SYN, ec.SYN_REPORT, 0))
    await asyncio.sleep(0.05)

    assert _abs_x(gamepad) == [16384, 0, 16384, 0]


def _axes_action(**overrides: object) -> MappingAction:
    settings: dict[str, object] = {"mode": "axes", "x_axis": "abs_x", "y_axis": "none"}
    settings.update(overrides)
    return MappingAction(
        action_type=ActionType.POINTER_MOVEMENT,
        pointer_movement=PointerMovementConfig(**settings),  # pyright: ignore[reportArgumentType]
    )


async def _send_at(device, timestamp_ns: int, value: int) -> None:
    sec, nsec = divmod(timestamp_ns, 1_000_000_000)
    for event_type, code, event_value in (
        (ec.EV_REL, ec.REL_X, value),
        (ec.EV_SYN, ec.SYN_REPORT, 0),
    ):
        await process_event(
            device,
            evdev.InputEvent(sec, nsec // 1_000, event_type, code, event_value),
            deps=grabbed_event_processing_deps(),
        )


@pytest.mark.asyncio
async def test_velocity_averages_only_reports_inside_the_window(monkeypatch):
    gamepad = FakeUInput()
    device = make_grabbed_device(
        monkeypatch,
        mapping={"pointer": _axes_action(full_speed=1000, window_ms=100)},
        gamepad_uinput=gamepad,
        passthrough_uinput=FakeUInput(),
    )
    start_ns = time.monotonic_ns() - 200_000_000

    for offset_ms in (0, 60, 120):
        await _send_at(device, start_ns + offset_ms * 1_000_000, 10)

    assert _abs_x(gamepad) == [round(0.1 * 32767), round(0.2 * 32767), round(0.2 * 32767)]


@pytest.mark.asyncio
async def test_kept_overshoot_on_one_way_axis_does_not_wind_up_below_rest(monkeypatch):
    gamepad = FakeUInput()
    device = make_grabbed_device(
        monkeypatch,
        mapping={
            "pointer": _axes_action(
                behavior="position", radius=1000, overshoot="keep", x_direction="max"
            )
        },
        gamepad_uinput=gamepad,
        passthrough_uinput=FakeUInput(),
    )

    await _send(device, (ec.EV_REL, ec.REL_X, -5000), (ec.EV_SYN, ec.SYN_REPORT, 0))
    await _send(device, (ec.EV_REL, ec.REL_X, 500), (ec.EV_SYN, ec.SYN_REPORT, 0))

    assert _abs_x(gamepad)[-1] == 16384


@pytest.mark.asyncio
async def test_new_pointer_config_seen_before_its_reset_releases_the_old_axis(monkeypatch):
    gamepad = FakeUInput()
    mapping = {"pointer": _axes_action(behavior="position", radius=1000, x_axis="abs_rx")}
    device = make_grabbed_device(
        monkeypatch,
        mapping_getter=lambda: mapping,
        gamepad_uinput=gamepad,
        passthrough_uinput=FakeUInput(),
    )
    await _send(device, (ec.EV_REL, ec.REL_X, 500), (ec.EV_SYN, ec.SYN_REPORT, 0))

    previous = mapping
    mapping = {"pointer": _axes_action(behavior="position", radius=1000)}
    await _send(device, (ec.EV_REL, ec.REL_X, 500), (ec.EV_SYN, ec.SYN_REPORT, 0))
    await device.reset_mapping_runtime_state(previous_mapping=previous)

    rx = [value for kind, code, value in gamepad.writes if (kind, code) == (ec.EV_ABS, ec.ABS_RX)]
    assert rx == [16384, 0]
    assert _abs_x(gamepad)[-1] == 0


@pytest.mark.asyncio
async def test_released_device_stops_settling_and_returns_axes_to_rest(monkeypatch):
    gamepad = FakeUInput()
    device = make_grabbed_device(
        monkeypatch,
        mapping={"pointer": _axes_action(behavior="position", radius=1000, recenter_ms=20)},
        gamepad_uinput=gamepad,
        passthrough_uinput=FakeUInput(),
    )
    await _send(device, (ec.EV_REL, ec.REL_X, 500), (ec.EV_SYN, ec.SYN_REPORT, 0))

    await device.release()
    writes_after_release = list(gamepad.writes)
    await asyncio.sleep(0.05)

    assert _abs_x(gamepad) == [16384, 0]
    assert gamepad.writes == writes_after_release
    assert "pointer:x" not in device.state.analog_gamepad_outputs
