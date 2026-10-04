import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import Mock

import evdev
import pytest

from keymasq.common.ipc import CommandType
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType, DeviceType, SuperkeyMode
from keymasq.common.model.profiles import RolloverGroup, RolloverMember, RolloverWinner
from keymasq.common.model.superkeys import SuperkeyConfig
from keymasq.common.virtual_device_templates import VirtualDeviceConfig, resolve_virtual_devices
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.runtime import adapters
from keymasq.keymasqd.runtime.default_controller_route import DefaultControllerRoute
from keymasq.keymasqd.runtime.grabbed_device import device as grabbed_device
from keymasq.keymasqd.runtime.grabbed_device import outputs
from keymasq.keymasqd.runtime.grabbed_device.device import GrabbedDevice
from keymasq.keymasqd.runtime.grabbed_device.event import pipeline
from keymasq.keymasqd.runtime.grabbed_device.event import rollover as rollover_stage
from keymasq.keymasqd.runtime.grabbed_device.event.rollover import (
    release_consumed_rollover_member,
    resettle_rollover_group,
)
from keymasq.keymasqd.runtime.rollover import (
    RolloverGroupState,
    RolloverRuntime,
    choose_active,
    replace_rollover_groups,
)
from keymasq.keymasqd.runtime.virtual_gamepads import GamepadOutputRouter
from tests.keymasqd.device_manager_support import (
    FakeUInput,
    grabbed_event_processing_deps,
    make_combo_grabbed_device,
    make_combo_runtime_setup,
    make_grabbed_device,
)

KEY_A = evdev.ecodes.KEY_A
KEY_D = evdev.ecodes.KEY_D
KEY_W = evdev.ecodes.KEY_W
ABS_X = evdev.ecodes.ABS_X
EV_ABS = evdev.ecodes.EV_ABS
EV_KEY = evdev.ecodes.EV_KEY
LEFT = -32768
RIGHT = 32767

BTN_SIDE = evdev.ecodes.BTN_SIDE
KEYBOARD = "1234:5678"
MOUSE = "046d:c52b"
BUTTON_MAP = {"key_a": "key_a", "key_d": "key_d", "key_w": "key_w"}
MOUSE_BUTTON_MAP = {"btn_side": "btn_side"}


def axis(value: int, target: str = "abs_x") -> MappingAction:
    return MappingAction(action_type=ActionType.GAMEPAD_AXIS, target=target, axis_value=value)


def key(target: str) -> MappingAction:
    return MappingAction(action_type=ActionType.KEYBOARD, target=target)


def kb(button: str) -> RolloverMember:
    return RolloverMember(hardware_id=KEYBOARD, button=button)


def group(
    *members: str | RolloverMember,
    winner: RolloverWinner = RolloverWinner.NEWEST,
    restore: bool = True,
) -> RolloverGroup:
    return RolloverGroup(
        name="strafe",
        members=[kb(member) if isinstance(member, str) else member for member in members],
        winner=winner,
        restore=restore,
    )


class Owner:
    def __init__(self) -> None:
        self.rollover: RolloverRuntime | None = None
        self.resettles: list[RolloverGroupState] = []

    def resettle(self, _rollover: RolloverRuntime, state: RolloverGroupState) -> None:
        self.resettles.append(state)


class Rig:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mapping: dict[str, MappingAction],
        groups: list[RolloverGroup],
        *,
        passthrough: FakeUInput | None = None,
        mouse_mapping: dict[str, MappingAction] | None = None,
    ) -> None:
        self.owner = Owner()
        replace_rollover_groups(self.owner, groups, resettle=self.owner.resettle)
        self.gamepad = FakeUInput()
        self.keyboard = FakeUInput()
        self.mapping = mapping
        self.mouse_mapping = mouse_mapping or {}
        self.device: GrabbedDevice = make_grabbed_device(
            monkeypatch,
            button_map=BUTTON_MAP,
            mapping_getter=lambda: self.mapping,
            rollover_getter=lambda: self.owner.rollover,
            gamepad_uinput=self.gamepad,
            keyboard_uinput=self.keyboard,
            passthrough_uinput=passthrough,
            running=True,
        )
        self.mouse: GrabbedDevice = make_grabbed_device(
            monkeypatch,
            path="/dev/input/event-mouse",
            hardware_id=MOUSE,
            interface_id="mouse",
            button_map=MOUSE_BUTTON_MAP,
            mapping_getter=lambda: self.mouse_mapping,
            rollover_getter=lambda: self.owner.rollover,
            gamepad_uinput=self.gamepad,
            keyboard_uinput=self.keyboard,
            running=True,
        )

    @property
    def rollover(self) -> RolloverRuntime:
        assert self.owner.rollover is not None
        return self.owner.rollover

    async def send(self, *events: tuple[int, int], device: GrabbedDevice | None = None) -> None:
        for code, value in events:
            await pipeline.process_event(
                device or self.device,
                SimpleNamespace(type=EV_KEY, code=code, value=value),
                deps=grabbed_event_processing_deps(),
            )

    async def click_side(self, value: int) -> None:
        await self.send((BTN_SIDE, value), device=self.mouse)

    async def run_resettles(self) -> None:
        while self.owner.resettles:
            state = self.owner.resettles.pop(0)
            await resettle_rollover_group(
                self.rollover, state, deps=grabbed_event_processing_deps()
            )

    def axis_writes(self, axis_code: int = ABS_X) -> list[int]:
        return [
            value
            for event_type, code, value in self.gamepad.writes
            if event_type == EV_ABS and code == axis_code
        ]

    def key_writes(self) -> list[tuple[int, int]]:
        return key_writes(self.keyboard)


def key_writes(uinput: FakeUInput) -> list[tuple[int, int]]:
    return [(code, value) for event_type, code, value in uinput.writes if event_type == EV_KEY]


@pytest.mark.parametrize(
    ("winner", "restore", "held", "lost", "expected"),
    [
        (RolloverWinner.NEWEST, True, ["a", "d"], set(), "d"),
        (RolloverWinner.NEWEST, False, ["a", "d"], {"d"}, "a"),
        (RolloverWinner.NEWEST, False, ["a"], {"a"}, None),
        (RolloverWinner.OLDEST, True, ["a", "d"], set(), "a"),
        (RolloverWinner.OLDEST, False, ["d"], {"d"}, None),
        (RolloverWinner.NEUTRAL, True, ["a", "d"], set(), None),
        (RolloverWinner.NEUTRAL, True, ["d"], {"d"}, "d"),
        (RolloverWinner.NEUTRAL, False, ["d"], {"d"}, None),
        (RolloverWinner.PRIORITY, True, ["d", "a"], set(), "a"),
        (RolloverWinner.PRIORITY, False, ["d", "a"], {"a"}, "d"),
        (RolloverWinner.NEWEST, True, [], set(), None),
    ],
)
def test_choose_active_follows_winner_and_restore(
    winner: RolloverWinner,
    restore: bool,
    held: list[str],
    lost: set[str],
    expected: str | None,
) -> None:
    rule = group("a", "d", winner=winner, restore=restore)
    chosen = choose_active(rule, [kb(name) for name in held], {kb(name) for name in lost})
    assert chosen == (kb(expected) if expected is not None else None)


@pytest.mark.asyncio
async def test_without_a_group_releasing_one_opposing_key_centers_the_axis(monkeypatch) -> None:
    rig = Rig(monkeypatch, {"key_a": axis(LEFT), "key_d": axis(RIGHT)}, [])

    await rig.send((KEY_A, 1), (KEY_D, 1), (KEY_A, 0), (KEY_D, 0))

    assert rig.axis_writes() == [LEFT, RIGHT, 0, 0]


