"""Rollover group parsing, layering, and member helpers.

A rollover group lists source buttons, on one or more hardware devices. At most
one held member is active at a time, and only the active member's mapping
reaches the outputs. The GUI, session, and daemon share these helpers so they
agree on the stored shape.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import cast

from keymasq.common.coercion import coerce_bool
from keymasq.common.model.actions import MappingAction, normalize_output_id
from keymasq.common.model.core import ActionType
from keymasq.common.model.profiles import RolloverGroup, RolloverMember, RolloverWinner
from keymasq.common.virtual_devices import virtual_gamepad_output_id

log = logging.getLogger(__name__)

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
    if token:
        log.warning("Unknown rollover winner %r, using %r", token, RolloverWinner.NEWEST.value)
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


def rollover_groups_from_toml(value: object) -> list[RolloverGroup]:
    """Parse a profile file's groups, rejecting any group that would otherwise be dropped."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("rollover_groups must be an array of tables")
    groups: list[RolloverGroup] = []
    owners: dict[RolloverMember, str] = {}
    for index, item in enumerate(cast(list[object], value), start=1):
        if not isinstance(item, Mapping):
            raise ValueError(f"rollover group {index} must be a table")
        values = cast(Mapping[str, object], item)
        name = values.get("name", "")
        if not isinstance(name, str):
            raise ValueError(f"rollover group {index} name must be a string")
        label = f"rollover group {name.strip()!r}" if name.strip() else f"rollover group {index}"
        winner = values.get("winner")
        winners = [choice.value for choice in RolloverWinner]
        if winner is not None and (
            not isinstance(winner, str) or winner.strip().lower() not in winners
        ):
            raise ValueError(
                f"{label} winner must be one of {', '.join(map(repr, winners))}, got {winner!r}"
            )
        restore = values.get("restore")
        if restore is not None and not isinstance(restore, bool):
            raise ValueError(f"{label} restore must be true or false, got {restore!r}")
        raw_members = values.get("members")
        if not isinstance(raw_members, list):
            raise ValueError(f"{label} needs a members array")
        members: list[RolloverMember] = []
        for position, raw_member in enumerate(cast(list[object], raw_members), start=1):
            member = rollover_member_from_data(raw_member)
            if member is None:
                raise ValueError(f"{label} member {position} needs a hardware_id and a button")
            if member in members:
                raise ValueError(f"{label} lists {member.hardware_id} {member.button} twice")
            owner = owners.get(member)
            if owner is not None:
                raise ValueError(
                    f"{member.hardware_id} {member.button} is in both {owner} and {label}"
                )
            owners[member] = label
            members.append(member)
        if len(members) < ROLLOVER_MIN_MEMBERS:
            raise ValueError(f"{label} needs at least {ROLLOVER_MIN_MEMBERS} members")
        groups.append(
            RolloverGroup(
                name=name.strip(),
                members=members,
                winner=parse_rollover_winner(winner),
                restore=True if restore is None else restore,
            )
        )
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


# Actions that fire once on press and hold nothing. A member runs them once per
# physical press, not again whenever it takes over.
ONE_SHOT_ACTION_TYPES = frozenset(
    {
        ActionType.EXEC,
        ActionType.COMPOSITOR_DISPATCH,
        ActionType.START_MACRO_RECORDING,
        ActionType.STOP_MACRO_RECORDING,
        ActionType.PLAY_MACRO_SLOT,
        ActionType.CANCEL_MACRO_PLAYBACK,
        ActionType.EMERGENCY_RESET,
        ActionType.PROFILE_ENABLE,
        ActionType.PROFILE_DISABLE,
        ActionType.PROFILE_TOGGLE,
        ActionType.MPRIS,
    }
)


def action_output_id(action: MappingAction) -> str:
    """The output an action writes to, with the default spelled out.

    Gamepad actions without an output ID go to the first virtual gamepad, so
    both spellings name the same output.
    """
    output_id = normalize_output_id(action.output_id)
    if output_id is None and action.action_type in (ActionType.GAMEPAD, ActionType.GAMEPAD_AXIS):
        return virtual_gamepad_output_id(1)
    return output_id or ""


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
