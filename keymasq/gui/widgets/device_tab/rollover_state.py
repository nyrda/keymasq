"""GTK-free rules for rollover group editing in a device tab."""

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from keymasq.common.gamepad_axes import gamepad_axis_range, normalize_gamepad_axis_target
from keymasq.common.model.actions import MappingAction, is_protected_button
from keymasq.common.model.core import ActionType
from keymasq.common.model.hardware import ButtonDefinition
from keymasq.common.model.profiles import RolloverGroup, RolloverMember, RolloverWinner
from keymasq.common.rollover import (
    ROLLOVER_MIN_MEMBERS,
    action_output_id,
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
# Group colors in style.css, rollover-group-0 and up.
ROLLOVER_GROUP_COLORS = 4


def rollover_winner_label(winner: RolloverWinner) -> str:
    return next(label for choice, label, _ in ROLLOVER_WINNER_CHOICES if choice is winner)


def rollover_mode_summary(group: RolloverGroup) -> str:
    summary = rollover_winner_label(group.winner)
    return f"{summary}, returns to held keys" if group.restore else summary


def rollover_group_color_class(groups: list[RolloverGroup], group: RolloverGroup) -> str:
    """CSS class for a group's color, the same on every device tab.

    Colors follow the group's place in its profile and repeat after
    ROLLOVER_GROUP_COLORS groups.
    """
    index = next((i for i, candidate in enumerate(groups) if candidate is group), 0)
    return f"rollover-group-{index % ROLLOVER_GROUP_COLORS}"


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


def is_wheel_button(evdev: str) -> bool:
    """Wheel directions are relative events, and groups only arbitrate key presses."""
    return evdev.lower().startswith("rel_")


def selection_block_reason(
    member: RolloverMember,
    groups: Iterable[RolloverGroup],
    editing: RolloverGroup | None,
    *,
    evdev: str = "",
) -> str | None:
    """Explain why a cell can't join the group being built, or return None."""
    if is_protected_button(member.button):
        return "Critical pointer buttons can't be used in a rollover group."
    if is_wheel_button(evdev):
        return "Scroll wheel directions can't be used in a rollover group."
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
    buttons: Sequence[ButtonDefinition],
) -> list[str] | None:
    """Find ungrouped keys that drive one axis to different values.

    Without a group, releasing one of them returns the axis to rest even
    while another is held. ``grouped`` lists buttons that are already in a
    group. Only ``buttons`` that can join a group are considered, in order.
    """
    by_axis: dict[tuple[str, str], list[str]] = {}
    for button in buttons:
        button_id = button.id
        action = mappings.get(button_id)
        if (
            action is None
            or button_id in grouped
            or is_protected_button(button_id)
            or is_wheel_button(button.evdev)
            or action.action_type != ActionType.GAMEPAD_AXIS
            or not is_plain_output_action(action)
        ):
            continue
        key = (action_output_id(action), normalize_gamepad_axis_target(action.target) or "")
        by_axis.setdefault(key, []).append(button_id)
    for members in by_axis.values():
        values = {mappings[member].axis_value for member in members}
        if len(members) >= ROLLOVER_MIN_MEMBERS and len(values) > 1:
            return members
    return None
