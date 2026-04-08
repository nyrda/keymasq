import asyncio
from unittest.mock import MagicMock

import evdev
import pytest

from keyforge.keyforged.superkey_state import (
    SuperkeyActionData,
    SuperkeyConfig,
    SuperkeyMachine,
)


@pytest.mark.asyncio
async def test_double_tap_action_fires_on_second_tap() -> None:
    keyboard_uinput = MagicMock()
    keyboard_uinput.write = MagicMock()
    keyboard_uinput.syn = MagicMock()

    config = SuperkeyConfig(
        name="test",
        tap_timeout_ms=200,
        double_tap_window_ms=300,
        hold_threshold_ms=300,
        tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
        double_tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_b")],
    )

    machine = SuperkeyMachine(
        config=config,
        event_name="btn_side",
        keyboard_uinput=keyboard_uinput,
        mouse_uinput=MagicMock(),
        gamepad_uinput=MagicMock(),
    )

    await machine.on_down()
    await machine.on_up()
    await machine.on_down()
    await machine.on_up()

    await asyncio.sleep(0.02)

    writes = [call.args for call in keyboard_uinput.write.call_args_list]

    assert (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1) in writes
    assert (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0) in writes
    assert (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1) not in writes


@pytest.mark.asyncio
async def test_superkey_macro_action_broadcasts_macro_trigger() -> None:
    callback = MagicMock()

    async def broadcast(payload: dict) -> None:
        callback(payload)

    config = SuperkeyConfig(
        name="macro_test",
        tap_actions=[SuperkeyActionData(action_type="macro", macro_name="demo_macro")],
    )

    machine = SuperkeyMachine(
        config=config,
        event_name="btn_side",
        keyboard_uinput=MagicMock(),
        mouse_uinput=MagicMock(),
        gamepad_uinput=MagicMock(),
        broadcast_callback=broadcast,
    )

    await machine.on_down()
    await machine.on_up()
    await asyncio.sleep(0.02)

    callback.assert_called()
    sent = callback.call_args[0][0]
    assert sent.get("action_type") == "macro"
    assert sent.get("macro_name") == "demo_macro"


@pytest.mark.asyncio
async def test_gamepad_trigger_action_writes_absolute_axis() -> None:
    keyboard_uinput = MagicMock()
    mouse_uinput = MagicMock()
    gamepad_uinput = MagicMock()

    config = SuperkeyConfig(
        name="trigger_test",
        hold_actions=[SuperkeyActionData(action_type="gamepad", target="btn_tl2")],
    )

    machine = SuperkeyMachine(
        config=config,
        event_name="btn_side",
        keyboard_uinput=keyboard_uinput,
        mouse_uinput=mouse_uinput,
        gamepad_uinput=gamepad_uinput,
    )

    action = SuperkeyActionData(action_type="gamepad", target="btn_tl2")

    await machine._execute_action_down(action)
    await machine._execute_action_up(action)

    assert (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 255) in [
        tuple(call.args) for call in gamepad_uinput.write.call_args_list
    ]
    assert (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0) in [
        tuple(call.args) for call in gamepad_uinput.write.call_args_list
    ]


@pytest.mark.asyncio
async def test_exec_action_broadcasts_via_callback() -> None:
    callback: list[dict] = []

    async def broadcast(payload: dict) -> None:
        callback.append(payload)

    machine = SuperkeyMachine(
        config=SuperkeyConfig(
            name="exec_test",
            hold_actions=[SuperkeyActionData(action_type="exec", exec_ref=77)],
        ),
        event_name="btn_side",
        keyboard_uinput=MagicMock(),
        mouse_uinput=MagicMock(),
        gamepad_uinput=MagicMock(),
        broadcast_callback=broadcast,
    )

    await machine._execute_action_down(machine.config.hold_actions[0])

    assert len(callback) == 1
    payload = callback[0]
    assert payload["action_type"] == "exec"
    assert payload["exec_ref"] == 77


