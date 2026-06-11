"""Timing transforms for the macro editor edit model."""

from collections.abc import Iterable

import evdev

from keymasq.gui.widgets.macro_editor.model import (
    EditableControl,
    EditableEvent,
    EditableMove,
    MacroEvent,
)

TimelineLists = tuple[
    list[EditableEvent],
    list[MacroEvent],
    list[MacroEvent],
    list[EditableMove],
    list[EditableControl],
]


def all_timestamps(
    events: list[EditableEvent],
    rel_events: list[MacroEvent],
    passthrough_events: list[MacroEvent],
    synthetic_moves: list[EditableMove],
    control_events: list[EditableControl],
    *,
    include_passthrough: bool = True,
) -> list[int]:
    stamps: list[int] = []
    for ev in events:
        stamps.append(int(ev.press_t_us))
        stamps.append(int(ev.release_t_us))
    for ev in rel_events:
        stamps.append(int(ev.get("t_us", 0)))
    if include_passthrough:
        for ev in passthrough_events:
            stamps.append(int(ev.get("t_us", 0)))
    for move in synthetic_moves:
        stamps.append(int(move.t_us))
        if move.mode == "abs":
            stamps.append(int(move.t_us) + 1)
    for control in control_events:
        stamps.append(int(control.t_us))
    return sorted(set(max(0, s) for s in stamps))


def build_time_mapping_with_gap_limits(
    events: list[EditableEvent],
    rel_events: list[MacroEvent],
    passthrough_events: list[MacroEvent],
    synthetic_moves: list[EditableMove],
    control_events: list[EditableControl],
    *,
    scale: float = 1.0,
    min_gap_us: int = 0,
    max_gap_us: int | None = None,
    include_passthrough: bool = True,
) -> dict[int, int]:
    stamps = all_timestamps(
        events,
        rel_events,
        passthrough_events,
        synthetic_moves,
        control_events,
        include_passthrough=include_passthrough,
    )
    if not stamps:
        return {}

    mapping: dict[int, int] = {stamps[0]: stamps[0]}
    prev_old = stamps[0]
    prev_new = stamps[0]
    min_gap_us = max(0, int(min_gap_us))
    max_gap = max(0, int(max_gap_us)) if max_gap_us is not None else None

    for t_us in stamps[1:]:
        gap_old = max(0, t_us - prev_old)
        gap = int(round(gap_old * scale))
        gap = max(gap, min_gap_us)
        if max_gap is not None:
            gap = min(gap, max_gap)
        prev_new += gap
        mapping[t_us] = prev_new
        prev_old = t_us

    return mapping


def build_trim_start_mapping(stamps: Iterable[int]) -> dict[int, int]:
    ordered = sorted(set(max(0, int(stamp)) for stamp in stamps))
    if not ordered:
        return {}
    start_t = ordered[0]
    if start_t <= 0:
        return {}
    return {t_us: t_us - start_t for t_us in ordered}


def build_shift_mapping(stamps: Iterable[int], *, at_us: int, delta_us: int) -> dict[int, int]:
    if delta_us == 0:
        return {}
    at_us = max(0, int(at_us))
    delta_us = int(delta_us)
    return {
        t_us: max(0, t_us + delta_us)
        for t_us in sorted(set(max(0, int(stamp)) for stamp in stamps))
        if t_us >= at_us
    }


def map_time(mapping: dict[int, int], t_us: int) -> int:
    keys = sorted(mapping.keys())
    t_us = int(t_us)
    if t_us in mapping:
        return int(mapping[t_us])
    if not keys:
        return t_us
    if t_us <= keys[0]:
        return int(mapping[keys[0]])
    if t_us >= keys[-1]:
        return int(mapping[keys[-1]])

    # Piecewise linear interpolation between nearest mapped anchors.
    for index in range(1, len(keys)):
        left = keys[index - 1]
        right = keys[index]
        if left <= t_us <= right:
            left_new = int(mapping[left])
            right_new = int(mapping[right])
            span = max(1, right - left)
            frac = (t_us - left) / span
            return int(round(left_new + (right_new - left_new) * frac))
    return t_us