@pytest.mark.asyncio
async def test_newest_with_restore_follows_the_newest_held_key_on_an_axis(monkeypatch) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": axis(LEFT), "key_d": axis(RIGHT)},
        [group("key_a", "key_d")],
    )

    await rig.send((KEY_A, 1), (KEY_D, 1), (KEY_A, 0))
    assert rig.axis_writes() == [LEFT, RIGHT]
    await rig.send((KEY_D, 0))
    assert rig.axis_writes() == [LEFT, RIGHT, 0]

    rig.gamepad.writes.clear()
    await rig.send((KEY_A, 1), (KEY_D, 1), (KEY_D, 0))
    assert rig.axis_writes() == [LEFT, RIGHT, LEFT]
    await rig.send((KEY_A, 0))
    assert rig.axis_writes() == [LEFT, RIGHT, LEFT, 0]
    assert rig.device.state.held_output_abs["gamepad"] == set()
    assert rig.device.state.held_source_actions == {}


@pytest.mark.asyncio
async def test_newest_with_restore_releases_and_repeats_keyboard_outputs(monkeypatch) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": key("key_left"), "key_d": key("key_right")},
        [group("key_a", "key_d")],
    )

    await rig.send((KEY_A, 1), (KEY_D, 1), (KEY_D, 0), (KEY_A, 0))

    left, right = evdev.ecodes.KEY_LEFT, evdev.ecodes.KEY_RIGHT
    assert rig.key_writes() == [
        (left, 1),
        (left, 0),
        (right, 1),
        (right, 0),
        (left, 1),
        (left, 0),
    ]
    assert rig.device.state.held_output_keys["keyboard"] == set()


@pytest.mark.asyncio
async def test_newest_without_restore_does_not_return_to_a_replaced_key(monkeypatch) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": axis(LEFT), "key_d": axis(RIGHT)},
        [group("key_a", "key_d", restore=False)],
    )

    await rig.send((KEY_A, 1), (KEY_D, 1), (KEY_D, 0))
    assert rig.axis_writes() == [LEFT, RIGHT, 0]

    await rig.send((KEY_A, 0))
    assert rig.axis_writes() == [LEFT, RIGHT, 0]

    await rig.send((KEY_A, 1), (KEY_A, 0))
    assert rig.axis_writes() == [LEFT, RIGHT, 0, LEFT, 0]


@pytest.mark.asyncio
async def test_oldest_ignores_later_presses_until_the_first_key_is_released(monkeypatch) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": axis(LEFT), "key_d": axis(RIGHT)},
        [group("key_a", "key_d", winner=RolloverWinner.OLDEST)],
    )

    await rig.send((KEY_A, 1), (KEY_D, 1))
    assert rig.axis_writes() == [LEFT]
    await rig.send((KEY_A, 0))
    assert rig.axis_writes() == [LEFT, RIGHT]
    await rig.send((KEY_D, 0))
    assert rig.axis_writes() == [LEFT, RIGHT, 0]


@pytest.mark.asyncio
async def test_neutral_centers_while_opposing_keys_are_held(monkeypatch) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": axis(LEFT), "key_d": axis(RIGHT)},
        [group("key_a", "key_d", winner=RolloverWinner.NEUTRAL)],
    )

    await rig.send((KEY_A, 1), (KEY_D, 1), (KEY_A, 0), (KEY_D, 0))

    assert rig.axis_writes() == [LEFT, 0, RIGHT, 0]


@pytest.mark.asyncio
async def test_priority_prefers_the_first_listed_member(monkeypatch) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": axis(LEFT), "key_d": axis(RIGHT)},
        [group("key_d", "key_a", winner=RolloverWinner.PRIORITY)],
    )

    await rig.send((KEY_A, 1), (KEY_D, 1), (KEY_D, 0), (KEY_A, 0))
    assert rig.axis_writes() == [LEFT, RIGHT, LEFT, 0]

    rig.gamepad.writes.clear()
    await rig.send((KEY_D, 1), (KEY_A, 1), (KEY_A, 0), (KEY_D, 0))
    assert rig.axis_writes() == [RIGHT, 0]


@pytest.mark.asyncio
async def test_autorepeat_follows_only_the_active_member(monkeypatch) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": key("key_left"), "key_d": key("key_right")},
        [group("key_a", "key_d", winner=RolloverWinner.OLDEST)],
    )

    await rig.send((KEY_A, 1), (KEY_D, 1), (KEY_D, 2), (KEY_A, 2))

    left = evdev.ecodes.KEY_LEFT
    assert rig.key_writes() == [(left, 1), (left, 2)]


@pytest.mark.asyncio
async def test_unmapped_members_pass_through_with_rollover(monkeypatch) -> None:
    passthrough = FakeUInput()
    rig = Rig(
        monkeypatch,
        {"key_w": key("key_up")},
        [group("key_a", "key_d")],
        passthrough=passthrough,
    )

    await rig.send((KEY_A, 1), (KEY_D, 1), (KEY_D, 0), (KEY_A, 0))

    assert key_writes(passthrough) == [
        (KEY_A, 1),
        (KEY_A, 0),
        (KEY_D, 1),
        (KEY_D, 0),
        (KEY_A, 1),
        (KEY_A, 0),
    ]


@pytest.mark.asyncio
async def test_rapidfire_member_is_released_even_on_a_shared_output(monkeypatch) -> None:
    rapid = axis(LEFT)
    rapid.rapidfire_enabled = True
    rapid.rapidfire_hold_ms = 20
    rapid.rapidfire_wait_ms = 20
    rig = Rig(
        monkeypatch,
        {"key_a": rapid, "key_d": axis(RIGHT)},
        [group("key_a", "key_d")],
    )

    await rig.send((KEY_A, 1))
    assert "key_a" in rig.device.state.rapidfire_tasks

    await rig.send((KEY_D, 1))
    assert "key_a" not in rig.device.state.rapidfire_tasks
    assert rig.axis_writes()[-1] == RIGHT

    await rig.send((KEY_D, 0))
    assert "key_a" in rig.device.state.rapidfire_tasks
    await rig.send((KEY_A, 0))

    assert rig.device.state.rapidfire_tasks == {}
    assert rig.axis_writes()[-1] == 0


def overload(*main: MappingAction, on_press: list[MappingAction] | None = None) -> MappingAction:
    return MappingAction(
        action_type=ActionType.SUPERKEY,
        superkey_config=SuperkeyConfig(
            name="overload",
            mode=SuperkeyMode.OVERLOAD,
            overload_actions=list(main),
            overload_down_actions=list(on_press or []),
        ),
    )


@pytest.mark.asyncio
async def test_overload_superkey_members_run_a_full_press_cycle_per_handover(
    monkeypatch,
) -> None:
    rig = Rig(
        monkeypatch,
        {
            "key_a": overload(axis(LEFT), on_press=[key("key_1")]),
            "key_d": overload(axis(RIGHT)),
        },
        [group("key_a", "key_d")],
    )

    await rig.send((KEY_A, 1), (KEY_D, 1), (KEY_D, 0), (KEY_A, 0))

    assert rig.axis_writes() == [LEFT, 0, RIGHT, 0, LEFT, 0]
    assert rig.key_writes() == [
        (evdev.ecodes.KEY_1, 1),
        (evdev.ecodes.KEY_1, 0),
        (evdev.ecodes.KEY_1, 1),
        (evdev.ecodes.KEY_1, 0),
    ]
    assert not any(rig.device.state.superkey_abs_refcounts.values())


@pytest.mark.asyncio
async def test_other_keys_keep_normal_dispatch(monkeypatch) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": axis(LEFT), "key_d": axis(RIGHT), "key_w": axis(LEFT, "abs_y")},
        [group("key_a", "key_d")],
    )

    await rig.send((KEY_A, 1), (KEY_W, 1), (KEY_W, 0), (KEY_A, 0))

    assert rig.axis_writes() == [LEFT, 0]
    assert rig.axis_writes(evdev.ecodes.ABS_Y) == [LEFT, 0]


