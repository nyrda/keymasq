"""Selection transforms and bounded undo history, independent of GTK."""

import copy
import json
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import evdev

from keymasq.gui.widgets.macro_editor.model import (
    EditableControl,
    EditableEvent,
    EditableMove,
    MacroEvent,
    _get_editor_order,
    _with_editor_order,
    parse_events,
    reconstruct_events,
)
from keymasq.gui.widgets.macro_editor.timing_ops import TimelineLists

type Item = EditableEvent | EditableMove | EditableControl | MacroEvent


def items(lists: TimelineLists) -> list[Item]:
    return [item for group in lists for item in group]


def start(item: Item) -> int:
    if isinstance(item, EditableEvent):
        return item.press_t_us
    if isinstance(item, dict):
        return int(item.get("t_us", 0))
    return item.t_us


def end(item: Item) -> int:
    if isinstance(item, EditableEvent):
        return item.release_t_us
    # The legacy absolute move emits a second event one microsecond later.
    return start(item) + (1 if isinstance(item, EditableMove) and item.mode == "abs" else 0)


def bounds(selected: Sequence[Item]) -> tuple[int, int]:
    return min(map(start, selected), default=0), max(map(end, selected), default=0)


def select_time_range(
    candidates: Sequence[Item], first: int, last: int
) -> tuple[list[Item], tuple[int, int]]:
    """Select a half-open time span, expanding through complete held inputs."""
    first, last = sorted((max(0, first), max(0, last)))
    # Merge overlapping holds before expanding so nested and chained holds
    # cannot leave a press or release outside the copied interval.
    spans: list[tuple[int, int]] = []
    for left, right in sorted(
        (start(item), end(item)) for item in candidates if end(item) > start(item)
    ):
        if spans and left < spans[-1][1]:
            spans[-1] = (spans[-1][0], max(right, spans[-1][1]))
        else:
            spans.append((left, right))
    if first == last:
        return [], (first, last)
    for left, right in spans:
        if left < last and right > first:
            first, last = min(first, left), max(last, right)
    selected = [item for item in candidates if first <= start(item) < last]
    return selected, (first, last)


def shift(item: Item, delta_us: int) -> None:
    if isinstance(item, EditableEvent):
        item.press_t_us += delta_us
        item.release_t_us += delta_us
    elif isinstance(item, dict):
        item["t_us"] = start(item) + delta_us
    else:
        item.t_us += delta_us


def move(selected: list[Item], delta_us: int) -> int:
    """Clamp the whole selection at zero without squeezing its spacing."""
    delta_us = max(delta_us, -bounds(selected)[0])
    for item in selected:
        shift(item, delta_us)
    return delta_us


def subset(lists: TimelineLists, selected: list[Item]) -> TimelineLists:
    ids = {id(item) for item in selected}
    events, relative, raw, moves, controls = lists
    return (
        [item for item in events if id(item) in ids],
        [item for item in relative if id(item) in ids],
        [item for item in raw if id(item) in ids],
        [item for item in moves if id(item) in ids],
        [item for item in controls if id(item) in ids],
    )


def remove_ids[T](group: list[T], ids: set[int]) -> None:
    group[:] = [item for item in group if id(item) not in ids]


def assign_missing_orders(lists: TimelineLists) -> int:
    """Give manual actions an order before appending a copied recording.

    Reconstruction puts unordered actions after all recorded actions. Freeze
    that order first, or a pasted press can precede the original key's release.
    """
    orders: list[int] = []
    for item in items(lists):
        if isinstance(item, EditableEvent):
            orders.extend(
                order
                for order in (item.original_press_order, item.original_release_order)
                if order is not None
            )
        else:
            order = _get_editor_order(item) if isinstance(item, dict) else item.original_order
            if order is not None:
                orders.append(order)
    next_order = max(orders, default=-1) + 1
    for event in lists[0]:
        if event.original_press_order is None:
            event.original_press_order = next_order
            next_order += 1
        if event.ev_type == evdev.ecodes.EV_KEY and event.original_release_order is None:
            event.original_release_order = next_order
            next_order += 1
    # Match reconstruct_events' fallback order: keys, REL, moves, controls, raw.
    for index, event in enumerate(lists[1]):
        if _get_editor_order(event) is None:
            lists[1][index] = _with_editor_order(event, next_order)
            next_order += 1
    for item in [*lists[3], *lists[4]]:
        if item.original_order is None:
            item.original_order = next_order
            next_order += 1
    for index, event in enumerate(lists[2]):
        if _get_editor_order(event) is None:
            lists[2][index] = _with_editor_order(event, next_order)
            next_order += 1
    return next_order