def apply_time_map(
    events: list[EditableEvent],
    rel_events: list[MacroEvent],
    passthrough_events: list[MacroEvent],
    synthetic_moves: list[EditableMove],
    control_events: list[EditableControl],
    mapping: dict[int, int],
) -> None:
    for ev in events:
        ev.press_t_us = map_time(mapping, ev.press_t_us)
        ev.release_t_us = max(ev.press_t_us + 1, map_time(mapping, ev.release_t_us))

    for ev in rel_events:
        ev["t_us"] = map_time(mapping, int(ev.get("t_us", 0)))

    for ev in passthrough_events:
        ev["t_us"] = map_time(mapping, int(ev.get("t_us", 0)))

    for move in synthetic_moves:
        move.t_us = map_time(mapping, move.t_us)

    for control in control_events:
        control.t_us = map_time(mapping, control.t_us)

    sort_timeline_items(events, rel_events, passthrough_events, synthetic_moves, control_events)


def compute_duration_us(
    events: list[EditableEvent],
    rel_events: list[MacroEvent],
    passthrough_events: list[MacroEvent],
    synthetic_moves: list[EditableMove],
    control_events: list[EditableControl],
) -> int:
    latest = 0
    if events:
        latest = max(latest, max(e.release_t_us for e in events))
    if rel_events:
        latest = max(latest, max(int(e.get("t_us", 0)) for e in rel_events))
    if passthrough_events:
        latest = max(latest, max(int(e.get("t_us", 0)) for e in passthrough_events))
    if synthetic_moves:
        latest = max(latest, max(m.t_us for m in synthetic_moves))
    if control_events:
        latest = max(latest, max(c.t_us for c in control_events))
    return max(0, int(latest))


def sort_timeline_items(
    events: list[EditableEvent],
    rel_events: list[MacroEvent],
    passthrough_events: list[MacroEvent],
    synthetic_moves: list[EditableMove],
    control_events: list[EditableControl],
) -> None:
    events.sort(key=lambda e: e.press_t_us)
    synthetic_moves.sort(key=lambda m: m.t_us)
    control_events.sort(key=lambda c: c.t_us)
    rel_events.sort(key=lambda e: int(e.get("t_us", 0)))
    passthrough_events.sort(key=lambda e: int(e.get("t_us", 0)))


def trim_startpoint(
    events: list[EditableEvent],
    rel_events: list[MacroEvent],
    passthrough_events: list[MacroEvent],
    synthetic_moves: list[EditableMove],
    control_events: list[EditableControl],
    at_us: int,
) -> TimelineLists:
    at_us = max(0, int(at_us))
    if at_us <= 0:
        return events, rel_events, passthrough_events, synthetic_moves, control_events

    kept_events = [ev for ev in events if ev.press_t_us >= at_us]
    kept_rel_events = [ev for ev in rel_events if int(ev.get("t_us", 0)) >= at_us]
    kept_passthrough_events = [
        ev for ev in passthrough_events if int(ev.get("t_us", 0)) >= at_us
    ]
    kept_synthetic_moves = [move for move in synthetic_moves if move.t_us >= at_us]
    kept_control_events = [control for control in control_events if control.t_us >= at_us]

    mapping = build_shift_mapping(
        all_timestamps(
            kept_events,
            kept_rel_events,
            kept_passthrough_events,
            kept_synthetic_moves,
            kept_control_events,
            include_passthrough=True,
        ),
        at_us=at_us,
        delta_us=-at_us,
    )
    apply_time_map(
        kept_events,
        kept_rel_events,
        kept_passthrough_events,
        kept_synthetic_moves,
        kept_control_events,
        mapping,
    )
    return (
        kept_events,
        kept_rel_events,
        kept_passthrough_events,
        kept_synthetic_moves,
        kept_control_events,
    )


def trim_endpoint(
    events: list[EditableEvent],
    rel_events: list[MacroEvent],
    passthrough_events: list[MacroEvent],
    synthetic_moves: list[EditableMove],
    control_events: list[EditableControl],
    at_us: int,
) -> TimelineLists:
    at_us = max(0, int(at_us))

    kept_events: list[EditableEvent] = []
    for ev in events:
        if ev.press_t_us >= at_us:
            continue
        if ev.release_t_us > at_us:
            ev.release_t_us = max(at_us, ev.press_t_us + 1)
        kept_events.append(ev)

    kept_rel_events = [ev for ev in rel_events if int(ev.get("t_us", 0)) <= at_us]
    kept_passthrough_events = [
        ev for ev in passthrough_events if int(ev.get("t_us", 0)) <= at_us
    ]
    kept_synthetic_moves = [move for move in synthetic_moves if move.t_us <= at_us]
    kept_control_events = [control for control in control_events if control.t_us <= at_us]
    sort_timeline_items(
        kept_events,
        kept_rel_events,
        kept_passthrough_events,
        kept_synthetic_moves,
        kept_control_events,
    )
    return (
        kept_events,
        kept_rel_events,
        kept_passthrough_events,
        kept_synthetic_moves,
        kept_control_events,
    )


