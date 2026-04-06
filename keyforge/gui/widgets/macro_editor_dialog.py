import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import copy
import math
import re
from dataclasses import dataclass

import evdev
from gi.repository import Adw, Gdk, GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keyforge.common.slurp import get_slurp_capture
from keyforge.gui.session_client import run_gui_task, session_request, session_request_async
from keyforge.gui.session_reload import notify_session_reload_async
from keyforge.session.compositor import detect_compositor_sync

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _passthrough_track(ev: dict) -> str:
    """Map a passthrough event to the track where it should be visualized."""
    ev_type = int(ev.get("type", -1))
    device_type = str(ev.get("device_type", "") or "")

    if ev_type == evdev.ecodes.EV_KEY:
        if device_type == "keyboard":
            return "keyboard"
        if device_type == "mouse":
            return "mouse"
    return "movement"


def _describe_passthrough_event(ev: dict) -> tuple[str, str]:
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

    type_name = _get_event_name(0, ev_type)
    name = _get_event_name(ev_type, code)
    return type_name, f"Raw {device_type} {type_name} {name} value {value} (code {code})"


_SCOPE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("all", "Everything"),
    ("keyboard", "Keyboard"),
    ("mouse", "Mouse"),
    ("movement", "Movement"),
)

_LOOP_MODE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("none", "Once"),
    ("count", "Count"),
    ("hold", "While Held"),
    ("toggle", "Toggle"),
)


def _build_option_dropdown(
    options: tuple[tuple[str, str], ...],
    active_id: str,
) -> Gtk.DropDown:
    dropdown = Gtk.DropDown.new_from_strings([label for _, label in options])
    dropdown.set_enable_search(False)
    _set_dropdown_selected_id(dropdown, options, active_id)
    return dropdown


def _set_dropdown_selected_id(
    dropdown: Gtk.DropDown,
    options: tuple[tuple[str, str], ...],
    option_id: str,
    default_id: str | None = None,
) -> None:
    fallback_id = default_id or options[0][0]
    selected_index = 0
    for index, (current_id, _) in enumerate(options):
        if current_id == fallback_id:
            selected_index = index
            break
    for index, (current_id, _) in enumerate(options):
        if current_id == option_id:
            selected_index = index
            break
    dropdown.set_selected(selected_index)


def _get_dropdown_selected_id(
    dropdown: Gtk.DropDown,
    options: tuple[tuple[str, str], ...],
    default_id: str,
) -> str:
    index = int(dropdown.get_selected())
    if 0 <= index < len(options):
        return options[index][0]
    return default_id


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class EditableEvent:
    device_type: str  # "keyboard" or "mouse"
    ev_type: int  # evdev EV_KEY (1)
    code: int  # e.g. 30=KEY_A, 272=BTN_LEFT
    press_t_us: int  # microseconds from macro start (press)
    release_t_us: int  # microseconds from macro start (release)


@dataclass
class EditableMove:
    mode: str  # "rel" or "abs"
    t_us: int
    x: int
    y: int
    scope: str = "all"


@dataclass
class EditableControl:
    mode: str  # wait_fixed | wait_random | exec_sync | exec_async
    t_us: int
    duration_ms: int = 0
    min_ms: int = 0
    max_ms: int = 0
    command: str = ""
    timeout_ms: int = 30000
    inhibit_mouse: bool = False


def parse_events(raw_events: list) -> tuple[list, list, list, list, list]:
    """
    Split raw event dicts into (editable_events, rel_events, passthrough_events, editable_moves).

    EV_KEY press/release pairs → EditableEvent.
    EV_REL events → movement waveform (read-only).
    Synthetic mouse-move REL events are parsed into EditableMove.
    Any unsupported/unmatched events are preserved in passthrough_events.
    EV_SYN events are discarded.
    """
    ev_key = evdev.ecodes.EV_KEY
    ev_rel = evdev.ecodes.EV_REL
    rel_x = evdev.ecodes.REL_X
    rel_y = evdev.ecodes.REL_Y

    editable: list[EditableEvent] = []
    rel_events: list[dict] = []
    passthrough_events: list[dict] = []
    editable_moves: list[EditableMove] = []
    control_events: list[EditableControl] = []
    open_presses: dict[tuple, list[int]] = {}  # (device_type, code) -> press_t_us stack
    synthetic_rel_by_move_id: dict[str, list[dict]] = {}

    for ev in raw_events:
        macro_action = str(ev.get("macro_action", "") or "")
        if macro_action:
            control_events.append(
                EditableControl(
                    mode=macro_action,
                    t_us=int(ev.get("t_us", 0)),
                    duration_ms=int(ev.get("duration_ms", 0) or 0),
                    min_ms=int(ev.get("min_ms", 0) or 0),
                    max_ms=int(ev.get("max_ms", 0) or 0),
                    command=str(ev.get("command", "") or ""),
                    timeout_ms=int(ev.get("timeout_ms", 30000) or 30000),
                    inhibit_mouse=bool(ev.get("inhibit_mouse", False)),
                )
            )
            continue

        if ev["type"] == ev_key:
            key = (ev["device_type"], ev["code"])
            if ev["value"] == 1:
                open_presses.setdefault(key, []).append(ev["t_us"])
            elif ev["value"] == 0:
                stack = open_presses.get(key)
                press_t = stack.pop() if stack else None
                if press_t is not None:
                    editable.append(
                        EditableEvent(
                            device_type=ev["device_type"],
                            ev_type=ev_key,
                            code=ev["code"],
                            press_t_us=press_t,
                            release_t_us=ev["t_us"],
                        )
                    )
                else:
                    passthrough_events.append(ev)
            else:
                passthrough_events.append(ev)
        elif ev["type"] == ev_rel:
            if ev.get("synthetic_move"):
                move_id = str(ev.get("move_id", ""))
                if move_id:
                    synthetic_rel_by_move_id.setdefault(move_id, []).append(ev)
                else:
                    passthrough_events.append(ev)
            else:
                rel_events.append(ev)
        else:
            passthrough_events.append(ev)

    for (device_type, code), presses in open_presses.items():
        for press_t in presses:
            passthrough_events.append(
                {
                    "device_type": device_type,
                    "type": ev_key,
                    "code": code,
                    "value": 1,
                    "t_us": press_t,
                }
            )

    for move_events in synthetic_rel_by_move_id.values():
        mode = str(move_events[0].get("move_mode", "rel"))
        if mode == "abs":
            target_x = None
            target_y = None
            t_us = None
            for ev in move_events:
                if ev.get("move_step") == 1:
                    if ev.get("code") == rel_x:
                        target_x = int(ev.get("value", 0))
                        t_us = int(ev.get("t_us", 0)) - 1
                    elif ev.get("code") == rel_y:
                        target_y = int(ev.get("value", 0))
                        t_us = int(ev.get("t_us", 0)) - 1
            if target_x is not None and target_y is not None and t_us is not None:
                editable_moves.append(EditableMove(mode="abs", t_us=t_us, x=target_x, y=target_y))
            else:
                passthrough_events.extend(move_events)
        else:
            rel_x = next(
                (int(e.get("value", 0)) for e in move_events if e.get("code") == rel_x), None
            )
            rel_y = next(
                (int(e.get("value", 0)) for e in move_events if e.get("code") == rel_y), None
            )
            t_us = next((int(e.get("t_us", 0)) for e in move_events), None)
            if rel_x is not None and rel_y is not None and t_us is not None:
                editable_moves.append(EditableMove(mode="rel", t_us=t_us, x=rel_x, y=rel_y))
            else:
                passthrough_events.extend(move_events)

    editable.sort(key=lambda e: e.press_t_us)
    editable_moves.sort(key=lambda m: m.t_us)
    control_events.sort(key=lambda c: c.t_us)
    passthrough_events.sort(key=lambda e: e["t_us"])
    return editable, rel_events, passthrough_events, editable_moves, control_events


def reconstruct_events(
    editable: list,
    rel_events: list,
    passthrough_events: list,
    editable_moves: list,
    control_events: list,
) -> list:
    """Reconstruct raw event list from editable, REL and passthrough events."""
    ev_key = evdev.ecodes.EV_KEY
    ev_rel = evdev.ecodes.EV_REL
    rel_x = evdev.ecodes.REL_X
    rel_y = evdev.ecodes.REL_Y
    raw: list[dict] = []

    for ev in editable:
        raw.append(
            {
                "device_type": ev.device_type,
                "type": ev_key,
                "code": ev.code,
                "value": 1,
                "t_us": ev.press_t_us,
            }
        )
        raw.append(
            {
                "device_type": ev.device_type,
                "type": ev_key,
                "code": ev.code,
                "value": 0,
                "t_us": ev.release_t_us,
            }
        )

    raw.extend(rel_events)

    for idx, move in enumerate(editable_moves):
        if move.mode == "gap":
            continue
        move_id = f"m{idx}"
        if move.mode == "abs":
            raw.extend(
                [
                    {
                        "device_type": "mouse",
                        "type": ev_rel,
                        "code": rel_x,
                        "value": -2147483648,
                        "t_us": int(move.t_us),
                        "synthetic_move": True,
                        "move_id": move_id,
                        "move_mode": "abs",
                        "move_step": 0,
                    },
                    {
                        "device_type": "mouse",
                        "type": ev_rel,
                        "code": rel_y,
                        "value": -2147483648,
                        "t_us": int(move.t_us),
                        "synthetic_move": True,
                        "move_id": move_id,
                        "move_mode": "abs",
                        "move_step": 0,
                    },
                    {
                        "device_type": "mouse",
                        "type": ev_rel,
                        "code": rel_x,
                        "value": int(move.x),
                        "t_us": int(move.t_us) + 1,
                        "synthetic_move": True,
                        "move_id": move_id,
                        "move_mode": "abs",
                        "move_step": 1,
                    },
                    {
                        "device_type": "mouse",
                        "type": ev_rel,
                        "code": rel_y,
                        "value": int(move.y),
                        "t_us": int(move.t_us) + 1,
                        "synthetic_move": True,
                        "move_id": move_id,
                        "move_mode": "abs",
                        "move_step": 1,
                    },
                ]
            )
        else:
            raw.extend(
                [
                    {
                        "device_type": "mouse",
                        "type": ev_rel,
                        "code": rel_x,
                        "value": int(move.x),
                        "t_us": int(move.t_us),
                        "synthetic_move": True,
                        "move_id": move_id,
                        "move_mode": "rel",
                    },
                    {
                        "device_type": "mouse",
                        "type": ev_rel,
                        "code": rel_y,
                        "value": int(move.y),
                        "t_us": int(move.t_us),
                        "synthetic_move": True,
                        "move_id": move_id,
                        "move_mode": "rel",
                    },
                ]
            )

    for control in control_events:
        event = {
            "device_type": "macro",
            "type": 0,
            "code": 0,
            "value": 0,
            "t_us": int(control.t_us),
            "macro_action": str(control.mode),
        }
        if control.mode == "wait_fixed":
            event["duration_ms"] = int(control.duration_ms)
        elif control.mode == "wait_random":
            event["min_ms"] = int(control.min_ms)
            event["max_ms"] = int(control.max_ms)
        elif control.mode in {"exec_sync", "exec_async"}:
            event["command"] = str(control.command)
            if control.mode == "exec_sync":
                event["timeout_ms"] = int(control.timeout_ms)
                event["inhibit_mouse"] = bool(control.inhibit_mouse)
        raw.append(event)

    raw.extend(passthrough_events)
    raw.sort(key=lambda e: e["t_us"])
    return raw


def _assign_lanes(events: list, device_type: str) -> tuple[dict, int]:
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


def _compute_macro_editor_dialog_size(parent: Gtk.Window) -> tuple[int, int]:
    width = 760
    height = 680

    try:
        parent_width = parent.get_width()
        parent_height = parent.get_height()
        if parent_width > 1:
            width = int(max(760, min(1500, parent_width * 0.9)))
        if parent_height > 1:
            height = int(max(620, min(1000, parent_height * 0.9)))
    except Exception:
        pass

    return width, height


# ---------------------------------------------------------------------------
# Timeline DrawingArea
# ---------------------------------------------------------------------------


