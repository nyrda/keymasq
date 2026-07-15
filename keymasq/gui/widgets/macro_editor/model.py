# pyright: reportUnusedFunction=false

from dataclasses import dataclass
from typing import Any

import evdev

from keymasq.common.coercion import bool_value, coerce_float, coerce_int
from keymasq.common.model.actions import (
    DEFAULT_NATURAL_MOUSE_MOVE_CURVE,
    DEFAULT_NATURAL_MOUSE_MOVE_JITTER,
    DEFAULT_NATURAL_MOUSE_MOVE_MAX_DURATION_MS,
    DEFAULT_NATURAL_MOUSE_MOVE_SPEED,
    DEFAULT_NATURAL_MOUSE_MOVE_TOLERANCE,
    MappingAction,
    normalize_natural_mouse_move_curve,
)
from keymasq.common.model.core import ActionType
from keymasq.gui.widgets.compositor_actions import describe_compositor_action

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MacroEvent = dict[str, Any]
_EDITOR_ORDER_ATTR = "_keymasq_editor_order"
_MISSING_EDITOR_ORDER = 2**63 - 1


class _OrderedMacroEvent(dict[str, Any]):
    pass


def _with_editor_order(ev: MacroEvent, order: int) -> MacroEvent:
    ordered = _OrderedMacroEvent(ev)
    setattr(ordered, _EDITOR_ORDER_ATTR, order)
    return ordered


def _get_editor_order(ev: MacroEvent) -> int | None:
    order = getattr(ev, _EDITOR_ORDER_ATTR, None)
    return order if isinstance(order, int) else None


def _original_order_or_last(order: int | None) -> int:
    return order if order is not None else _MISSING_EDITOR_ORDER


def _get_key_name(code: int) -> str:
    """Return a human-readable name for an evdev key code."""
    ecode_map = evdev.ecodes.bytype.get(evdev.ecodes.EV_KEY, {})
    name = ecode_map.get(code, str(code))
    if isinstance(name, (list, tuple)):
        name = name[0]
    return str(name)


def _get_event_name(event_type: int, code: int) -> str:
    """Return a human-readable name for any evdev event code."""
    ecode_map = evdev.ecodes.bytype.get(int(event_type), {})
    name = ecode_map.get(int(code), str(code))
    if isinstance(name, (list, tuple)):
        name = name[0]
    return str(name)


def _get_event_type_name(event_type: int) -> str:
    """Return a human-readable name for an evdev event type."""
    return str(evdev.ecodes.EV.get(int(event_type), str(event_type)))


def _passthrough_track(ev: MacroEvent) -> str:
    """Map a passthrough event to the track where it should be visualized."""
    ev_type = int(ev.get("type", -1))
    device_type = str(ev.get("device_type", "") or "")

    if ev_type == evdev.ecodes.EV_KEY:
        if device_type == "keyboard":
            return "keyboard"
        if device_type == "mouse":
            return "mouse"
        if device_type == "gamepad":
            return "gamepad"
    if ev_type == evdev.ecodes.EV_ABS and device_type == "gamepad":
        return "gamepad"
    return "movement"


def _describe_passthrough_event(ev: MacroEvent) -> tuple[str, str]:
    """Return a title and detail string for a raw passthrough event."""
    ev_type = int(ev.get("type", -1))
    code = int(ev.get("code", 0))
    value = int(ev.get("value", 0))
    device_type = str(ev.get("device_type", "") or "other")

    if ev_type == evdev.ecodes.EV_KEY:
        name = _get_key_name(code)
        action = {0: "release", 1: "press", 2: "repeat"}.get(value, f"value {value}")
        detail = f"Raw {device_type} key {action} ({name}, code {code})"
        if value in {0, 1}:
            detail += " without a matching pair"
        return name, detail

    type_name = _get_event_type_name(ev_type)
    name = _get_event_name(ev_type, code)
    return type_name, f"Raw {device_type} {type_name} {name} value {value} (code {code})"


