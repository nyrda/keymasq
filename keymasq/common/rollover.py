"""Rollover group parsing, layering, and member helpers.

A rollover group lists source buttons, on one or more hardware devices. At most
one held member is active at a time, and only the active member's mapping
reaches the outputs. The GUI, session, and daemon share these helpers so they
agree on the stored shape.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import cast

from keymasq.common.coercion import coerce_bool
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.common.model.profiles import RolloverGroup, RolloverMember, RolloverWinner

ROLLOVER_MIN_MEMBERS = 2

_PLAIN_OUTPUT_ACTION_TYPES = frozenset(
    {
        ActionType.KEYBOARD,
        ActionType.MOUSE,
        ActionType.GAMEPAD,
        ActionType.GAMEPAD_AXIS,
    }
)


def parse_rollover_winner(value: object) -> RolloverWinner:
    token = str(value or "").strip().lower()
    for winner in RolloverWinner:
        if winner.value == token:
            return winner
    return RolloverWinner.NEWEST


def rollover_member_from_data(data: object) -> RolloverMember | None:
    if not isinstance(data, Mapping):
        return None
    values = cast(Mapping[str, object], data)
    hardware_id = str(values.get("hardware_id") or "").strip()
    button = str(values.get("button") or "").strip()
    if not hardware_id or not button:
        return None
    return RolloverMember(hardware_id=hardware_id, button=button)


def rollover_member_to_data(member: RolloverMember) -> dict[str, object]:
    return {"hardware_id": member.hardware_id, "button": member.button}


def normalize_rollover_members(value: object) -> list[RolloverMember]:
    """Return unique, valid members in their stored order."""
    if not isinstance(value, list | tuple):
        return []
    members: list[RolloverMember] = []
    for raw in cast(Iterable[object], value):
        member = rollover_member_from_data(raw)
        if member is not None and member not in members:
            members.append(member)
    return members


def rollover_group_from_data(data: object) -> RolloverGroup | None:
    """Parse one stored or IPC group. Groups need at least two members."""
    if not isinstance(data, Mapping):
        return None
    values = cast(Mapping[str, object], data)
    members = normalize_rollover_members(values.get("members"))
    if len(members) < ROLLOVER_MIN_MEMBERS:
        return None
    return RolloverGroup(
        name=str(values.get("name") or "").strip(),
        members=members,
        winner=parse_rollover_winner(values.get("winner")),
        restore=coerce_bool(values.get("restore"), True),
    )


def rollover_groups_from_data(value: object) -> list[RolloverGroup]:
    if not isinstance(value, list | tuple):
        return []
    groups: list[RolloverGroup] = []
    for item in cast(Iterable[object], value):
        group = rollover_group_from_data(item)
        if group is not None:
            groups.append(group)
    return groups


def rollover_group_to_data(group: RolloverGroup) -> dict[str, object]:
    return {
        "name": group.name,
        "members": [rollover_member_to_data(member) for member in group.members],
        "winner": group.winner.value,
        "restore": bool(group.restore),
    }


def layer_rollover_groups(groups: Iterable[RolloverGroup]) -> list[RolloverGroup]:
    """Apply groups in priority order, lowest first.

    A group replaces every earlier group that shares a member with it, so an
    input never belongs to two groups and members of different profiles are
    never merged into one group.
    """
    layered: list[RolloverGroup] = []
    for group in groups:
        members = set(group.members)
        if len(members) < ROLLOVER_MIN_MEMBERS:
            continue
        layered = [existing for existing in layered if members.isdisjoint(existing.members)]
        layered.append(
            RolloverGroup(
                name=group.name,
                members=list(group.members),
                winner=group.winner,
                restore=group.restore,
            )
        )
    return layered


def is_plain_output_action(action: MappingAction | None) -> bool:
    """Whether an action holds one key, button, or axis value for as long as it is pressed.

    Any action can be a group member. Plain members that write the same output
    hand over by overwriting it, so an axis moves straight from one value to the
    next. Every other member is released and pressed like a real key.
    """
    return (
        action is not None
        and action.action_type in _PLAIN_OUTPUT_ACTION_TYPES
        and not action.tap_enabled
        and not action.rapidfire_enabled
    )


def remove_rollover_member(
    groups: Iterable[RolloverGroup],
    member: RolloverMember,
) -> list[RolloverGroup]:
    """Drop one member everywhere, and drop groups left with too few members."""
    return _drop_rollover_members(groups, lambda candidate: candidate == member)


def remove_rollover_device(
    groups: Iterable[RolloverGroup],
    hardware_id: str,
) -> list[RolloverGroup]:
    """Drop every member of one device, and drop groups left with too few members."""
    return _drop_rollover_members(groups, lambda candidate: candidate.hardware_id == hardware_id)


def _drop_rollover_members(
    groups: Iterable[RolloverGroup],
    drop: Callable[[RolloverMember], bool],
) -> list[RolloverGroup]:
    remaining: list[RolloverGroup] = []
    for group in groups:
        members = [candidate for candidate in group.members if not drop(candidate)]
        if len(members) < ROLLOVER_MIN_MEMBERS:
            continue
        remaining.append(
            RolloverGroup(
                name=group.name,
                members=members,
                winner=group.winner,
                restore=group.restore,
            )
        )
    return remaining


def rollover_group_for_member(
    groups: Iterable[RolloverGroup],
    member: RolloverMember,
) -> RolloverGroup | None:
    for group in groups:
        if member in group.members:
            return group
    return None


def default_rollover_group_name(
    members: Iterable[RolloverMember],
    labels: Mapping[RolloverMember, str] | None = None,
) -> str:
    """Name a group after its members, for example "A / D"."""
    names = [(labels or {}).get(member) or member.button for member in members]
    return " / ".join(names)