class TimelineWidget(Gtk.DrawingArea):
    """
    Custom Gtk.DrawingArea that renders the macro timeline with three tracks:
      K  — keyboard key press/release rectangles
      M  — mouse click press/release rectangles
      ≈  — mouse movement waveform (read-only, from EV_REL events)

    The left 28px column is used for track labels and scrolls with the content.
    Horizontal scrolling is driven by the editor's custom offset model; the
    widget receives the current offset via set_scroll_offset().
    """

    LABEL_WIDTH = 28
    RULER_HEIGHT = 24
    TRACK_HEIGHT = 88  # minimum track height; expands when lanes > 2
    LANE_HEIGHT_MIN = 32  # minimum height per sub-lane
    MIN_EVENT_WIDTH = 4

    def __init__(self, editor: "MacroEditorDialog"):
        super().__init__()
        self._editor = editor
        self._pps = 200.0  # pixels per second (zoom level)
        self._scroll_offset = 0.0  # horizontal scroll offset in px
        self._selected: object | None = None
        self._hover_x: float | None = None
        self._hover_y: float | None = None
        self._context_menu_x: float | None = None

        # Lane assignment — recomputed before each draw
        self._kb_lanes: dict[int, int] = {}  # id(ev) -> lane index
        self._m_lanes: dict[int, int] = {}
        self._kb_num_lanes: int = 1
        self._m_num_lanes: int = 1

        # Drag state
        self._drag_event: EditableEvent | None = None
        self._drag_move: EditableMove | None = None
        self._drag_control: EditableControl | None = None
        self._drag_selected_obj: object | None = None
        self._drag_orig_press: int = 0
        self._drag_orig_release: int = 0
        self._in_drag: bool = False

        self.set_draw_func(self._draw, None)
        self.set_vexpand(False)
        self.set_hexpand(True)
        self._recompute_lanes()  # sets initial size request

        # Single gesture handles both click-to-select and drag-to-move.
        # Using GestureDrag alone avoids the race between GestureClick.pressed
        # and GestureDrag.drag-begin firing on the same button-press, which
        # caused the "must click first, then drag" two-step requirement.
        drag = Gtk.GestureDrag.new()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

        # Right-click context menu
        right_click = Gtk.GestureClick.new()
        right_click.set_button(3)
        right_click.connect("pressed", self._on_right_click)
        self.add_controller(right_click)

        # Ctrl+scroll for zoom
        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        motion = Gtk.EventControllerMotion.new()
        motion.connect("motion", self._on_pointer_motion)
        motion.connect("leave", self._on_pointer_leave)
        self.add_controller(motion)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_scroll_offset(self, offset: float) -> None:
        self._scroll_offset = offset
        self.queue_draw()

    # ------------------------------------------------------------------
    # Lane layout helpers
    # ------------------------------------------------------------------

    def _get_track_h(self, num_lanes: int) -> int:
        """Compute track height for a given number of lanes."""
        return max(self.TRACK_HEIGHT, num_lanes * self.LANE_HEIGHT_MIN + 8)

    @property
    def _kb_track_h(self) -> int:
        return self._get_track_h(self._kb_num_lanes)

    @property
    def _m_track_h(self) -> int:
        return self._get_track_h(self._m_num_lanes)

    @property
    def _kb_y(self) -> int:
        return self.RULER_HEIGHT

    @property
    def _m_y(self) -> int:
        return self.RULER_HEIGHT + self._kb_track_h

    @property
    def _wave_y(self) -> int:
        return self.RULER_HEIGHT + self._kb_track_h + self._m_track_h

    @property
    def _total_height(self) -> int:
        return self.RULER_HEIGHT + self._kb_track_h + self._m_track_h + self.TRACK_HEIGHT

    def _recompute_lanes(self) -> None:
        """Recompute lane assignments from current events and update size request."""
        events = self._editor._events
        self._kb_lanes, self._kb_num_lanes = _assign_lanes(events, "keyboard")
        self._m_lanes, self._m_num_lanes = _assign_lanes(events, "mouse")
        self.set_size_request(-1, self._total_height)

    # ------------------------------------------------------------------
    # Coordinate helpers
    # ------------------------------------------------------------------

    def _time_to_x(self, t_us: int) -> float:
        """Convert a time in microseconds to an x coordinate in the drawing area."""
        return self.LABEL_WIDTH + t_us / 1e6 * self._pps - self._scroll_offset

    def _x_to_time_us(self, x: float) -> int:
        """Convert a drawing-area x coordinate to microseconds."""
        return int((x - self.LABEL_WIDTH + self._scroll_offset) / self._pps * 1e6)

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def _draw(self, area, cr, width, height, user_data) -> None:
        self._recompute_lanes()
        events = self._editor._events
        rel_events = self._editor._rel_events
        duration_us = self._editor._duration_us

        # Dark background
        cr.set_source_rgb(0.12, 0.12, 0.12)
        cr.paint()

        self._draw_ruler(cr, width, duration_us)
        self._draw_keyboard_track(cr, width, events)
        self._draw_mouse_track(cr, width, events)
        self._draw_movement_track(cr, width, rel_events)
        self._draw_passthrough_markers(cr, width)
        self._draw_synthetic_move_markers(cr, width)
        self._draw_control_markers(cr, width)
        self._draw_labels(cr, height)
        self._draw_pointer_guide(cr, width, height)

        # Horizontal separator lines between tracks
        cr.set_source_rgba(0.30, 0.30, 0.30, 0.8)
        cr.set_line_width(1)
        for y in [self._kb_y, self._m_y, self._wave_y]:
            cr.move_to(0, y + 0.5)
            cr.line_to(width, y + 0.5)
            cr.stroke()

    def _draw_ruler(self, cr, width: int, duration_us: int) -> None:
        cr.set_source_rgb(0.17, 0.17, 0.17)
        cr.rectangle(0, 0, width, self.RULER_HEIGHT)
        cr.fill()

        if duration_us <= 0:
            return

        visible_s = max((width - self.LABEL_WIDTH) / self._pps, 0.001)
        intervals = [
            0.001,
            0.005,
            0.01,
            0.025,
            0.05,
            0.1,
            0.25,
            0.5,
            1.0,
            2.0,
            5.0,
            10.0,
            30.0,
        ]
        tick_interval = intervals[-1]
        for iv in intervals:
            if visible_s / iv <= 12:
                tick_interval = iv
                break

        duration_s = duration_us / 1e6
        cr.select_font_face("monospace", 0, 0)
        cr.set_font_size(9)

        t = 0.0
        while t <= duration_s + tick_interval:
            x = self._time_to_x(int(t * 1e6))
            if self.LABEL_WIDTH - 1 <= x <= width:
                cr.set_source_rgba(0.55, 0.55, 0.55, 1.0)
                cr.set_line_width(1)
                cr.move_to(x + 0.5, self.RULER_HEIGHT - 6)
                cr.line_to(x + 0.5, self.RULER_HEIGHT)
                cr.stroke()

                if t >= 1.0:
                    label = f"{t:.1f}s"
                elif t >= 0.01:
                    label = f"{int(t * 1000)}ms"
                elif t > 0:
                    label = f"{t * 1000:.1f}ms"
                else:
                    label = "0"

                extents = cr.text_extents(label)
                lx = x - extents[2] / 2
                if lx >= self.LABEL_WIDTH:
                    cr.set_source_rgba(0.60, 0.60, 0.60, 1.0)
                    cr.move_to(lx, self.RULER_HEIGHT - 8)
                    cr.show_text(label)

            t = round(t + tick_interval, 6)

    def _draw_event_rect(
        self,
        cr,
        ev: EditableEvent,
        y_top: float,
        track_h: float,
        fill_rgba,
        border_rgba,
        sel_fill_rgba,
        sel_border_rgba,
        width: int,
    ) -> None:
        x1 = self._time_to_x(ev.press_t_us)
        x2 = self._time_to_x(ev.release_t_us)
        w = max(x2 - x1, self.MIN_EVENT_WIDTH)

        if x1 > width or x1 + w < self.LABEL_WIDTH:
            return  # Completely off screen

        is_sel = ev is self._selected
        margin = max(1, min(4, int(track_h * 0.10)))
        rect_y = y_top + margin
        rect_h = track_h - margin * 2

        # Fill
        cr.set_source_rgba(*(sel_fill_rgba if is_sel else fill_rgba))
        cr.rectangle(x1, rect_y, w, rect_h)
        cr.fill()

        # Border
        cr.set_source_rgba(*(sel_border_rgba if is_sel else border_rgba))
        cr.set_line_width(2.0 if is_sel else 1.0)
        cr.rectangle(x1 + 0.5, rect_y + 0.5, max(w - 1, 0), max(rect_h - 1, 0))
        cr.stroke()

        # Label (only if wide and tall enough)
        if w > 28 and rect_h > 10:
            name = _get_key_name(ev.code)
            cr.select_font_face("sans", 0, 0)
            cr.set_font_size(min(9.0, rect_h * 0.65))
            extents = cr.text_extents(name)
            # Center text in the rect (Cairo baseline: ty - height/2 - y_bearing)
            tx = x1 + (w - extents[2]) / 2 - extents[0]
            ty = rect_y + rect_h / 2 - extents[3] / 2 - extents[1]
            if tx >= self.LABEL_WIDTH:
                cr.set_source_rgba(1.0, 1.0, 1.0, 0.9)
                cr.move_to(tx, ty)
                cr.show_text(name)

    def _draw_track_with_lanes(
        self,
        cr,
        width: int,
        events: list,
        device_type: str,
        y_top: int,
        track_h: int,
        num_lanes: int,
        lanes_dict: dict,
        bg_rgb,
        fill_rgba,
        border_rgba,
        sel_fill_rgba,
        sel_border_rgba,
    ) -> None:
        """Draw a track with sub-lane support for overlapping events."""
        cr.set_source_rgb(*bg_rgb)
        cr.rectangle(self.LABEL_WIDTH, y_top, width - self.LABEL_WIDTH, track_h)
        cr.fill()

        lane_h = track_h / num_lanes

        # Draw subtle lane dividers when multiple lanes are active
        if num_lanes > 1:
            cr.set_source_rgba(0.30, 0.30, 0.36, 0.45)
            cr.set_line_width(0.5)
            for lane_i in range(1, num_lanes):
                ly = y_top + lane_i * lane_h + 0.25
                cr.move_to(self.LABEL_WIDTH, ly)
                cr.line_to(width, ly)
                cr.stroke()

        for ev in events:
            if ev.device_type != device_type:
                continue
            lane = lanes_dict.get(id(ev), 0)
            ev_y = y_top + lane * lane_h
            self._draw_event_rect(
                cr,
                ev,
                ev_y,
                lane_h,
                fill_rgba=fill_rgba,
                border_rgba=border_rgba,
                sel_fill_rgba=sel_fill_rgba,
                sel_border_rgba=sel_border_rgba,
                width=width,
            )

    def _draw_keyboard_track(self, cr, width: int, events: list) -> None:
        self._draw_track_with_lanes(
            cr,
            width,
            events,
            device_type="keyboard",
            y_top=self._kb_y,
            track_h=self._kb_track_h,
            num_lanes=self._kb_num_lanes,
            lanes_dict=self._kb_lanes,
            bg_rgb=(0.12, 0.12, 0.15),
            fill_rgba=(0.22, 0.40, 0.80, 0.75),
            border_rgba=(0.35, 0.55, 0.90, 0.9),
            sel_fill_rgba=(0.35, 0.60, 1.00, 0.92),
            sel_border_rgba=(0.60, 0.82, 1.00, 1.0),
        )

    def _draw_mouse_track(self, cr, width: int, events: list) -> None:
        self._draw_track_with_lanes(
            cr,
            width,
            events,
            device_type="mouse",
            y_top=self._m_y,
            track_h=self._m_track_h,
            num_lanes=self._m_num_lanes,
            lanes_dict=self._m_lanes,
            bg_rgb=(0.15, 0.12, 0.09),
            fill_rgba=(0.78, 0.50, 0.08, 0.75),
            border_rgba=(0.90, 0.62, 0.20, 0.9),
            sel_fill_rgba=(1.00, 0.70, 0.20, 0.92),
            sel_border_rgba=(1.00, 0.85, 0.40, 1.0),
        )

    def _draw_movement_track(self, cr, width: int, rel_events: list) -> None:
        y_top = self._wave_y
        track_h = self.TRACK_HEIGHT

        cr.set_source_rgb(0.11, 0.13, 0.11)
        cr.rectangle(self.LABEL_WIDTH, y_top, width - self.LABEL_WIDTH, track_h)
        cr.fill()

        if not rel_events:
            return

        draw_width = max(width - self.LABEL_WIDTH, 1)
        bins = [0.0] * draw_width

        for ev in rel_events:
            x = self._time_to_x(ev["t_us"])
            idx = int(x - self.LABEL_WIDTH)
            if 0 <= idx < draw_width:
                bins[idx] += abs(ev.get("value", 0))

        max_val = max(bins) if bins else 0
        if max_val == 0:
            return

        bar_h = track_h - 8
        cr.set_source_rgba(0.45, 0.60, 0.45, 0.60)
        for i, val in enumerate(bins):
            if val == 0:
                continue
            h = val / max_val * bar_h
            x = self.LABEL_WIDTH + i
            y = y_top + track_h - 4 - h
            cr.rectangle(x, y, 1, h)
        cr.fill()

    def _draw_passthrough_markers(self, cr, width: int) -> None:
        self._draw_passthrough_markers_for_track(
            cr,
            width,
            track="keyboard",
            y_top=self._kb_y,
            track_h=self._kb_track_h,
        )
        self._draw_passthrough_markers_for_track(
            cr,
            width,
            track="mouse",
            y_top=self._m_y,
            track_h=self._m_track_h,
        )
        self._draw_passthrough_markers_for_track(
            cr,
            width,
            track="movement",
            y_top=self._wave_y,
            track_h=self.TRACK_HEIGHT,
        )

    def _draw_passthrough_markers_for_track(
        self,
        cr,
        width: int,
        *,
        track: str,
        y_top: int,
        track_h: int,
    ) -> None:
        for ev, x, y, size in self._get_passthrough_marker_layouts(track, width, y_top, track_h):
            cr.set_source_rgba(1.0, 0.2, 0.2, 0.92)
            cr.move_to(x, y - size)
            cr.line_to(x + size, y)
            cr.line_to(x, y + size)
            cr.line_to(x - size, y)
            cr.close_path()
            cr.fill()

            cr.set_source_rgba(1.0, 0.95, 0.95, 0.9)
            cr.set_line_width(1.0)
            cr.move_to(x, y - size)
            cr.line_to(x + size, y)
            cr.line_to(x, y + size)
            cr.line_to(x - size, y)
            cr.close_path()
            cr.stroke()

            if ev is self._selected:
                cr.set_source_rgba(1.0, 1.0, 1.0, 0.98)
                cr.set_line_width(1.2)
                cr.arc(x, y, size + 3.0, 0, 6.283185307179586)
                cr.stroke()

    def _get_passthrough_marker_layouts(
        self,
        track: str,
        width: int,
        y_top: int,
        track_h: int,
    ) -> list[tuple[dict, float, float, float]]:
        unknown_events = [
            ev for ev in self._editor._passthrough_events if _passthrough_track(ev) == track
        ]
        if not unknown_events:
            return []

        stack_per_x: dict[int, int] = {}
        base_y = y_top + track_h - 10
        max_stack = 6
        layouts: list[tuple[dict, float, float, float]] = []

        for ev in unknown_events:
            x = self._time_to_x(int(ev.get("t_us", 0)))
            if x < self.LABEL_WIDTH - 4 or x > width + 4:
                continue

            x_px = int(x)
            stack_idx = stack_per_x.get(x_px, 0)
            stack_per_x[x_px] = min(stack_idx + 1, max_stack)

            y = base_y - (stack_idx % max_stack) * 5
            is_press = int(ev.get("value", -1)) == 1
            size = 4.2 if is_press else 3.2
            layouts.append((ev, x, y, size))

        return layouts

    def _draw_labels(self, cr, height: int) -> None:
        # Background column
        cr.set_source_rgb(0.09, 0.09, 0.09)
        cr.rectangle(0, 0, self.LABEL_WIDTH, height)
        cr.fill()

        # Right border of label column
        cr.set_source_rgba(0.30, 0.30, 0.30, 0.8)
        cr.set_line_width(1)
        cr.move_to(self.LABEL_WIDTH + 0.5, 0)
        cr.line_to(self.LABEL_WIDTH + 0.5, height)
        cr.stroke()

        cr.select_font_face("monospace", 0, 0)
        cr.set_font_size(11)
        cr.set_source_rgba(0.62, 0.62, 0.62, 1.0)

        labels = [
            (self._kb_y + self._kb_track_h * 0.5, "K"),
            (self._m_y + self._m_track_h * 0.5, "M"),
            (self._wave_y + self.TRACK_HEIGHT * 0.5, "≈"),
        ]
        for y_center, label in labels:
            extents = cr.text_extents(label)
            x = (self.LABEL_WIDTH - extents[2]) / 2 - extents[0]
            y = y_center - extents[3] / 2 - extents[1]
            cr.move_to(x, y)
            cr.show_text(label)

    def _draw_synthetic_move_markers(self, cr, width: int) -> None:
        moves = self._editor._synthetic_moves
        if not moves:
            return

        base_y = self._wave_y + 14
        cr.select_font_face("sans", 0, 0)
        cr.set_font_size(9)

        for move in moves:
            x = self._time_to_x(move.t_us)
            if x < self.LABEL_WIDTH - 4 or x > width + 4:
                continue

            if move.mode == "abs":
                cr.set_source_rgba(0.30, 0.90, 1.00, 0.95)
            elif move.mode == "gap":
                cr.set_source_rgba(0.85, 0.45, 1.00, 0.95)
            else:
                cr.set_source_rgba(1.00, 0.80, 0.20, 0.95)

            radius = 4.4
            cr.arc(x, base_y, radius, 0, 6.283185307179586)
            cr.fill()

            if move is self._selected:
                cr.set_source_rgba(1.0, 1.0, 1.0, 0.95)
                cr.set_line_width(1.2)
                cr.arc(x, base_y, radius + 2.0, 0, 6.283185307179586)
                cr.stroke()

            if move.mode == "abs":
                label = "A"
            elif move.mode == "gap":
                label = "G"
            else:
                label = "R"
            extents = cr.text_extents(label)
            cr.set_source_rgba(0.05, 0.05, 0.05, 1.0)
            cr.move_to(x - extents[2] / 2 - extents[0], base_y + extents[3] / 2)
            cr.show_text(label)

    def _draw_control_markers(self, cr, width: int) -> None:
        controls = self._editor._control_events
        if not controls:
            return

        base_y = self._wave_y + self.TRACK_HEIGHT - 14
        cr.select_font_face("sans", 0, 0)
        cr.set_font_size(9)

        for control in controls:
            x = self._time_to_x(control.t_us)
            if x < self.LABEL_WIDTH - 4 or x > width + 4:
                continue

            if control.mode == "wait_fixed":
                cr.set_source_rgba(0.25, 0.85, 0.95, 0.95)
                label = "WF"
            elif control.mode == "wait_random":
                cr.set_source_rgba(0.35, 0.95, 0.45, 0.95)
                label = "WR"
            elif control.mode == "exec_sync":
                cr.set_source_rgba(1.00, 0.62, 0.12, 0.95)
                label = "XS"
            else:
                cr.set_source_rgba(0.95, 0.50, 0.15, 0.95)
                label = "XA"

            size = 7.0
            cr.move_to(x, base_y - size)
            cr.line_to(x + size, base_y)
            cr.line_to(x, base_y + size)
            cr.line_to(x - size, base_y)
            cr.close_path()
            cr.fill()

            extents = cr.text_extents(label)
            cr.set_source_rgba(0.94, 0.94, 0.94, 0.95)
            cr.move_to(x - extents[2] / 2 - extents[0], base_y - 11)
            cr.show_text(label)

    def _draw_pointer_guide(self, cr, width: int, height: int) -> None:
        if self._context_menu_x is not None:
            self._draw_vertical_guide(cr, width, height, float(self._context_menu_x))

        if self._hover_x is None or self._hover_y is None:
            return

        x = float(self._hover_x)
        y = float(self._hover_y)
        if y < self.RULER_HEIGHT or y > height:
            return

        cr.set_source_rgba(1.0, 1.0, 1.0, 0.35)
        cr.set_line_width(1.0)
        cr.move_to(self.LABEL_WIDTH, y + 0.5)
        cr.line_to(width, y + 0.5)
        cr.stroke()

        self._draw_vertical_guide(cr, width, height, x)

        t_us = max(0, self._x_to_time_us(x))
        label = _format_time_us(t_us)

        cr.select_font_face("monospace", 0, 0)
        cr.set_font_size(10)
        extents = cr.text_extents(label)
        pad_x = 6.0
        pad_y = 3.0
        bubble_w = extents[2] + pad_x * 2
        bubble_h = extents[3] + pad_y * 2

        bx = min(max(x + 14.0, self.LABEL_WIDTH + 4.0), width - bubble_w - 4.0)
        by = min(max(y - bubble_h - 12.0, self.RULER_HEIGHT + 2.0), height - bubble_h - 2.0)

        cr.set_source_rgba(0.08, 0.08, 0.08, 0.90)
        cr.rectangle(bx, by, bubble_w, bubble_h)
        cr.fill()

        cr.set_source_rgba(0.85, 0.85, 0.85, 0.95)
        cr.rectangle(bx + 0.5, by + 0.5, bubble_w - 1.0, bubble_h - 1.0)
        cr.stroke()

        cr.set_source_rgba(1.0, 1.0, 1.0, 0.95)
        tx = bx + pad_x - extents[0]
        ty = by + pad_y - extents[1]
        cr.move_to(tx, ty)
        cr.show_text(label)

    def _draw_vertical_guide(self, cr, width: int, height: int, x: float) -> None:
        if not (self.LABEL_WIDTH <= x <= width):
            return
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.35)
        cr.set_line_width(1.0)
        cr.move_to(x + 0.5, self.RULER_HEIGHT)
        cr.line_to(x + 0.5, height)
        cr.stroke()

    # ------------------------------------------------------------------
    # Hit testing
    # ------------------------------------------------------------------

    def _get_track_at_y(self, y: float) -> str | None:
        """Return 'keyboard', 'mouse', 'movement', or None (ruler)."""
        if y < self.RULER_HEIGHT:
            return None
        if y < self._m_y:
            return "keyboard"
        if y < self._wave_y:
            return "mouse"
        if y < self._wave_y + self.TRACK_HEIGHT:
            return "movement"
        return None

    def _hit_test_move(self, x: float, y: float) -> EditableMove | None:
        base_y = self._wave_y + 14
        for move in reversed(self._editor._synthetic_moves):
            mx = self._time_to_x(move.t_us)
            dx = x - mx
            dy = y - base_y
            if dx * dx + dy * dy <= 9.0 * 9.0:
                return move
        return None

    def _hit_test_control(self, x: float, y: float) -> EditableControl | None:
        base_y = self._wave_y + self.TRACK_HEIGHT - 14
        for control in reversed(self._editor._control_events):
            cx = self._time_to_x(control.t_us)
            dx = x - cx
            dy = y - base_y
            if dx * dx + dy * dy <= 14.0 * 14.0:
                return control
        return None

    def _hit_test_passthrough(self, track: str, x: float, y: float):
        if track == "keyboard":
            y_top = self._kb_y
            track_h = self._kb_track_h
        elif track == "mouse":
            y_top = self._m_y
            track_h = self._m_track_h
        else:
            y_top = self._wave_y
            track_h = self.TRACK_HEIGHT

        width = self.get_allocated_width()
        for ev, mx, my, size in reversed(
            self._get_passthrough_marker_layouts(
                track,
                max(width, self.LABEL_WIDTH),
                y_top,
                track_h,
            )
        ):
            dx = x - mx
            dy = y - my
            radius = size + 4.0
            if dx * dx + dy * dy <= radius * radius:
                return ev
        return None

    def _hit_test(self, x: float, y: float):
        track = self._get_track_at_y(y)
        if track == "keyboard":
            device_type = "keyboard"
            track_y = self._kb_y
            track_h = self._kb_track_h
            num_lanes = self._kb_num_lanes
            lanes_dict = self._kb_lanes
        elif track == "mouse":
            device_type = "mouse"
            track_y = self._m_y
            track_h = self._m_track_h
            num_lanes = self._m_num_lanes
            lanes_dict = self._m_lanes
        elif track == "movement":
            control = self._hit_test_control(x, y)
            if control is not None:
                return control
            move = self._hit_test_move(x, y)
            if move is not None:
                return move
            return self._hit_test_passthrough("movement", x, y)
        else:
            return None

        lane_h = track_h / num_lanes
        clicked_lane = int((y - track_y) / lane_h)
        clicked_lane = max(0, min(clicked_lane, num_lanes - 1))

        for ev in reversed(self._editor._events):
            if ev.device_type != device_type:
                continue
            if lanes_dict.get(id(ev), 0) != clicked_lane:
                continue
            x1 = self._time_to_x(ev.press_t_us)
            x2 = self._time_to_x(ev.release_t_us)
            w = max(x2 - x1, self.MIN_EVENT_WIDTH)
            if x1 <= x <= x1 + w:
                return ev
        return self._hit_test_passthrough(track, x, y)

    # ------------------------------------------------------------------
    # Gesture handlers
    # ------------------------------------------------------------------

    def _on_drag_begin(self, gesture, start_x, start_y) -> None:
        # Always hit-test on press; actual movement only starts after threshold.
        hit = self._hit_test(start_x, start_y)
        self._drag_selected_obj = hit
        self._drag_event = hit if isinstance(hit, EditableEvent) else None
        self._drag_move = hit if isinstance(hit, EditableMove) else None
        self._drag_control = hit if isinstance(hit, EditableControl) else None
        if self._drag_event:
            ev = self._drag_event
            self._drag_orig_press = ev.press_t_us
            self._drag_orig_release = ev.release_t_us
        elif self._drag_move:
            self._drag_orig_press = self._drag_move.t_us
            self._drag_orig_release = self._drag_move.t_us
        elif self._drag_control:
            self._drag_orig_press = self._drag_control.t_us
            self._drag_orig_release = self._drag_control.t_us
        self._in_drag = False

    def _on_drag_update(self, gesture, offset_x, offset_y) -> None:
        if (
            self._drag_event is None and self._drag_move is None and self._drag_control is None
        ) or self._editor._drag_locked:
            return
        if not self._in_drag:
            if abs(offset_x) < 4:
                return
            self._in_drag = True

        delta_us = int(offset_x / self._pps * 1e6)
        if self._drag_event is not None:
            new_press = self._drag_orig_press + delta_us
            duration = self._drag_orig_release - self._drag_orig_press
            max_t = max(self._editor._duration_us - duration, 0)
            new_press = max(0, min(new_press, max_t))
            new_release = new_press + duration

            self._drag_event.press_t_us = new_press
            self._drag_event.release_t_us = new_release
            self._editor._on_selection_changed(self._drag_event)
        elif self._drag_move is not None:
            new_t = max(0, self._drag_orig_press + delta_us)
            self._drag_move.t_us = new_t
            self._editor._on_selection_changed(self._drag_move)
        elif self._drag_control is not None:
            new_t = max(0, self._drag_orig_press + delta_us)
            self._drag_control.t_us = new_t
            self._editor._control_events.sort(key=lambda c: c.t_us)
            self._editor._on_selection_changed(self._drag_control)
        self.queue_draw()

    def _on_drag_end(self, gesture, offset_x, offset_y) -> None:
        if self._in_drag and self._drag_event:
            # Commit the move: re-sort and refresh stats.
            self._editor._events.sort(key=lambda e: e.press_t_us)
            self._editor._update_stats()
        elif self._in_drag and self._drag_move:
            if self._drag_move.mode == "gap":
                self._editor._move_gap_note(self._drag_move, self._drag_orig_press)
            else:
                self._editor._synthetic_moves.sort(key=lambda m: m.t_us)
                self._editor._update_stats()
        elif self._in_drag and self._drag_control:
            self._editor._refresh_after_timing_edit()
        elif not self._in_drag:
            selected_obj = self._drag_selected_obj
            if selected_obj is not self._selected:
                self._selected = selected_obj
                self._editor._on_selection_changed(selected_obj)
        self._drag_event = None
        self._drag_move = None
        self._drag_control = None
        self._drag_selected_obj = None
        self._in_drag = False
        self.queue_draw()

    def _on_pointer_motion(self, controller, x, y) -> None:
        self._hover_x = x
        self._hover_y = y
        self.queue_draw()

    def _on_pointer_leave(self, controller) -> None:
        self._hover_x = None
        self._hover_y = None
        self.queue_draw()

    def _on_scroll(self, controller, dx, dy) -> bool:
        try:
            state = controller.get_current_event_state()
        except Exception:
            state = 0
        zoom_modifier = bool(state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK))
        if zoom_modifier:
            step = dy if abs(dy) > 0 else dx
            if abs(step) > 0:
                zoom_factor = (1.0 / 1.12) if step > 0 else 1.12
                self._editor._zoom_timeline(zoom_factor)
                return True

        step = dx if abs(dx) > 0 else dy
        if abs(step) > 0:
            self._editor._scroll_timeline_by(float(step) * 48.0)
            return True
        return False

    def _on_right_click(self, gesture, n_press, x, y) -> None:
        ev = self._hit_test(x, y)
        track = self._get_track_at_y(y)
        t_us = max(0, self._x_to_time_us(x))
        t_label = _format_time_us(t_us)

        self._context_menu_x = x
        self.queue_draw()

        popover = Gtk.Popover()

        def _on_popover_closed(_popover) -> None:
            self._context_menu_x = None
            self.queue_draw()

        popover.connect("closed", _on_popover_closed)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        box.set_margin_start(4)
        box.set_margin_end(4)

        if track == "keyboard":
            add_btn = Gtk.Button(label=f"Add Keystroke at {t_label}")
            add_btn.add_css_class("flat")

            def _add_key(_b, _t=t_us, _p=popover):
                _p.popdown()
                self._editor._present_add_key_dialog(default_t_us=_t)

            add_btn.connect("clicked", _add_key)
            box.append(add_btn)

        elif track == "mouse":
            add_btn = Gtk.Button(label=f"Add Mouse Click at {t_label}")
            add_btn.add_css_class("flat")

            def _add_click(_b, _t=t_us, _p=popover):
                _p.popdown()
                rect = Gdk.Rectangle()
                rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
                self._editor._show_add_click_popover(self, default_t_us=_t, pointing_to=rect)

            add_btn.connect("clicked", _add_click)
            box.append(add_btn)

        elif track == "movement":
            add_rel_btn = Gtk.Button(label=f"Add Move REL at {t_label}")
            add_rel_btn.add_css_class("flat")

            def _add_rel(_b, _t=t_us, _p=popover):
                _p.popdown()
                rect = Gdk.Rectangle()
                rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
                self._editor._show_add_move_popover(
                    self, mode="rel", default_t_us=_t, pointing_to=rect
                )

            add_rel_btn.connect("clicked", _add_rel)
            box.append(add_rel_btn)

            add_abs_btn = Gtk.Button(label=f"Add Move ABS at {t_label}")
            add_abs_btn.add_css_class("flat")

            def _add_abs(_b, _t=t_us, _p=popover):
                _p.popdown()
                rect = Gdk.Rectangle()
                rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
                self._editor._show_add_move_popover(
                    self, mode="abs", default_t_us=_t, pointing_to=rect
                )

            add_abs_btn.connect("clicked", _add_abs)
            box.append(add_abs_btn)

        if track in ("keyboard", "mouse", "movement"):
            if box.get_first_child():
                box.append(Gtk.Separator())

            gap_scope = "all"
            if track == "keyboard":
                gap_scope = "keyboard"
            elif track == "mouse":
                gap_scope = "mouse"
            elif track == "movement":
                gap_scope = "movement"

            gap_btn = Gtk.Button(label=f"Insert Gap Note at {t_label}")
            gap_btn.add_css_class("flat")

            def _insert_gap(_b, _t=t_us, _scope=gap_scope, _p=popover):
                _p.popdown()
                rect = Gdk.Rectangle()
                rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
                self._editor._show_insert_gap_popover(
                    self,
                    default_t_us=_t,
                    default_scope=_scope,
                    pointing_to=rect,
                )

            gap_btn.connect("clicked", _insert_gap)
            box.append(gap_btn)

            wait_random_btn = Gtk.Button(label=f"Insert Wait (random) at {t_label}")
            wait_random_btn.add_css_class("flat")

            def _insert_wait_random(_b, _t=t_us, _p=popover):
                _p.popdown()
                rect = Gdk.Rectangle()
                rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
                self._editor._show_add_control_popover(
                    self, "wait_random", default_t_us=_t, pointing_to=rect
                )

            wait_random_btn.connect("clicked", _insert_wait_random)
            box.append(wait_random_btn)

            exec_sync_btn = Gtk.Button(label=f"Insert Exec Sync at {t_label}")
            exec_sync_btn.add_css_class("flat")

            def _insert_exec_sync(_b, _t=t_us, _p=popover):
                _p.popdown()
                control = EditableControl(
                    mode="exec_sync",
                    t_us=int(_t),
                    command="",
                    timeout_ms=min(30000, self._editor._macro_exec_timeout_max_ms),
                    inhibit_mouse=False,
                )
                self._editor._insert_control_event(control)

            exec_sync_btn.connect("clicked", _insert_exec_sync)
            box.append(exec_sync_btn)

            exec_async_btn = Gtk.Button(label=f"Insert Exec Async at {t_label}")
            exec_async_btn.add_css_class("flat")

            def _insert_exec_async(_b, _t=t_us, _p=popover):
                _p.popdown()
                control = EditableControl(mode="exec_async", t_us=int(_t), command="")
                self._editor._insert_control_event(control)

            exec_async_btn.connect("clicked", _insert_exec_async)
            box.append(exec_async_btn)

        if box.get_first_child():
            box.append(Gtk.Separator())

        start_btn = Gtk.Button(label=f"Set Startpoint at {t_label}")
        start_btn.add_css_class("flat")

        def _set_start(_b, _t=t_us, _p=popover):
            _p.popdown()
            self._editor._set_startpoint(_t)

        start_btn.connect("clicked", _set_start)
        box.append(start_btn)

        end_btn = Gtk.Button(label=f"Set Endpoint at {t_label}")
        end_btn.add_css_class("flat")

        def _set_end(_b, _t=t_us, _p=popover):
            _p.popdown()
            self._editor._set_endpoint(_t)

        end_btn.connect("clicked", _set_end)
        box.append(end_btn)

        if isinstance(ev, EditableEvent) and track in ("keyboard", "mouse"):
            if box.get_first_child():
                box.append(Gtk.Separator())
            del_btn = Gtk.Button(label=f"Delete {_get_key_name(ev.code)}")
            del_btn.add_css_class("flat")
            del_btn.add_css_class("destructive-action")

            def _delete(_b, _ev=ev, _p=popover):
                _p.popdown()
                self._editor._delete_event(_ev)

            del_btn.connect("clicked", _delete)
            box.append(del_btn)

        if isinstance(ev, dict):
            if box.get_first_child():
                box.append(Gtk.Separator())
            title, _detail = _describe_passthrough_event(ev)
            del_btn = Gtk.Button(label=f"Delete Raw Event ({title})")
            del_btn.add_css_class("flat")
            del_btn.add_css_class("destructive-action")

            def _delete_raw(_b, _ev=ev, _p=popover):
                _p.popdown()
                self._editor._delete_event(_ev)

            del_btn.connect("clicked", _delete_raw)
            box.append(del_btn)

        if isinstance(ev, EditableMove):
            if box.get_first_child():
                box.append(Gtk.Separator())
            if ev.mode == "gap":
                label = f"Delete Gap Note ({ev.x}ms, {ev.scope})"
            else:
                label = f"Delete Move {ev.mode.upper()} ({ev.x}, {ev.y})"
            del_move_btn = Gtk.Button(label=label)
            del_move_btn.add_css_class("flat")
            del_move_btn.add_css_class("destructive-action")

            def _delete_move(_b, _ev=ev, _p=popover):
                _p.popdown()
                self._editor._delete_event(_ev)

            del_move_btn.connect("clicked", _delete_move)
            box.append(del_move_btn)

        if isinstance(ev, EditableControl):
            if box.get_first_child():
                box.append(Gtk.Separator())
            del_control_btn = Gtk.Button(label=f"Delete {ev.mode.replace('_', ' ').title()}")
            del_control_btn.add_css_class("flat")
            del_control_btn.add_css_class("destructive-action")

            def _delete_control(_b, _ev=ev, _p=popover):
                _p.popdown()
                self._editor._delete_event(_ev)

            del_control_btn.connect("clicked", _delete_control)
            box.append(del_control_btn)

        if not box.get_first_child():
            return

        popover.set_child(box)
        popover.set_parent(self)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.popup()