@pytest.mark.asyncio
async def test_stop_releases_active_hold_action() -> None:
    keyboard_uinput = MagicMock()
    keyboard_uinput.write = MagicMock()
    keyboard_uinput.syn = MagicMock()

    machine = SuperkeyMachine(
        config=SuperkeyConfig(
            name="hold_stop_test",
            hold_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
        ),
        event_name="btn_side",
        keyboard_uinput=keyboard_uinput,
        mouse_uinput=MagicMock(),
        gamepad_uinput=MagicMock(),
    )

    await machine._start_holding()
    await machine.stop()

    writes = [tuple(call.args) for call in keyboard_uinput.write.call_args_list]
    assert writes == [
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
    ]


@pytest.mark.asyncio
async def test_stop_releases_active_tap_hold_action() -> None:
    keyboard_uinput = MagicMock()
    keyboard_uinput.write = MagicMock()
    keyboard_uinput.syn = MagicMock()

    machine = SuperkeyMachine(
        config=SuperkeyConfig(
            name="tap_hold_stop_test",
            tap_hold_actions=[SuperkeyActionData(action_type="keyboard", target="key_b")],
        ),
        event_name="btn_side",
        keyboard_uinput=keyboard_uinput,
        mouse_uinput=MagicMock(),
        gamepad_uinput=MagicMock(),
    )

    await machine._start_tap_holding()
    await machine.stop()

    writes = [tuple(call.args) for call in keyboard_uinput.write.call_args_list]
    assert writes == [
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
    ]


@pytest.mark.asyncio
async def test_stop_cancels_rapidfire_task_without_extra_writes() -> None:
    keyboard_uinput = MagicMock()
    keyboard_uinput.write = MagicMock()
    keyboard_uinput.syn = MagicMock()

    machine = SuperkeyMachine(
        config=SuperkeyConfig(
            name="rapidfire_stop_test",
            hold_actions=[
                SuperkeyActionData(
                    action_type="keyboard",
                    target="key_c",
                    rapidfire_enabled=True,
                    rapidfire_hold_ms=100,
                    rapidfire_wait_ms=100,
                )
            ],
        ),
        event_name="btn_side",
        keyboard_uinput=keyboard_uinput,
        mouse_uinput=MagicMock(),
        gamepad_uinput=MagicMock(),
    )

    await machine._start_holding()
    await asyncio.sleep(0)
    await machine.stop()

    writes_after_stop = [tuple(call.args) for call in keyboard_uinput.write.call_args_list]

    await asyncio.sleep(0.15)

    assert machine._rapidfire_tasks == []
    assert [tuple(call.args) for call in keyboard_uinput.write.call_args_list] == writes_after_stop
    assert writes_after_stop == [
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_C, 1),
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_C, 0),
    ]


@pytest.mark.asyncio
async def test_rapidfire_hold_release_emits_single_key_up() -> None:
    keyboard_uinput = MagicMock()
    keyboard_uinput.write = MagicMock()
    keyboard_uinput.syn = MagicMock()

    machine = SuperkeyMachine(
        config=SuperkeyConfig(
            name="rapidfire_release_test",
            hold_actions=[
                SuperkeyActionData(
                    action_type="keyboard",
                    target="key_d",
                    rapidfire_enabled=True,
                    rapidfire_hold_ms=100,
                    rapidfire_wait_ms=100,
                )
            ],
        ),
        event_name="btn_side",
        keyboard_uinput=keyboard_uinput,
        mouse_uinput=MagicMock(),
        gamepad_uinput=MagicMock(),
    )

    await machine._start_holding()
    await asyncio.sleep(0)
    await machine.on_up()

    writes_after_release = [tuple(call.args) for call in keyboard_uinput.write.call_args_list]

    await asyncio.sleep(0.15)

    assert machine._rapidfire_tasks == []
    final_writes = [tuple(call.args) for call in keyboard_uinput.write.call_args_list]
    assert final_writes == writes_after_release
    assert writes_after_release == [
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_D, 1),
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_D, 0),
    ]


@pytest.mark.asyncio
async def test_tap_action_fires_after_double_tap_window_expires() -> None:
    keyboard_uinput = MagicMock()
    keyboard_uinput.write = MagicMock()
    keyboard_uinput.syn = MagicMock()

    machine = SuperkeyMachine(
        config=SuperkeyConfig(
            name="tap_timeout_test",
            tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_e")],
            double_tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_f")],
            double_tap_window_ms=10,
        ),
        event_name="btn_side",
        keyboard_uinput=keyboard_uinput,
        mouse_uinput=MagicMock(),
        gamepad_uinput=MagicMock(),
    )

    await machine.on_down()
    await machine.on_up()
    await asyncio.sleep(0.03)

    writes = [tuple(call.args) for call in keyboard_uinput.write.call_args_list]
    assert (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_E, 1) in writes
    assert (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F, 1) not in writes