@pytest.mark.asyncio
async def test_replacing_groups_keeps_the_active_release_and_swallows_suppressed_ones(
    monkeypatch,
) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": axis(LEFT), "key_d": axis(RIGHT)},
        [group("key_a", "key_d", winner=RolloverWinner.OLDEST)],
    )
    await rig.send((KEY_A, 1), (KEY_D, 1))
    assert rig.axis_writes() == [LEFT]

    replace_rollover_groups(rig.owner, [])

    await rig.send((KEY_D, 0))
    assert rig.axis_writes() == [LEFT]
    await rig.send((KEY_A, 0))
    assert rig.axis_writes() == [LEFT, 0]
    assert rig.device.state.rollover_quarantined == set()


@pytest.mark.asyncio
async def test_unchanged_groups_keep_live_state(monkeypatch) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": axis(LEFT), "key_d": axis(RIGHT)},
        [group("key_a", "key_d")],
    )
    before = rig.rollover
    await rig.send((KEY_A, 1), (KEY_D, 1))

    replace_rollover_groups(rig.owner, [group("key_a", "key_d")])

    assert rig.rollover is before
    await rig.send((KEY_D, 0), (KEY_A, 0))
    assert rig.axis_writes() == [LEFT, RIGHT, LEFT, 0]


@pytest.mark.asyncio
async def test_releasing_tracked_outputs_forgets_group_state(monkeypatch) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": axis(LEFT), "key_d": axis(RIGHT)},
        [group("key_a", "key_d")],
    )
    await rig.send((KEY_A, 1), (KEY_D, 1))

    rig.device.release_tracked_outputs()
    state = rig.rollover.group_for(kb("key_a"))
    assert state is not None
    assert state.held == []
    assert state.active is None
    assert rig.owner.resettles == []

    rig.gamepad.writes.clear()
    await rig.send((KEY_A, 0), (KEY_D, 0), (KEY_D, 1))
    assert rig.axis_writes()[-1] == RIGHT


@pytest.mark.asyncio
async def test_members_on_two_devices_hand_over_one_axis(monkeypatch) -> None:
    side = RolloverMember(hardware_id=MOUSE, button="btn_side")
    rig = Rig(
        monkeypatch,
        {"key_a": axis(LEFT)},
        [group("key_a", side)],
        mouse_mapping={"btn_side": axis(RIGHT)},
    )

    await rig.send((KEY_A, 1))
    await rig.click_side(1)
    await rig.click_side(0)
    await rig.send((KEY_A, 0))

    assert rig.axis_writes() == [LEFT, RIGHT, LEFT, 0]
    assert rig.device.state.held_source_actions == {}
    assert rig.mouse.state.held_source_actions == {}


@pytest.mark.parametrize("restore", [True, False])
@pytest.mark.asyncio
async def test_a_member_on_another_device_takes_over_when_a_device_leaves(
    monkeypatch,
    restore: bool,
) -> None:
    side = RolloverMember(hardware_id=MOUSE, button="btn_side")
    rig = Rig(
        monkeypatch,
        {"key_a": axis(LEFT)},
        [group("key_a", side, restore=restore)],
        mouse_mapping={"btn_side": axis(RIGHT)},
    )
    await rig.send((KEY_A, 1))
    await rig.click_side(1)
    assert rig.axis_writes() == [LEFT, RIGHT]

    rig.mouse.release_tracked_outputs()
    assert rig.axis_writes() == [LEFT, RIGHT, 0]
    assert len(rig.owner.resettles) == int(restore)

    for state in rig.owner.resettles:
        await resettle_rollover_group(rig.rollover, state, deps=grabbed_event_processing_deps())

    expected = [LEFT, RIGHT, 0, LEFT] if restore else [LEFT, RIGHT, 0]
    assert rig.axis_writes() == expected
    await rig.send((KEY_A, 0))
    assert rig.axis_writes() == ([*expected, 0] if restore else expected)


@pytest.mark.asyncio
async def test_set_rollover_groups_installs_and_clears_groups(monkeypatch) -> None:
    manager = DeviceManager()
    payload = [
        {
            "name": "strafe",
            "members": [
                {"hardware_id": KEYBOARD, "button": "key_a"},
                {"hardware_id": MOUSE, "button": "btn_side"},
            ],
            "winner": "oldest",
        }
    ]

    result = await manager.set_rollover_groups(payload)

    assert result == {"updated": True, "group_count": 1}
    assert manager.rollover is not None
    assert manager.rollover.groups == [
        RolloverGroup(
            name="strafe",
            members=[kb("key_a"), RolloverMember(hardware_id=MOUSE, button="btn_side")],
            winner=RolloverWinner.OLDEST,
            restore=True,
        )
    ]
    assert manager.rollover.group_for(RolloverMember(MOUSE, "btn_side")) is not None

    await manager.set_rollover_groups([])
    assert manager.rollover is None

    await manager.set_rollover_groups(payload)
    await manager.release_all_devices()
    assert manager.rollover is None


@pytest.mark.asyncio
async def test_device_manager_resettle_hands_over_after_a_device_leaves(monkeypatch) -> None:
    manager = DeviceManager()
    gamepad = FakeUInput()
    await manager.set_rollover_groups(
        [
            {
                "name": "strafe",
                "members": [
                    {"hardware_id": KEYBOARD, "button": "key_a"},
                    {"hardware_id": MOUSE, "button": "btn_side"},
                ],
            }
        ]
    )
    keyboard = make_grabbed_device(
        monkeypatch,
        button_map=BUTTON_MAP,
        mapping={"key_a": axis(LEFT)},
        rollover_getter=lambda: manager.rollover,
        gamepad_uinput=gamepad,
        running=True,
    )
    mouse = make_grabbed_device(
        monkeypatch,
        path="/dev/input/event-mouse",
        hardware_id=MOUSE,
        interface_id="mouse",
        button_map=MOUSE_BUTTON_MAP,
        mapping={"btn_side": axis(RIGHT)},
        rollover_getter=lambda: manager.rollover,
        gamepad_uinput=gamepad,
        running=True,
    )
    for device, code in ((keyboard, KEY_A), (mouse, BTN_SIDE)):
        await pipeline.process_event(
            device,
            SimpleNamespace(type=EV_KEY, code=code, value=1),
            deps=grabbed_event_processing_deps(),
        )

    mouse.release_tracked_outputs()
    for _ in range(5):
        await asyncio.sleep(0)

    axis_writes = [value for event_type, _code, value in gamepad.writes if event_type == EV_ABS]
    assert axis_writes == [LEFT, RIGHT, 0, LEFT]


def test_release_all_keys_without_rollover_support_is_harmless(monkeypatch) -> None:
    device = make_grabbed_device(monkeypatch, button_map=BUTTON_MAP)
    outputs.release_all_keys(
        device,
        evdev_mod=evdev,
        uinput_writer=lambda uinput: uinput,
    )
    assert device.state.rollover_quarantined == set()


def held_keys(uinput: FakeUInput) -> set[int]:
    state: dict[int, int] = {}
    for code, value in key_writes(uinput):
        state[code] = value
    return {code for code, value in state.items() if value}