# ---------------------------------------------------------------------------
# Main editor dialog
# ---------------------------------------------------------------------------


class MacroEditorDialog(Adw.Dialog):
    """
    Full timeline editor for a recorded macro.

    Loads a macro through the session API, presents an interactive Cairo
    timeline with keyboard, mouse-click, and mouse-movement tracks, and saves
    changes through session API calls.
    """

    def __init__(self, parent: Gtk.Window, macro_name: str):
        dialog_width, dialog_height = _compute_macro_editor_dialog_size(parent)
        super().__init__(
            title=f"Edit macro ({macro_name})",
            content_width=dialog_width,
            content_height=dialog_height,
        )
        self._parent = parent
        self._macro_name = macro_name
        self._macro_data: dict = {}
        self._events: list[EditableEvent] = []
        self._rel_events: list[dict] = []
        self._passthrough_events: list[dict] = []
        self._synthetic_moves: list[EditableMove] = []
        self._control_events: list[EditableControl] = []
        self._duration_us: int = 0
        self._macro_loop_mode: str = "none"
        self._macro_loop_count: int = 1
        self._macro_move_to_start: bool = False
        self._macro_start_x: int = 0
        self._macro_start_y: int = 0
        self._macro_block_mouse_movement: bool = False
        self._capture_delay_seconds: float = 2.0
        self._capture_timeout_id: int = 0
        self._capture_pending: bool = False
        self._slurp_capture = get_slurp_capture()
        self._slurp_capture.set_compositor(detect_compositor_sync())
        self._slurp_available = self._slurp_capture.available
        self._timing_scale_spin: Gtk.SpinButton | None = None
        self._timing_min_gap_spin: Gtk.SpinButton | None = None
        self._timing_max_gap_spin: Gtk.SpinButton | None = None
        self._timing_extend_ms_spin: Gtk.SpinButton | None = None
        self._insert_gap_at_spin: Gtk.SpinButton | None = None
        self._insert_gap_ms_spin: Gtk.SpinButton | None = None
        self._insert_gap_scope_combo: Gtk.DropDown | None = None
        self._timeline_scroll_x: float = 0.0
        self._timeline_scroll_max: float = 0.0
        self._timeline_scroll_adj: Gtk.Adjustment | None = None
        self._auto_zoom_enabled: bool = True
        self._auto_zoom_min_pps: float = 90.0
        self._zoom_min_pps: float = 50.0
        self._zoom_max_pps: float = 4000.0
        self._macro_exec_timeout_max_ms: int = 30000
        self._initial_macro_data: dict = {}
        self._macro_exists = False

        # Suppress property-panel spin callbacks during programmatic updates
        self._updating_props = False

        # Drag-to-move is locked by default; user must explicitly unlock it.
        self._drag_locked: bool = True

        self._install_css()
        self._build_ui()
        self._load_initial_state_async()

    def _install_css(self) -> None:
        css = """
        .macro-editor-outline {
            border: 1px solid #000;
            border-radius: 6px;
        }
        """
        provider = Gtk.CssProvider()
        provider.load_from_string(css)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_initial_state_async(self) -> None:
        run_gui_task(
            self._load_initial_state,
            self._on_initial_state_loaded,
        )

    def _load_initial_state(self) -> dict[str, object]:
        timeout_max = 30000
        try:
            status = session_request({"command": "get_status"}) or {}
            timeout_max = int(status.get("macro_exec_timeout_max_ms", 30000) or 30000)
        except Exception:
            timeout_max = 30000

        macro: dict | None = None
        try:
            response = session_request({"command": "get_macro", "name": self._macro_name}) or {}
            loaded_macro = response.get("macro")
            if response.get("status") == "ok" and isinstance(loaded_macro, dict):
                macro = loaded_macro
        except Exception:
            macro = None

        return {
            "timeout_max": max(1, timeout_max),
            "macro": macro,
        }

    def _on_initial_state_loaded(self, result: dict[str, object] | None) -> bool:
        payload = result or {}
        timeout_max_raw = payload.get("timeout_max", 30000)
        timeout_max = timeout_max_raw if isinstance(timeout_max_raw, int) else 30000
        self._macro_exec_timeout_max_ms = max(1, timeout_max)

        timeout_adjustment = self._control_timeout_spin.get_adjustment()
        timeout_adjustment.set_upper(self._macro_exec_timeout_max_ms)
        timeout_adjustment.set_value(
            min(timeout_adjustment.get_value(), float(self._macro_exec_timeout_max_ms))
        )

        macro = payload.get("macro")
        if isinstance(macro, dict):
            self._macro_exists = True
            self._apply_macro_state(macro)
            self._initial_macro_data = copy.deepcopy(macro)
            self._refresh_loaded_macro_state()
        return False

    def _refresh_loaded_macro_state(self) -> None:
        self._update_stats()
        self._timeline.queue_draw()
        self._update_canvas_width()

    def _apply_macro_state(self, macro: dict) -> None:
        self._macro_data = copy.deepcopy(macro)
        raw_events = self._macro_data.get("events", [])
        (
            self._events,
            self._rel_events,
            self._passthrough_events,
            self._synthetic_moves,
            self._control_events,
        ) = parse_events(raw_events)
        self._duration_us = int(self._macro_data.get("duration_ms", 0) or 0) * 1000
        self._macro_move_to_start = bool(self._macro_data.get("move_to_start", False))
        self._macro_start_x = int(self._macro_data.get("start_x", 0) or 0)
        self._macro_start_y = int(self._macro_data.get("start_y", 0) or 0)
        self._macro_block_mouse_movement = bool(self._macro_data.get("block_mouse_movement", False))
        self._macro_loop_mode = str(self._macro_data.get("loop_mode", "none") or "none")
        self._macro_loop_count = max(1, int(self._macro_data.get("loop_count", 1) or 1))
        for note in self._macro_data.get("gap_notes", []):
            if not isinstance(note, dict):
                continue
            self._synthetic_moves.append(
                EditableMove(
                    mode="gap",
                    t_us=int(note.get("at_us", 0) or 0),
                    x=int(note.get("gap_ms", 0) or 0),
                    y=0,
                    scope=str(note.get("scope", "all") or "all"),
                )
            )

        if self._events:
            self._duration_us = max(self._duration_us, max(e.release_t_us for e in self._events))
        if self._rel_events:
            self._duration_us = max(
                self._duration_us, max(int(e.get("t_us", 0)) for e in self._rel_events)
            )
        if self._passthrough_events:
            self._duration_us = max(
                self._duration_us,
                max(int(e.get("t_us", 0)) for e in self._passthrough_events),
            )
        if self._synthetic_moves:
            self._duration_us = max(
                self._duration_us,
                max(m.t_us + (1 if m.mode == "abs" else 0) for m in self._synthetic_moves),
            )
        if self._control_events:
            self._duration_us = max(
                self._duration_us,
                max(self._control_end_time_us(c) for c in self._control_events),
            )

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.set_margin_top(8)
        root.set_margin_bottom(8)
        root.set_margin_start(8)
        root.set_margin_end(8)

        root.append(self._build_toolbar())
        root.append(Gtk.Separator())
        root.append(self._build_timeline_area())
        root.append(Gtk.Separator())
        root.append(self._build_property_panel())
        root.append(self._build_name_row())
        root.append(self._build_footer())

        frame = Gtk.Frame()
        frame.add_css_class("macro-editor-outline")
        frame.set_child(root)
        self.set_child(frame)
        GLib.idle_add(self._update_canvas_width)

    def _build_toolbar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.set_margin_top(4)
        bar.set_margin_bottom(4)
        bar.set_margin_start(4)
        bar.set_margin_end(4)

        timing_btn = Gtk.MenuButton(label="Timing Tools")
        timing_btn.add_css_class("flat")
        timing_btn.set_popover(self._build_timing_popover())
        bar.append(timing_btn)

        undo_btn = Gtk.Button(label="Undo All")
        undo_btn.add_css_class("flat")
        undo_btn.set_tooltip_text("Restore macro to loaded state")
        undo_btn.connect("clicked", self._on_undo_all_changes)
        bar.append(undo_btn)

        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep.set_margin_start(4)
        sep.set_margin_end(4)
        bar.append(sep)

        zoom_out_btn = Gtk.Button(label="−")
        zoom_out_btn.add_css_class("flat")
        zoom_out_btn.set_tooltip_text("Zoom out")
        zoom_out_btn.connect("clicked", self._on_zoom_out)
        bar.append(zoom_out_btn)

        zoom_in_btn = Gtk.Button(label="+")
        zoom_in_btn.add_css_class("flat")
        zoom_in_btn.set_tooltip_text("Zoom in")
        zoom_in_btn.connect("clicked", self._on_zoom_in)
        bar.append(zoom_in_btn)

        reset_fit_btn = Gtk.Button(label="Reset Fit")
        reset_fit_btn.add_css_class("flat")
        reset_fit_btn.set_tooltip_text("Fit timeline to visible width")
        reset_fit_btn.connect("clicked", self._on_reset_fit)
        bar.append(reset_fit_btn)

        sep2 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep2.set_margin_start(4)
        sep2.set_margin_end(4)
        bar.append(sep2)

        self._lock_btn = Gtk.ToggleButton(label="Lock Move")
        self._lock_btn.set_active(True)  # locked by default
        self._lock_btn.set_tooltip_text("When locked, events cannot be dragged")
        self._lock_btn.connect("toggled", self._on_move_lock_toggled)
        bar.append(self._lock_btn)

        self._stats_label = Gtk.Label()
        self._stats_label.add_css_class("dim-label")
        self._stats_label.add_css_class("caption")
        self._stats_label.set_hexpand(True)
        self._stats_label.set_halign(Gtk.Align.END)
        bar.append(self._stats_label)
        self._update_stats()

        return bar

    def _build_timing_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(10)
        box.set_margin_end(10)

        title = Gtk.Label(label="Timing Tools")
        title.add_css_class("heading")
        title.set_halign(Gtk.Align.START)
        box.append(title)

        hint = Gtk.Label(label="Trim silence and shape waiting times")
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        hint.set_halign(Gtk.Align.START)
        box.append(hint)

        trim_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        trim_start_btn = Gtk.Button(label="Trim Start")
        trim_start_btn.connect("clicked", self._on_trim_start_clicked)
        trim_row.append(trim_start_btn)
        trim_end_btn = Gtk.Button(label="Trim End")
        trim_end_btn.connect("clicked", self._on_trim_end_clicked)
        trim_row.append(trim_end_btn)
        box.append(trim_row)

        box.append(Gtk.Separator())

        scale_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        scale_row.append(Gtk.Label(label="Scale:"))
        timing_scale_spin = Gtk.SpinButton()
        self._timing_scale_spin = timing_scale_spin
        timing_scale_spin.set_adjustment(
            Gtk.Adjustment(value=1.00, lower=0.10, upper=10.00, step_increment=0.10)
        )
        timing_scale_spin.set_digits(2)
        timing_scale_spin.set_width_chars(5)
        scale_row.append(timing_scale_spin)
        scale_row.append(Gtk.Label(label="x"))
        apply_scale_btn = Gtk.Button(label="Apply")
        apply_scale_btn.connect("clicked", self._on_apply_scale_clicked)
        scale_row.append(apply_scale_btn)
        box.append(scale_row)

        gap_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        gap_row.append(Gtk.Label(label="Min gap (ms):"))
        timing_min_gap_spin = Gtk.SpinButton()
        self._timing_min_gap_spin = timing_min_gap_spin
        timing_min_gap_spin.set_adjustment(
            Gtk.Adjustment(value=0.0, lower=0.0, upper=2000.0, step_increment=1.0)
        )
        timing_min_gap_spin.set_digits(0)
        timing_min_gap_spin.set_width_chars(5)
        gap_row.append(timing_min_gap_spin)
        gap_row.append(Gtk.Label(label="Max gap (ms):"))
        timing_max_gap_spin = Gtk.SpinButton()
        self._timing_max_gap_spin = timing_max_gap_spin
        timing_max_gap_spin.set_adjustment(
            Gtk.Adjustment(value=250.0, lower=0.0, upper=10000.0, step_increment=10.0)
        )
        timing_max_gap_spin.set_digits(0)
        timing_max_gap_spin.set_width_chars(5)
        gap_row.append(timing_max_gap_spin)
        box.append(gap_row)

        apply_gap_btn = Gtk.Button(label="Apply Gap Limits")
        apply_gap_btn.connect("clicked", self._on_apply_gap_limits_clicked)
        box.append(apply_gap_btn)

        box.append(Gtk.Separator())

        extend_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        extend_row.append(Gtk.Label(label="Add time (ms):"))
        timing_extend_ms_spin = Gtk.SpinButton()
        self._timing_extend_ms_spin = timing_extend_ms_spin
        timing_extend_ms_spin.set_adjustment(
            Gtk.Adjustment(value=100.0, lower=1.0, upper=600000.0, step_increment=10.0)
        )
        timing_extend_ms_spin.set_digits(0)
        timing_extend_ms_spin.set_width_chars(7)
        extend_row.append(timing_extend_ms_spin)
        box.append(extend_row)

        extend_btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        add_start_btn = Gtk.Button(label="Add at Start")
        add_start_btn.connect("clicked", self._on_add_time_start_clicked)
        extend_btn_row.append(add_start_btn)
        add_end_btn = Gtk.Button(label="Add at End")
        add_end_btn.connect("clicked", self._on_add_time_end_clicked)
        extend_btn_row.append(add_end_btn)
        box.append(extend_btn_row)

        box.append(Gtk.Separator())

        insert_title = Gtk.Label(label="Insert Gap")
        insert_title.add_css_class("heading")
        insert_title.set_halign(Gtk.Align.START)
        box.append(insert_title)

        at_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        at_row.append(Gtk.Label(label="At (ms):"))
        insert_gap_at_spin = Gtk.SpinButton()
        self._insert_gap_at_spin = insert_gap_at_spin
        insert_gap_at_spin.set_adjustment(
            Gtk.Adjustment(value=0.0, lower=0.0, upper=3600000.0, step_increment=1.0)
        )
        insert_gap_at_spin.set_digits(0)
        insert_gap_at_spin.set_width_chars(7)
        at_row.append(insert_gap_at_spin)
        box.append(at_row)

        gap_insert_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        gap_insert_row.append(Gtk.Label(label="Gap (ms):"))
        insert_gap_ms_spin = Gtk.SpinButton()
        self._insert_gap_ms_spin = insert_gap_ms_spin
        insert_gap_ms_spin.set_adjustment(
            Gtk.Adjustment(value=100.0, lower=1.0, upper=60000.0, step_increment=10.0)
        )
        insert_gap_ms_spin.set_digits(0)
        insert_gap_ms_spin.set_width_chars(7)
        gap_insert_row.append(insert_gap_ms_spin)
        box.append(gap_insert_row)

        scope_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        scope_row.append(Gtk.Label(label="Scope:"))
        self._insert_gap_scope_combo = _build_option_dropdown(_SCOPE_OPTIONS, "all")
        scope_row.append(self._insert_gap_scope_combo)
        box.append(scope_row)

        insert_btn = Gtk.Button(label="Insert Gap")
        insert_btn.add_css_class("suggested-action")
        insert_btn.connect("clicked", self._on_insert_gap_clicked)
        box.append(insert_btn)

        pop.set_child(box)
        return pop

    def _build_timeline_area(self) -> Gtk.Widget:
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        self._scrolled = Gtk.ScrolledWindow()
        self._scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
        self._scrolled.set_overlay_scrolling(False)
        self._scrolled.set_vexpand(False)
        self._scrolled.connect("notify::width", self._on_timeline_viewport_changed)

        self._timeline = TimelineWidget(self)
        self._scrolled.set_child(self._timeline)

        container.append(self._scrolled)

        timeline_scroll_adj = Gtk.Adjustment(
            value=0.0,
            lower=0.0,
            upper=1.0,
            step_increment=32.0,
            page_increment=160.0,
            page_size=1.0,
        )
        self._timeline_scroll_adj = timeline_scroll_adj
        timeline_scroll_adj.connect(
            "value-changed", self._on_timeline_scroll_adjustment_changed
        )

        hscroll = Gtk.Scrollbar(
            orientation=Gtk.Orientation.HORIZONTAL, adjustment=timeline_scroll_adj
        )
        hscroll.set_hexpand(True)
        container.append(hscroll)

        return container

    def _on_timeline_viewport_changed(self, _widget, _pspec) -> None:
        GLib.idle_add(self._update_canvas_width)

    def _build_property_panel(self) -> Gtk.Widget:
        self._revealer = Gtk.Revealer()
        self._revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._revealer.set_reveal_child(False)

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        panel.set_margin_top(8)
        panel.set_margin_bottom(4)
        panel.set_margin_start(8)
        panel.set_margin_end(8)

        # Title row
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._prop_title = Gtk.Label()
        self._prop_title.add_css_class("heading")
        self._prop_title.set_halign(Gtk.Align.START)
        self._prop_title.set_hexpand(True)
        title_row.append(self._prop_title)
        panel.append(title_row)

        # Timing row
        timing_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        timing_row.set_halign(Gtk.Align.START)

        self._press_label = Gtk.Label(label="Press:")
        timing_row.append(self._press_label)
        self._press_spin = Gtk.SpinButton()
        self._press_spin.set_adjustment(
            Gtk.Adjustment(value=0, lower=0, upper=3600000, step_increment=1, page_increment=10)
        )
        self._press_spin.set_digits(0)
        self._press_spin.set_width_chars(8)
        self._press_spin.connect("value-changed", self._on_press_changed)
        timing_row.append(self._press_spin)
        self._press_unit_label = Gtk.Label(label="ms")
        timing_row.append(self._press_unit_label)

        self._duration_text_label = Gtk.Label(label="Duration:")
        timing_row.append(self._duration_text_label)
        self._duration_spin = Gtk.SpinButton()
        self._duration_spin.set_adjustment(
            Gtk.Adjustment(value=1, lower=1, upper=3600000, step_increment=1, page_increment=10)
        )
        self._duration_spin.set_digits(0)
        self._duration_spin.set_width_chars(7)
        self._duration_spin.connect("value-changed", self._on_duration_changed)
        timing_row.append(self._duration_spin)
        self._duration_unit_label = Gtk.Label(label="ms")
        timing_row.append(self._duration_unit_label)

        self._release_label = Gtk.Label(label="  Release:")
        timing_row.append(self._release_label)
        self._release_spin = Gtk.SpinButton()
        self._release_spin.set_adjustment(
            Gtk.Adjustment(value=0, lower=0, upper=3600000, step_increment=1, page_increment=10)
        )
        self._release_spin.set_digits(0)
        self._release_spin.set_width_chars(8)
        self._release_spin.connect("value-changed", self._on_release_changed)
        timing_row.append(self._release_spin)
        self._release_unit_label = Gtk.Label(label="ms")
        timing_row.append(self._release_unit_label)

        panel.append(timing_row)

        move_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        move_row.set_halign(Gtk.Align.START)
        self._move_mode_label = Gtk.Label(label="Mode: REL")
        move_row.append(self._move_mode_label)
        self._move_x_label = Gtk.Label(label="X:")
        move_row.append(self._move_x_label)
        self._move_x_spin = Gtk.SpinButton()
        self._move_x_spin.set_adjustment(
            Gtk.Adjustment(value=0, lower=-10000, upper=10000, step_increment=1)
        )
        self._move_x_spin.set_digits(0)
        self._move_x_spin.set_width_chars(7)
        self._move_x_spin.connect("value-changed", self._on_move_x_changed)
        move_row.append(self._move_x_spin)
        self._move_y_label = Gtk.Label(label="Y:")
        move_row.append(self._move_y_label)
        self._move_y_spin = Gtk.SpinButton()
        self._move_y_spin.set_adjustment(
            Gtk.Adjustment(value=0, lower=-10000, upper=10000, step_increment=1)
        )
        self._move_y_spin.set_digits(0)
        self._move_y_spin.set_width_chars(7)
        self._move_y_spin.connect("value-changed", self._on_move_y_changed)
        move_row.append(self._move_y_spin)
        panel.append(move_row)
        self._move_row = move_row
        self._move_row.set_visible(False)

        gap_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        gap_row.set_halign(Gtk.Align.START)
        gap_row.append(Gtk.Label(label="Scope:"))
        self._gap_scope_combo = _build_option_dropdown(_SCOPE_OPTIONS, "all")
        self._gap_scope_combo.connect("notify::selected", self._on_gap_scope_changed)
        gap_row.append(self._gap_scope_combo)
        panel.append(gap_row)
        self._gap_row = gap_row
        self._gap_row.set_visible(False)

        control_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._control_mode_label = Gtk.Label()
        self._control_mode_label.add_css_class("dim-label")
        self._control_mode_label.set_halign(Gtk.Align.START)
        control_row.append(self._control_mode_label)

        control_ab_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._control_a_label = Gtk.Label(label="A:")
        control_ab_row.append(self._control_a_label)
        self._control_a_spin = Gtk.SpinButton()
        self._control_a_spin.set_adjustment(
            Gtk.Adjustment(value=0, lower=0, upper=600000, step_increment=1)
        )
        self._control_a_spin.set_digits(0)
        self._control_a_spin.set_width_chars(7)
        self._control_a_spin.connect("value-changed", self._on_control_a_changed)
        control_ab_row.append(self._control_a_spin)
        self._control_b_label = Gtk.Label(label="B:")
        control_ab_row.append(self._control_b_label)
        self._control_b_spin = Gtk.SpinButton()
        self._control_b_spin.set_adjustment(
            Gtk.Adjustment(value=0, lower=0, upper=600000, step_increment=1)
        )
        self._control_b_spin.set_digits(0)
        self._control_b_spin.set_width_chars(7)
        self._control_b_spin.connect("value-changed", self._on_control_b_changed)
        control_ab_row.append(self._control_b_spin)
        control_row.append(control_ab_row)

        control_cmd_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        control_cmd_row.append(Gtk.Label(label="Command:"))
        self._control_cmd_entry = Gtk.Entry()
        self._control_cmd_entry.set_hexpand(True)
        self._control_cmd_entry.connect("changed", self._on_control_command_changed)
        control_cmd_row.append(self._control_cmd_entry)
        control_row.append(control_cmd_row)

        control_sync_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        control_sync_row.append(Gtk.Label(label="Timeout (ms):"))
        self._control_timeout_spin = Gtk.SpinButton()
        self._control_timeout_spin.set_adjustment(
            Gtk.Adjustment(
                value=min(30000, self._macro_exec_timeout_max_ms),
                lower=1,
                upper=self._macro_exec_timeout_max_ms,
                step_increment=100,
            )
        )
        self._control_timeout_spin.set_digits(0)
        self._control_timeout_spin.set_width_chars(8)
        self._control_timeout_spin.connect("value-changed", self._on_control_timeout_changed)
        control_sync_row.append(self._control_timeout_spin)
        self._control_inhibit_check = Gtk.CheckButton(label="Inhibit mouse")
        self._control_inhibit_check.connect("toggled", self._on_control_inhibit_toggled)
        control_sync_row.append(self._control_inhibit_check)
        control_row.append(control_sync_row)

        self._control_timeout_hint_label = Gtk.Label()
        self._control_timeout_hint_label.add_css_class("dim-label")
        self._control_timeout_hint_label.set_halign(Gtk.Align.START)
        control_row.append(self._control_timeout_hint_label)

        panel.append(control_row)
        self._control_row = control_row
        self._control_ab_row = control_ab_row
        self._control_cmd_row = control_cmd_row
        self._control_sync_row = control_sync_row
        self._control_timeout_hint_label.set_visible(False)
        self._control_row.set_visible(False)

        # Action row
        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._key_info_label = Gtk.Label()
        self._key_info_label.add_css_class("dim-label")
        self._key_info_label.set_hexpand(True)
        self._key_info_label.set_halign(Gtk.Align.START)
        action_row.append(self._key_info_label)

        change_key_btn = Gtk.Button(label="Change Key…")
        change_key_btn.add_css_class("flat")
        change_key_btn.connect("clicked", self._on_change_key_clicked)
        action_row.append(change_key_btn)
        self._change_key_btn = change_key_btn

        delete_btn = Gtk.Button(label="Delete Event")
        delete_btn.add_css_class("destructive-action")
        delete_btn.add_css_class("flat")
        delete_btn.connect("clicked", self._on_delete_event)
        action_row.append(delete_btn)

        panel.append(action_row)
        panel.append(Gtk.Separator())

        self._revealer.set_child(panel)
        return self._revealer

    def _build_name_row(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_top(8)
        outer.set_margin_start(8)
        outer.set_margin_end(8)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name_label = Gtk.Label(label="Name:")
        row.append(name_label)

        self._name_entry = Gtk.Entry()
        self._name_entry.set_text(self._macro_name)
        self._name_entry.set_hexpand(True)
        row.append(self._name_entry)
        outer.append(row)

        loop_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        loop_row.append(Gtk.Label(label="Loop:"))
        self._macro_loop_mode_combo = _build_option_dropdown(
            _LOOP_MODE_OPTIONS,
            self._macro_loop_mode,
        )
        self._macro_loop_mode_combo.connect(
            "notify::selected",
            self._on_macro_loop_mode_changed,
        )
        loop_row.append(self._macro_loop_mode_combo)

        self._macro_loop_count_label = Gtk.Label(label="Count:")
        loop_row.append(self._macro_loop_count_label)
        self._macro_loop_count_spin = Gtk.SpinButton()
        self._macro_loop_count_spin.set_adjustment(
            Gtk.Adjustment(value=self._macro_loop_count, lower=1, upper=10000, step_increment=1)
        )
        self._macro_loop_count_spin.set_digits(0)
        self._macro_loop_count_spin.set_width_chars(6)
        self._macro_loop_count_spin.connect("value-changed", self._on_macro_loop_count_changed)
        loop_row.append(self._macro_loop_count_spin)
        outer.append(loop_row)

        self._exec_summary_label = Gtk.Label()
        self._exec_summary_label.add_css_class("dim-label")
        self._exec_summary_label.set_halign(Gtk.Align.START)
        outer.append(self._exec_summary_label)

        start_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._macro_move_to_start_check = Gtk.CheckButton(label="Move mouse to:")
        self._macro_move_to_start_check.set_active(self._macro_move_to_start)
        self._macro_move_to_start_check.connect("toggled", self._on_macro_move_to_start_toggled)
        start_row.append(self._macro_move_to_start_check)

        self._macro_start_x_spin = Gtk.SpinButton()
        self._macro_start_x_spin.set_adjustment(
            Gtk.Adjustment(value=self._macro_start_x, lower=-100000, upper=100000, step_increment=1)
        )
        self._macro_start_x_spin.set_digits(0)
        self._macro_start_x_spin.set_width_chars(7)
        self._macro_start_x_spin.connect("value-changed", self._on_macro_start_pos_changed)
        start_row.append(self._macro_start_x_spin)

        self._macro_start_y_spin = Gtk.SpinButton()
        self._macro_start_y_spin.set_adjustment(
            Gtk.Adjustment(value=self._macro_start_y, lower=-100000, upper=100000, step_increment=1)
        )
        self._macro_start_y_spin.set_digits(0)
        self._macro_start_y_spin.set_width_chars(7)
        self._macro_start_y_spin.connect("value-changed", self._on_macro_start_pos_changed)
        start_row.append(self._macro_start_y_spin)

        start_row.append(Gtk.Label(label="at the start of the macro"))

        outer.append(start_row)

        capture_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        capture_row.set_margin_start(24)

        if not self._slurp_available:
            capture_label = Gtk.Label(label="Capture new position in:")
            capture_label.add_css_class("dim-label")
            capture_row.append(capture_label)

        self._macro_capture_delay_spin = Gtk.SpinButton()
        self._macro_capture_delay_spin.set_adjustment(
            Gtk.Adjustment(
                value=self._capture_delay_seconds, lower=0.2, upper=15.0, step_increment=0.2
            )
        )
        self._macro_capture_delay_spin.set_digits(1)
        self._macro_capture_delay_spin.set_width_chars(4)
        self._macro_capture_delay_spin.set_visible(not self._slurp_available)
        capture_row.append(self._macro_capture_delay_spin)

        if not self._slurp_available:
            capture_row.append(Gtk.Label(label="s"))

        btn_label = "Capture" if self._slurp_available else "Capture"
        self._macro_capture_btn = Gtk.Button(label=btn_label)
        self._macro_capture_btn.connect("clicked", self._on_capture_start_position_clicked)
        capture_row.append(self._macro_capture_btn)

        self._macro_capture_status = Gtk.Label(label="")
        self._macro_capture_status.add_css_class("dim-label")
        self._macro_capture_status.set_halign(Gtk.Align.START)
        self._macro_capture_status.set_hexpand(True)
        capture_row.append(self._macro_capture_status)

        outer.append(capture_row)

        self._macro_block_mouse_check = Gtk.CheckButton(
            label="Block physical mouse movement during playback"
        )
        self._macro_block_mouse_check.set_active(self._macro_block_mouse_movement)
        outer.append(self._macro_block_mouse_check)

        self._update_loop_controls()
        self._update_macro_move_start_controls()
        self._update_exec_summary_label()
        return outer

    def _on_macro_loop_mode_changed(self, combo: Gtk.DropDown, _pspec=None) -> None:
        self._macro_loop_mode = _get_dropdown_selected_id(combo, _LOOP_MODE_OPTIONS, "none")
        self._update_loop_controls()

    def _on_macro_loop_count_changed(self, spin: Gtk.SpinButton) -> None:
        self._macro_loop_count = max(1, int(spin.get_value()))

    def _update_loop_controls(self) -> None:
        is_count = self._macro_loop_mode == "count"
        self._macro_loop_count_label.set_visible(is_count)
        self._macro_loop_count_spin.set_visible(is_count)

    def _on_macro_move_to_start_toggled(self, check: Gtk.CheckButton) -> None:
        self._macro_move_to_start = check.get_active()
        self._update_macro_move_start_controls()

    def _on_macro_start_pos_changed(self, spin: Gtk.SpinButton) -> None:
        self._macro_start_x = int(self._macro_start_x_spin.get_value())
        self._macro_start_y = int(self._macro_start_y_spin.get_value())

    def _update_macro_move_start_controls(self) -> None:
        enabled = self._macro_move_to_start
        self._macro_start_x_spin.set_sensitive(enabled)
        self._macro_start_y_spin.set_sensitive(enabled)
        if self._slurp_available:
            self._macro_capture_delay_spin.set_sensitive(False)
        else:
            self._macro_capture_delay_spin.set_sensitive(enabled)
        self._macro_capture_btn.set_sensitive(enabled and not self._capture_pending)

    def _on_capture_start_position_clicked(self, btn: Gtk.Button) -> None:
        self._cancel_capture_start_position("")

        if self._slurp_available:
            self._capture_pending = True
            self._macro_capture_btn.set_sensitive(False)
            self._macro_capture_status.set_text("Click to capture position...")
            self._slurp_capture.capture_point(self._on_slurp_capture_result)
        else:
            self._capture_delay_seconds = float(self._macro_capture_delay_spin.get_value())
            self._capture_pending = True
            self._macro_capture_btn.set_sensitive(False)
            self._macro_capture_status.set_text(
                f"Move cursor now... capturing in {self._capture_delay_seconds:.1f}s"
            )
            self._capture_timeout_id = GLib.timeout_add(
                int(self._capture_delay_seconds * 1000),
                self._capture_start_position_after_delay,
            )

    def _on_slurp_capture_result(self, result) -> None:
        self._capture_pending = False
        self._update_macro_move_start_controls()

        if result is None:
            self._macro_capture_status.set_text("Capture cancelled or failed")
            return

        self._macro_start_x_spin.set_value(result.x)
        self._macro_start_y_spin.set_value(result.y)
        self._macro_move_to_start_check.set_active(True)
        self._macro_capture_status.set_text(f"Captured: {result.x}, {result.y}")

    def _capture_start_position_after_delay(self) -> bool:
        self._capture_timeout_id = 0
        if not self._capture_pending:
            return False
        self._macro_capture_status.set_text("Reading cursor position...")
        session_request_async(
            {"command": "get_cursor_position"},
            self._on_capture_start_position_response,
            timeout=5.0,
        )
        return False

    def _on_capture_start_position_response(self, response: dict | None) -> bool:
        self._capture_pending = False
        self._update_macro_move_start_controls()

        if not response or response.get("status") != "ok":
            message = (
                (response or {}).get("message") or (response or {}).get("error") or "Capture failed"
            )
            if "Unknown command: get_cursor_position" in message:
                message = "Please restart Keyforge Session, then try again"
            self._macro_capture_status.set_text(message)
            return False

        self._macro_start_x_spin.set_value(int(response.get("x", 0)))
        self._macro_start_y_spin.set_value(int(response.get("y", 0)))
        self._macro_move_to_start_check.set_active(True)
        self._macro_capture_status.set_text("Captured")
        return False

    def _cancel_capture_start_position(self, status_text: str) -> None:
        if self._capture_timeout_id:
            GLib.source_remove(self._capture_timeout_id)
            self._capture_timeout_id = 0
        self._capture_pending = False
        if hasattr(self, "_macro_capture_status"):
            self._macro_capture_status.set_text(status_text)
        if hasattr(self, "_macro_capture_btn"):
            self._update_macro_move_start_controls()

    def _build_footer(self) -> Gtk.Widget:
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END)
        footer.set_margin_top(8)
        footer.set_margin_bottom(4)
        footer.set_margin_end(8)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.close())
        footer.append(cancel_btn)

        copy_btn = Gtk.Button(label="Save as Copy…")
        copy_btn.connect("clicked", self._on_save_as_copy)
        footer.append(copy_btn)

        save_btn = Gtk.Button(label="Save Changes")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._on_save)
        footer.append(save_btn)

        return footer

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _update_stats(self) -> None:
        duration_s = self._duration_us / 1e6
        synthetic_count = sum(
            4 if m.mode == "abs" else 2 for m in self._synthetic_moves if m.mode != "gap"
        )
        event_count = (
            len(self._events) * 2
            + len(self._rel_events)
            + len(self._passthrough_events)
            + synthetic_count
            + len(self._control_events)
        )
        self._stats_label.set_label(f"{duration_s:.3f}s · {event_count} events")
        self._update_exec_summary_label()

    def _update_exec_summary_label(self) -> None:
        if not hasattr(self, "_exec_summary_label"):
            return
        sync_count = sum(1 for c in self._control_events if c.mode == "exec_sync")
        async_count = sum(1 for c in self._control_events if c.mode == "exec_async")
        total = sync_count + async_count
        if total == 0:
            self._exec_summary_label.set_label("Exec actions: none")
            return
        self._exec_summary_label.set_label(
            f"Exec actions: {total} (sync {sync_count}, async {async_count})"
        )

    def _control_end_time_us(self, control: EditableControl) -> int:
        end_t = int(control.t_us)
        if control.mode == "wait_fixed":
            end_t += max(0, int(control.duration_ms)) * 1000
        elif control.mode == "wait_random":
            end_t += max(int(control.min_ms), int(control.max_ms)) * 1000
        return end_t

    def _control_affects_timing(self, control: EditableControl) -> bool:
        return control.mode == "wait_fixed"

    def _timeline_end_us(self) -> int:
        stamps = self._all_timestamps(include_passthrough=True)
        if not stamps:
            return max(0, int(self._duration_us))
        return max(max(stamps), max(0, int(self._duration_us)))

    def _apply_auto_zoom(self, viewport_width: int) -> None:
        if not self._auto_zoom_enabled:
            return
        if viewport_width <= TimelineWidget.LABEL_WIDTH + 8:
            return

        end_us = self._timeline_end_us()
        if end_us <= 0:
            self._timeline._pps = 300.0
            return

        available_px = float(max(viewport_width - TimelineWidget.LABEL_WIDTH - 4, 1))
        fit_pps = available_px / (end_us / 1e6)
        self._timeline._pps = max(self._auto_zoom_min_pps, min(self._zoom_max_pps, fit_pps))

    def _zoom_timeline(self, factor: float) -> None:
        if factor <= 0:
            return
        self._auto_zoom_enabled = False
        self._timeline._pps = max(
            self._zoom_min_pps,
            min(self._zoom_max_pps, self._timeline._pps * float(factor)),
        )
        self._update_canvas_width()
        self._timeline.queue_draw()

    def _update_canvas_width(self) -> bool:
        if not hasattr(self, "_scrolled") or not hasattr(self, "_timeline"):
            return False

        viewport_width = self._scrolled.get_width()
        viewport_width = viewport_width if viewport_width > 1 else 720

        self._apply_auto_zoom(viewport_width)

        duration_s = self._timeline_end_us() / 1e6
        content_width = int(TimelineWidget.LABEL_WIDTH + duration_s * self._timeline._pps + 4)
        canvas_width = max(viewport_width, 1)
        self._timeline_scroll_max = max(float(content_width - viewport_width), 0.0)

        self._timeline.set_size_request(canvas_width, self._timeline._total_height)
        self._sync_timeline_scroll_adjustment(viewport_width)
        self._set_timeline_scroll(self._timeline_scroll_x)
        return False

    def _sync_timeline_scroll_adjustment(self, viewport_width: int) -> None:
        if self._timeline_scroll_adj is None:
            return
        page_size = float(max(viewport_width, 1))
        upper = self._timeline_scroll_max + page_size
        self._timeline_scroll_adj.set_lower(0.0)
        self._timeline_scroll_adj.set_upper(upper)
        self._timeline_scroll_adj.set_page_size(page_size)
        self._timeline_scroll_adj.set_step_increment(32.0)
        self._timeline_scroll_adj.set_page_increment(max(page_size * 0.8, 64.0))

    def _set_timeline_scroll(self, value: float, *, sync_adjustment: bool = True) -> None:
        clamped = max(0.0, min(self._timeline_scroll_max, float(value)))
        self._timeline_scroll_x = clamped
        self._timeline.set_scroll_offset(clamped)
        if sync_adjustment and self._timeline_scroll_adj is not None:
            if abs(self._timeline_scroll_adj.get_value() - clamped) > 0.5:
                self._timeline_scroll_adj.set_value(clamped)

    def _scroll_timeline_by(self, delta_px: float) -> None:
        self._set_timeline_scroll(self._timeline_scroll_x + delta_px)

    def _on_timeline_scroll_adjustment_changed(self, adj: Gtk.Adjustment) -> None:
        self._set_timeline_scroll(adj.get_value(), sync_adjustment=False)

    def _all_timestamps(self, include_passthrough: bool = True) -> list[int]:
        stamps: list[int] = []
        for ev in self._events:
            stamps.append(int(ev.press_t_us))
            stamps.append(int(ev.release_t_us))
        for ev in self._rel_events:
            stamps.append(int(ev.get("t_us", 0)))
        if include_passthrough:
            for ev in self._passthrough_events:
                stamps.append(int(ev.get("t_us", 0)))
        for move in self._synthetic_moves:
            stamps.append(int(move.t_us))
            if move.mode == "abs":
                stamps.append(int(move.t_us) + 1)
        for control in self._control_events:
            if not self._control_affects_timing(control):
                continue
            stamps.append(int(control.t_us))
            stamps.append(self._control_end_time_us(control))
        return sorted(set(max(0, s) for s in stamps))

    def _apply_time_map(self, mapping: dict[int, int]) -> None:
        keys = sorted(mapping.keys())

        def map_t(t: int) -> int:
            t = int(t)
            if t in mapping:
                return int(mapping[t])
            if not keys:
                return t
            if t <= keys[0]:
                return int(mapping[keys[0]])
            if t >= keys[-1]:
                return int(mapping[keys[-1]])

            # Piecewise linear interpolation between nearest mapped anchors.
            for i in range(1, len(keys)):
                left = keys[i - 1]
                right = keys[i]
                if left <= t <= right:
                    left_new = int(mapping[left])
                    right_new = int(mapping[right])
                    span = max(1, right - left)
                    frac = (t - left) / span
                    return int(round(left_new + (right_new - left_new) * frac))
            return t

        for ev in self._events:
            ev.press_t_us = map_t(ev.press_t_us)
            ev.release_t_us = max(ev.press_t_us + 1, map_t(ev.release_t_us))

        for ev in self._rel_events:
            ev["t_us"] = map_t(int(ev.get("t_us", 0)))

        for ev in self._passthrough_events:
            ev["t_us"] = map_t(int(ev.get("t_us", 0)))

        for move in self._synthetic_moves:
            move.t_us = map_t(move.t_us)

        for control in self._control_events:
            control.t_us = map_t(control.t_us)

        self._events.sort(key=lambda e: e.press_t_us)
        self._synthetic_moves.sort(key=lambda m: m.t_us)
        self._control_events.sort(key=lambda c: c.t_us)
        self._rel_events.sort(key=lambda e: int(e.get("t_us", 0)))
        self._passthrough_events.sort(key=lambda e: int(e.get("t_us", 0)))

    def _recompute_duration(self) -> None:
        latest = 0
        if self._events:
            latest = max(latest, max(e.release_t_us for e in self._events))
        if self._rel_events:
            latest = max(latest, max(int(e.get("t_us", 0)) for e in self._rel_events))
        if self._passthrough_events:
            latest = max(latest, max(int(e.get("t_us", 0)) for e in self._passthrough_events))
        if self._synthetic_moves:
            latest = max(
                latest,
                max(m.t_us + (1 if m.mode == "abs" else 0) for m in self._synthetic_moves),
            )
        timed_controls = [c for c in self._control_events if self._control_affects_timing(c)]
        if timed_controls:
            latest = max(latest, max(self._control_end_time_us(c) for c in timed_controls))
        self._duration_us = max(0, int(latest))

    def _refresh_after_timing_edit(self) -> None:
        selected_obj = self._timeline._selected
        self._recompute_duration()
        self._update_stats()
        self._update_canvas_width()
        self._timeline.queue_draw()
        if selected_obj is not None:
            self._on_selection_changed(selected_obj)

    def _build_time_mapping_with_gap_limits(
        self,
        *,
        scale: float = 1.0,
        min_gap_us: int = 0,
        max_gap_us: int | None = None,
        include_passthrough: bool = True,
    ) -> dict[int, int]:
        stamps = self._all_timestamps(include_passthrough=include_passthrough)
        if not stamps:
            return {}

        mapping: dict[int, int] = {stamps[0]: stamps[0]}
        prev_old = stamps[0]
        prev_new = stamps[0]
        min_gap_us = max(0, int(min_gap_us))
        max_gap = max(0, int(max_gap_us)) if max_gap_us is not None else None

        for t in stamps[1:]:
            gap_old = max(0, t - prev_old)
            gap = int(round(gap_old * scale))
            gap = max(gap, min_gap_us)
            if max_gap is not None:
                gap = min(gap, max_gap)
            prev_new += gap
            mapping[t] = prev_new
            prev_old = t

        return mapping

    def _on_trim_start_clicked(self, _btn) -> None:
        stamps = self._all_timestamps()
        if not stamps:
            return
        start_t = stamps[0]
        if start_t <= 0:
            return
        mapping = {t: t - start_t for t in stamps}
        self._apply_time_map(mapping)
        self._refresh_after_timing_edit()

    def _on_trim_end_clicked(self, _btn) -> None:
        self._recompute_duration()
        self._update_stats()
        self._update_canvas_width()
        self._timeline.queue_draw()

    def _on_apply_scale_clicked(self, _btn) -> None:
        if not self._timing_scale_spin:
            return
        scale = float(self._timing_scale_spin.get_value())
        if math.isclose(scale, 1.0, rel_tol=1e-6):
            return
        mapping = self._build_time_mapping_with_gap_limits(
            scale=scale,
            include_passthrough=False,
        )
        if not mapping:
            return
        self._apply_time_map(mapping)
        for move in self._synthetic_moves:
            if move.mode == "gap":
                move.x = max(1, int(round(move.x * scale)))
        self._refresh_after_timing_edit()

    def _on_apply_gap_limits_clicked(self, _btn) -> None:
        if not self._timing_min_gap_spin or not self._timing_max_gap_spin:
            return

        min_gap_us = int(float(self._timing_min_gap_spin.get_value()) * 1000)
        max_gap_us = int(float(self._timing_max_gap_spin.get_value()) * 1000)
        max_gap: int | None = max_gap_us if max_gap_us > 0 else None
        if max_gap is not None and max_gap < min_gap_us:
            max_gap = min_gap_us

        mapping = self._build_time_mapping_with_gap_limits(
            min_gap_us=min_gap_us,
            max_gap_us=max_gap,
            include_passthrough=False,
        )
        if not mapping:
            return
        self._apply_time_map(mapping)
        self._refresh_after_timing_edit()

    def _on_add_time_start_clicked(self, _btn) -> None:
        if not self._timing_extend_ms_spin:
            return
        delta_us = int(float(self._timing_extend_ms_spin.get_value()) * 1000)
        if delta_us <= 0:
            return

        changed = self._shift_timeline_for_gap(
            at_us=0,
            delta_us=delta_us,
            scope="all",
            exclude_gap_note=None,
        )
        if not changed:
            return

        self._events.sort(key=lambda e: e.press_t_us)
        self._synthetic_moves.sort(key=lambda m: m.t_us)
        self._rel_events.sort(key=lambda e: int(e.get("t_us", 0)))
        self._passthrough_events.sort(key=lambda e: int(e.get("t_us", 0)))
        self._refresh_after_timing_edit()

    def _on_add_time_end_clicked(self, _btn) -> None:
        if not self._timing_extend_ms_spin:
            return
        delta_us = int(float(self._timing_extend_ms_spin.get_value()) * 1000)
        if delta_us <= 0:
            return

        self._duration_us = max(0, int(self._duration_us + delta_us))
        self._update_stats()
        self._update_canvas_width()
        self._timeline.queue_draw()

    def _on_insert_gap_clicked(self, _btn) -> None:
        if (
            not self._insert_gap_at_spin
            or not self._insert_gap_ms_spin
            or not self._insert_gap_scope_combo
        ):
            return

        at_us = int(float(self._insert_gap_at_spin.get_value()) * 1000)
        gap_us = int(float(self._insert_gap_ms_spin.get_value()) * 1000)
        scope = _get_dropdown_selected_id(self._insert_gap_scope_combo, _SCOPE_OPTIONS, "all")
        self._insert_gap(at_us=at_us, gap_us=gap_us, scope=scope, add_note=True)

    def _insert_gap(self, *, at_us: int, gap_us: int, scope: str, add_note: bool) -> None:
        if gap_us <= 0:
            return

        changed = self._shift_timeline_for_gap(
            at_us=at_us,
            delta_us=gap_us,
            scope=scope,
            exclude_gap_note=None,
        )

        if add_note:
            self._synthetic_moves.append(
                EditableMove(mode="gap", t_us=at_us, x=max(1, gap_us // 1000), y=0, scope=scope)
            )
            changed = True

        if not changed:
            return

        self._events.sort(key=lambda e: e.press_t_us)
        self._synthetic_moves.sort(key=lambda m: m.t_us)
        self._rel_events.sort(key=lambda e: int(e.get("t_us", 0)))
        self._passthrough_events.sort(key=lambda e: int(e.get("t_us", 0)))
        self._refresh_after_timing_edit()

    def _clear_selection_if_removed(self) -> None:
        selected = self._timeline._selected
        if isinstance(selected, EditableEvent) and selected not in self._events:
            self._timeline._selected = None
            self._revealer.set_reveal_child(False)
            return
        if isinstance(selected, EditableMove) and selected not in self._synthetic_moves:
            self._timeline._selected = None
            self._revealer.set_reveal_child(False)
            return
        if isinstance(selected, EditableControl) and selected not in self._control_events:
            self._timeline._selected = None
            self._revealer.set_reveal_child(False)
            return
        if isinstance(selected, dict) and selected not in self._passthrough_events:
            self._timeline._selected = None
            self._revealer.set_reveal_child(False)

    def _set_startpoint(self, at_us: int) -> None:
        at_us = max(0, int(at_us))
        if at_us <= 0:
            return

        kept_events: list[EditableEvent] = []
        for ev in self._events:
            if ev.press_t_us < at_us:
                continue
            kept_events.append(ev)
        self._events = kept_events

        self._rel_events = [ev for ev in self._rel_events if int(ev.get("t_us", 0)) >= at_us]
        self._passthrough_events = [
            ev for ev in self._passthrough_events if int(ev.get("t_us", 0)) >= at_us
        ]
        self._synthetic_moves = [move for move in self._synthetic_moves if move.t_us >= at_us]
        self._control_events = [
            control for control in self._control_events if control.t_us >= at_us
        ]

        self._shift_timeline_for_gap(
            at_us=at_us,
            delta_us=-at_us,
            scope="all",
            exclude_gap_note=None,
        )

        self._events.sort(key=lambda e: e.press_t_us)
        self._synthetic_moves.sort(key=lambda m: m.t_us)
        self._control_events.sort(key=lambda c: c.t_us)
        self._rel_events.sort(key=lambda e: int(e.get("t_us", 0)))
        self._passthrough_events.sort(key=lambda e: int(e.get("t_us", 0)))
        self._clear_selection_if_removed()
        self._refresh_after_timing_edit()

    def _set_endpoint(self, at_us: int) -> None:
        at_us = max(0, int(at_us))

        kept_events: list[EditableEvent] = []
        for ev in self._events:
            if ev.press_t_us >= at_us:
                continue
            if ev.release_t_us > at_us:
                ev.release_t_us = max(at_us, ev.press_t_us + 1)
            kept_events.append(ev)
        self._events = kept_events

        self._rel_events = [ev for ev in self._rel_events if int(ev.get("t_us", 0)) <= at_us]
        self._passthrough_events = [
            ev for ev in self._passthrough_events if int(ev.get("t_us", 0)) <= at_us
        ]
        self._synthetic_moves = [move for move in self._synthetic_moves if move.t_us <= at_us]
        self._control_events = [
            control for control in self._control_events if control.t_us <= at_us
        ]

        self._events.sort(key=lambda e: e.press_t_us)
        self._synthetic_moves.sort(key=lambda m: m.t_us)
        self._control_events.sort(key=lambda c: c.t_us)
        self._rel_events.sort(key=lambda e: int(e.get("t_us", 0)))
        self._passthrough_events.sort(key=lambda e: int(e.get("t_us", 0)))
        self._clear_selection_if_removed()
        self._refresh_after_timing_edit()

    def _shift_timeline_for_gap(
        self,
        *,
        at_us: int,
        delta_us: int,
        scope: str,
        exclude_gap_note: EditableMove | None,
    ) -> bool:
        if delta_us == 0:
            return False

        changed = False

        if scope in ("all", "keyboard", "mouse"):
            for ev in self._events:
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
            for ev in self._rel_events:
                t_us = int(ev.get("t_us", 0))
                if t_us >= at_us:
                    ev["t_us"] = max(0, t_us + delta_us)
                    changed = True
            for move in self._synthetic_moves:
                if move.mode == "gap":
                    continue
                if move.t_us >= at_us:
                    move.t_us = max(0, move.t_us + delta_us)
                    changed = True

        for move in self._synthetic_moves:
            if move.mode != "gap":
                continue
            if exclude_gap_note is not None and move is exclude_gap_note:
                continue
            if move.t_us >= at_us:
                move.t_us = max(0, move.t_us + delta_us)
                changed = True

        if scope in ("all", "movement"):
            for control in self._control_events:
                if control.t_us >= at_us:
                    control.t_us = max(0, control.t_us + delta_us)
                    changed = True

        for ev in self._passthrough_events:
            t_us = int(ev.get("t_us", 0))
            if t_us < at_us:
                continue

            ev_type = int(ev.get("type", -1))
            device_type = str(ev.get("device_type", ""))
            matches_scope = False
            if scope == "all":
                matches_scope = True
            elif scope == "keyboard":
                matches_scope = ev_type == evdev.ecodes.EV_KEY and device_type == "keyboard"
            elif scope == "mouse":
                matches_scope = ev_type == evdev.ecodes.EV_KEY and device_type == "mouse"
            elif scope == "movement":
                matches_scope = ev_type == evdev.ecodes.EV_REL and device_type == "mouse"

            if matches_scope:
                ev["t_us"] = max(0, t_us + delta_us)
                changed = True

        return changed

    def _move_gap_note(self, note: EditableMove, old_t_us: int) -> None:
        if note.mode != "gap":
            return
        new_t_us = int(note.t_us)
        if new_t_us == int(old_t_us):
            return

        gap_us = max(1, int(note.x) * 1000)
        self._shift_timeline_for_gap(
            at_us=int(old_t_us),
            delta_us=-gap_us,
            scope=note.scope,
            exclude_gap_note=note,
        )
        self._shift_timeline_for_gap(
            at_us=new_t_us,
            delta_us=gap_us,
            scope=note.scope,
            exclude_gap_note=note,
        )

        self._events.sort(key=lambda e: e.press_t_us)
        self._synthetic_moves.sort(key=lambda m: m.t_us)
        self._rel_events.sort(key=lambda e: int(e.get("t_us", 0)))
        self._passthrough_events.sort(key=lambda e: int(e.get("t_us", 0)))
        self._refresh_after_timing_edit()

    def _change_gap_note_amount(self, note: EditableMove, old_gap_ms: int) -> None:
        if note.mode != "gap":
            return
        old_gap_us = max(1, int(old_gap_ms) * 1000)
        new_gap_us = max(1, int(note.x) * 1000)
        if new_gap_us == old_gap_us:
            return

        self._shift_timeline_for_gap(
            at_us=int(note.t_us),
            delta_us=-old_gap_us,
            scope=note.scope,
            exclude_gap_note=note,
        )
        self._shift_timeline_for_gap(
            at_us=int(note.t_us),
            delta_us=new_gap_us,
            scope=note.scope,
            exclude_gap_note=note,
        )

        self._events.sort(key=lambda e: e.press_t_us)
        self._synthetic_moves.sort(key=lambda m: m.t_us)
        self._rel_events.sort(key=lambda e: int(e.get("t_us", 0)))
        self._passthrough_events.sort(key=lambda e: int(e.get("t_us", 0)))
        self._refresh_after_timing_edit()

    def _change_gap_note_scope(self, note: EditableMove, old_scope: str) -> None:
        if note.mode != "gap":
            return
        if note.scope == old_scope:
            return

        gap_us = max(1, int(note.x) * 1000)
        self._shift_timeline_for_gap(
            at_us=int(note.t_us),
            delta_us=-gap_us,
            scope=old_scope,
            exclude_gap_note=note,
        )
        self._shift_timeline_for_gap(
            at_us=int(note.t_us),
            delta_us=gap_us,
            scope=note.scope,
            exclude_gap_note=note,
        )

        self._events.sort(key=lambda e: e.press_t_us)
        self._synthetic_moves.sort(key=lambda m: m.t_us)
        self._rel_events.sort(key=lambda e: int(e.get("t_us", 0)))
        self._passthrough_events.sort(key=lambda e: int(e.get("t_us", 0)))
        self._refresh_after_timing_edit()

    # ------------------------------------------------------------------
    # Property panel updates
    # ------------------------------------------------------------------

    def _on_selection_changed(self, selected_obj) -> None:
        if selected_obj is None:
            self._revealer.set_reveal_child(False)
            return

        self._revealer.set_reveal_child(True)
        if isinstance(selected_obj, EditableControl):
            control = selected_obj
            self._prop_title.set_label("Control")
            self._key_info_label.set_label(control.mode.replace("_", " ").title())
            self._press_label.set_label("At:")
            self._duration_text_label.set_visible(False)
            self._duration_spin.set_visible(False)
            self._duration_unit_label.set_visible(False)
            self._release_label.set_visible(False)
            self._release_spin.set_visible(False)
            self._release_unit_label.set_visible(False)
            self._change_key_btn.set_visible(False)
            self._move_row.set_visible(False)
            self._gap_row.set_visible(False)
            self._control_row.set_visible(True)

            self._updating_props = True
            try:
                self._press_spin.set_value(control.t_us / 1000)
                self._control_mode_label.set_label(control.mode.replace("_", " ").title())
                self._control_a_label.set_visible(False)
                self._control_a_spin.set_visible(False)
                self._control_b_label.set_visible(False)
                self._control_b_spin.set_visible(False)
                self._control_cmd_row.set_visible(False)
                self._control_sync_row.set_visible(False)
                self._control_timeout_hint_label.set_visible(False)

                if control.mode == "wait_fixed":
                    self._control_a_label.set_label("Duration (ms):")
                    self._control_a_label.set_visible(True)
                    self._control_a_spin.set_visible(True)
                    self._control_a_spin.set_value(max(1, int(control.duration_ms)))
                elif control.mode == "wait_random":
                    self._control_a_label.set_label("Min (ms):")
                    self._control_b_label.set_label("Max (ms):")
                    self._control_a_label.set_visible(True)
                    self._control_a_spin.set_visible(True)
                    self._control_b_label.set_visible(True)
                    self._control_b_spin.set_visible(True)
                    self._control_a_spin.set_value(max(1, int(control.min_ms)))
                    self._control_b_spin.set_value(max(1, int(control.max_ms)))
                elif control.mode == "exec_async":
                    self._control_cmd_row.set_visible(True)
                    self._control_cmd_entry.set_text(control.command)
                elif control.mode == "exec_sync":
                    self._control_cmd_row.set_visible(True)
                    self._control_sync_row.set_visible(True)
                    self._control_timeout_hint_label.set_visible(True)
                    self._control_cmd_entry.set_text(control.command)
                    self._control_timeout_spin.set_value(max(1, int(control.timeout_ms)))
                    self._control_inhibit_check.set_active(bool(control.inhibit_mouse))
                    self._update_timeout_clamp_hint(int(control.timeout_ms))
            finally:
                self._updating_props = False
            return

        self._control_row.set_visible(False)
        if isinstance(selected_obj, EditableMove):
            move = selected_obj
            if move.mode == "gap":
                self._prop_title.set_label("Gap Note")
                self._key_info_label.set_label(f"Insert {int(move.x)}ms gap ({move.scope})")
            else:
                self._prop_title.set_label(f"Mouse Move ({move.mode.upper()})")
                self._key_info_label.set_label(f"Move {move.mode.upper()} (x={move.x}, y={move.y})")

            self._press_label.set_label("At:")
            self._duration_text_label.set_visible(False)
            self._duration_spin.set_visible(False)
            self._duration_unit_label.set_visible(False)
            self._release_label.set_visible(False)
            self._release_spin.set_visible(False)
            self._release_unit_label.set_visible(False)
            self._change_key_btn.set_visible(False)
            self._move_row.set_visible(True)
            self._gap_row.set_visible(move.mode == "gap")
            self._move_mode_label.set_label(
                f"Mode: {move.mode.upper()}" if move.mode != "gap" else "Mode: GAP"
            )
            self._move_x_label.set_label("Gap (ms):" if move.mode == "gap" else "X:")
            self._move_y_label.set_label("Unused:" if move.mode == "gap" else "Y:")
            self._move_y_spin.set_sensitive(move.mode != "gap")

            self._updating_props = True
            try:
                self._press_spin.set_value(move.t_us / 1000)
                self._move_x_spin.set_value(move.x)
                self._move_y_spin.set_value(move.y)
                _set_dropdown_selected_id(self._gap_scope_combo, _SCOPE_OPTIONS, move.scope)
            finally:
                self._updating_props = False
            return

        if isinstance(selected_obj, dict):
            title, detail = _describe_passthrough_event(selected_obj)
            self._prop_title.set_label(title)
            self._key_info_label.set_label(detail)
            self._press_label.set_label("At:")
            self._duration_text_label.set_visible(False)
            self._duration_spin.set_visible(False)
            self._duration_unit_label.set_visible(False)
            self._release_label.set_visible(False)
            self._release_spin.set_visible(False)
            self._release_unit_label.set_visible(False)
            self._change_key_btn.set_visible(False)
            self._move_row.set_visible(False)
            self._gap_row.set_visible(False)
            self._control_row.set_visible(False)

            self._updating_props = True
            try:
                self._press_spin.set_value(int(selected_obj.get("t_us", 0)) / 1000)
            finally:
                self._updating_props = False
            return

        assert isinstance(selected_obj, EditableEvent)
        ev = selected_obj
        name = _get_key_name(ev.code)
        self._prop_title.set_label(name)
        self._key_info_label.set_label(f"{name} (code {ev.code})")
        self._press_label.set_label("Press:")
        self._duration_text_label.set_visible(True)
        self._duration_spin.set_visible(True)
        self._duration_unit_label.set_visible(True)
        self._release_label.set_visible(True)
        self._release_spin.set_visible(True)
        self._release_unit_label.set_visible(True)
        self._change_key_btn.set_visible(True)
        self._move_row.set_visible(False)
        self._gap_row.set_visible(False)

        self._updating_props = True
        try:
            self._press_spin.set_value(ev.press_t_us / 1000)
            self._duration_spin.set_value(max(1, round((ev.release_t_us - ev.press_t_us) / 1000)))
            self._release_spin.set_value(ev.release_t_us / 1000)
        finally:
            self._updating_props = False

    def _refresh_after_key_timing_change(self, ev: EditableEvent) -> None:
        self._events.sort(key=lambda e: e.press_t_us)
        self._recompute_duration()
        self._update_stats()
        self._update_canvas_width()
        self._timeline.queue_draw()
        self._on_selection_changed(ev)

    def _refresh_after_passthrough_timing_change(self, ev: dict) -> None:
        self._passthrough_events.sort(key=lambda e: int(e.get("t_us", 0)))
        self._recompute_duration()
        self._update_stats()
        self._update_canvas_width()
        self._timeline.queue_draw()
        self._on_selection_changed(ev)

    def _on_press_changed(self, spin) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if selected_obj is None:
            return
        new_t = int(spin.get_value() * 1000)
        if isinstance(selected_obj, EditableControl):
            selected_obj.t_us = max(0, new_t)
            self._refresh_after_control_change(selected_obj)
            return
        if isinstance(selected_obj, EditableMove):
            old_t = int(selected_obj.t_us)
            selected_obj.t_us = max(0, new_t)
            if selected_obj.mode == "gap":
                self._move_gap_note(selected_obj, old_t)
            else:
                self._synthetic_moves.sort(key=lambda m: m.t_us)
                self._on_selection_changed(selected_obj)
                self._update_stats()
                self._timeline.queue_draw()
            return
        if isinstance(selected_obj, dict):
            selected_obj["t_us"] = max(0, new_t)
            self._refresh_after_passthrough_timing_change(selected_obj)
            return

        assert isinstance(selected_obj, EditableEvent)
        ev = selected_obj
        dur = ev.release_t_us - ev.press_t_us
        ev.press_t_us = new_t
        ev.release_t_us = new_t + dur
        self._refresh_after_key_timing_change(ev)

    def _on_release_changed(self, spin) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if selected_obj is None or isinstance(selected_obj, (EditableMove, EditableControl, dict)):
            return
        assert isinstance(selected_obj, EditableEvent)
        ev = selected_obj
        new_t = int(spin.get_value() * 1000)
        min_release = ev.press_t_us + 1000
        if new_t < min_release:
            new_t = min_release
        ev.release_t_us = new_t
        self._refresh_after_key_timing_change(ev)

    def _on_duration_changed(self, spin) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if selected_obj is None or isinstance(selected_obj, (EditableMove, EditableControl, dict)):
            return
        assert isinstance(selected_obj, EditableEvent)
        ev = selected_obj
        duration_us = max(1000, int(spin.get_value() * 1000))
        ev.release_t_us = ev.press_t_us + duration_us
        self._refresh_after_key_timing_change(ev)

    def _on_delete_event(self, btn) -> None:
        self._delete_event(self._timeline._selected)

    def _delete_event(self, ev) -> None:
        deleted = False
        if isinstance(ev, EditableEvent) and ev in self._events:
            self._events.remove(ev)
            deleted = True
        elif isinstance(ev, dict) and ev in self._passthrough_events:
            self._passthrough_events.remove(ev)
            deleted = True
        elif isinstance(ev, EditableMove) and ev in self._synthetic_moves:
            self._synthetic_moves.remove(ev)
            deleted = True
        elif isinstance(ev, EditableControl) and ev in self._control_events:
            self._control_events.remove(ev)
            deleted = True

        if deleted:
            if self._timeline._selected is ev:
                self._timeline._selected = None
                self._revealer.set_reveal_child(False)
            self._refresh_after_timing_edit()

    def _on_move_lock_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._drag_locked = btn.get_active()
        btn.set_label("Lock Move" if self._drag_locked else "Move Unlocked")

    def _on_change_key_clicked(self, btn) -> None:
        ev = self._timeline._selected
        if ev is None or isinstance(ev, (EditableMove, EditableControl, dict)):
            return
        assert isinstance(ev, EditableEvent)

        from keyforge.common.models import ActionType, MappingAction
        from keyforge.gui.widgets.key_selector_dialog import KeySelectorDialog

        # Build a MappingAction that pre-selects the current key in the dialog.
        key_name_lower = _get_key_name(ev.code).lower()
        if ev.device_type == "keyboard":
            current_action = MappingAction(
                action_type=ActionType.KEYBOARD,
                target=key_name_lower,
            )
        else:
            current_action = MappingAction(
                action_type=ActionType.MOUSE,
                target=key_name_lower,
            )

        dialog = KeySelectorDialog(self._parent, _get_key_name(ev.code), current_action)
        dialog.connect("key-selected", self._on_key_selected_for_edit)
        dialog.present(self._parent)

    def _on_key_selected_for_edit(self, dialog, action) -> None:
        from keyforge.common.models import ActionType

        ev = self._timeline._selected
        if ev is None or action is None or isinstance(ev, (EditableMove, EditableControl, dict)):
            return
        assert isinstance(ev, EditableEvent)

        target = getattr(action, "target", None)
        if action.action_type == ActionType.KEYBOARD and target:
            code = getattr(evdev.ecodes, target.upper(), None)
            if code is not None:
                ev.code = code
                ev.device_type = "keyboard"
        elif action.action_type == ActionType.MOUSE and target:
            code = getattr(evdev.ecodes, target.upper(), None)
            if code is not None:
                ev.code = code
                ev.device_type = "mouse"
        else:
            return

        self._on_selection_changed(ev)
        self._timeline.queue_draw()

    def _on_move_x_changed(self, spin) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableMove):
            return
        old_gap_ms = int(selected_obj.x)
        selected_obj.x = int(spin.get_value())
        if selected_obj.mode == "gap" and selected_obj.x < 1:
            selected_obj.x = 1
            self._updating_props = True
            try:
                spin.set_value(1)
            finally:
                self._updating_props = False
        if selected_obj.mode == "gap":
            self._change_gap_note_amount(selected_obj, old_gap_ms)
            return
        self._on_selection_changed(selected_obj)
        self._timeline.queue_draw()

    def _on_move_y_changed(self, spin) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableMove):
            return
        selected_obj.y = int(spin.get_value())
        self._on_selection_changed(selected_obj)
        self._timeline.queue_draw()

    def _on_gap_scope_changed(self, combo: Gtk.DropDown, _pspec=None) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableMove) or selected_obj.mode != "gap":
            return
        old_scope = selected_obj.scope
        selected_obj.scope = _get_dropdown_selected_id(combo, _SCOPE_OPTIONS, "all")
        self._change_gap_note_scope(selected_obj, old_scope)

    def _refresh_after_control_change(self, control: EditableControl) -> None:
        self._control_events.sort(key=lambda c: c.t_us)
        if self._control_affects_timing(control):
            self._recompute_duration()
        self._update_stats()
        self._update_canvas_width()
        self._timeline.queue_draw()
        self._on_selection_changed(control)

    def _update_timeout_clamp_hint(self, timeout_ms: int) -> None:
        max_timeout = max(1, int(self._macro_exec_timeout_max_ms))
        if timeout_ms > max_timeout:
            self._control_timeout_hint_label.set_label(
                f"Runtime clamp: {timeout_ms}ms -> {max_timeout}ms"
            )
        else:
            self._control_timeout_hint_label.set_label(f"Policy max timeout: {max_timeout}ms")

    def _on_control_a_changed(self, spin: Gtk.SpinButton) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableControl):
            return
        if selected_obj.mode == "wait_fixed":
            selected_obj.duration_ms = max(1, int(spin.get_value()))
        elif selected_obj.mode == "wait_random":
            selected_obj.min_ms = max(1, int(spin.get_value()))
            if selected_obj.max_ms < selected_obj.min_ms:
                selected_obj.max_ms = selected_obj.min_ms
        self._refresh_after_control_change(selected_obj)

    def _on_control_b_changed(self, spin: Gtk.SpinButton) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableControl):
            return
        if selected_obj.mode == "wait_random":
            selected_obj.max_ms = max(1, int(spin.get_value()))
            if selected_obj.max_ms < selected_obj.min_ms:
                selected_obj.max_ms = selected_obj.min_ms
            self._refresh_after_control_change(selected_obj)

    def _on_control_command_changed(self, entry: Gtk.Entry) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableControl):
            return
        if selected_obj.mode in {"exec_sync", "exec_async"}:
            selected_obj.command = entry.get_text().strip()
            self._refresh_after_control_change(selected_obj)

    def _on_control_timeout_changed(self, spin: Gtk.SpinButton) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableControl):
            return
        if selected_obj.mode == "exec_sync":
            selected_obj.timeout_ms = max(1, int(spin.get_value()))
            self._update_timeout_clamp_hint(selected_obj.timeout_ms)
            self._refresh_after_control_change(selected_obj)

    def _on_control_inhibit_toggled(self, check: Gtk.CheckButton) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableControl):
            return
        if selected_obj.mode == "exec_sync":
            selected_obj.inhibit_mouse = bool(check.get_active())
            self._refresh_after_control_change(selected_obj)

    # ------------------------------------------------------------------
    # Zoom
    # ------------------------------------------------------------------

    def _on_reset_fit(self, _btn) -> None:
        self._auto_zoom_enabled = True
        self._set_timeline_scroll(0.0)
        self._update_canvas_width()
        self._timeline.queue_draw()

    def _on_undo_all_changes(self, _btn) -> None:
        if not self._initial_macro_data:
            return

        self._cancel_capture_start_position("")
        self._apply_macro_state(self._initial_macro_data)

        self._timeline._selected = None
        self._revealer.set_reveal_child(False)
        self._timeline._context_menu_x = None
        self._timeline._hover_x = None
        self._timeline._hover_y = None

        self._name_entry.set_text(self._macro_name)
        _set_dropdown_selected_id(
            self._macro_loop_mode_combo,
            _LOOP_MODE_OPTIONS,
            self._macro_loop_mode,
        )
        self._macro_loop_count_spin.set_value(self._macro_loop_count)
        self._update_loop_controls()
        self._macro_move_to_start_check.set_active(self._macro_move_to_start)
        self._macro_start_x_spin.set_value(self._macro_start_x)
        self._macro_start_y_spin.set_value(self._macro_start_y)
        self._macro_capture_delay_spin.set_value(self._capture_delay_seconds)
        self._macro_block_mouse_check.set_active(self._macro_block_mouse_movement)
        self._update_macro_move_start_controls()

        self._auto_zoom_enabled = True
        self._set_timeline_scroll(0.0)
        self._update_stats()
        self._update_canvas_width()
        self._timeline.queue_draw()

    def _on_zoom_in(self, btn) -> None:
        self._zoom_timeline(1.25)

    def _on_zoom_out(self, btn) -> None:
        self._zoom_timeline(1.0 / 1.25)

    # ------------------------------------------------------------------
    # Add event popovers
    # ------------------------------------------------------------------

    def _on_add_key(self, btn) -> None:
        self._present_add_key_dialog()

    def _on_add_click(self, btn) -> None:
        self._show_add_click_popover(btn)

    def _on_add_move_rel(self, btn) -> None:
        self._show_add_move_popover(btn, mode="rel")

    def _on_add_move_abs(self, btn) -> None:
        self._show_add_move_popover(btn, mode="abs")

    def _show_insert_gap_popover(
        self,
        anchor: Gtk.Widget,
        default_t_us: int | None = None,
        default_scope: str = "all",
        pointing_to=None,
    ) -> None:
        popover = Gtk.Popover()
        popover.set_parent(anchor)
        if pointing_to is not None:
            popover.set_pointing_to(pointing_to)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        title = Gtk.Label(label="Insert Gap Note")
        title.add_css_class("heading")
        box.append(title)

        at_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        at_row.append(Gtk.Label(label="At:"))
        at_spin = Gtk.SpinButton()
        at_spin.set_adjustment(
            Gtk.Adjustment(
                value=(default_t_us or 0) / 1000,
                lower=0,
                upper=3600000,
                step_increment=1,
            )
        )
        at_spin.set_digits(0)
        at_spin.set_width_chars(7)
        at_row.append(at_spin)
        at_row.append(Gtk.Label(label="ms"))
        box.append(at_row)

        gap_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        gap_row.append(Gtk.Label(label="Gap:"))
        gap_spin = Gtk.SpinButton()
        gap_spin.set_adjustment(Gtk.Adjustment(value=100, lower=1, upper=60000, step_increment=10))
        gap_spin.set_digits(0)
        gap_spin.set_width_chars(7)
        gap_row.append(gap_spin)
        gap_row.append(Gtk.Label(label="ms"))
        box.append(gap_row)

        scope_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        scope_row.append(Gtk.Label(label="Scope:"))
        scope_combo = _build_option_dropdown(_SCOPE_OPTIONS, default_scope,)
        scope_row.append(scope_combo)
        box.append(scope_row)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _btn: popover.popdown())
        btn_row.append(cancel)

        insert = Gtk.Button(label="Insert")
        insert.add_css_class("suggested-action")

        def on_insert(_btn) -> None:
            self._insert_gap(
                at_us=int(at_spin.get_value() * 1000),
                gap_us=int(gap_spin.get_value() * 1000),
                scope=_get_dropdown_selected_id(scope_combo, _SCOPE_OPTIONS, "all"),
                add_note=True,
            )
            popover.popdown()

        insert.connect("clicked", on_insert)
        btn_row.append(insert)
        box.append(btn_row)

        popover.set_child(box)
        popover.popup()

    def _show_add_move_popover(
        self,
        anchor: Gtk.Widget,
        mode: str = "rel",
        default_t_us: int | None = None,
        pointing_to=None,
    ) -> None:
        popover = Gtk.Popover()
        popover.set_parent(anchor)
        if pointing_to is not None:
            popover.set_pointing_to(pointing_to)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        title = Gtk.Label(label=f"Add Mouse Move ({mode.upper()})")
        title.add_css_class("heading")
        box.append(title)

        at_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        at_row.append(Gtk.Label(label="At time:"))
        if default_t_us is not None:
            default_t = default_t_us / 1e6
        else:
            default_t = (self._duration_us / 1e6 / 2) if self._duration_us else 0.5
        at_spin = Gtk.SpinButton()
        at_spin.set_adjustment(
            Gtk.Adjustment(value=default_t, lower=0, upper=3600, step_increment=0.1)
        )
        at_spin.set_digits(3)
        at_row.append(at_spin)
        at_row.append(Gtk.Label(label="s"))
        box.append(at_row)

        xy_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        xy_row.append(Gtk.Label(label="X:"))
        x_spin = Gtk.SpinButton()
        x_spin.set_adjustment(
            Gtk.Adjustment(
                value=200 if mode == "abs" else 50, lower=-10000, upper=10000, step_increment=1
            )
        )
        x_spin.set_digits(0)
        x_spin.set_width_chars(7)
        xy_row.append(x_spin)
        xy_row.append(Gtk.Label(label="Y:"))
        y_spin = Gtk.SpinButton()
        y_spin.set_adjustment(
            Gtk.Adjustment(
                value=200 if mode == "abs" else 50, lower=-10000, upper=10000, step_increment=1
            )
        )
        y_spin.set_digits(0)
        y_spin.set_width_chars(7)
        xy_row.append(y_spin)
        box.append(xy_row)

        hint = Gtk.Label(
            label=(
                "ABS uses ydotool-style reset-to-edge then move-to-target"
                if mode == "abs"
                else "REL applies direct delta movement"
            )
        )
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        hint.set_halign(Gtk.Align.START)
        box.append(hint)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _: popover.popdown())
        btn_row.append(cancel)
        add = Gtk.Button(label="Add")
        add.add_css_class("suggested-action")

        def on_add(_btn):
            move = EditableMove(
                mode=mode,
                t_us=int(at_spin.get_value() * 1e6),
                x=int(x_spin.get_value()),
                y=int(y_spin.get_value()),
            )
            self._synthetic_moves.append(move)
            self._synthetic_moves.sort(key=lambda m: m.t_us)
            self._duration_us = max(self._duration_us, move.t_us + (1 if move.mode == "abs" else 0))
            self._update_stats()
            self._timeline.queue_draw()
            popover.popdown()

        add.connect("clicked", on_add)
        btn_row.append(add)
        box.append(btn_row)

        popover.set_child(box)
        popover.popup()

    def _insert_control_event(self, control: EditableControl) -> None:
        self._control_events.append(control)
        self._control_events.sort(key=lambda c: c.t_us)
        self._timeline._selected = control
        if self._control_affects_timing(control):
            self._refresh_after_timing_edit()
            self._on_selection_changed(control)
            return
        self._update_stats()
        self._update_canvas_width()
        self._timeline.queue_draw()
        self._on_selection_changed(control)

    def _show_add_control_popover(
        self,
        anchor: Gtk.Widget,
        control_mode: str,
        default_t_us: int | None = None,
        pointing_to=None,
    ) -> None:
        popover = Gtk.Popover()
        popover.set_parent(anchor)
        if pointing_to is not None:
            popover.set_pointing_to(pointing_to)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        title_text = {
            "wait_fixed": "Insert Wait (Fixed)",
            "wait_random": "Insert Wait (Random)",
            "exec_sync": "Insert Exec Sync",
            "exec_async": "Insert Exec Async",
        }.get(control_mode, "Insert Control")

        title = Gtk.Label(label=title_text)
        title.add_css_class("heading")
        title.set_halign(Gtk.Align.START)
        box.append(title)

        at_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        at_row.append(Gtk.Label(label="At (ms):"))
        at_spin = Gtk.SpinButton()
        at_spin.set_adjustment(
            Gtk.Adjustment(
                value=(default_t_us or 0) / 1000, lower=0, upper=3600000, step_increment=1
            )
        )
        at_spin.set_digits(0)
        at_spin.set_width_chars(7)
        at_row.append(at_spin)
        box.append(at_row)

        duration_spin: Gtk.SpinButton | None = None
        min_spin: Gtk.SpinButton | None = None
        max_spin: Gtk.SpinButton | None = None
        timeout_spin: Gtk.SpinButton | None = None
        inhibit_check: Gtk.CheckButton | None = None
        cmd_entry: Gtk.Entry | None = None

        if control_mode == "wait_fixed":
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.append(Gtk.Label(label="Duration (ms):"))
            duration_spin_widget = Gtk.SpinButton()
            duration_spin = duration_spin_widget
            duration_spin_widget.set_adjustment(
                Gtk.Adjustment(value=100, lower=1, upper=600000, step_increment=10)
            )
            duration_spin_widget.set_digits(0)
            duration_spin_widget.set_width_chars(8)
            row.append(duration_spin_widget)
            box.append(row)
        elif control_mode == "wait_random":
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.append(Gtk.Label(label="Min (ms):"))
            min_spin_widget = Gtk.SpinButton()
            min_spin = min_spin_widget
            min_spin_widget.set_adjustment(
                Gtk.Adjustment(value=50, lower=1, upper=600000, step_increment=10)
            )
            min_spin_widget.set_digits(0)
            min_spin_widget.set_width_chars(7)
            row.append(min_spin_widget)
            row.append(Gtk.Label(label="Max (ms):"))
            max_spin_widget = Gtk.SpinButton()
            max_spin = max_spin_widget
            max_spin_widget.set_adjustment(
                Gtk.Adjustment(value=150, lower=1, upper=600000, step_increment=10)
            )
            max_spin_widget.set_digits(0)
            max_spin_widget.set_width_chars(7)
            row.append(max_spin_widget)
            box.append(row)
        elif control_mode in {"exec_sync", "exec_async"}:
            cmd_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            cmd_label = Gtk.Label(label="Command:")
            cmd_label.set_halign(Gtk.Align.START)
            cmd_row.append(cmd_label)
            cmd_entry_widget = Gtk.Entry()
            cmd_entry = cmd_entry_widget
            cmd_entry_widget.set_placeholder_text("/absolute/path/to/script.sh")
            cmd_row.append(cmd_entry_widget)
            box.append(cmd_row)

            if control_mode == "exec_sync":
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                row.append(Gtk.Label(label="Timeout (ms):"))
                timeout_spin_widget = Gtk.SpinButton()
                timeout_spin = timeout_spin_widget
                timeout_spin_widget.set_adjustment(
                    Gtk.Adjustment(
                        value=min(30000, self._macro_exec_timeout_max_ms),
                        lower=1,
                        upper=self._macro_exec_timeout_max_ms,
                        step_increment=100,
                    )
                )
                timeout_spin_widget.set_digits(0)
                timeout_spin_widget.set_width_chars(8)
                row.append(timeout_spin_widget)
                box.append(row)

                timeout_hint = Gtk.Label(
                    label=f"Policy max timeout: {self._macro_exec_timeout_max_ms}ms"
                )
                timeout_hint.add_css_class("dim-label")
                timeout_hint.set_halign(Gtk.Align.START)
                box.append(timeout_hint)

                inhibit_check_widget = Gtk.CheckButton(label="Inhibit mouse movement while waiting")
                inhibit_check = inhibit_check_widget
                inhibit_check_widget.set_active(False)
                box.append(inhibit_check_widget)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _b: popover.popdown())
        footer.append(cancel_btn)

        add_btn = Gtk.Button(label="Insert")
        add_btn.add_css_class("suggested-action")

        def on_insert(_b) -> None:
            t_us = int(at_spin.get_value() * 1000)
            control = EditableControl(mode=control_mode, t_us=t_us)

            if control_mode == "wait_fixed" and duration_spin is not None:
                control.duration_ms = max(1, int(duration_spin.get_value()))
            elif control_mode == "wait_random" and min_spin is not None and max_spin is not None:
                mn = max(1, int(min_spin.get_value()))
                mx = max(mn, int(max_spin.get_value()))
                control.min_ms = mn
                control.max_ms = mx
            elif control_mode in {"exec_sync", "exec_async"} and cmd_entry is not None:
                command = cmd_entry.get_text().strip()
                control.command = command
                if control_mode == "exec_sync":
                    control.timeout_ms = (
                        max(1, int(timeout_spin.get_value())) if timeout_spin is not None else 30000
                    )
                    control.inhibit_mouse = bool(
                        inhibit_check.get_active() if inhibit_check is not None else False
                    )

            self._insert_control_event(control)
            popover.popdown()

        add_btn.connect("clicked", on_insert)
        footer.append(add_btn)
        box.append(footer)

        popover.set_child(box)
        popover.popup()

    def _present_add_key_dialog(self, default_t_us: int | None = None) -> None:
        from keyforge.common.models import ActionType, MappingAction
        from keyforge.gui.widgets.key_selector_dialog import KeySelectorDialog

        if default_t_us is None:
            default_t_us = int((self._duration_us / 2) if self._duration_us else 500000)

        dialog = KeySelectorDialog(
            self._parent,
            "Add Keystroke",
            MappingAction(action_type=ActionType.KEYBOARD),
        )
        dialog.connect(
            "key-selected",
            lambda picker, action, at_us=default_t_us: self._on_key_selected_for_insert(
                picker, action, at_us
            ),
        )
        dialog.present(self._parent)

    def _on_key_selected_for_insert(self, dialog, action, default_t_us: int) -> None:
        from keyforge.common.models import ActionType

        if action is None or action.action_type != ActionType.KEYBOARD:
            return

        target = getattr(action, "target", None)
        if not target:
            return

        code = getattr(evdev.ecodes, str(target).upper(), None)
        if code is None:
            return

        ev = EditableEvent(
            device_type="keyboard",
            ev_type=evdev.ecodes.EV_KEY,
            code=code,
            press_t_us=max(0, int(default_t_us)),
            release_t_us=max(1000, int(default_t_us) + 50000),
        )
        self._events.append(ev)
        self._events.sort(key=lambda item: item.press_t_us)
        self._timeline._selected = ev
        self._revealer.set_reveal_child(True)
        self._refresh_after_key_timing_change(ev)

    def _show_add_click_popover(
        self,
        anchor: Gtk.Widget,
        default_t_us: int | None = None,
        pointing_to=None,
    ) -> None:
        popover = Gtk.Popover()
        popover.set_parent(anchor)
        if pointing_to is not None:
            popover.set_pointing_to(pointing_to)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        title = Gtk.Label(label="Add Mouse Click")
        title.add_css_class("heading")
        box.append(title)

        at_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        at_row.append(Gtk.Label(label="At time:"))
        if default_t_us is not None:
            default_t = default_t_us / 1e6
        else:
            default_t = (self._duration_us / 1e6 / 2) if self._duration_us else 0.5
        at_spin = Gtk.SpinButton()
        at_spin.set_adjustment(
            Gtk.Adjustment(value=default_t, lower=0, upper=3600, step_increment=0.1)
        )
        at_spin.set_digits(3)
        at_row.append(at_spin)
        at_row.append(Gtk.Label(label="s"))
        box.append(at_row)

        mouse_buttons = [
            ("Left Button", evdev.ecodes.BTN_LEFT),
            ("Right Button", evdev.ecodes.BTN_RIGHT),
            ("Middle Button", evdev.ecodes.BTN_MIDDLE),
            ("Side Button", evdev.ecodes.BTN_SIDE),
            ("Extra Button", evdev.ecodes.BTN_EXTRA),
        ]

        btn_ui_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_ui_row.append(Gtk.Label(label="Button:"))
        btn_model = Gtk.StringList()
        for name, _ in mouse_buttons:
            btn_model.append(name)
        btn_dropdown = Gtk.DropDown()
        btn_dropdown.set_model(btn_model)
        btn_dropdown.set_size_request(160, -1)
        btn_ui_row.append(btn_dropdown)
        box.append(btn_ui_row)

        hold_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hold_row.append(Gtk.Label(label="Hold:"))
        hold_spin = Gtk.SpinButton()
        hold_spin.set_adjustment(
            Gtk.Adjustment(value=0.080, lower=0.001, upper=10, step_increment=0.010)
        )
        hold_spin.set_digits(3)
        hold_row.append(hold_spin)
        hold_row.append(Gtk.Label(label="s"))
        box.append(hold_row)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _: popover.popdown())
        footer.append(cancel)

        add = Gtk.Button(label="Add")
        add.add_css_class("suggested-action")

        def on_add(_btn):
            t_us = int(at_spin.get_value() * 1e6)
            hold_us = int(hold_spin.get_value() * 1e6)
            idx = btn_dropdown.get_selected()
            _, code = mouse_buttons[idx]
            ev = EditableEvent(
                device_type="mouse",
                ev_type=evdev.ecodes.EV_KEY,
                code=code,
                press_t_us=t_us,
                release_t_us=t_us + hold_us,
            )
            self._events.append(ev)
            self._events.sort(key=lambda e: e.press_t_us)
            self._update_stats()
            self._timeline.queue_draw()
            popover.popdown()

        add.connect("clicked", on_add)
        footer.append(add)
        box.append(footer)

        popover.set_child(box)
        popover.popup()

    # ------------------------------------------------------------------
    # Save / Save as copy
    # ------------------------------------------------------------------

    def _on_save(self, btn) -> None:
        new_name = self._name_entry.get_text().strip()
        if not self._validate_name_for_save(new_name):
            return

        macro_payload = self._build_macro_payload(new_name)
        revision = int(self._macro_data.get("revision", 1))
        run_gui_task(
            lambda: self._save_macro_request(new_name, macro_payload, revision),
            lambda result, requested_name=new_name: self._on_save_finished(result, requested_name),
            on_start=lambda: btn.set_sensitive(False),
            on_done=lambda: btn.set_sensitive(True),
        )

    def _save_macro_request(
        self,
        new_name: str,
        macro_payload: dict,
        revision: int,
    ) -> dict | None:
        if not self._macro_exists:
            return session_request({"command": "create_macro", "macro": macro_payload}) or {}

        if new_name != self._macro_name:
            create_result = (
                session_request({"command": "create_macro", "macro": macro_payload}) or {}
            )
            if create_result.get("status") != "ok":
                return create_result
            session_request(
                {
                    "command": "delete_macro",
                    "name": self._macro_name,
                    "expected_revision": revision,
                }
            )
            return create_result

        return (
            session_request(
                {
                    "command": "update_macro",
                    "name": self._macro_name,
                    "macro": macro_payload,
                    "expected_revision": revision,
                }
            )
            or {}
        )

    def _on_save_finished(self, result: dict | None, requested_name: str) -> bool:
        if (result or {}).get("status") != "ok":
            self._show_name_conflict(requested_name)
            return False

        notify_session_reload_async()
        self.close()
        return False

    def _on_save_as_copy(self, btn) -> None:
        dialog = Adw.Dialog(title="Save as Copy", content_width=360)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        lbl = Gtk.Label(label="Name for the copy:")
        lbl.set_halign(Gtk.Align.START)
        box.append(lbl)

        entry = Gtk.Entry()
        entry.set_text(f"{self._macro_name}_copy")
        entry.select_region(0, -1)
        box.append(entry)

        error_lbl = Gtk.Label()
        error_lbl.add_css_class("error")
        error_lbl.add_css_class("caption")
        error_lbl.set_halign(Gtk.Align.START)
        error_lbl.set_visible(False)
        box.append(error_lbl)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.END)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _: dialog.close())
        btn_row.append(cancel)

        save = Gtk.Button(label="Save Copy")
        save.add_css_class("suggested-action")

        def on_save_copy(_b):
            name = entry.get_text().strip()
            if not name:
                error_lbl.set_label("Name cannot be empty")
                error_lbl.set_visible(True)
                return
            if not re.match(r"^[a-zA-Z0-9_\-]+$", name):
                error_lbl.set_label("Only letters, numbers, underscores and hyphens")
                error_lbl.set_visible(True)
                return
            copy_payload = self._build_macro_payload(name)
            run_gui_task(
                lambda: session_request({"command": "create_macro", "macro": copy_payload}) or {},
                lambda result, requested_name=name: self._on_save_copy_finished(
                    result,
                    requested_name,
                    error_lbl,
                    dialog,
                ),
                on_start=lambda: save.set_sensitive(False),
                on_done=lambda: save.set_sensitive(True),
            )

        save.connect("clicked", on_save_copy)
        entry.connect("activate", on_save_copy)
        btn_row.append(save)
        box.append(btn_row)

        dialog.set_child(box)
        dialog.present(self._parent)

    def _on_save_copy_finished(
        self,
        result: dict | None,
        requested_name: str,
        error_label: Gtk.Label,
        dialog: Adw.Dialog,
    ) -> bool:
        if (result or {}).get("status") != "ok":
            error_label.set_label(
                (result or {}).get("message", f"'{requested_name}' already exists")
            )
            error_label.set_visible(True)
            return False
        notify_session_reload_async()
        dialog.close()
        return False

    def _validate_name_for_save(self, name: str) -> bool:
        if not name:
            return False
        if not re.match(r"^[a-zA-Z0-9_\-]+$", name):
            return False
        return True

    def _show_name_conflict(self, name: str) -> None:
        d = Adw.AlertDialog()
        d.set_heading("Name Conflict")
        d.set_body(f"A macro named '{name}' already exists. Choose a different name.")
        d.add_response("ok", "OK")
        d.present(self._parent)

    def _build_macro_payload(self, name: str) -> dict:
        raw_events = reconstruct_events(
            self._events,
            self._rel_events,
            self._passthrough_events,
            self._synthetic_moves,
            self._control_events,
        )

        duration_us = self._duration_us
        if raw_events:
            duration_us = max(duration_us, max(e["t_us"] for e in raw_events))

        device_types = list({e.device_type for e in self._events})
        if self._rel_events:
            if "mouse" not in device_types:
                device_types.append("mouse")
        if self._synthetic_moves:
            if "mouse" not in device_types:
                device_types.append("mouse")

        data = dict(self._macro_data)
        data["name"] = name
        data["events"] = raw_events
        data["gap_notes"] = [
            {
                "at_us": int(m.t_us),
                "gap_ms": int(m.x),
                "scope": str(m.scope),
            }
            for m in self._synthetic_moves
            if m.mode == "gap"
        ]
        data["duration_ms"] = duration_us // 1000
        data["device_types"] = device_types
        data["loop_mode"] = _get_dropdown_selected_id(
            self._macro_loop_mode_combo,
            _LOOP_MODE_OPTIONS,
            "none",
        )
        data["loop_count"] = max(1, int(self._macro_loop_count_spin.get_value()))
        data["move_to_start"] = bool(self._macro_move_to_start_check.get_active())
        data["start_x"] = int(self._macro_start_x_spin.get_value())
        data["start_y"] = int(self._macro_start_y_spin.get_value())
        data["block_mouse_movement"] = bool(self._macro_block_mouse_check.get_active())
        return data

    def close(self) -> None:
        self._cancel_capture_start_position("")
        super().close()