@pytest.mark.asyncio
async def test_tap_bundle_emits_keys_in_order_and_releases_in_reverse() -> None:
    keyboard_uinput = MagicMock()
    keyboard_uinput.write = MagicMock()
    keyboard_uinput.syn = MagicMock()

    machine = SuperkeyMachine(
        config=SuperkeyConfig(
            name="tap_bundle_test",
            tap_actions=[
                SuperkeyActionData(action_type="keyboard", target="key_leftctrl"),
                SuperkeyActionData(action_type="keyboard", target="key_c"),
            ],
        ),
        event_name="btn_side",
        keyboard_uinput=keyboard_uinput,
        mouse_uinput=MagicMock(),
        gamepad_uinput=MagicMock(),
    )

    await machine._emit_tap()

    writes = [tuple(call.args) for call in keyboard_uinput.write.call_args_list]
    assert writes == [
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 1),
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_C, 1),
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_C, 0),
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 0),
    ]


@pytest.mark.asyncio
async def test_hold_bundle_releases_in_reverse_order() -> None:
    keyboard_uinput = MagicMock()
    keyboard_uinput.write = MagicMock()
    keyboard_uinput.syn = MagicMock()

    machine = SuperkeyMachine(
        config=SuperkeyConfig(
            name="hold_bundle_test",
            hold_actions=[
                SuperkeyActionData(action_type="keyboard", target="key_leftctrl"),
                SuperkeyActionData(action_type="keyboard", target="key_v"),
            ],
        ),
        event_name="btn_side",
        keyboard_uinput=keyboard_uinput,
        mouse_uinput=MagicMock(),
        gamepad_uinput=MagicMock(),
    )

    await machine._emit_hold_down()
    await machine._emit_hold_up()

    writes = [tuple(call.args) for call in keyboard_uinput.write.call_args_list]
    assert writes == [
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 1),
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_V, 1),
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_V, 0),
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 0),
    ]


@pytest.mark.asyncio
async def test_keyboard_actions_notify_key_event_tracker() -> None:
    tracked: list[tuple[str, int, int]] = []
    keyboard_uinput = MagicMock()
    keyboard_uinput.write = MagicMock()
    keyboard_uinput.syn = MagicMock()

    def track(action_type: str, code: int, value: int) -> None:
        tracked.append((action_type, code, value))

    machine = SuperkeyMachine(
        config=SuperkeyConfig(name="tracker_test"),
        event_name="btn_side",
        keyboard_uinput=keyboard_uinput,
        mouse_uinput=MagicMock(),
        gamepad_uinput=MagicMock(),
        key_event_tracker=track,
    )

    action = SuperkeyActionData(action_type="keyboard", target="key_g")
    await machine._execute_action_down(action)
    await machine._execute_action_up(action)

    assert tracked == [
        ("keyboard", evdev.ecodes.KEY_G, 1),
        ("keyboard", evdev.ecodes.KEY_G, 0),
    ]


@pytest.mark.asyncio
async def test_stop_rapidfire_tasks_is_safe_without_task() -> None:
    machine = SuperkeyMachine(
        config=SuperkeyConfig(name="no_task"),
        event_name="btn_side",
        keyboard_uinput=MagicMock(),
        mouse_uinput=MagicMock(),
        gamepad_uinput=MagicMock(),
    )

    await machine._stop_rapidfire_tasks()
    assert machine._rapidfire_tasks == []


def test_get_uinput_returns_expected_device() -> None:
    keyboard = MagicMock()
    mouse = MagicMock()
    gamepad = MagicMock()
    machine = SuperkeyMachine(
        config=SuperkeyConfig(name="devices"),
        event_name="btn_side",
        keyboard_uinput=keyboard,
        mouse_uinput=mouse,
        gamepad_uinput=gamepad,
    )

    assert machine._get_uinput("keyboard") is keyboard
    assert machine._get_uinput("mouse") is mouse
    assert machine._get_uinput("gamepad") is gamepad
    assert machine._get_uinput("unknown") is None