@pytest.mark.asyncio
async def test_a_member_release_swallowed_by_combo_recall_does_not_come_back(
    monkeypatch,
) -> None:
    def trigger(evdev_name: str) -> dict[str, str]:
        return {"hardware_id": KEYBOARD, "source": "kbd", "evdev": evdev_name}

    setup = await make_combo_runtime_setup(
        monkeypatch,
        [
            {
                "id": "a-c",
                "name": "a-c",
                "steps": [{"events": [trigger("key_a"), trigger("key_c")]}],
                "action": {"action": "keyboard", "target": "key_f13"},
                "recall_trigger_keys": True,
            }
        ],
        hardware_id=KEYBOARD,
        button_map={"key_a": "key_a", "key_c": "key_c", "key_d": "key_d"},
    )
    manager, device = setup.manager, setup.device
    await manager.set_rollover_groups(
        [
            {
                "name": "strafe",
                "members": [
                    {"hardware_id": KEYBOARD, "button": "key_a"},
                    {"hardware_id": KEYBOARD, "button": "key_d"},
                ],
            }
        ]
    )
    device.rollover_getter = lambda: manager.rollover

    key_c = evdev.ecodes.KEY_C
    for code, value in ((KEY_A, 1), (key_c, 1), (key_c, 0), (KEY_A, 0), (KEY_D, 1), (KEY_D, 0)):
        await pipeline.process_event(
            device,
            SimpleNamespace(type=EV_KEY, code=code, value=value),
            deps=grabbed_event_processing_deps(),
        )
        await asyncio.sleep(0)

    assert held_keys(setup.passthrough) == set()


@pytest.mark.asyncio
async def test_a_device_leaving_after_a_seamless_handover_keeps_the_new_members_value(
    monkeypatch,
) -> None:
    side = RolloverMember(hardware_id=MOUSE, button="btn_side")
    rig = Rig(
        monkeypatch,
        {"key_a": axis(LEFT)},
        [group("key_a", side)],
        mouse_mapping={"btn_side": axis(RIGHT)},
    )
    await rig.send((KEY_A, 1))
    await rig.click_side(1)

    rig.device.release_tracked_outputs()
    for state in rig.owner.resettles:
        await resettle_rollover_group(rig.rollover, state, deps=grabbed_event_processing_deps())

    assert rig.axis_writes() == [LEFT, RIGHT]
    await rig.click_side(0)
    assert rig.axis_writes() == [LEFT, RIGHT, 0]


@pytest.mark.asyncio
async def test_changing_other_groups_keeps_a_groups_held_keys(monkeypatch) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": axis(LEFT), "key_d": axis(RIGHT)},
        [group("key_a", "key_d")],
    )
    await rig.send((KEY_A, 1))

    walk = RolloverGroup(name="walk", members=[kb("key_w"), kb("key_s")])
    replace_rollover_groups(
        rig.owner,
        [group("key_a", "key_d"), walk],
        resettle=rig.owner.resettle,
    )
    await rig.send((KEY_D, 1), (KEY_A, 0))

    assert rig.axis_writes() == [LEFT, RIGHT]
    await rig.send((KEY_D, 0))
    assert rig.axis_writes() == [LEFT, RIGHT, 0]


async def combo_rollover_setup(
    monkeypatch,
    *,
    restore_trigger_keys: list[str] | None = None,
    mapping: dict[str, MappingAction] | None = None,
    extra_groups: list[dict[str, object]] | None = None,
    winner: str = "newest",
) -> SimpleNamespace:

    def trigger(evdev_name: str) -> dict[str, str]:
        return {"hardware_id": KEYBOARD, "source": "kbd", "evdev": evdev_name}

    combo: dict[str, object] = {
        "id": "a-c",
        "name": "a-c",
        "steps": [{"events": [trigger("key_a"), trigger("key_c")]}],
        "action": {"action": "keyboard", "target": "key_f13"},
        "recall_trigger_keys": True,
    }
    if restore_trigger_keys is not None:
        combo["restore_trigger_keys"] = restore_trigger_keys
    setup = await make_combo_runtime_setup(
        monkeypatch,
        [combo],
        hardware_id=KEYBOARD,
        button_map={"key_a": "key_a", "key_c": "key_c", "key_d": "key_d", "key_w": "key_w"},
        mapping=mapping,
    )
    manager, device = setup.manager, setup.device
    await manager.set_rollover_groups(
        [
            {
                "name": "strafe",
                "members": [
                    {"hardware_id": KEYBOARD, "button": "key_a"},
                    {"hardware_id": KEYBOARD, "button": "key_d"},
                ],
                "winner": winner,
            },
            *(extra_groups or []),
        ]
    )
    device.rollover_getter = lambda: manager.rollover

    async def send(*events: tuple[int, int]) -> None:
        for code, value in events:
            await pipeline.process_event(
                device,
                SimpleNamespace(type=EV_KEY, code=code, value=value),
                deps=grabbed_event_processing_deps(),
            )
            for _ in range(3):
                await asyncio.sleep(0)

    return SimpleNamespace(
        manager=manager,
        device=device,
        passthrough=setup.passthrough,
        keyboard=setup.keyboard,
        send=send,
    )


@pytest.mark.asyncio
async def test_a_recalled_member_still_held_does_not_come_back_after_a_handover(
    monkeypatch,
) -> None:
    rig = await combo_rollover_setup(monkeypatch)
    key_c = evdev.ecodes.KEY_C

    await rig.send((KEY_A, 1), (key_c, 1), (key_c, 0), (KEY_D, 1), (KEY_D, 0))
    assert key_writes(rig.passthrough) == [(KEY_A, 1), (KEY_A, 0), (KEY_D, 1), (KEY_D, 0)]
    assert key_writes(rig.keyboard) == [(evdev.ecodes.KEY_F13, 1), (evdev.ecodes.KEY_F13, 0)]
    await rig.send((KEY_A, 0))
    assert key_writes(rig.passthrough) == [(KEY_A, 1), (KEY_A, 0), (KEY_D, 1), (KEY_D, 0)]


@pytest.mark.asyncio
async def test_a_combo_restores_a_recalled_member_through_its_group(monkeypatch) -> None:
    rig = await combo_rollover_setup(monkeypatch, restore_trigger_keys=["key_a"])
    key_c = evdev.ecodes.KEY_C

    await rig.send((KEY_A, 1), (key_c, 1))
    assert held_keys(rig.passthrough) == set()
    assert held_keys(rig.keyboard) == {evdev.ecodes.KEY_F13}
    await rig.send((KEY_D, 1))
    assert held_keys(rig.passthrough) == {KEY_D}
    await rig.send((key_c, 0))
    assert held_keys(rig.passthrough) == {KEY_D}
    await rig.send((KEY_D, 0))
    assert held_keys(rig.passthrough) == {KEY_A}
    await rig.send((KEY_A, 0))
    assert held_keys(rig.passthrough) == set()


@pytest.mark.asyncio
async def test_a_suppressed_member_repeat_after_its_group_is_removed_changes_nothing(
    monkeypatch,
) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": axis(LEFT), "key_d": axis(RIGHT)},
        [group("key_a", "key_d", winner=RolloverWinner.OLDEST)],
    )
    await rig.send((KEY_A, 1), (KEY_D, 1))
    replace_rollover_groups(rig.owner, [], resettle=rig.owner.resettle)

    await rig.send((KEY_A, 0), (KEY_D, 2), (KEY_D, 0))

    assert rig.axis_writes() == [LEFT, 0]
    await rig.send((KEY_D, 1), (KEY_D, 0))
    assert rig.axis_writes() == [LEFT, 0, RIGHT, 0]


@pytest.mark.asyncio
async def test_changing_a_group_keeps_its_held_keys(monkeypatch) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": axis(LEFT), "key_d": axis(RIGHT)},
        [group("key_a", "key_d")],
    )
    await rig.send((KEY_A, 1))
    replace_rollover_groups(
        rig.owner,
        [group("key_a", "key_d", restore=False)],
        resettle=rig.owner.resettle,
    )
    await rig.run_resettles()

    await rig.send((KEY_D, 1), (KEY_A, 0))
    assert rig.axis_writes() == [LEFT, RIGHT]
    await rig.send((KEY_D, 0))
    assert rig.axis_writes() == [LEFT, RIGHT, 0]


