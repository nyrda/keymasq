"""Step-gap calculations for the macro editor timeline."""

from dataclasses import dataclass
from typing import Literal

import evdev

from keymasq.gui.widgets.macro_editor.document import selection_order
from keymasq.gui.widgets.macro_editor.model import (
    EditableControl,
    EditableEvent,
    EditableMove,
    MacroEvent,
    _passthrough_track,
)

type GapItem = EditableEvent | EditableMove | EditableControl | MacroEvent
type GapScope = Literal["timeline", "track"]


@dataclass(frozen=True, slots=True)
class TimelineGap:
    """Timing between one action step and the step that follows it."""

    previous_step_start_us: int
    previous_end_us: int
    next_start_us: int
    previous_items: tuple[GapItem, ...]
    next_items: tuple[GapItem, ...]
    scope: GapScope = "timeline"
    track: str | None = None

    @property
    def duration_us(self) -> int:
        return self.next_start_us - self.previous_end_us

    @property
    def minimum_us(self) -> int:
        """Keep the next step at or after the preceding step's start."""
        return self.previous_step_start_us - self.previous_end_us


@dataclass(frozen=True, slots=True)
class _TimelineStep:
    start_us: int
    end_us: int
    items: tuple[GapItem, ...]


def build_timeline_gaps(
    events: list[EditableEvent],
    moves: list[EditableMove],
    controls: list[EditableControl],
) -> list[TimelineGap]:
    """Return ruler gaps between consecutive editable action groups."""
    steps = _build_steps_from_items([*events, *moves, *controls])
    if len(steps) < 2:
        return []

    gaps: list[TimelineGap] = []
    previous_end_us = steps[0].end_us
    frontier_items = steps[0].items
    for index, step in enumerate(steps[1:], start=1):
        previous_step = steps[index - 1]
        gaps.append(
            TimelineGap(
                previous_step_start_us=previous_step.start_us,
                previous_end_us=previous_end_us,
                next_start_us=step.start_us,
                previous_items=frontier_items,
                next_items=step.items,
            )
        )
        if step.end_us >= previous_end_us:
            previous_end_us = step.end_us
            frontier_items = step.items
    return gaps


def build_track_gaps(
    items: list[GapItem],
    *,
    track: str,
) -> list[TimelineGap]:
    """Return gaps between chronological action steps in one semantic track."""
    steps = _build_steps_from_items(items)
    gaps: list[TimelineGap] = []
    for index, step in enumerate(steps[1:], start=1):
        previous = steps[index - 1]
        gaps.append(
            TimelineGap(
                previous_step_start_us=previous.start_us,
                previous_end_us=previous.end_us,
                next_start_us=step.start_us,
                previous_items=previous.items,
                next_items=step.items,
                scope="track",
                track=track,
            )
        )
    return gaps


def set_timeline_gap_next_action(
    events: list[EditableEvent],
    rel_events: list[MacroEvent],
    passthrough_events: list[MacroEvent],
    moves: list[EditableMove],
    controls: list[EditableControl],
    gap: TimelineGap,
    target_us: int,
) -> int:
    """Set one gap by moving only the action step after it."""
    normalized_target_us = max(gap.minimum_us, int(target_us))
    delta_us = normalized_target_us - gap.duration_us
    if delta_us == 0:
        return 0

    repeat_owners = _repeat_owners(events, passthrough_events)
    _shift_items(
        events,
        rel_events,
        passthrough_events,
        moves,
        controls,
        items=gap.next_items,
        delta_us=delta_us,
        repeat_owners=repeat_owners,
    )
    return delta_us


def set_timeline_gap_and_following(
    events: list[EditableEvent],
    rel_events: list[MacroEvent],
    passthrough_events: list[MacroEvent],
    moves: list[EditableMove],
    controls: list[EditableControl],
    gap: TimelineGap,
    target_us: int,
) -> int:
    """Set one gap and shift every action from the following step onward."""
    normalized_target_us = max(gap.minimum_us, int(target_us))
    delta_us = normalized_target_us - gap.duration_us
    if delta_us == 0:
        return 0

    repeat_owners = _repeat_owners(events, passthrough_events)
    _shift_suffix(
        events,
        rel_events,
        passthrough_events,
        moves,
        controls,
        at_us=gap.next_start_us,
        delta_us=delta_us,
        repeat_owners=repeat_owners,
    )
    return delta_us