_CONTROL_MACRO_ACTIONS = {
    "wait",
    "wait_random",
    "exec_sync",
    "exec_async",
    "compositor_dispatch",
}

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class EditableEvent:
    device_type: str  # "keyboard", "mouse", or "gamepad"
    ev_type: int  # evdev EV_KEY or EV_ABS
    code: int  # e.g. 30=KEY_A, 272=BTN_LEFT
    press_t_us: int  # microseconds from macro start (press)
    release_t_us: int  # microseconds from macro start (release)
    value: int = 0
    output_id: str | None = None
    original_press_order: int | None = None
    original_release_order: int | None = None


@dataclass
class EditableMove:
    mode: str  # "rel", "abs", or "natural"
    t_us: int
    x: int
    y: int
    speed: float = DEFAULT_NATURAL_MOUSE_MOVE_SPEED
    jitter: float = DEFAULT_NATURAL_MOUSE_MOVE_JITTER
    curve: str = DEFAULT_NATURAL_MOUSE_MOVE_CURVE
    tolerance: int = DEFAULT_NATURAL_MOUSE_MOVE_TOLERANCE
    max_duration_ms: int = DEFAULT_NATURAL_MOUSE_MOVE_MAX_DURATION_MS
    stop_on_failure: bool = False
    original_order: int | None = None


@dataclass
class EditableControl:
    mode: str  # wait | wait_random | exec_sync | exec_async | compositor_dispatch
    t_us: int
    duration_us: int = 0
    min_us: int = 0
    max_us: int = 0
    command: str = ""
    timeout_ms: int = 30000
    inhibit_mouse: bool = False
    compositor_id: str = ""
    compositor_dispatcher: str = ""
    compositor_args: str = ""
    original_order: int | None = None


def _control_to_compositor_action(control: EditableControl) -> MappingAction:
    return MappingAction(
        action_type=ActionType.COMPOSITOR_DISPATCH,
        compositor_id=control.compositor_id or None,
        compositor_dispatcher=control.compositor_dispatcher,
        compositor_args=control.compositor_args,
    )


def _move_macro_action(mode: str) -> str:
    if mode == "natural":
        return "mouse_move_natural_abs"
    if mode == "abs":
        return "mouse_move_abs"
    return "mouse_move_rel"


def _move_mode_from_action(action: str) -> str:
    if action == "mouse_move_natural_abs":
        return "natural"
    if action == "mouse_move_abs":
        return "abs"
    return "rel"


def _move_to_mapping_action(move: EditableMove) -> MappingAction:
    if move.mode == "natural":
        return MappingAction(
            action_type=ActionType.MOUSE_MOVE_NATURAL_ABS,
            move_x=int(move.x),
            move_y=int(move.y),
            move_speed=float(move.speed),
            move_jitter=float(move.jitter),
            move_curve=normalize_natural_mouse_move_curve(move.curve),
            move_tolerance=int(move.tolerance),
            move_max_duration_ms=int(move.max_duration_ms),
            move_stop_on_failure=bool(move.stop_on_failure),
        )
    return MappingAction(
        action_type=ActionType.MOUSE_MOVE_ABS if move.mode == "abs" else ActionType.MOUSE_MOVE_REL,
        move_x=int(move.x),
        move_y=int(move.y),
    )


def _apply_mapping_action_to_move(move: EditableMove, action: MappingAction) -> bool:
    if action.action_type == ActionType.MOUSE_MOVE_NATURAL_ABS:
        move.mode = "natural"
        move.x = int(action.move_x)
        move.y = int(action.move_y)
        move.speed = float(action.move_speed)
        move.jitter = float(action.move_jitter)
        move.curve = normalize_natural_mouse_move_curve(action.move_curve)
        move.tolerance = int(action.move_tolerance)
        move.max_duration_ms = int(action.move_max_duration_ms)
        move.stop_on_failure = bool(action.move_stop_on_failure)
        return True
    if action.action_type == ActionType.MOUSE_MOVE_ABS:
        move.mode = "abs"
    elif action.action_type == ActionType.MOUSE_MOVE_REL:
        move.mode = "rel"
    else:
        return False
    move.x = int(action.move_x)
    move.y = int(action.move_y)
    return True