@pytest.mark.asyncio
async def test_a_changed_group_settles_held_keys_by_its_new_rule(monkeypatch) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": axis(LEFT), "key_d": axis(RIGHT)},
        [group("key_a", "key_d", winner=RolloverWinner.OLDEST)],
    )
    await rig.send((KEY_A, 1), (KEY_D, 1))
    assert rig.axis_writes() == [LEFT]

    replace_rollover_groups(rig.owner, [group("key_a", "key_d")], resettle=rig.owner.resettle)
    await rig.run_resettles()
    assert rig.axis_writes() == [LEFT, RIGHT]
    await rig.send((KEY_D, 2))
    assert rig.axis_writes() == [LEFT, RIGHT, RIGHT]
    await rig.send((KEY_D, 0), (KEY_A, 0))
    assert rig.axis_writes() == [LEFT, RIGHT, RIGHT, LEFT, 0]


@pytest.mark.asyncio
async def test_a_slow_member_action_holds_up_neither_other_groups_nor_other_devices(
    monkeypatch,
) -> None:
    side = RolloverMember(hardware_id=MOUSE, button="btn_side")
    rig = Rig(
        monkeypatch,
        {"key_a": axis(LEFT), "key_d": axis(RIGHT), "key_w": key("key_x")},
        [group("key_a", "key_d"), group(side, "key_w")],
        mouse_mapping={
            "btn_side": MappingAction(action_type=ActionType.MOUSE_MOVE_NATURAL_ABS),
        },
    )
    gate = asyncio.Event()

    async def slow_move(*_args: object) -> None:
        await gate.wait()

    rig.mouse.natural_mouse_mover = slow_move
    await rig.send((KEY_A, 1))
    moving = asyncio.create_task(rig.click_side(1))
    await asyncio.sleep(0)
    assert not moving.done()

    await asyncio.wait_for(rig.send((KEY_A, 0)), timeout=1)
    assert rig.axis_writes() == [LEFT, 0]
    await asyncio.wait_for(rig.send((KEY_W, 1)), timeout=1)
    assert rig.key_writes() == []

    gate.set()
    await moving
    for _ in range(5):
        await asyncio.sleep(0)
    assert rig.key_writes() == [(evdev.ecodes.KEY_X, 1)]
    await rig.send((KEY_W, 0))
    assert rig.key_writes() == [(evdev.ecodes.KEY_X, 1), (evdev.ecodes.KEY_X, 0)]


@pytest.mark.asyncio
async def test_an_event_waiting_on_a_replaced_group_uses_its_replacement(monkeypatch) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": axis(LEFT), "key_d": axis(RIGHT)},
        [group("key_a", "key_d", winner=RolloverWinner.OLDEST)],
    )
    await rig.send((KEY_A, 1))
    old_state = rig.rollover.states[0]

    async with old_state.lock:
        await rig.send((KEY_D, 1))
        await settle_tasks()
        replace_rollover_groups(rig.owner, [group("key_a", "key_d")], resettle=rig.owner.resettle)
    for _ in range(5):
        await asyncio.sleep(0)

    assert rig.axis_writes() == [LEFT, RIGHT]
    await rig.send((KEY_D, 0), (KEY_A, 0))
    assert rig.axis_writes() == [LEFT, RIGHT, LEFT, 0]


@pytest.mark.asyncio
async def test_a_queued_press_is_dropped_when_its_device_goes_away(monkeypatch) -> None:
    side = RolloverMember(hardware_id=MOUSE, button="btn_side")
    rig = Rig(
        monkeypatch,
        {"key_w": key("key_x")},
        [group(side, "key_w")],
        mouse_mapping={
            "btn_side": MappingAction(action_type=ActionType.MOUSE_MOVE_NATURAL_ABS),
        },
    )
    gate = asyncio.Event()

    async def slow_move(*_args: object) -> None:
        await gate.wait()

    rig.mouse.natural_mouse_mover = slow_move
    moving = asyncio.create_task(rig.click_side(1))
    await asyncio.sleep(0)
    await rig.send((KEY_W, 1))

    rig.device.release_tracked_outputs()
    gate.set()
    await moving
    for _ in range(5):
        await asyncio.sleep(0)

    assert rig.key_writes() == []
    assert rig.rollover.states[0].member(kb("key_w")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 2])
async def test_a_queued_event_of_a_key_its_removed_group_held_back_changes_nothing(
    monkeypatch,
    value: int,
) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": axis(LEFT), "key_d": axis(RIGHT)},
        [group("key_a", "key_d", winner=RolloverWinner.OLDEST)],
    )
    await rig.send((KEY_A, 1), (KEY_D, 1))

    async with rig.rollover.states[0].lock:
        await rig.send((KEY_D, value))
        replace_rollover_groups(rig.owner, [], resettle=rig.owner.resettle)
    for _ in range(5):
        await asyncio.sleep(0)

    assert rig.axis_writes() == [LEFT]
    if value:
        await rig.send((KEY_D, 0))
    await rig.send((KEY_A, 0))
    assert rig.axis_writes() == [LEFT, 0]


@pytest.mark.asyncio
async def test_a_new_group_adopts_a_key_that_is_already_down(monkeypatch) -> None:
    rig = Rig(monkeypatch, {"key_a": axis(LEFT), "key_d": axis(RIGHT)}, [])
    await rig.send((KEY_A, 1))

    replace_rollover_groups(
        rig.owner,
        [group("key_a", "key_d")],
        resettle=rig.owner.resettle,
        runtimes=[rig.device, rig.mouse],
    )
    await rig.run_resettles()
    await rig.send((KEY_D, 1), (KEY_A, 0))

    assert rig.axis_writes() == [LEFT, RIGHT]
    await rig.send((KEY_D, 0))
    assert rig.axis_writes() == [LEFT, RIGHT, 0]


@pytest.mark.asyncio
async def test_a_merged_group_keeps_the_press_order_across_old_groups(monkeypatch) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": key("key_left"), "key_w": key("key_up")},
        [group("key_a", "key_d"), group("key_w", "key_s")],
    )
    await rig.send((KEY_W, 1), (KEY_A, 1))

    replace_rollover_groups(rig.owner, [group("key_a", "key_w")], resettle=rig.owner.resettle)
    await rig.run_resettles()

    assert held_keys(rig.keyboard) == {evdev.ecodes.KEY_LEFT}


@pytest.mark.asyncio
async def test_merging_groups_keeps_the_winner_on_a_shared_axis(monkeypatch) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": axis(LEFT), "key_w": axis(RIGHT)},
        [group("key_a", "key_d"), group("key_w", "key_s")],
    )
    await rig.send((KEY_A, 1), (KEY_W, 1))
    assert rig.axis_writes() == [LEFT, RIGHT]

    replace_rollover_groups(
        rig.owner,
        [group("key_a", "key_w", winner=RolloverWinner.PRIORITY)],
        resettle=rig.owner.resettle,
    )
    await rig.run_resettles()

    assert rig.axis_writes() == [LEFT, RIGHT, LEFT]
    await rig.send((KEY_A, 0))
    assert rig.axis_writes()[-1] == RIGHT
    await rig.send((KEY_W, 0))
    assert rig.axis_writes()[-1] == 0


