import asyncio
import itertools
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from unittest.mock import Mock

import evdev
import pytest

from keymasq.common.ipc import CommandType
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType, SuperkeyMode
from keymasq.common.model.superkeys import SuperkeyConfig
from keymasq.keymasqd.runtime import adapters
from keymasq.keymasqd.runtime.grabbed_device import device as grabbed_device
from keymasq.keymasqd.runtime.grabbed_device.device import GrabbedDevice
from keymasq.keymasqd.runtime.grabbed_device.event import pipeline
from keymasq.keymasqd.superkey_state import SuperkeyActionData
from tests.keymasqd.device_manager_support import FakeUInput, make_combo_runtime_setup

KEYBOARD = "1234:5678"
EV_KEY = evdev.ecodes.EV_KEY
EV_REL = evdev.ecodes.EV_REL
REL_X = evdev.ecodes.REL_X
KEY_A = evdev.ecodes.KEY_A
KEY_B = evdev.ecodes.KEY_B
KEY_C = evdev.ecodes.KEY_C
KEY_E = evdev.ecodes.KEY_E
KEY_S = evdev.ecodes.KEY_S
KEY_V = evdev.ecodes.KEY_V
KEY_X = evdev.ecodes.KEY_X
BUTTONS = {
    name: name
    for name in (
        "key_a",
        "key_b",
        "key_c",
        "key_e",
        "key_s",
        "key_v",
        "key_esc",
        "key_leftctrl",
        "key_leftalt",
    )
}
MAPPED_RESET = [(KEY_E, 1), (KEY_E, 0)]
CTRL_ALT_ESC_DOUBLE_TAP = [
    (evdev.ecodes.KEY_LEFTCTRL, 1),
    (evdev.ecodes.KEY_LEFTALT, 1),
    (evdev.ecodes.KEY_ESC, 1),
    (evdev.ecodes.KEY_ESC, 0),
    (evdev.ecodes.KEY_ESC, 1),
    (evdev.ecodes.KEY_ESC, 0),
]


def natural_move(x: int, *, speed: float = 100.0) -> MappingAction:
    return MappingAction(
        action_type=ActionType.MOUSE_MOVE_NATURAL_ABS,
        move_x=x,
        move_speed=speed,
        move_jitter=0.0,
        move_tolerance=1,
        move_max_duration_ms=600_000,
    )


def key(target: str) -> MappingAction:
    return MappingAction(action_type=ActionType.KEYBOARD, target=target)


SLOW_MOVE = natural_move(1_000_000)
COMBO_SLOW_MOVE = {
    "id": "move",
    "name": "move",
    "steps": [
        {
            "events": [
                {"hardware_id": KEYBOARD, "source": "kbd", "evdev": "key_c"},
                {"hardware_id": KEYBOARD, "source": "kbd", "evdev": "key_v"},
            ]
        }
    ],
    "action": {
        "action": "mouse_move_natural_abs",
        "x": 1_000_000,
        "speed": 100.0,
        "max_duration_ms": 600_000,
    },
}


