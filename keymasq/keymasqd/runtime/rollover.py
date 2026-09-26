"""Rollover group state shared by every grabbed interface of every device.

A group tracks which members are physically held, in press order, and which
member is active. Members can be on different devices. Only the active member's
mapping reaches the outputs. The pipeline stage in
``grabbed_device.event.rollover`` feeds member events in and performs the
handovers this module decides on.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast

from keymasq.common.combos import normalize_combo_evdev
from keymasq.common.gamepad_axes import normalize_gamepad_axis_target
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.common.model.profiles import RolloverGroup, RolloverMember, RolloverWinner
from keymasq.common.rollover import is_plain_output_action
from keymasq.keymasqd.output_helpers import resolve_output_code

if TYPE_CHECKING:
    from keymasq.keymasqd.runtime.grabbed_device.types import GrabbedDeviceRuntime

log = logging.getLogger("keymasqd.rollover")

type RolloverOutput = tuple[object, ...]
# A held key or axis a runtime releases when it goes away: kind, bucket, code.
type TrackedOutput = tuple[str, str, int]
type RolloverGetterFn = Callable[[], "RolloverRuntime | None"]
type RolloverResettle = Callable[["RolloverRuntime", "RolloverGroupState"], None]

_press_sequence = itertools.count(1)


def next_press_sequence() -> int:
    """Order physical key presses across every device."""
    return next(_press_sequence)


def rollover_output(
    action: MappingAction | None,
    *,
    event_code: int,
    runtime_path: str,
) -> RolloverOutput | None:
    """Identify the one output a plain member action writes.

    Two members with the same output hand over without a release in between,
    so an axis moves straight from one value to the next. Other actions return
    None, so their handover is a full release and press.
    """
    if action is None or action.action_type == ActionType.PASSTHROUGH:
        return ("passthrough", runtime_path, int(event_code))
    if not is_plain_output_action(action):
        return None
    if action.action_type == ActionType.GAMEPAD_AXIS:
        return (
            "axis",
            action.output_id or "",
            normalize_gamepad_axis_target(action.target or ""),
        )
    return (
        action.action_type.value,
        action.output_id or "",
        str(action.target or "").lower(),
        tuple(action.keys or ()),
    )


def choose_active(
    group: RolloverGroup,
    held: Sequence[RolloverMember],
    lost: set[RolloverMember] | frozenset[RolloverMember],
) -> RolloverMember | None:
    """Pick the active member from members held in press order."""
    if group.winner is RolloverWinner.NEUTRAL:
        if len(held) != 1:
            return None
        only = held[0]
        return only if group.restore or only not in lost else None
    candidates = list(held) if group.restore else [member for member in held if member not in lost]
    if not candidates:
        return None
    if group.winner is RolloverWinner.OLDEST:
        return candidates[0]
    if group.winner is RolloverWinner.PRIORITY:
        order = {member: index for index, member in enumerate(group.members)}
        return min(candidates, key=lambda member: order.get(member, len(order)))
    return candidates[-1]


@dataclass(eq=False)
class HeldMember:
    member: RolloverMember
    runtime: GrabbedDeviceRuntime
    event_code: int
    event_name: str
    # When the key physically went down, see next_press_sequence(). Handovers
    # and restores leave it alone, so merged groups keep the real order.
    press_seq: int = 0
    output: RolloverOutput | None = None
    # The group pressed this member's mapping and has not released it yet.
    pressed: bool = False
    # Outputs this member's presses added to its runtime's tracking. A handover
    # to a member on another device moves exactly these.
    tracked: set[TrackedOutput] = field(default_factory=set)
    # A combo recalled this member's output. It stays out of the running
    # until the combo restores it or the key is released.
    recalled: bool = False


@dataclass(eq=False)
class RolloverGroupState:
    group: RolloverGroup
    held: list[HeldMember] = field(default_factory=list)
    lost: set[RolloverMember] = field(default_factory=set)
    active: HeldMember | None = None
    # Serializes this group's handovers. Other groups have their own lock, so
    # a slow member action never holds up unrelated groups.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Events holding or waiting for the lock. While this is nonzero, new events
    # queue behind them instead of blocking their device's event loop.
    pending: int = 0

    def member(self, member: RolloverMember) -> HeldMember | None:
        return next((held for held in self.held if held.member == member), None)

    def next_active(self) -> HeldMember | None:
        chosen = choose_active(
            self.group,
            [held.member for held in self.held if not held.recalled],
            self.lost,
        )
        return self.member(chosen) if chosen is not None else None

    def mark_losers(self, winner: HeldMember | None) -> None:
        """Without restore, a held member that loses stays out until pressed again."""
        if self.group.restore:
            return
        for held in self.held:
            if held is not winner:
                self.lost.add(held.member)

    def retire(
        self, carried: set[RolloverMember] | frozenset[RolloverMember] = frozenset()
    ) -> None:
        """Stop tracking the group without emitting anything.

        Members in ``carried`` moved to a replacement group, which takes over
        their state. A pressed member keeps its held source action, so its
        physical release still reaches its mapping. Other held members never
        pressed an output, so their repeats and release are swallowed.
        """
        for held in self.held:
            if held.member not in carried and not held.pressed:
                held.runtime.state.rollover_quarantined.add(held.event_name)
        self.held.clear()
        self.lost.clear()
        self.active = None

    def forget_runtime(self, runtime: GrabbedDeviceRuntime) -> bool:
        """Drop members of a runtime whose outputs were released elsewhere.

        The runtime's keys count as released. Returns True when another held
        member should become active, for example a member on a device that is
        still connected.
        """
        removed = [held for held in self.held if held.runtime is runtime]
        if not removed:
            return False
        self.held = [held for held in self.held if held.runtime is not runtime]
        self.lost -= {held.member for held in removed}
        if self.active is not None and self.active.runtime is runtime:
            self.active = None
        return self.next_active() is not self.active


class RolloverRuntime:
    """Active rollover groups and their live state, across all devices."""

    def __init__(
        self,
        groups: Sequence[RolloverGroup],
        *,
        resettle: RolloverResettle | None = None,
        previous: RolloverRuntime | None = None,
    ) -> None:
        """Build the runtime, carrying live state over from ``previous``.

        A group whose members and settings did not change keeps its state
        object, including its lock. A changed group starts from the held
        members of the old groups it shares members with, so keys that are
        down stay accounted for. adopt_held_keys() adds member keys that were
        down outside any group.
        """
        self.groups = list(groups)
        old_states = list(previous.states) if previous is not None else []
        self.states: list[RolloverGroupState] = []
        fresh: list[RolloverGroupState] = []
        for group in self.groups:
            state = next(
                (candidate for candidate in old_states if _same_behavior(candidate.group, group)),
                None,
            )
            if state is None:
                state = RolloverGroupState(group)
                fresh.append(state)
            else:
                old_states.remove(state)
                state.group = group
            self.states.append(state)
        # Groups that were not kept as they are.
        self.replaced = old_states
        self.carried: set[RolloverMember] = set()
        for state in fresh:
            _carry_held_members(state, old_states)
            self.carried.update(held.member for held in state.held)
        self._fresh = fresh
        self._resettle = resettle
        self._by_member: dict[RolloverMember, RolloverGroupState] = {}
        for state in self.states:
            for member in state.group.members:
                self._by_member.setdefault(member, state)

    def group_for(self, member: RolloverMember) -> RolloverGroupState | None:
        return self._by_member.get(member)

    def adopt_held_keys(self, runtimes: Iterable[GrabbedDeviceRuntime]) -> list[RolloverGroupState]:
        """Take over member keys that are down in new or changed groups.

        A key pressed before its group existed went through normal dispatch
        and still holds its action. Adopting it lets the group arbitrate it,
        so its release cannot center an axis another member now drives.
        Returns the groups with held keys, which need a settle.
        """
        runtimes = list(runtimes)
        for state in self._fresh:
            for member in state.group.members:
                if state.member(member) is None:
                    held = _held_input(member, runtimes)
                    if held is not None:
                        state.held.append(held)
            state.held.sort(key=lambda held: held.press_seq)
        return [state for state in self._fresh if state.held]

    def request_resettle(self, state: RolloverGroupState) -> None:
        if self._resettle is not None:
            self._resettle(self, state)

    def retire(self) -> None:
        for state in self.states:
            state.retire()

    def forget_runtime(self, runtime: GrabbedDeviceRuntime) -> None:
        for state in self.states:
            if state.forget_runtime(runtime):
                self.request_resettle(state)


def _same_behavior(old: RolloverGroup, new: RolloverGroup) -> bool:
    """Whether live state carries over. A rename alone keeps it."""
    return old.members == new.members and old.winner is new.winner and old.restore == new.restore


def _carry_held_members(
    state: RolloverGroupState,
    old_states: Sequence[RolloverGroupState],
) -> None:
    """Move held members of replaced groups into ``state``, in press order."""
    members = set(state.group.members)
    for old in old_states:
        for held in old.held:
            if held.member not in members or state.member(held.member) is not None:
                continue
            state.held.append(held)
            if held.member in old.lost:
                state.lost.add(held.member)
            # Only one member can stay active. Another pressed member is
            # dealt with by the next settle.
            if held is old.active and state.active is None:
                state.active = held
    state.held.sort(key=lambda held: held.press_seq)


def _held_input(
    member: RolloverMember,
    runtimes: Sequence[GrabbedDeviceRuntime],
) -> HeldMember | None:
    """A member key that is down outside any group, as a pressed member."""
    for runtime in runtimes:
        if runtime.hardware_id != member.hardware_id:
            continue
        state = runtime.state
        for event_name in state.held_source_keys:
            if runtime.evdev_to_button.get(event_name.lower()) != member.button:
                continue
            if event_name in state.rollover_quarantined:
                # Its group held it back, so it drives nothing.
                return None
            code = resolve_output_code(event_name)
            if code is None:
                return None
            recalled = normalize_combo_evdev(event_name) in state.combo_recalled_bindings
            # Down on its own mapping or passed through. A key that a pending
            # combo holds back drives nothing yet.
            drives_output = event_name in state.held_source_actions or int(
                code
            ) in state.held_output_keys.get("passthrough", set())
            return HeldMember(
                member=member,
                runtime=runtime,
                event_code=int(code),
                event_name=event_name,
                press_seq=state.held_source_press_order.get(event_name, 0),
                output=rollover_output(
                    state.held_source_actions.get(event_name),
                    event_code=int(code),
                    runtime_path=runtime.path,
                ),
                pressed=drives_output and not recalled,
                recalled=recalled,
            )
    return None


class RolloverOwner(Protocol):
    rollover: RolloverRuntime | None


def replace_rollover_groups(
    owner: RolloverOwner,
    groups: Sequence[RolloverGroup],
    *,
    resettle: RolloverResettle | None = None,
    runtimes: Iterable[GrabbedDeviceRuntime] = (),
) -> None:
    """Install the active groups, keeping live state when nothing changed.

    ``runtimes`` are the grabbed devices, whose held member keys new and
    changed groups adopt. Callers hold the lock of every current group, so no
    handover is in flight.
    """
    previous = owner.rollover
    if previous is not None and previous.groups == list(groups):
        return
    rollover = RolloverRuntime(groups, resettle=resettle, previous=previous) if groups else None
    if previous is not None:
        if rollover is None:
            previous.retire()
        else:
            for state in rollover.replaced:
                state.retire(rollover.carried)
    owner.rollover = rollover
    if rollover is not None:
        for state in rollover.adopt_held_keys(runtimes):
            rollover.request_resettle(state)
    log.info("Rollover groups: %d", len(groups))


def forget_runtime_rollover(runtime: object) -> None:
    """Clear rollover tracking for a runtime whose held outputs were released."""
    state = getattr(runtime, "state", None)
    quarantined = cast(set[str] | None, getattr(state, "rollover_quarantined", None))
    if quarantined is not None:
        quarantined.clear()
    if state is not None and hasattr(state, "rollover_epoch"):
        state.rollover_epoch += 1
    getter = cast(RolloverGetterFn | None, getattr(runtime, "rollover_getter", None))
    rollover = getter() if getter is not None else None
    if rollover is not None:
        rollover.forget_runtime(cast("GrabbedDeviceRuntime", runtime))
