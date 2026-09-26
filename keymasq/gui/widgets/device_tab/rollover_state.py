"""GTK-free rules for rollover group editing in a device tab."""

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from keymasq.common.gamepad_axes import gamepad_axis_range, normalize_gamepad_axis_target
from keymasq.common.model.actions import MappingAction, is_protected_button
from keymasq.common.model.core import ActionType
from keymasq.common.model.profiles import RolloverGroup, RolloverMember, RolloverWinner
from keymasq.common.rollover import (
    ROLLOVER_MIN_MEMBERS,
    is_plain_output_action,
    rollover_group_for_member,
)

ROLLOVER_WINNER_CHOICES: tuple[tuple[RolloverWinner, str, str], ...] = (
    (
        RolloverWinner.NEWEST,
        "Last pressed wins",
        "The most recently pressed key is active.",
    ),
    (
        RolloverWinner.OLDEST,
        "First pressed wins",
        "Later keys wait until the first key is released.",
    ),
    (
        RolloverWinner.NEUTRAL,
        "Cancel out",
        "Holding two or more keys makes none of them active.",
    ),
    (
        RolloverWinner.PRIORITY,
        "Fixed priority",
        "The held key highest in the member list is active.",
    ),
)

ROLLOVER_RESTORE_TITLE = "Return to held keys"
ROLLOVER_RESTORE_SUBTITLE = (
    "When the active key is released, a key that is still held takes over."
)
ROLLOVER_MEMBER_PREFIX = "↹"


def rollover_winner_label(winner: RolloverWinner) -> str:
    return next(label for choice, label, _ in ROLLOVER_WINNER_CHOICES if choice is winner)


def rollover_mode_summary(group: RolloverGroup) -> str:
    summary = rollover_winner_label(group.winner)
    return f"{summary}, returns to held keys" if group.restore else summary


def rollover_member_caption(description: str) -> str:
    """Card caption for a member.

    The rollover mark replaces the action's leading icon, and the group name
    goes in the tooltip because cards are narrow.
    """
    head, separator, rest = description.partition(" ")
    if separator and head and not any(char.isalnum() for char in head):
        description = rest
    return f"{ROLLOVER_MEMBER_PREFIX} {description}"


def rollover_member_tooltip(group: RolloverGroup, description: str) -> str:
    return (
        f"{description}\nRollover group {group.name}: {rollover_mode_summary(group)}. "
        "Click to edit the group."
    )


def axis_display_name(target: str | None) -> str:
    axis = gamepad_axis_range(target)
    if axis is not None:
        return axis.label
    return str(target or "an axis").upper()


@dataclass
class RolloverSelection:
    """Keys picked in rollover selection mode.

    The selection belongs to the window, so it stays active while the user
    switches device tabs to add keys from other devices.
    """

    profile_name: str
    selected: list[RolloverMember] = field(default_factory=list)
    # A member of the group being edited, or None while creating a group.
    editing_member: RolloverMember | None = None

    def toggle(self, member: RolloverMember) -> None:
        if member in self.selected:
            self.selected.remove(member)
        else:
            self.selected.append(member)

    @property
    def can_finish(self) -> bool:
        return len(self.selected) >= ROLLOVER_MIN_MEMBERS


def selection_block_reason(
    member: RolloverMember,
    groups: Iterable[RolloverGroup],
    editing: RolloverGroup | None,
) -> str | None:
    """Explain why a cell can't join the group being built, or return None."""
    if is_protected_button(member.button):
        return "Critical pointer buttons can't be used in a rollover group."
    owner = rollover_group_for_member(groups, member)
    if owner is not None and owner != editing:
        return f"Already in the rollover group {owner.name}."
    return None


def selection_summary(
    devices: Sequence[tuple[str, Sequence[str]]],
    current_device: str,
) -> str:
    """Describe the selected keys, grouped by device in selection order.

    Device names are left out only when every key is on ``current_device``.
    """
    if not devices:
        return "Click keys to add them. Switch device tabs to add keys from other devices."
    if len(devices) == 1 and devices[0][0] == current_device:
        return f"Selected: {', '.join(devices[0][1])}"
    parts = [f"{', '.join(labels)} on {device}" for device, labels in devices]
    return f"Selected: {'; '.join(parts)}"


def suggest_axis_rollover_members(
    mappings: Mapping[str, MappingAction],
    grouped: Collection[str],
    button_order: Sequence[str],
) -> list[str] | None:
    """Find ungrouped keys that drive one axis to different values.

    Without a group, releasing one of them returns the axis to rest even
    while another is held. ``grouped`` lists buttons that are already in a
    group.
    """
    by_axis: dict[tuple[str, str], list[str]] = {}
    order = {button_id: index for index, button_id in enumerate(button_order)}
    for button_id in sorted(mappings, key=lambda item: (order.get(item, len(order)), item)):
        action = mappings[button_id]
        if (
            button_id in grouped
            or action.action_type != ActionType.GAMEPAD_AXIS
            or not is_plain_output_action(action)
        ):
            continue
        key = (action.output_id or "", normalize_gamepad_axis_target(action.target) or "")
        by_axis.setdefault(key, []).append(button_id)
    for members in by_axis.values():
        values = {mappings[member].axis_value for member in members}
        if len(members) >= ROLLOVER_MIN_MEMBERS and len(values) > 1:
            return members
    return None