def _describe_compositor_control(control: EditableControl) -> str:
    action = _control_to_compositor_action(control)
    description = describe_compositor_action(action)
    if description is not None:
        return description
    dispatcher = control.compositor_dispatcher or "dispatch"
    args = str(control.compositor_args or "").strip()
    suffix = f" {args}" if args else ""
    return f"Compositor -> {dispatcher}{suffix}"


def parse_events(
    raw_events: list[MacroEvent],
) -> tuple[
    list[EditableEvent],
    list[MacroEvent],
    list[MacroEvent],
    list[EditableMove],
    list[EditableControl],
]:
    """
    Split raw event dicts into (editable_events, rel_events, passthrough_events, editable_moves).

    EV_KEY press/release pairs → EditableEvent.
    EV_REL events → movement waveform (read-only).
    Semantic mouse-move macro actions are parsed into EditableMove.
    Any unsupported/unmatched events are preserved in passthrough_events.
    EV_SYN events are discarded.
    """
    ev_key = evdev.ecodes.EV_KEY
    ev_abs = evdev.ecodes.EV_ABS
    ev_rel = evdev.ecodes.EV_REL

    editable: list[EditableEvent] = []
    rel_events: list[MacroEvent] = []
    passthrough_events: list[MacroEvent] = []
    editable_moves: list[EditableMove] = []
    control_events: list[EditableControl] = []
    open_presses: dict[tuple, list[tuple[int, int]]] = {}

    for original_order, ev in enumerate(raw_events):
        macro_action = str(ev.get("macro_action", "") or "")
        if macro_action in {"mouse_move_abs", "mouse_move_rel", "mouse_move_natural_abs"}:
            editable_moves.append(
                EditableMove(
                    mode=_move_mode_from_action(macro_action),
                    t_us=int(ev.get("t_us", 0)),
                    x=int(ev.get("x", 0) or 0),
                    y=int(ev.get("y", 0) or 0),
                    speed=coerce_float(ev.get("speed"), DEFAULT_NATURAL_MOUSE_MOVE_SPEED),
                    jitter=coerce_float(ev.get("jitter"), DEFAULT_NATURAL_MOUSE_MOVE_JITTER),
                    curve=normalize_natural_mouse_move_curve(ev.get("curve")),
                    tolerance=coerce_int(
                        ev.get("tolerance"),
                        DEFAULT_NATURAL_MOUSE_MOVE_TOLERANCE,
                    ),
                    max_duration_ms=coerce_int(
                        ev.get("max_duration_ms"),
                        DEFAULT_NATURAL_MOUSE_MOVE_MAX_DURATION_MS,
                    ),
                    stop_on_failure=bool_value(ev.get("stop_on_failure")),
                    original_order=original_order,
                )
            )
            continue
        if macro_action in _CONTROL_MACRO_ACTIONS:
            control_events.append(
                EditableControl(
                    mode=macro_action,
                    t_us=int(ev.get("t_us", 0)),
                    duration_us=int(ev.get("duration_us", 0) or 0),
                    min_us=int(ev.get("min_us", 0) or 0),
                    max_us=int(ev.get("max_us", 0) or 0),
                    command=str(ev.get("command", "") or ""),
                    timeout_ms=int(ev.get("timeout_ms", 30000) or 30000),
                    inhibit_mouse=bool(ev.get("inhibit_mouse", False)),
                    compositor_id=str(ev.get("compositor", "") or ""),
                    compositor_dispatcher=str(ev.get("dispatcher", "") or ""),
                    compositor_args=str(ev.get("args", "") or ""),
                    original_order=original_order,
                )
            )
            continue
        if macro_action:
            passthrough_events.append(_with_editor_order(ev, original_order))
            continue

        if ev["type"] == ev_key:
            output_id = str(ev.get("output_id", "") or "").strip() or None
            key = (ev["device_type"], ev["code"], output_id)
            if ev["value"] == 1:
                open_presses.setdefault(key, []).append((ev["t_us"], original_order))
            elif ev["value"] == 0:
                stack = open_presses.get(key)
                press = stack.pop() if stack else None
                if press is not None:
                    press_t, press_order = press
                    editable.append(
                        EditableEvent(
                            device_type=ev["device_type"],
                            ev_type=ev_key,
                            code=ev["code"],
                            press_t_us=press_t,
                            release_t_us=ev["t_us"],
                            output_id=output_id if ev["device_type"] == "gamepad" else None,
                            original_press_order=press_order,
                            original_release_order=original_order,
                        )
                    )
                else:
                    passthrough_events.append(_with_editor_order(ev, original_order))
            else:
                passthrough_events.append(_with_editor_order(ev, original_order))
        elif ev["type"] == ev_abs and ev.get("device_type") == "gamepad":
            t_us = int(ev.get("t_us", 0))
            output_id = str(ev.get("output_id", "") or "").strip() or None
            editable.append(
                EditableEvent(
                    device_type="gamepad",
                    ev_type=ev_abs,
                    code=int(ev["code"]),
                    press_t_us=t_us,
                    release_t_us=t_us + 1,
                    value=int(ev.get("value", 0) or 0),
                    output_id=output_id,
                    original_press_order=original_order,
                )
            )
        elif ev["type"] == ev_rel:
            rel_events.append(_with_editor_order(ev, original_order))
        elif ev["type"] == evdev.ecodes.EV_SYN:
            # Sync events carry no timeline meaning; recorder and playback
            # both drop them, so discard instead of keeping as passthrough.
            continue
        else:
            passthrough_events.append(_with_editor_order(ev, original_order))

    for (device_type, code, output_id), presses in open_presses.items():
        for press_t, press_order in presses:
            event = {
                "device_type": device_type,
                "type": ev_key,
                "code": code,
                "value": 1,
                "t_us": press_t,
            }
            if device_type == "gamepad" and output_id:
                event["output_id"] = output_id
            passthrough_events.append(_with_editor_order(event, press_order))

    editable.sort(key=lambda e: (e.press_t_us, _original_order_or_last(e.original_press_order)))
    editable_moves.sort(key=lambda m: (m.t_us, _original_order_or_last(m.original_order)))
    control_events.sort(key=lambda c: (c.t_us, _original_order_or_last(c.original_order)))
    passthrough_events.sort(
        key=lambda e: (int(e["t_us"]), _original_order_or_last(_get_editor_order(e)))
    )
    return editable, rel_events, passthrough_events, editable_moves, control_events