async def wait_until(condition: Callable[[], bool], timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


async def cursor_rig(
    monkeypatch: pytest.MonkeyPatch,
    mapping: dict[str, MappingAction],
    combos: list[object] | None = None,
    rollover_members: tuple[str, ...] = (),
) -> SimpleNamespace:
    output = FakeUInput()
    setup = await make_combo_runtime_setup(
        monkeypatch,
        [],
        hardware_id=KEYBOARD,
        button_map=BUTTONS,
        mapping=mapping,
        keyboard_uinput=output,
    )
    manager, keyboard = setup.manager, setup.device
    await manager.set_combos(combos or [])
    if rollover_members:
        await manager.set_rollover_groups(
            [
                {
                    "name": "moves",
                    "members": [
                        {"hardware_id": KEYBOARD, "button": button} for button in rollover_members
                    ],
                }
            ]
        )
        keyboard.rollover_getter = lambda: manager.rollover
    manager.output_state.mouse_uinput = output

    def rel_writes() -> list[int]:
        return [
            value
            for event_type, code, value in output.writes
            if event_type == EV_REL and code == REL_X
        ]

    async def compositor(event_type: CommandType, data: dict[str, object]) -> None:
        if event_type == CommandType.CURSOR_POSITION_REQUEST:
            manager.handle_cursor_position_response(
                {"request_id": data["request_id"], "status": "ok", "x": sum(rel_writes()), "y": 0}
            )

    manager.broadcast_callback = compositor
    inputs: asyncio.Queue[SimpleNamespace] = asyncio.Queue()

    async def read_events(_runtime: GrabbedDevice) -> AsyncIterator[SimpleNamespace]:
        while True:
            yield await inputs.get()

    monkeypatch.setattr(pipeline, "read_events", read_events)
    keyboard.natural_mouse_mover = manager.move_cursor_natural
    keyboard.emergency_resetter = manager.emergency_reset
    keyboard.device = Mock()
    keyboard.task = asyncio.create_task(
        pipeline.event_loop(keyboard, asyncio_mod=adapters.ASYNCIO_RUNTIME, log=grabbed_device.log)
    )

    def feed(*events: tuple[int, int]) -> None:
        for code, value in events:
            inputs.put_nowait(SimpleNamespace(type=EV_KEY, code=code, value=value))

    async def moving() -> bool:
        before = len(rel_writes())
        await asyncio.sleep(0.05)
        return len(rel_writes()) > before

    return SimpleNamespace(
        manager=manager,
        keyboard=keyboard,
        output=output,
        rel_writes=rel_writes,
        feed=feed,
        moving=moving,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reset", [MAPPED_RESET, CTRL_ALT_ESC_DOUBLE_TAP])
@pytest.mark.parametrize(
    ("move_keys", "combos"),
    [([(KEY_A, 1)], None), ([(KEY_C, 1), (KEY_V, 1)], [COMBO_SLOW_MOVE])],
)
async def test_a_reset_on_the_moving_keyboard_stops_a_natural_move_right_away(
    monkeypatch,
    reset: list[tuple[int, int]],
    move_keys: list[tuple[int, int]],
    combos: list[object] | None,
) -> None:
    rig = await cursor_rig(
        monkeypatch,
        {"key_a": SLOW_MOVE, "key_e": MappingAction(action_type=ActionType.EMERGENCY_RESET)},
        combos,
    )
    rig.feed(*move_keys)
    await wait_until(lambda: bool(rig.rel_writes()))

    rig.feed(*reset)
    await wait_until(lambda: rig.manager.grabbed_devices == {})

    assert not await rig.moving()


@pytest.mark.asyncio
@pytest.mark.parametrize("reset", [MAPPED_RESET, CTRL_ALT_ESC_DOUBLE_TAP])
async def test_a_reset_on_the_moving_keyboard_stops_a_rollover_members_natural_move(
    monkeypatch,
    reset: list[tuple[int, int]],
) -> None:
    rig = await cursor_rig(
        monkeypatch,
        {"key_a": SLOW_MOVE, "key_e": MappingAction(action_type=ActionType.EMERGENCY_RESET)},
        rollover_members=("key_a", "key_s"),
    )
    rig.feed((KEY_A, 1))
    await wait_until(lambda: bool(rig.rel_writes()))

    rig.feed(*reset)
    await wait_until(lambda: rig.manager.grabbed_devices == {})

    assert not await rig.moving()


@pytest.mark.asyncio
async def test_other_keys_and_the_key_release_leave_a_natural_move_running(monkeypatch) -> None:
    rig = await cursor_rig(monkeypatch, {"key_a": SLOW_MOVE, "key_b": key("key_x")})
    rig.feed((KEY_A, 1))
    await wait_until(lambda: bool(rig.rel_writes()))

    rig.feed((KEY_B, 1), (KEY_A, 0))
    await wait_until(lambda: (EV_KEY, KEY_X, 1) in rig.output.writes)

    assert await rig.moving()
    await rig.manager.emergency_reset()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second_move",
    [
        natural_move(100, speed=30_000.0),
        MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="tap move",
                tap_actions=[
                    SuperkeyActionData(
                        action_type=ActionType.MOUSE_MOVE_NATURAL_ABS.value,
                        move_x=100,
                        move_speed=30_000.0,
                        move_jitter=0.0,
                        move_tolerance=1,
                        move_max_duration_ms=600_000,
                    )
                ],
            ),
        ),
    ],
)
async def test_natural_moves_run_one_after_another_in_press_order(
    monkeypatch,
    second_move: MappingAction,
) -> None:
    rig = await cursor_rig(
        monkeypatch,
        {"key_a": natural_move(300, speed=30_000.0), "key_s": second_move},
    )
    rig.feed((KEY_A, 1), (KEY_A, 0), (KEY_S, 1), (KEY_S, 0))

    def arrived_after_first_target() -> bool:
        positions = list(itertools.accumulate(rig.rel_writes(), initial=0))
        return max(positions) >= 299 and abs(positions[-1] - 100) <= 1

    await wait_until(arrived_after_first_target)
    await rig.manager.emergency_reset()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("move_keys", "combos"),
    [([(KEY_A, 1)], None), ([(KEY_C, 1), (KEY_V, 1)], [COMBO_SLOW_MOVE])],
)
async def test_releasing_the_device_stops_its_natural_move(
    monkeypatch,
    move_keys: list[tuple[int, int]],
    combos: list[object] | None,
) -> None:
    rig = await cursor_rig(monkeypatch, {"key_a": SLOW_MOVE}, combos)
    rig.feed(*move_keys)
    await wait_until(lambda: bool(rig.rel_writes()))

    await rig.keyboard.release()

    assert not await rig.moving()


@pytest.mark.asyncio
async def test_an_overload_presses_its_later_actions_once_the_natural_move_arrives(
    monkeypatch,
) -> None:
    overload = MappingAction(
        action_type=ActionType.SUPERKEY,
        superkey_config=SuperkeyConfig(
            name="move and press",
            mode=SuperkeyMode.OVERLOAD,
            overload_actions=[natural_move(300, speed=30_000.0), key("key_x")],
        ),
    )
    rig = await cursor_rig(monkeypatch, {"key_a": overload})
    rig.feed((KEY_A, 1))
    await wait_until(lambda: (EV_KEY, KEY_X, 1) in rig.output.writes)

    pressed_at = rig.output.writes.index((EV_KEY, KEY_X, 1))
    moved_before = sum(
        value
        for event_type, code, value in rig.output.writes[:pressed_at]
        if event_type == EV_REL and code == REL_X
    )
    assert abs(moved_before - 300) <= 1
    await rig.manager.emergency_reset()