async def settle_tasks() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_default_and_explicit_gamepad_output_hand_over_without_rest(monkeypatch) -> None:
    explicit = MappingAction(
        action_type=ActionType.GAMEPAD_AXIS,
        target="abs_x",
        axis_value=RIGHT,
        output_id="virtual-gamepad-1",
    )
    rig = Rig(monkeypatch, {"key_a": axis(LEFT), "key_d": explicit}, [group("key_a", "key_d")])

    await rig.send((KEY_A, 1), (KEY_D, 1))

    assert rig.axis_writes() == [LEFT, RIGHT]


@pytest.mark.asyncio
async def test_a_group_removed_while_a_combo_recalled_a_member_leaves_no_key_down(
    monkeypatch,
) -> None:
    rig = await combo_rollover_setup(
        monkeypatch,
        restore_trigger_keys=["key_a"],
        mapping={"key_a": key("key_left"), "key_d": key("key_right")},
    )
    key_c = evdev.ecodes.KEY_C

    await rig.send((KEY_A, 1), (key_c, 1))
    await rig.manager.set_rollover_groups([])
    await rig.send((key_c, 0), (KEY_A, 0))

    assert held_keys(rig.keyboard) == set()


@pytest.mark.asyncio
async def test_a_later_event_of_a_key_waits_for_its_queued_event_after_the_group_goes(
    monkeypatch,
) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": key("key_left"), "key_d": key("key_right")},
        [group("key_a", "key_d")],
    )
    await rig.send((KEY_A, 1))

    async with rig.rollover.states[0].lock:
        await rig.send((KEY_D, 1))
        replace_rollover_groups(rig.owner, [], resettle=rig.owner.resettle)
        await rig.send((KEY_D, 0))
    await settle_tasks()
    await rig.send((KEY_A, 0))

    left, right = evdev.ecodes.KEY_LEFT, evdev.ecodes.KEY_RIGHT
    assert rig.key_writes() == [(left, 1), (right, 1), (right, 0), (left, 0)]


@pytest.mark.asyncio
async def test_a_group_does_not_adopt_a_held_key_that_drives_nothing(monkeypatch) -> None:
    rig = Rig(monkeypatch, {"key_a": key("key_left"), "key_d": key("key_right")}, [])
    rig.device.state.held_source_keys.add("key_d")

    replace_rollover_groups(
        rig.owner,
        [group("key_a", "key_d")],
        resettle=rig.owner.resettle,
        runtimes=[rig.device, rig.mouse],
    )
    await rig.run_resettles()
    await rig.send((KEY_D, 0))

    assert rig.key_writes() == []


@pytest.mark.asyncio
async def test_group_updates_do_not_stall_a_device_behind_a_busy_group(monkeypatch) -> None:
    walk = {
        "name": "walk",
        "members": [
            {"hardware_id": KEYBOARD, "button": "key_w"},
            {"hardware_id": KEYBOARD, "button": "key_s"},
        ],
    }
    rig = await combo_rollover_setup(monkeypatch, extra_groups=[walk])
    strafe, walking = rig.manager.rollover.states
    await walking.lock.acquire()
    await strafe.lock.acquire()
    update = asyncio.create_task(rig.manager.set_rollover_groups([]))
    await asyncio.sleep(0)
    strafe.lock.release()
    try:
        await asyncio.wait_for(rig.send((KEY_A, 1)), timeout=1)
    finally:
        walking.lock.release()
    await update
    await settle_tasks()
    await rig.send((KEY_A, 0))

    assert held_keys(rig.passthrough) == set()


CURSOR_MOVE = MappingAction(
    action_type=ActionType.MOUSE_MOVE_NATURAL_ABS,
    move_x=1_000_000,
    move_speed=100.0,
    move_max_duration_ms=600_000,
)


async def cursor_member_setup(
    monkeypatch, mouse_mapping: dict[str, MappingAction], *, restore: bool = True
) -> SimpleNamespace:
    setup = await make_combo_runtime_setup(
        monkeypatch,
        [],
        hardware_id=KEYBOARD,
        button_map=BUTTON_MAP,
        mapping={"key_a": CURSOR_MOVE},
    )
    manager, keyboard = setup.manager, setup.device
    mouse = make_combo_grabbed_device(
        monkeypatch,
        manager,
        hardware_id=MOUSE,
        path="/dev/input/event-mouse",
        source="mouse",
        button_map=MOUSE_BUTTON_MAP,
        mapping_getter=lambda: mouse_mapping,
        keyboard_uinput=setup.keyboard,
    )
    aim: dict[str, object] = {
        "name": "aim",
        "members": [
            {"hardware_id": KEYBOARD, "button": "key_a"},
            {"hardware_id": MOUSE, "button": "btn_side"},
        ],
        "restore": restore,
    }
    await manager.set_rollover_groups([aim])
    pointer = FakeUInput()
    manager.output_state.mouse_uinput = pointer

    def cursor_x() -> int:
        return sum(
            value
            for event_type, code, value in pointer.writes
            if event_type == evdev.ecodes.EV_REL and code == evdev.ecodes.REL_X
        )

    async def compositor(event_type: CommandType, data: dict[str, object]) -> None:
        if event_type == CommandType.CURSOR_POSITION_REQUEST:
            manager.handle_cursor_position_response(
                {"request_id": data["request_id"], "status": "ok", "x": cursor_x(), "y": 0}
            )

    manager.broadcast_callback = compositor
    inputs: dict[str, asyncio.Queue[SimpleNamespace]] = {
        KEYBOARD: asyncio.Queue(),
        MOUSE: asyncio.Queue(),
    }

    async def read_events(runtime: GrabbedDevice) -> AsyncIterator[SimpleNamespace]:
        while True:
            yield await inputs[runtime.hardware_id].get()

    monkeypatch.setattr(pipeline, "read_events", read_events)
    for device in (keyboard, mouse):
        device.rollover_getter = lambda: manager.rollover
        device.natural_mouse_mover = manager.move_cursor_natural
        device.device = Mock()
        device.task = asyncio.create_task(
            pipeline.event_loop(
                device, asyncio_mod=adapters.ASYNCIO_RUNTIME, log=grabbed_device.log
            )
        )

    def feed(device: GrabbedDevice, code: int, *, value: int = 1) -> None:
        inputs[device.hardware_id].put_nowait(SimpleNamespace(type=EV_KEY, code=code, value=value))

    async def press(device: GrabbedDevice, code: int, *, value: int = 1) -> None:
        feed(device, code, value=value)
        await settle_tasks()

    async def move_on_keyboard() -> None:
        await press(keyboard, KEY_A)
        while not pointer.writes:
            await asyncio.sleep(0.01)

    async def reset_during_update(**changes: object) -> None:
        update = asyncio.create_task(manager.set_rollover_groups([{**aim, **changes}]))
        await asyncio.sleep(0)
        reset = asyncio.create_task(manager.emergency_reset())
        await asyncio.sleep(0)
        await press(mouse, BTN_SIDE, value=0)
        await press(mouse, BTN_SIDE)
        await asyncio.wait_for(reset, timeout=1)
        await update
        await asyncio.sleep(0.1)
        moved = len(pointer.writes)
        await asyncio.sleep(0.1)
        assert len(pointer.writes) == moved
        assert manager.rollover is None
        assert manager.grabbed_devices == {}

    return SimpleNamespace(
        manager=manager,
        aim=aim,
        feed=feed,
        keyboard=keyboard,
        mouse=mouse,
        keys=setup.keyboard,
        press=press,
        move_on_keyboard=move_on_keyboard,
        reset_during_update=reset_during_update,
    )


@pytest.mark.asyncio
async def test_emergency_reset_does_not_wait_for_a_member_moving_the_cursor(monkeypatch) -> None:
    rig = await cursor_member_setup(monkeypatch, {"btn_side": key("key_x")})
    await rig.move_on_keyboard()
    await rig.press(rig.mouse, BTN_SIDE)
    assert rig.mouse.state.rollover_tasks

    await rig.reset_during_update(restore=False)

    assert key_writes(rig.keys) == []