def set_timeline_gap_track_following(
    events: list[EditableEvent],
    rel_events: list[MacroEvent],
    passthrough_events: list[MacroEvent],
    moves: list[EditableMove],
    controls: list[EditableControl],
    gap: TimelineGap,
    target_us: int,
) -> int:
    """Set one track gap and move later actions from that semantic track."""
    normalized_target_us = max(gap.minimum_us, int(target_us))
    delta_us = normalized_target_us - gap.duration_us
    if delta_us == 0:
        return 0

    repeat_owners = _repeat_owners(events, passthrough_events)
    following_items = _track_items_at_or_after(
        events,
        rel_events,
        passthrough_events,
        moves,
        controls,
        repeat_owners=repeat_owners,
        track=gap.track,
        at_us=gap.next_start_us,
    )
    _shift_items(
        events,
        rel_events,
        passthrough_events,
        moves,
        controls,
        items=following_items,
        delta_us=delta_us,
        repeat_owners=repeat_owners,
    )
    return delta_us


def _track_items_at_or_after(
    events: list[EditableEvent],
    rel_events: list[MacroEvent],
    passthrough_events: list[MacroEvent],
    moves: list[EditableMove],
    controls: list[EditableControl],
    *,
    repeat_owners: dict[int, EditableEvent],
    track: str | None,
    at_us: int,
) -> tuple[GapItem, ...]:
    if track in {"keyboard", "mouse", "gamepad"}:
        items: list[GapItem] = [event for event in events if event.device_type == track]
    elif track == "movement":
        items = [*moves, *rel_events]
    elif track == "control":
        items = list(controls)
    else:
        return ()
    if track != "control":
        items.extend(
            event
            for event in passthrough_events
            if id(event) not in repeat_owners and _passthrough_track(event) == track
        )
    return tuple(item for item in items if _item_start_us(item) >= at_us)


def _build_steps_from_items(items: list[GapItem]) -> list[_TimelineStep]:
    items.sort(key=lambda item: (_item_start_us(item), selection_order(item)))

    groups: list[list[GapItem]] = []
    for item in items:
        if not groups or _item_start_us(groups[-1][0]) != _item_start_us(item):
            groups.append([item])
        else:
            groups[-1].append(item)
    return [
        _TimelineStep(
            start_us=_item_start_us(group[0]),
            end_us=max(_item_end_us(item) for item in group),
            items=tuple(group),
        )
        for group in groups
    ]


def _item_start_us(item: GapItem) -> int:
    if isinstance(item, EditableEvent):
        return max(0, int(item.press_t_us))
    if isinstance(item, (EditableMove, EditableControl)):
        return max(0, int(item.t_us))
    return max(0, int(item.get("t_us", 0)))


def _item_end_us(item: GapItem) -> int:
    if isinstance(item, EditableEvent):
        return max(item.press_t_us, int(item.release_t_us))
    return _item_start_us(item)


def _shift_items(
    events: list[EditableEvent],
    rel_events: list[MacroEvent],
    passthrough_events: list[MacroEvent],
    moves: list[EditableMove],
    controls: list[EditableControl],
    *,
    items: tuple[GapItem, ...],
    delta_us: int,
    repeat_owners: dict[int, EditableEvent],
) -> None:
    item_ids = {id(item) for item in items}
    shifted_owner_ids: set[int] = set()
    for event in events:
        if id(event) not in item_ids:
            continue
        shifted_owner_ids.add(id(event))
        event.press_t_us += delta_us
        event.release_t_us += delta_us
    for event in rel_events:
        if id(event) in item_ids:
            event["t_us"] = int(event.get("t_us", 0)) + delta_us
    for event in passthrough_events:
        owner = repeat_owners.get(id(event))
        if id(event) in item_ids or (owner is not None and id(owner) in shifted_owner_ids):
            event["t_us"] = int(event.get("t_us", 0)) + delta_us
    for move in moves:
        if id(move) in item_ids:
            move.t_us += delta_us
    for control in controls:
        if id(control) in item_ids:
            control.t_us += delta_us
    _sort_items(events, rel_events, passthrough_events, moves, controls)