def reconstruct_events(
    editable: list[EditableEvent],
    rel_events: list[MacroEvent],
    passthrough_events: list[MacroEvent],
    editable_moves: list[EditableMove],
    control_events: list[EditableControl],
) -> list[MacroEvent]:
    """Reconstruct raw event list from editable, REL and passthrough events."""
    raw: list[MacroEvent] = []

    for ev in editable:
        if ev.ev_type == evdev.ecodes.EV_KEY:
            press_event: MacroEvent = {
                "device_type": ev.device_type,
                "type": ev.ev_type,
                "code": ev.code,
                "value": 1,
                "t_us": ev.press_t_us,
            }
            release_event: MacroEvent = {
                "device_type": ev.device_type,
                "type": ev.ev_type,
                "code": ev.code,
                "value": 0,
                "t_us": ev.release_t_us,
            }
            if ev.device_type == "gamepad" and ev.output_id:
                press_event["output_id"] = ev.output_id
                release_event["output_id"] = ev.output_id
            if ev.original_press_order is not None:
                press_event = _with_editor_order(press_event, ev.original_press_order)
            raw.append(press_event)
            if ev.original_release_order is not None:
                release_event = _with_editor_order(release_event, ev.original_release_order)
            raw.append(release_event)
            continue

        instant_event: MacroEvent = {
            "device_type": ev.device_type,
            "type": ev.ev_type,
            "code": ev.code,
            "value": int(ev.value),
            "t_us": ev.press_t_us,
        }
        if ev.device_type == "gamepad" and ev.output_id:
            instant_event["output_id"] = ev.output_id
        if ev.original_press_order is not None:
            instant_event = _with_editor_order(instant_event, ev.original_press_order)
        raw.append(instant_event)

    raw.extend(rel_events)

    for move in editable_moves:
        move_event: MacroEvent = {
            "device_type": "macro",
            "type": 0,
            "code": 0,
            "value": 0,
            "t_us": int(move.t_us),
            "macro_action": _move_macro_action(move.mode),
            "x": int(move.x),
            "y": int(move.y),
        }
        if move.mode == "natural":
            move_event["speed"] = float(move.speed)
            move_event["jitter"] = float(move.jitter)
            move_event["curve"] = normalize_natural_mouse_move_curve(move.curve)
            move_event["tolerance"] = int(move.tolerance)
            move_event["max_duration_ms"] = int(move.max_duration_ms)
            move_event["stop_on_failure"] = bool(move.stop_on_failure)
        if move.original_order is not None:
            move_event = _with_editor_order(move_event, move.original_order)
        raw.append(move_event)

    for control in control_events:
        control_event: MacroEvent = {
            "device_type": "macro",
            "type": 0,
            "code": 0,
            "value": 0,
            "t_us": int(control.t_us),
            "macro_action": str(control.mode),
        }
        if control.mode == "wait":
            control_event["duration_us"] = int(control.duration_us)
        elif control.mode == "wait_random":
            control_event["min_us"] = int(control.min_us)
            control_event["max_us"] = int(control.max_us)
        elif control.mode in {"exec_sync", "exec_async"}:
            control_event["command"] = str(control.command)
            if control.mode == "exec_sync":
                control_event["timeout_ms"] = int(control.timeout_ms)
                control_event["inhibit_mouse"] = bool(control.inhibit_mouse)
        elif control.mode == "compositor_dispatch":
            if control.compositor_id:
                control_event["compositor"] = str(control.compositor_id)
            control_event["dispatcher"] = str(control.compositor_dispatcher)
            control_event["args"] = str(control.compositor_args)
        if control.original_order is not None:
            control_event = _with_editor_order(control_event, control.original_order)
        raw.append(control_event)

    raw.extend(passthrough_events)
    original_orders = [order for ev in raw if (order := _get_editor_order(ev)) is not None]
    max_original_order = max(original_orders, default=-1)

    def sort_key(item: tuple[int, MacroEvent]) -> tuple[int, int]:
        index, ev = item
        original_order = _get_editor_order(ev)
        order = original_order if original_order is not None else max_original_order + 1 + index
        return int(ev["t_us"]), order

    return [dict(ev) for _, ev in sorted(enumerate(raw), key=sort_key)]


def _assign_lanes(events: list[EditableEvent], device_type: str) -> tuple[dict[int, int], int]:
    """
    Greedy interval scheduling: assign vertical lane indices to events of
    a given device_type so that overlapping events occupy different lanes.
    Returns (dict mapping id(ev) -> lane_index, num_lanes).
    """
    track_evs = sorted(
        [e for e in events if e.device_type == device_type],
        key=lambda e: e.press_t_us,
    )
    lane_ends: list[int] = []
    result: dict[int, int] = {}
    for ev in track_evs:
        placed = False
        for i, end_t in enumerate(lane_ends):
            if ev.press_t_us >= end_t:
                lane_ends[i] = max(ev.release_t_us, ev.press_t_us + 1)
                result[id(ev)] = i
                placed = True
                break
        if not placed:
            result[id(ev)] = len(lane_ends)
            lane_ends.append(max(ev.release_t_us, ev.press_t_us + 1))
    return result, max(len(lane_ends), 1)


def _format_time_us(t_us: int) -> str:
    """Format microseconds to a compact human-readable string."""
    t_s = t_us / 1e6
    if t_s >= 1.0:
        return f"{t_s:.3f}s"
    return f"{t_s * 1000:.1f}ms"