@pytest.mark.asyncio
async def test_emergency_reset_cancels_a_resettle_waiting_behind_a_member(monkeypatch) -> None:
    rig = await cursor_member_setup(monkeypatch, {"btn_side": key("key_x")})
    await rig.press(rig.mouse, BTN_SIDE)
    await rig.move_on_keyboard()
    rig.keyboard.release_tracked_outputs()

    await rig.reset_during_update(restore=False)

    assert key_writes(rig.keys) == [(evdev.ecodes.KEY_X, 1), (evdev.ecodes.KEY_X, 0)]


@pytest.mark.asyncio
async def test_emergency_reset_stops_a_reader_in_the_middle_of_a_member_event(monkeypatch) -> None:
    rig = await cursor_member_setup(monkeypatch, {"btn_side": key("key_x")})
    combos = rig.mouse.event_callback
    combos_done = asyncio.Event()

    async def slow_combos(*args: object, **kwargs: object) -> object:
        await combos_done.wait()
        return await combos(*args, **kwargs)

    rig.mouse.event_callback = slow_combos
    await rig.move_on_keyboard()
    await rig.press(rig.mouse, BTN_SIDE)

    reset = asyncio.create_task(rig.manager.emergency_reset())
    await asyncio.sleep(0)
    combos_done.set()
    await asyncio.wait_for(reset, timeout=1)

    assert key_writes(rig.keys) == []


@pytest.mark.asyncio
async def test_a_group_update_during_a_full_release_waits_for_the_devices(monkeypatch) -> None:
    rig = await cursor_member_setup(monkeypatch, {"btn_side": key("key_x")})
    reset = asyncio.create_task(rig.manager.emergency_reset())
    while rig.manager.rollover is not None:
        await asyncio.sleep(0)
    assert rig.manager.grabbed_devices

    await rig.manager.set_rollover_groups([rig.aim])

    assert rig.manager.grabbed_devices == {}
    await reset


@pytest.mark.asyncio
async def test_emergency_reset_keeps_a_waiting_group_update_from_pressing_a_member(
    monkeypatch,
) -> None:
    mouse_mapping = {"btn_side": key("key_x")}
    rig = await cursor_member_setup(monkeypatch, mouse_mapping, restore=False)
    await rig.press(rig.mouse, BTN_SIDE)
    mouse_mapping["btn_side"] = key("key_y")
    await rig.move_on_keyboard()

    await rig.reset_during_update(winner="oldest", restore=True)

    assert evdev.ecodes.KEY_Y not in {code for code, _value in key_writes(rig.keys)}


@pytest.mark.asyncio
async def test_the_release_of_a_forgotten_member_leaves_the_new_winner_alone(monkeypatch) -> None:
    side = RolloverMember(hardware_id=MOUSE, button="btn_side")
    rig = Rig(
        monkeypatch,
        {"key_a": axis(LEFT)},
        [group("key_a", side)],
        mouse_mapping={"btn_side": axis(RIGHT)},
    )
    await rig.click_side(1)
    await rig.send((KEY_A, 1))
    rig.device.release_tracked_outputs()
    await rig.run_resettles()
    assert rig.axis_writes()[-1] == RIGHT

    await rig.send((KEY_A, 2), (KEY_A, 0))

    assert rig.axis_writes()[-1] == RIGHT
    await rig.click_side(0)
    assert rig.axis_writes()[-1] == 0


@pytest.mark.asyncio
async def test_a_device_leaving_cancels_its_queued_member_action(monkeypatch) -> None:
    rig = Rig(
        monkeypatch,
        {"key_w": MappingAction(action_type=ActionType.MOUSE_MOVE_NATURAL_ABS)},
        [group("key_w", "key_d")],
    )
    gate = asyncio.Event()
    cancelled: list[str] = []

    async def slow_move(*_args: object) -> None:
        try:
            await gate.wait()
        except asyncio.CancelledError:
            cancelled.append("move")
            raise

    rig.device.natural_mouse_mover = slow_move
    async with rig.rollover.states[0].lock:
        await rig.send((KEY_W, 1))
    await settle_tasks()
    assert rig.device.state.rollover_tasks

    rig.device.release_tracked_outputs()
    await settle_tasks()

    assert cancelled == ["move"]
    assert not rig.device.state.rollover_tasks


@pytest.mark.asyncio
async def test_a_consumed_release_after_a_queued_press_releases_the_output(monkeypatch) -> None:
    rig = Rig(monkeypatch, {"key_a": key("key_x")}, [group("key_a", "key_d")])
    deps = grabbed_event_processing_deps()

    async with rig.rollover.states[0].lock:
        await rig.send((KEY_A, 1))
        await release_consumed_rollover_member(
            rig.device,
            SimpleNamespace(type=EV_KEY, code=KEY_A, value=0),
            "key_a",
            deps=deps,
        )
    await settle_tasks()

    assert rig.key_writes() == [(evdev.ecodes.KEY_X, 1), (evdev.ecodes.KEY_X, 0)]


@pytest.mark.asyncio
async def test_a_merged_winner_keeps_the_action_it_pressed_with(monkeypatch) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": axis(LEFT), "key_w": axis(RIGHT)},
        [group("key_a", "key_d"), group("key_w", "key_s")],
    )
    await rig.send((KEY_A, 1), (KEY_W, 1))
    rig.mapping = {"key_a": key("key_q"), "key_w": axis(RIGHT)}

    replace_rollover_groups(
        rig.owner,
        [group("key_a", "key_w", winner=RolloverWinner.PRIORITY)],
        resettle=rig.owner.resettle,
    )
    await rig.run_resettles()
    assert rig.axis_writes() == [LEFT, RIGHT, LEFT]
    await rig.send((KEY_A, 0))

    assert rig.key_writes() == []
    assert rig.axis_writes()[-1] == RIGHT


@pytest.mark.asyncio
async def test_a_device_leaving_after_a_trigger_handover_keeps_the_trigger(monkeypatch) -> None:
    abs_z = evdev.ecodes.ABS_Z
    side = RolloverMember(hardware_id=MOUSE, button="btn_side")
    rig = Rig(
        monkeypatch,
        {"key_a": axis(100, target="abs_z")},
        [group("key_a", side)],
        mouse_mapping={"btn_side": axis(200, target="abs_z")},
    )
    await rig.send((KEY_A, 1))
    await rig.click_side(1)

    rig.device.release_tracked_outputs()

    assert rig.axis_writes(abs_z) == [100, 200]


@pytest.mark.asyncio
async def test_a_recording_control_member_is_not_recorded(monkeypatch) -> None:
    rig = Rig(
        monkeypatch,
        {
            "key_a": MappingAction(action_type=ActionType.STOP_MACRO_RECORDING),
            "key_d": key("key_x"),
        },
        [group("key_a", "key_d")],
    )
    recorded: list[int] = []
    monkeypatch.setattr(
        rollover_stage,
        "record_source_event",
        lambda _runtime, event, *, deps: recorded.append(int(event.code)),
    )

    await rig.send((KEY_A, 1), (KEY_D, 1))

    assert recorded == [KEY_D]


@pytest.mark.asyncio
async def test_a_queued_press_is_recorded_when_it_arrives(monkeypatch) -> None:
    rig = Rig(monkeypatch, {"key_a": key("key_x")}, [group("key_a", "key_d")])
    recorded: list[int] = []
    monkeypatch.setattr(
        rollover_stage,
        "record_source_event",
        lambda _runtime, event, *, deps: recorded.append(int(event.code)),
    )

    async with rig.rollover.states[0].lock:
        await rig.send((KEY_A, 1))
        assert recorded == [KEY_A]
    await settle_tasks()

    assert recorded == [KEY_A]