def _shift_suffix(
    events: list[EditableEvent],
    rel_events: list[MacroEvent],
    passthrough_events: list[MacroEvent],
    moves: list[EditableMove],
    controls: list[EditableControl],
    *,
    at_us: int,
    delta_us: int,
    repeat_owners: dict[int, EditableEvent],
) -> None:
    shifted_owner_ids: set[int] = set()
    for event in events:
        if event.press_t_us < at_us:
            continue
        shifted_owner_ids.add(id(event))
        event.press_t_us += delta_us
        event.release_t_us += delta_us
    for event in rel_events:
        timestamp_us = int(event.get("t_us", 0))
        if timestamp_us >= at_us:
            event["t_us"] = timestamp_us + delta_us
    for event in passthrough_events:
        timestamp_us = int(event.get("t_us", 0))
        owner = repeat_owners.get(id(event))
        should_shift = (
            id(owner) in shifted_owner_ids if owner is not None else timestamp_us >= at_us
        )
        if should_shift:
            event["t_us"] = timestamp_us + delta_us
    for move in moves:
        if move.t_us >= at_us:
            move.t_us += delta_us
    for control in controls:
        if control.t_us >= at_us:
            control.t_us += delta_us
    _sort_items(events, rel_events, passthrough_events, moves, controls)


def _repeat_owners(
    events: list[EditableEvent],
    passthrough_events: list[MacroEvent],
) -> dict[int, EditableEvent]:
    owners_by_key: dict[tuple[str, int, str | None], list[EditableEvent]] = {}
    for event in events:
        if event.ev_type != evdev.ecodes.EV_KEY:
            continue
        owners_by_key.setdefault(_editable_key_identity(event), []).append(event)
    for candidates in owners_by_key.values():
        candidates.sort(
            key=lambda event: (
                event.press_t_us,
                event.original_press_order
                if event.original_press_order is not None
                else -1,
            )
        )

    owners: dict[int, EditableEvent] = {}
    for repeat in passthrough_events:
        if not _is_repeat(repeat):
            continue
        timestamp_us = int(repeat.get("t_us", 0))
        identity = _raw_key_identity(repeat)
        candidates = owners_by_key.get(identity, ())
        repeat_order = _raw_original_order(repeat)
        for candidate in reversed(candidates):
            if _event_owns_repeat(candidate, timestamp_us, repeat_order):
                owners[id(repeat)] = candidate
                break
    return owners


def _event_owns_repeat(
    event: EditableEvent,
    timestamp_us: int,
    repeat_order: int | None,
) -> bool:
    if timestamp_us < event.press_t_us or timestamp_us > event.release_t_us:
        return False
    if (
        timestamp_us == event.press_t_us
        and repeat_order is not None
        and event.original_press_order is not None
        and repeat_order <= event.original_press_order
    ):
        return False
    if timestamp_us != event.release_t_us:
        return True
    if repeat_order is None or event.original_release_order is None:
        return False
    return repeat_order < event.original_release_order


def _raw_original_order(event: MacroEvent) -> int | None:
    source, order, _priority = selection_order(event)
    return order if source == 0 else None


def _is_repeat(event: MacroEvent) -> bool:
    return int(event.get("type", -1)) == evdev.ecodes.EV_KEY and int(event.get("value", -1)) == 2


def _editable_key_identity(event: EditableEvent) -> tuple[str, int, str | None]:
    return (
        str(event.device_type),
        int(event.code),
        str(event.output_id).strip() if event.output_id else None,
    )


def _raw_key_identity(event: MacroEvent) -> tuple[str, int, str | None]:
    device_type = str(event.get("device_type", "") or "")
    output_id = (
        str(event.get("output_id", "") or "").strip() or None if device_type == "gamepad" else None
    )
    return device_type, int(event.get("code", 0)), output_id


def _sort_items(
    events: list[EditableEvent],
    rel_events: list[MacroEvent],
    passthrough_events: list[MacroEvent],
    moves: list[EditableMove],
    controls: list[EditableControl],
) -> None:
    events.sort(key=lambda event: event.press_t_us)
    rel_events.sort(key=lambda event: int(event.get("t_us", 0)))
    passthrough_events.sort(key=lambda event: int(event.get("t_us", 0)))
    moves.sort(key=lambda move: move.t_us)
    controls.sort(key=lambda control: control.t_us)
