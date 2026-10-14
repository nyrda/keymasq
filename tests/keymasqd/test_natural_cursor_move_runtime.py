import asyncio
import itertools
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from unittest.mock import Mock

import evdev
import pytest

from keymasq.common.ipc import CommandType
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.analog import AnalogActionThreshold, AnalogControlConfig
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
EV_ABS = evdev.ecodes.EV_ABS
ABS_Z = evdev.ecodes.ABS_Z
REL_X = evdev.ecodes.REL_X
KEY_A = evdev.ecodes.KEY_A
KEY_B = evdev.ecodes.KEY_B
KEY_C = evdev.ecodes.KEY_C
KEY_E = evdev.ecodes.KEY_E
KEY_S = evdev.ecodes.KEY_S
KEY_V = evdev.ecodes.KEY_V
KEY_X = evdev.ecodes.KEY_X
KEY_Y = evdev.ecodes.KEY_Y
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
    analog_inputs: dict[str, object] | None = None,
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
    manager.active_mappings[KEYBOARD] = mapping
    keyboard.mapping_getter = lambda: manager.active_mappings.get(KEYBOARD, {})
    if analog_inputs:
        keyboard.update_analog_inputs(analog_inputs)
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

    def feed(*events: tuple[int, ...]) -> None:
        for event in events:
            event_type, code, value = event if len(event) == 3 else (EV_KEY, *event)
            inputs.put_nowait(SimpleNamespace(type=event_type, code=code, value=value))

    async def moving() -> bool:
        before = len(rel_writes())
        await asyncio.sleep(0.05)
        return len(rel_writes()) > before

    def key_values(code: int) -> list[int]:
        return [
            value
            for event_type, written_code, value in output.writes
            if event_type == EV_KEY and written_code == code
        ]

    def moved_before(write: tuple[int, int, int]) -> int:
        return sum(
            value
            for event_type, code, value in output.writes[: output.writes.index(write)]
            if event_type == EV_REL and code == REL_X
        )

    return SimpleNamespace(
        manager=manager,
        keyboard=keyboard,
        output=output,
        rel_writes=rel_writes,
        feed=feed,
        moving=moving,
        key_values=key_values,
        moved_before=moved_before,
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


def superkey_move(x: int, *, speed: float = 100.0) -> SuperkeyActionData:
    return SuperkeyActionData(
        action_type=ActionType.MOUSE_MOVE_NATURAL_ABS.value,
        move_x=x,
        move_speed=speed,
        move_jitter=0.0,
        move_tolerance=1,
        move_max_duration_ms=600_000,
    )


def superkey_key(target: str) -> SuperkeyActionData:
    return SuperkeyActionData(action_type=ActionType.KEYBOARD.value, target=target)


def pattern(
    *,
    tap: list[SuperkeyActionData] | None = None,
    double_tap: list[SuperkeyActionData] | None = None,
    hold: list[SuperkeyActionData] | None = None,
) -> MappingAction:
    return MappingAction(
        action_type=ActionType.SUPERKEY,
        superkey_config=SuperkeyConfig(
            name="pattern",
            tap_actions=list(tap or []),  # type: ignore[arg-type]
            double_tap_actions=list(double_tap or []),  # type: ignore[arg-type]
            hold_actions=list(hold or []),  # type: ignore[arg-type]
            hold_threshold_ms=10,
        ),
    )


def overload(
    *,
    held: list[MappingAction] | None = None,
    on_press: list[MappingAction] | None = None,
    on_release: list[MappingAction] | None = None,
) -> MappingAction:
    return MappingAction(
        action_type=ActionType.SUPERKEY,
        superkey_config=SuperkeyConfig(
            name="overload",
            mode=SuperkeyMode.OVERLOAD,
            overload_actions=list(held or []),
            overload_down_actions=list(on_press or []),
            overload_up_actions=list(on_release or []),
        ),
    )


def threshold(*actions: MappingAction) -> MappingAction:
    return MappingAction(
        action_type=ActionType.ANALOG_CONTROL,
        analog_control_config=AnalogControlConfig(
            name="trigger",
            input_type="axis",
            thresholds=[
                AnalogActionThreshold(
                    axis="x",
                    trigger_min=0.5,
                    trigger_max=1.0,
                    release_min=0.4,
                    release_max=1.0,
                    actions=list(actions),
                )
            ],
        ),
    )


def move_data(x: int, *, speed: float = 100.0) -> dict[str, object]:
    return {
        "action": "mouse_move_natural_abs",
        "x": x,
        "speed": speed,
        "jitter": 0.0,
        "tolerance": 1,
        "max_duration_ms": 600_000,
    }


def combo_superkey(superkey: dict[str, object]) -> list[object]:
    return [
        {
            **COMBO_SLOW_MOVE,
            "action": {"action": "superkey", "superkey": {"name": "combo", **superkey}},
        }
    ]


TRIGGER_AXIS: dict[str, object] = {
    "trigger": {
        "type": "axis",
        "axes": [{"role": "x", "evdev": "abs_z", "minimum": 0, "maximum": 255}],
    }
}
AXIS_PRESSED = (EV_ABS, ABS_Z, 255)
AXIS_RELEASED = (EV_ABS, ABS_Z, 0)
PRESS = [(KEY_A, 1)]
TAP = [(KEY_A, 1), (KEY_A, 0)]
COMBO_PRESS = [(KEY_C, 1), (KEY_V, 1)]
COMBO_TAP = [*COMBO_PRESS, (KEY_V, 0), (KEY_C, 0)]
FAST_MOVE = natural_move(300, speed=30_000.0)
FAST_MOVE_THEN_X = [superkey_move(300, speed=30_000.0), superkey_key("key_x")]
FAST_MOVE_THEN_X_DATA = [
    move_data(300, speed=30_000.0),
    {"action": "keyboard", "target": "key_x"},
]
X_SLOW_MOVE_Y = [superkey_key("key_x"), superkey_move(1_000_000), superkey_key("key_y")]


def scenario(
    events: list[tuple[int, ...]],
    mapping: dict[str, MappingAction] | None = None,
    *,
    combos: list[object] | None = None,
    analog_inputs: dict[str, object] | None = None,
    then: list[tuple[int, ...]] | None = None,
    presses: int = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        events=events,
        mapping=mapping or {},
        combos=combos,
        analog_inputs=analog_inputs,
        then=then or [],
        presses=presses,
    )


SLOW_SUPERKEY_MOVES = {
    "overload": scenario(PRESS, {"key_a": overload(held=[SLOW_MOVE])}),
    "overload on press": scenario(PRESS, {"key_a": overload(on_press=[SLOW_MOVE])}),
    "overload on release": scenario(TAP, {"key_a": overload(on_release=[SLOW_MOVE])}),
    "tap": scenario(TAP, {"key_a": pattern(tap=[superkey_move(1_000_000)])}),
    "double tap": scenario(TAP + TAP, {"key_a": pattern(double_tap=[superkey_move(1_000_000)])}),
    "analog threshold": scenario(
        [AXIS_PRESSED], {"trigger": threshold(SLOW_MOVE)}, analog_inputs=TRIGGER_AXIS
    ),
    "combo tap": scenario(
        COMBO_TAP,
        combos=combo_superkey({"mode": "pattern", "tap_actions": [move_data(1_000_000)]}),
    ),
    "combo overload": scenario(
        COMBO_PRESS,
        combos=combo_superkey({"mode": "overload", "overload_actions": [move_data(1_000_000)]}),
    ),
    "combo overload on release": scenario(
        COMBO_TAP,
        combos=combo_superkey(
            {"mode": "overload", "overload_up_actions": [move_data(1_000_000)]}
        ),
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("reset", [MAPPED_RESET, CTRL_ALT_ESC_DOUBLE_TAP])
@pytest.mark.parametrize(
    "trigger", list(SLOW_SUPERKEY_MOVES.values()), ids=list(SLOW_SUPERKEY_MOVES)
)
async def test_typing_and_a_reset_reach_the_keyboard_while_a_superkey_moves_the_cursor(
    monkeypatch,
    trigger: SimpleNamespace,
    reset: list[tuple[int, int]],
) -> None:
    rig = await cursor_rig(
        monkeypatch,
        {
            **trigger.mapping,
            "key_b": key("key_x"),
            "key_e": MappingAction(action_type=ActionType.EMERGENCY_RESET),
        },
        trigger.combos,
        analog_inputs=trigger.analog_inputs,
    )
    rig.feed(*trigger.events)
    await wait_until(lambda: bool(rig.rel_writes()))

    rig.feed((KEY_B, 1), (KEY_B, 0))
    await wait_until(lambda: rig.key_values(KEY_X) == [1, 0])
    assert await rig.moving()

    rig.feed(*reset)
    await wait_until(lambda: rig.manager.grabbed_devices == {})
    assert not await rig.moving()


PAIRED_SEQUENCES = {
    "overload": scenario(TAP, {"key_a": overload(held=[FAST_MOVE, key("key_x")])}),
    "overload pressed twice": scenario(
        TAP + TAP, {"key_a": overload(held=[FAST_MOVE, key("key_x")])}, presses=2
    ),
    "tap": scenario(TAP, {"key_a": pattern(tap=FAST_MOVE_THEN_X)}),
    "tap pressed twice": scenario(
        TAP + [(KEY_B, 1), (KEY_B, 0)] + TAP,
        {"key_a": pattern(tap=FAST_MOVE_THEN_X)},
        presses=2,
    ),
    "hold": scenario(PRESS, {"key_a": pattern(hold=FAST_MOVE_THEN_X)}, then=[(KEY_A, 0)]),
    "analog threshold": scenario(
        [AXIS_PRESSED, AXIS_RELEASED],
        {"trigger": threshold(FAST_MOVE, key("key_x"))},
        analog_inputs=TRIGGER_AXIS,
    ),
    "combo tap": scenario(
        COMBO_TAP,
        combos=combo_superkey({"mode": "pattern", "tap_actions": FAST_MOVE_THEN_X_DATA}),
    ),
    "combo overload": scenario(
        COMBO_TAP,
        combos=combo_superkey({"mode": "overload", "overload_actions": FAST_MOVE_THEN_X_DATA}),
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", list(PAIRED_SEQUENCES.values()), ids=list(PAIRED_SEQUENCES))
async def test_a_key_after_a_natural_move_is_pressed_on_arrival_and_released_after_its_press(
    monkeypatch,
    trigger: SimpleNamespace,
) -> None:
    rig = await cursor_rig(
        monkeypatch,
        trigger.mapping,
        trigger.combos,
        analog_inputs=trigger.analog_inputs,
    )
    rig.feed(*trigger.events)
    if trigger.then:
        await wait_until(lambda: bool(rig.rel_writes()))
        rig.feed(*trigger.then)

    await wait_until(lambda: rig.key_values(KEY_X) == [1, 0] * trigger.presses)
    await asyncio.sleep(0.05)

    assert rig.key_values(KEY_X) == [1, 0] * trigger.presses
    assert abs(rig.moved_before((EV_KEY, KEY_X, 1)) - 300) <= 1
    await rig.manager.emergency_reset()


async def press_emergency_reset(rig: SimpleNamespace) -> None:
    rig.feed(*MAPPED_RESET)
    await wait_until(lambda: rig.manager.grabbed_devices == {})


async def release_the_keyboard(rig: SimpleNamespace) -> None:
    await rig.keyboard.release()


async def remap_the_superkey(rig: SimpleNamespace) -> None:
    await rig.manager.set_mapping(KEYBOARD, {"key_a": {"action": "keyboard", "target": "key_c"}})


HOLDING_X_DURING_A_SLOW_MOVE = {
    "overload": scenario(
        PRESS, {"key_a": overload(held=[key("key_x"), SLOW_MOVE, key("key_y")])}
    ),
    "overload on release": scenario(
        TAP, {"key_a": overload(held=[key("key_x")], on_release=[SLOW_MOVE, key("key_y")])}
    ),
    "tap": scenario(TAP, {"key_a": pattern(tap=X_SLOW_MOVE_Y)}),
    "hold": scenario(PRESS, {"key_a": pattern(hold=X_SLOW_MOVE_Y)}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cancel", [press_emergency_reset, release_the_keyboard, remap_the_superkey]
)
@pytest.mark.parametrize(
    "trigger",
    list(HOLDING_X_DURING_A_SLOW_MOVE.values()),
    ids=list(HOLDING_X_DURING_A_SLOW_MOVE),
)
async def test_cancelling_a_superkey_sequence_stops_its_move_and_releases_what_it_pressed(
    monkeypatch,
    trigger: SimpleNamespace,
    cancel: Callable[[SimpleNamespace], Awaitable[None]],
) -> None:
    rig = await cursor_rig(
        monkeypatch,
        {**trigger.mapping, "key_e": MappingAction(action_type=ActionType.EMERGENCY_RESET)},
    )
    rig.feed(*trigger.events)
    await wait_until(lambda: bool(rig.rel_writes()))
    assert rig.key_values(KEY_X) == [1]

    await cancel(rig)

    assert not await rig.moving()
    assert rig.key_values(KEY_X) == [1, 0]
    rig.feed((KEY_A, 0))
    await asyncio.sleep(0.05)
    assert rig.key_values(KEY_X) == [1, 0]
    assert rig.key_values(KEY_Y) == []