@pytest.mark.asyncio
async def test_a_one_shot_member_fires_once_per_press(monkeypatch) -> None:
    rig = Rig(
        monkeypatch,
        {
            "key_a": MappingAction(action_type=ActionType.PROFILE_TOGGLE, profile_name="Game"),
            "key_d": axis(RIGHT),
        },
        [group("key_a", "key_d")],
    )
    fired: list[str] = []
    execute = rollover_stage.apply_mapped_action_or_passthrough

    async def counting(runtime, event, event_name, mapping, **kwargs):
        if event_name == "key_a" and int(event.value) == 1:
            fired.append(event_name)
        return await execute(runtime, event, event_name, mapping, **kwargs)

    monkeypatch.setattr(rollover_stage, "apply_mapped_action_or_passthrough", counting)

    await rig.send((KEY_A, 1), (KEY_D, 1), (KEY_D, 0))

    assert fired == ["key_a"]


@pytest.mark.asyncio
async def test_a_release_waits_for_queued_events_of_its_key_before_quarantine(monkeypatch) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": key("key_left"), "key_d": key("key_right")},
        [group("key_a", "key_d", winner=RolloverWinner.OLDEST)],
    )
    await rig.send((KEY_A, 1), (KEY_D, 1))

    async with rig.rollover.states[0].lock:
        await rig.send((KEY_D, 0), (KEY_D, 1))
        replace_rollover_groups(rig.owner, [], resettle=rig.owner.resettle)
        await rig.send((KEY_D, 0))
    await settle_tasks()

    left, right = evdev.ecodes.KEY_LEFT, evdev.ecodes.KEY_RIGHT
    assert rig.key_writes() == [(left, 1), (right, 1), (right, 0)]
    await rig.send((KEY_A, 0))
    assert rig.key_writes() == [(left, 1), (right, 1), (right, 0), (left, 0)]


@pytest.mark.asyncio
async def test_a_cancelled_lock_wait_leaves_the_group_idle(monkeypatch) -> None:
    rig = Rig(monkeypatch, {"key_a": key("key_x")}, [group("key_a", "key_d")])
    state = rig.rollover.states[0]

    async with state.lock:
        await rig.send((KEY_A, 1))
        await settle_tasks()
        rig.device.release_tracked_outputs()
        await settle_tasks()

    assert state.pending == 0


@pytest.mark.asyncio
async def test_the_quarantined_release_of_a_recording_control_member_is_not_recorded(
    monkeypatch,
) -> None:
    rig = Rig(
        monkeypatch,
        {
            "key_a": MappingAction(action_type=ActionType.START_MACRO_RECORDING),
            "key_d": key("key_x"),
        },
        [group("key_a", "key_d")],
    )
    recorded: list[tuple[int, int]] = []
    monkeypatch.setattr(
        rollover_stage,
        "record_source_event",
        lambda _runtime, event, *, deps: recorded.append((int(event.code), int(event.value))),
    )
    await rig.send((KEY_A, 1), (KEY_D, 1))
    replace_rollover_groups(rig.owner, [], resettle=rig.owner.resettle)

    await rig.send((KEY_A, 0))

    assert recorded == [(KEY_D, 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize(("winner", "expected"), [("newest", KEY_A), ("oldest", KEY_D)])
async def test_a_recalled_key_pressed_again_counts_as_the_newest_press(
    monkeypatch,
    winner: str,
    expected: int,
) -> None:
    rig = await combo_rollover_setup(monkeypatch, winner=winner)
    key_c = evdev.ecodes.KEY_C
    await rig.send((KEY_A, 1), (key_c, 1), (key_c, 0), (KEY_D, 1))
    assert held_keys(rig.passthrough) == {KEY_D}

    await rig.send((KEY_A, 1))

    assert held_keys(rig.passthrough) == {expected}


@pytest.mark.asyncio
async def test_default_routed_buttons_on_two_pads_hand_over_without_a_release(
    monkeypatch,
) -> None:
    import logging

    btn_south = evdev.ecodes.BTN_SOUTH
    virtual = FakeUInput()
    state = SimpleNamespace(
        virtual_gamepad_uinputs={"virtual-gamepad-1": virtual},
        virtual_device_specs={
            spec.output_id: spec for spec in resolve_virtual_devices(1, VirtualDeviceConfig())
        },
    )
    router = GamepadOutputRouter(logging.getLogger(__name__))
    owner = Owner()
    pads = []
    for index, hardware_id in enumerate(("aaaa:0001", "aaaa:0002")):
        pad = make_grabbed_device(
            monkeypatch,
            path=f"/dev/input/event-pad{index}",
            hardware_id=hardware_id,
            interface_id=f"pad{index}",
            device_type=DeviceType.GAMEPAD,
            default_output="virtual-gamepad-1",
            button_map={"south": "btn_south"},
            mapping_getter=dict,
            rollover_getter=lambda: owner.rollover,
            gamepad_output_resolver=lambda output_id, context: router.resolve(
                state, {}, output_id, context=context
            ),
            running=True,
        )
        pad.default_route = DefaultControllerRoute("virtual-gamepad-1")
        pads.append(pad)
    replace_rollover_groups(
        owner,
        [
            RolloverGroup(
                name="south",
                members=[
                    RolloverMember(hardware_id="aaaa:0001", button="south"),
                    RolloverMember(hardware_id="aaaa:0002", button="south"),
                ],
            )
        ],
    )

    for pad, value in ((pads[0], 1), (pads[1], 1), (pads[1], 0)):
        await pipeline.process_event(
            pad,
            SimpleNamespace(type=EV_KEY, code=btn_south, value=value),
            deps=grabbed_event_processing_deps(),
        )

    assert [value for _type, code, value in virtual.writes if code == btn_south] == [1, 1, 1]


@pytest.mark.asyncio
async def test_a_queued_consumed_release_releases_a_press_whose_group_went_away(
    monkeypatch,
) -> None:
    rig = Rig(monkeypatch, {"key_a": key("key_x")}, [group("key_a", "key_d")])

    async with rig.rollover.states[0].lock:
        await rig.send((KEY_A, 1))
        await release_consumed_rollover_member(
            rig.device,
            SimpleNamespace(type=EV_KEY, code=KEY_A, value=0),
            "key_a",
            deps=grabbed_event_processing_deps(),
        )
        replace_rollover_groups(rig.owner, [], resettle=rig.owner.resettle)
    await settle_tasks()

    assert rig.key_writes() == [(evdev.ecodes.KEY_X, 1), (evdev.ecodes.KEY_X, 0)]


@pytest.mark.asyncio
async def test_queued_presses_compete_in_the_order_they_arrived(monkeypatch) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": key("key_left"), "key_d": key("key_right")},
        [group("key_a", "key_d")],
    )

    async with rig.rollover.states[0].lock:
        await rig.send((KEY_A, 1), (KEY_A, 0), (KEY_A, 1), (KEY_D, 1))
        await settle_tasks()
    await settle_tasks()

    assert held_keys(rig.keyboard) == {evdev.ecodes.KEY_RIGHT}


@pytest.mark.asyncio
async def test_a_recording_control_release_is_not_recorded_after_a_mapping_change(
    monkeypatch,
) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": MappingAction(action_type=ActionType.START_MACRO_RECORDING)},
        [group("key_a", "key_d")],
    )
    recorded: list[tuple[int, int]] = []
    monkeypatch.setattr(
        rollover_stage,
        "record_source_event",
        lambda _runtime, event, *, deps: recorded.append((int(event.code), int(event.value))),
    )
    await rig.send((KEY_A, 1))
    rig.mapping = {"key_a": key("key_x")}

    await rig.send((KEY_A, 0))

    assert recorded == []
