import pytest

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.common.model.profiles import RolloverGroup, RolloverMember, RolloverWinner
from keymasq.common.rollover import (
    default_rollover_group_name,
    is_plain_output_action,
    layer_rollover_groups,
    normalize_rollover_members,
    parse_rollover_winner,
    remove_rollover_device,
    remove_rollover_member,
    rollover_group_for_member,
    rollover_group_from_data,
    rollover_group_to_data,
)

KEYBOARD = "1234:5678"
MOUSE = "046d:c52b"


def kb(button: str) -> RolloverMember:
    return RolloverMember(hardware_id=KEYBOARD, button=button)


def data(button: str, hardware_id: str = KEYBOARD) -> dict[str, str]:
    return {"hardware_id": hardware_id, "button": button}


def test_group_data_round_trips_and_defaults() -> None:
    group = RolloverGroup(
        name="Walk",
        members=[kb("key_w"), RolloverMember(hardware_id=MOUSE, button="btn_side")],
        winner=RolloverWinner.NEUTRAL,
        restore=False,
    )

    assert rollover_group_to_data(group)["members"] == [
        data("key_w"),
        data("btn_side", MOUSE),
    ]
    assert rollover_group_from_data(rollover_group_to_data(group)) == group
    assert rollover_group_from_data({"members": [data("key_a"), data("key_d")]}) == RolloverGroup(
        name="",
        members=[kb("key_a"), kb("key_d")],
        winner=RolloverWinner.NEWEST,
        restore=True,
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        "key_a",
        {"members": [data("key_a")]},
        {"members": [data("key_a"), data(" key_a "), data("")]},
        {"members": ["key_a", "key_d"]},
        {"members": 3},
    ],
)
def test_groups_need_two_distinct_members(value: object) -> None:
    assert rollover_group_from_data(value) is None


def test_member_normalization_and_winner_parsing() -> None:
    members = normalize_rollover_members(
        [data(" key_a"), data("key_d"), data("key_a"), data("key_a", MOUSE), {"button": "x"}, None]
    )
    assert members == [kb("key_a"), kb("key_d"), RolloverMember(hardware_id=MOUSE, button="key_a")]
    assert parse_rollover_winner("OLDEST") is RolloverWinner.OLDEST
    assert parse_rollover_winner("unknown") is RolloverWinner.NEWEST


def test_layering_replaces_overlapping_groups_in_order() -> None:
    lower = RolloverGroup(name="lower", members=[kb("a"), kb("d")])
    unrelated = RolloverGroup(name="unrelated", members=[kb("w"), kb("s")])
    upper = RolloverGroup(name="upper", members=[kb("d"), kb("right")])
    other_device = RolloverGroup(
        name="other device",
        members=[RolloverMember(MOUSE, "a"), RolloverMember(MOUSE, "d")],
    )

    layered = layer_rollover_groups([lower, unrelated, upper, other_device])

    assert [group.name for group in layered] == ["unrelated", "upper", "other device"]
    assert layered[1] is not upper


def test_removing_members_drops_groups_that_become_too_small() -> None:
    side = RolloverMember(hardware_id=MOUSE, button="btn_side")
    groups = [
        RolloverGroup(name="pair", members=[kb("a"), side]),
        RolloverGroup(name="triple", members=[kb("a"), kb("w"), kb("s")]),
    ]

    assert remove_rollover_member(groups, kb("a")) == [
        RolloverGroup(name="triple", members=[kb("w"), kb("s")])
    ]
    assert remove_rollover_device(groups, MOUSE) == [groups[1]]
    assert rollover_group_for_member(groups, side) is groups[0]
    assert rollover_group_for_member(groups, RolloverMember(MOUSE, "a")) is None


def test_default_name_uses_labels() -> None:
    assert default_rollover_group_name([kb("key_a"), kb("key_d")], {kb("key_a"): "A"}) == (
        "A / key_d"
    )


@pytest.mark.parametrize(
    ("action", "plain"),
    [
        (None, False),
        (MappingAction(action_type=ActionType.PASSTHROUGH), False),
        (MappingAction(action_type=ActionType.KEYBOARD, target="key_left"), True),
        (MappingAction(action_type=ActionType.MOUSE, target="btn_left"), True),
        (MappingAction(action_type=ActionType.GAMEPAD, target="btn_south"), True),
        (MappingAction(action_type=ActionType.GAMEPAD_AXIS, target="abs_x", axis_value=1), True),
        (MappingAction(action_type=ActionType.KEYBOARD, target="key_a", tap_enabled=True), False),
        (
            MappingAction(action_type=ActionType.KEYBOARD, target="key_a", rapidfire_enabled=True),
            False,
        ),
        (MappingAction(action_type=ActionType.MACRO, macro_name="combo"), False),
        (MappingAction(action_type=ActionType.SUPERKEY, superkey_name="dash"), False),
    ],
)
def test_plain_output_actions(action: MappingAction | None, plain: bool) -> None:
    assert is_plain_output_action(action) is plain