def ripple_delete_range(
    events: list[EditableEvent],
    rel_events: list[MacroEvent],
    passthrough_events: list[MacroEvent],
    synthetic_moves: list[EditableMove],
    control_events: list[EditableControl],
    t0_us: int,
    t1_us: int,
) -> TimelineLists:
    """Delete everything intersecting [t0_us, t1_us] and close the gap.

    Press/release pairs that fully span the range (press before, release after)
    are kept; closing the gap shortens them instead.
    """
    t0_us = max(0, int(t0_us))
    t1_us = int(t1_us)
    if t1_us <= t0_us:
        return events, rel_events, passthrough_events, synthetic_moves, control_events

    kept_events = [
        ev
        for ev in events
        if ev.release_t_us < t0_us
        or ev.press_t_us > t1_us
        or (ev.press_t_us < t0_us and ev.release_t_us > t1_us)
    ]
    kept_rel_events = [
        ev for ev in rel_events if not t0_us <= int(ev.get("t_us", 0)) <= t1_us
    ]
    kept_passthrough_events = [
        ev for ev in passthrough_events if not t0_us <= int(ev.get("t_us", 0)) <= t1_us
    ]
    kept_synthetic_moves = [move for move in synthetic_moves if not t0_us <= move.t_us <= t1_us]
    kept_control_events = [
        control for control in control_events if not t0_us <= control.t_us <= t1_us
    ]

    shift_timeline_for_gap(
        kept_events,
        kept_rel_events,
        kept_passthrough_events,
        kept_synthetic_moves,
        kept_control_events,
        at_us=t1_us,
        delta_us=-(t1_us - t0_us),
        scope="all",
        exclude_control=None,
    )
    return (
        kept_events,
        kept_rel_events,
        kept_passthrough_events,
        kept_synthetic_moves,
        kept_control_events,
    )


def shift_timeline_for_gap(
    events: list[EditableEvent],
    rel_events: list[MacroEvent],
    passthrough_events: list[MacroEvent],
    synthetic_moves: list[EditableMove],
    control_events: list[EditableControl],
    *,
    at_us: int,
    delta_us: int,
    scope: str,
    exclude_control: EditableControl | None,
) -> bool:
    if delta_us == 0:
        return False

    changed = False
    at_us = int(at_us)
    delta_us = int(delta_us)

    if scope in ("all", "keyboard", "mouse", "gamepad"):
        for ev in events:
            if scope != "all" and ev.device_type != scope:
                continue
            if ev.press_t_us >= at_us:
                ev.press_t_us = max(0, ev.press_t_us + delta_us)
                changed = True
            if ev.release_t_us >= at_us:
                ev.release_t_us = max(0, ev.release_t_us + delta_us)
                changed = True
            if ev.release_t_us <= ev.press_t_us:
                ev.release_t_us = ev.press_t_us + 1

    if scope in ("all", "movement"):
        for ev in rel_events:
            t_us = int(ev.get("t_us", 0))
            if t_us >= at_us:
                ev["t_us"] = max(0, t_us + delta_us)
                changed = True
        for move in synthetic_moves:
            if move.t_us >= at_us:
                move.t_us = max(0, move.t_us + delta_us)
                changed = True

    if scope in ("all", "movement"):
        for control in control_events:
            if exclude_control is not None and control is exclude_control:
                continue
            if control.t_us >= at_us:
                control.t_us = max(0, control.t_us + delta_us)
                changed = True

    for ev in passthrough_events:
        t_us = int(ev.get("t_us", 0))
        if t_us < at_us:
            continue

        if _passthrough_matches_scope(ev, scope):
            ev["t_us"] = max(0, t_us + delta_us)
            changed = True

    if changed:
        sort_timeline_items(events, rel_events, passthrough_events, synthetic_moves, control_events)
    return changed


def _passthrough_matches_scope(ev: MacroEvent, scope: str) -> bool:
    ev_type = int(ev.get("type", -1))
    device_type = str(ev.get("device_type", ""))
    if scope == "all":
        return True
    if scope == "keyboard":
        return ev_type == evdev.ecodes.EV_KEY and device_type == "keyboard"
    if scope == "mouse":
        return ev_type == evdev.ecodes.EV_KEY and device_type == "mouse"
    if scope == "gamepad":
        return ev_type == evdev.ecodes.EV_KEY and device_type == "gamepad"
    if scope == "movement":
        return ev_type == evdev.ecodes.EV_REL and device_type == "mouse"
    return False
