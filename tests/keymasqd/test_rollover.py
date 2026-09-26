import asyncio
from types import SimpleNamespace

import evdev
import pytest

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType, SuperkeyMode
from keymasq.common.model.profiles import RolloverGroup, RolloverMember, RolloverWinner
from keymasq.common.model.superkeys import SuperkeyConfig
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.runtime.grabbed_device import outputs
from keymasq.keymasqd.runtime.grabbed_device.device import GrabbedDevice
from keymasq.keymasqd.runtime.grabbed_device.event import pipeline
from keymasq.keymasqd.runtime.grabbed_device.event.rollover import resettle_rollover_group
from keymasq.keymasqd.runtime.rollover import (
    RolloverGroupState,
    RolloverRuntime,
    choose_active,
    replace_rollover_groups,
)
from tests.keymasqd.device_manager_support import (
    FakeUInput,
    grabbed_event_processing_deps,
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
        # A second hardware device that writes to the same virtual gamepad.
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
        """Run the resettles the runtime asked for, as the device manager does."""
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

    # Releasing the older key keeps the newer direction.
    await rig.send((KEY_A, 1), (KEY_D, 1), (KEY_A, 0))
    assert rig.axis_writes() == [LEFT, RIGHT]
    await rig.send((KEY_D, 0))
    assert rig.axis_writes() == [LEFT, RIGHT, 0]

    # Releasing the newer key returns to the key that is still held, with no 0 between.
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

    # Pressing the replaced key again makes it a candidate once more.
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

    # Overwriting the axis would leave the rapidfire task running.
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
    # A's On Press runs again when A takes over from D.
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

    # D never pressed its output, so its release must not center the axis.
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
    # Every held member was on the released interface, so nothing takes over.
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

    # The mouse button takes over the axis and hands it back without centering.
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

    # The mouse goes away while its button drives the axis.
    rig.mouse.release_tracked_outputs()
    assert rig.axis_writes() == [LEFT, RIGHT, 0]
    assert len(rig.owner.resettles) == int(restore)

    for state in rig.owner.resettles:
        await resettle_rollover_group(rig.rollover, state, deps=grabbed_event_processing_deps())

    # The key that is still held on the keyboard takes over with return to held keys.
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

    # Losing the session drops its groups with its devices.
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
    """Keys whose last write on ``uinput`` left them pressed."""
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

    # The combo recalled A and swallowed its release. D's release must not
    # hand the group back to A, which is no longer held.
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

    # The keyboard goes away while A is held but no longer drives the axis.
    rig.device.release_tracked_outputs()
    for state in rig.owner.resettles:
        await resettle_rollover_group(rig.rollover, state, deps=grabbed_event_processing_deps())

    assert rig.axis_writes()[-1] == RIGHT
    await rig.click_side(0)
    assert rig.axis_writes()[-1] == 0


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

    # A was held before the change, so releasing it leaves D in control.
    assert rig.axis_writes() == [LEFT, RIGHT]
    await rig.send((KEY_D, 0))
    assert rig.axis_writes() == [LEFT, RIGHT, 0]


async def combo_rollover_setup(
    monkeypatch,
    *,
    restore_trigger_keys: list[str] | None = None,
) -> SimpleNamespace:
    """Unmapped A and D in a group, and an A+C combo that recalls its triggers."""

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

    async def send(*events: tuple[int, int]) -> None:
        for code, value in events:
            await pipeline.process_event(
                device,
                SimpleNamespace(type=EV_KEY, code=code, value=value),
                deps=grabbed_event_processing_deps(),
            )
            for _ in range(3):
                await asyncio.sleep(0)

    return SimpleNamespace(passthrough=setup.passthrough, send=send)


@pytest.mark.asyncio
async def test_a_recalled_member_still_held_does_not_come_back_after_a_handover(
    monkeypatch,
) -> None:
    rig = await combo_rollover_setup(monkeypatch)
    key_c = evdev.ecodes.KEY_C

    # The combo recalls A while it stays physically held.
    await rig.send((KEY_A, 1), (key_c, 1), (key_c, 0), (KEY_D, 1), (KEY_D, 0))
    # D's release must not hand the group back to the recalled A: its
    # physical release is swallowed as part of the recall.
    assert held_keys(rig.passthrough) == set()
    await rig.send((KEY_A, 0))
    assert held_keys(rig.passthrough) == set()


@pytest.mark.asyncio
async def test_a_combo_restores_a_recalled_member_through_its_group(monkeypatch) -> None:
    rig = await combo_rollover_setup(monkeypatch, restore_trigger_keys=["key_a"])
    key_c = evdev.ecodes.KEY_C

    await rig.send((KEY_A, 1), (key_c, 1), (KEY_D, 1))
    assert held_keys(rig.passthrough) == {KEY_D}
    # The combo ends while D is the newest key, so restoring A leaves D alone.
    await rig.send((key_c, 0))
    assert held_keys(rig.passthrough) == {KEY_D}
    # A is held and restored, so it takes over once D is released.
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

    # D never drove the axis, so neither its repeat nor its release does.
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
    # A was held when the group changed, so its release leaves D in control.
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
    # Newest wins now, so the held D takes over.
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

    # Another group on the keyboard is not blocked by the mouse's action.
    await asyncio.wait_for(rig.send((KEY_A, 0)), timeout=1)
    assert rig.axis_writes() == [LEFT, 0]
    # A key in the busy group queues instead of stalling the keyboard.
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
        # D queues behind the busy group, then the group changes.
        await rig.send((KEY_D, 1))
        replace_rollover_groups(rig.owner, [group("key_a", "key_d")], resettle=rig.owner.resettle)
    for _ in range(5):
        await asyncio.sleep(0)

    # D reached the new newest-wins group, not the retired oldest-wins one.
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

    # The keyboard disconnects while its press waits behind the mouse.
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
        # D's event queues behind the busy group, which is then removed.
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

    # A was down before the group, and its release still leaves D in control.
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

    # A went down last, so it wins the newest-wins group and W lets go.
    assert held_keys(rig.keyboard) == {evdev.ecodes.KEY_LEFT}


@pytest.mark.asyncio
async def test_merging_groups_keeps_the_winner_on_a_shared_axis(monkeypatch) -> None:
    rig = Rig(
        monkeypatch,
        {"key_a": axis(LEFT), "key_w": axis(RIGHT)},
        [group("key_a", "key_d"), group("key_w", "key_s")],
    )
    await rig.send((KEY_W, 1), (KEY_A, 1))
    assert rig.axis_writes() == [RIGHT, LEFT]

    replace_rollover_groups(
        rig.owner,
        [group("key_a", "key_w", winner=RolloverWinner.PRIORITY)],
        resettle=rig.owner.resettle,
    )
    await rig.run_resettles()

    # A has priority. W gives up the axis without passing through rest.
    assert 0 not in rig.axis_writes()
    assert rig.axis_writes()[-1] == LEFT
    await rig.send((KEY_A, 0))
    assert rig.axis_writes()[-1] == RIGHT
    await rig.send((KEY_W, 0))
    assert rig.axis_writes()[-1] == 0