@dataclass
class Fragment:
    events: list[MacroEvent]
    duration_us: int
    preserve_range: bool = False

    @classmethod
    def from_clipboard(cls, data: bytes) -> "Fragment":
        payload = json.loads(data)
        if not isinstance(payload, dict):
            raise ValueError("The clipboard does not contain a macro section.")
        events = payload.get("events")
        duration = payload.get("duration_us")
        if not isinstance(events, list) or type(duration) is not int or duration < 1:
            raise ValueError("The copied macro section has invalid events or duration.")
        for event in events:
            if not isinstance(event, dict) or not isinstance(event.get("device_type"), str):
                raise ValueError("The copied macro section contains an invalid event.")
            if any(
                type(event.get(field)) is not int for field in ("t_us", "type", "code", "value")
            ):
                raise ValueError("The copied macro section contains an invalid event value.")
            if not 0 <= event["t_us"] <= duration:
                raise ValueError("The copied macro section contains an invalid timestamp.")
        # Validate semantic action fields before a paste can move existing items.
        parse_events(events)
        return cls(events, duration, payload.get("preserve_range") is True)

    @classmethod
    def capture(
        cls,
        lists: TimelineLists,
        selected: list[Item],
        *,
        time_range: tuple[int, int] | None = None,
    ) -> "Fragment":
        first, last = time_range if time_range is not None else bounds(selected)
        if time_range is not None:
            if (
                first < 0
                or last <= first
                or any(start(item) < first or end(item) > last for item in selected)
            ):
                raise ValueError("The selected range must contain each complete action.")
        events = copy.deepcopy(reconstruct_events(*subset(lists, selected)))
        for event in events:
            event["t_us"] = int(event["t_us"]) - first
        return cls(events, max(1, last - first), time_range is not None)

    def clipboard_payload(self) -> dict[str, Any]:
        return {
            "events": copy.deepcopy(self.events),
            "duration_us": self.duration_us,
            "preserve_range": self.preserve_range,
        }

    def paste(self, lists: TimelineLists, at_us: int, *, insert: bool = False) -> list[Item]:
        at_us = max(0, at_us)
        if insert:
            for item in items(lists):
                if start(item) >= at_us:
                    shift(item, self.duration_us)
                elif isinstance(item, EditableEvent) and item.release_t_us > at_us:
                    item.release_t_us += self.duration_us
        # Rebase source order after destination events. Equal-time source order
        # remains intact, including release-before-press at block boundaries.
        next_order = assign_missing_orders(lists)
        raw = []
        for event in self.events:
            cloned = copy.deepcopy(event)
            cloned["t_us"] = int(cloned["t_us"]) + at_us
            raw.append(cloned)
        added = parse_events(raw)
        # parse_events numbers its input; give the new items destination-local orders.
        for item in items(added):
            if isinstance(item, EditableEvent):
                if item.original_press_order is not None:
                    item.original_press_order += next_order
                if item.original_release_order is not None:
                    item.original_release_order += next_order
            elif not isinstance(item, dict) and item.original_order is not None:
                item.original_order += next_order
        for group in (added[1], added[2]):
            group[:] = [
                _with_editor_order(item, (_get_editor_order(item) or 0) + next_order)
                for item in group
            ]
        lists[0].extend(added[0])
        lists[1].extend(added[1])
        lists[2].extend(added[2])
        lists[3].extend(added[3])
        lists[4].extend(added[4])
        return items(added)

def scale(selected: list[Item], factor: float, *, scale_waits: bool = False) -> None:
    if factor <= 0:
        raise ValueError("Timing factor must be positive")
    anchor = bounds(selected)[0]
    for item in selected:
        old_start = start(item)
        new_start = anchor + round((old_start - anchor) * factor)
        if isinstance(item, EditableEvent):
            item.release_t_us = max(
                new_start + 1, anchor + round((item.release_t_us - anchor) * factor)
            )
            item.press_t_us = new_start
        else:
            shift(item, new_start - old_start)
        if scale_waits and isinstance(item, EditableControl):
            if item.mode == "wait":
                item.duration_us = max(0, round(item.duration_us * factor))
            elif item.mode == "wait_random":
                item.min_us = max(0, round(item.min_us * factor))
                item.max_us = max(item.min_us, round(item.max_us * factor))


def pause_sections(selected: Sequence[Item]) -> list[tuple[int, int]]:
    """Group occupied time so only positive gaps between groups are adjustable."""
    # A recorded pointer trajectory is one occupied section. Changing a pause
    # must not turn every sample interval into a new pause or alter the path.
    motion = [
        item
        for item in selected
        if isinstance(item, dict) and item.get("type") == evdev.ecodes.EV_REL
    ]
    motion_ids = {id(item) for item in motion}
    spans = [(start(item), end(item)) for item in selected if id(item) not in motion_ids]
    if motion:
        spans.append(bounds(motion))
    sections: list[tuple[int, int]] = []
    for first, last in sorted(spans):
        if sections and first <= sections[-1][1]:
            sections[-1] = (sections[-1][0], max(sections[-1][1], last))
        else:
            sections.append((first, last))
    return sections


def set_pauses(selected: list[Item], pause_us: int) -> None:
    """Replace positive idle gaps, preserving holds and overlapping groups."""
    sections = pause_sections(selected)
    if len(sections) < 2:
        return
    previous_end = sections[0][1]
    delta = 0
    anchors = [sections[0][0]]
    offsets = [0]
    for item_start, item_end in sections[1:]:
        delta += max(0, pause_us) - (item_start - previous_end)
        anchors.append(item_start)
        offsets.append(delta)
        previous_end = item_end
    for item in selected:
        shift(item, offsets[bisect_right(anchors, start(item)) - 1])


@dataclass
class EditHistory:
    limit: int = 100
    current: dict[str, Any] | None = None
    past: list[dict[str, Any]] = field(default_factory=list)
    future: list[dict[str, Any]] = field(default_factory=list)

    def record(self, payload: dict[str, Any]) -> None:
        if payload == self.current:
            return
        if self.current is not None:
            self.past.append(self.current)
            self.past = self.past[-self.limit :]
        self.current = copy.deepcopy(payload)
        self.future.clear()

    def undo(self) -> dict[str, Any] | None:
        if not self.past or self.current is None:
            return None
        self.future.append(self.current)
        self.current = self.past.pop()
        return copy.deepcopy(self.current)

    def redo(self) -> dict[str, Any] | None:
        if not self.future or self.current is None:
            return None
        self.past.append(self.current)
        self.current = self.future.pop()
        return copy.deepcopy(self.current)
