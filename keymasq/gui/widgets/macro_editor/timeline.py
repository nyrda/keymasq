import gi

# pyright: reportUnknownLambdaType=false

gi.require_version("Gtk", "4.0")

from bisect import bisect_right
from typing import Any

import evdev
from gi.repository import Gdk, GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.widgets.macro_editor import timeline_render
from keymasq.gui.widgets.macro_editor.gaps import (
    GapItem,
    TimelineGap,
    build_timeline_gaps,
    build_track_gaps,
)
from keymasq.gui.widgets.macro_editor.model import (
    EditableControl,
    EditableEvent,
    EditableMove,
    MacroEvent,
    _assign_lanes,
    _describe_passthrough_event,
    _format_time_us,
    _get_event_name,
    _get_key_name,
    _passthrough_track,
)

# ---------------------------------------------------------------------------
# Timeline DrawingArea
# ---------------------------------------------------------------------------


class TimelineWidget(Gtk.DrawingArea):
    """
    Custom Gtk.DrawingArea that renders the macro timeline with four tracks:
      K  — keyboard key press/release rectangles
      M  — mouse click press/release rectangles
      G  — gamepad button press/release rectangles
      ≈  — mouse movement waveform (read-only, from EV_REL events)

    The left 28px column is used for track labels and scrolls with the content.
    Horizontal scrolling is driven by the editor's custom offset model; the
    widget receives the current offset via set_scroll_offset().
    """

    LABEL_WIDTH = 28
    TIMELINE_PAD = 10  # px gap between the label column and t=0, so edge markers stay visible
    RULER_HEIGHT = 24
    TRACK_HEIGHT = 88  # minimum track height; expands when lanes > 2
    LANE_HEIGHT_MIN = 32  # minimum height per sub-lane
    MIN_EVENT_WIDTH = 4
    EDGE_SCROLL_MARGIN = 36  # px zone at each viewport edge that triggers auto-scroll
    EDGE_SCROLL_MAX_SPEED = 900.0  # px/s at the deepest point of the margin

    def __init__(self, editor: Any):
        super().__init__()
        self._editor = editor
        self._pps = 200.0  # pixels per second (zoom level)
        self._scroll_offset = 0.0  # horizontal scroll offset in px
        self._selected: object | None = None
        self._hover_x: float | None = None
        self._hover_y: float | None = None
        self._context_menu_x: float | None = None
        self._gap_segments: list[TimelineGap] = []
        self._gap_next_starts: list[int] = []
        self._positive_gap_starts: list[int] = []
        self._positive_gaps: list[TimelineGap] = []
        self._track_gaps: dict[str, list[TimelineGap]] = {}
        self._track_next_starts: dict[str, list[int]] = {}
        self._track_positive_gaps: dict[str, list[TimelineGap]] = {}
        self._track_positive_starts: dict[str, list[int]] = {}
        self._hover_gap: TimelineGap | None = None
        self._selected_gap: TimelineGap | None = None
        self._suppressed_hover_gap: TimelineGap | None = None
        self._gap_double_click_handled = False
        self._gap_popover: Gtk.Popover | None = None
        self._gap_spin: Gtk.SpinButton | None = None
        self._gap_next_only_check: Gtk.CheckButton | None = None
        self._gap_track_following_check: Gtk.CheckButton | None = None
        self._gap_timeline_following_check: Gtk.CheckButton | None = None
        self._gap_cancel_button: Gtk.Button | None = None
        self._gap_apply_button: Gtk.Button | None = None

        # Lane assignment — recomputed before each draw
        self._kb_lanes: dict[int, int] = {}  # id(ev) -> lane index
        self._m_lanes: dict[int, int] = {}
        self._g_lanes: dict[int, int] = {}
        self._kb_num_lanes: int = 1
        self._m_num_lanes: int = 1
        self._g_num_lanes: int = 1

        # Drag state
        self._drag_event: EditableEvent | None = None
        self._drag_move: EditableMove | None = None
        self._drag_control: EditableControl | None = None
        self._drag_selected_obj: object | None = None
        self._drag_selected_gap: TimelineGap | None = None
        self._drag_orig_press: int = 0
        self._drag_orig_release: int = 0
        self._in_drag: bool = False

        # Edge auto-scroll while dragging an event past the visible slice.
        # The drag delta is gesture-relative, so the scroll movement since
        # drag-begin has to be folded into the applied time delta.
        self._drag_start_x: float = 0.0
        self._drag_start_y: float = 0.0
        self._drag_scroll_origin: float = 0.0
        self._drag_last_offset_x: float = 0.0
        self._autoscroll_velocity: float = 0.0
        self._autoscroll_tick_id: int = 0
        self._autoscroll_last_time: int | None = None

        # Erase-mode drag state. Left-drag bands are clamped to the track the
        # drag started in; a right-drag ripple band uses the sentinel "all".
        # The anchor is kept in time-space so edge auto-scroll can shift the
        # visible slice without the band's fixed end drifting with it.
        self._erase_track: str | None = None
        self._erase_anchor_t_us: int = 0
        self._erase_x0: float | None = None
        self._erase_x1: float | None = None
        self._erase_pending: list[object] = []
        self._right_press_x: float = 0.0
        self._right_press_y: float = 0.0

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

        gap_click = Gtk.GestureClick.new()
        gap_click.set_button(1)
        gap_click.connect("pressed", self._on_gap_click_pressed)
        self.add_controller(gap_click)

        # Right-click context menu
        right_click = Gtk.GestureClick.new()
        right_click.set_button(3)
        right_click.connect("pressed", self._on_right_click)
        self.add_controller(right_click)

        # Right-drag ripple delete (erase mode only): sweeps all lanes at once
        # and collapses the deleted time span.
        right_drag = Gtk.GestureDrag.new()
        right_drag.set_button(3)
        right_drag.connect("drag-begin", self._on_right_drag_begin)
        right_drag.connect("drag-update", self._on_right_drag_update)
        right_drag.connect("drag-end", self._on_right_drag_end)
        self.add_controller(right_drag)

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

    def refresh_gaps(self) -> None:
        """Refresh cached action gaps after the editor document changes."""
        if self._editor._drag_locked:
            self._set_gap_segments([])
            self._set_track_gaps({})
            self._hover_gap = None
            self._selected_gap = None
            self._suppressed_hover_gap = None
            self.queue_draw()
            return
        self._rebuild_gaps()

    def _rebuild_gaps(self) -> None:
        """Build the gap cache when an interaction needs it."""
        self._set_gap_segments(
            build_timeline_gaps(
                self._editor._events,
                self._editor._synthetic_moves,
                self._editor._control_events,
            )
        )
        track_items: dict[str, list[GapItem]] = {}
        for event in self._editor._events:
            if event.device_type in {"keyboard", "mouse", "gamepad"}:
                track_items.setdefault(event.device_type, []).append(event)
        track_items["movement"] = list(self._editor._synthetic_moves)
        track_items["control"] = list(self._editor._control_events)
        self._set_track_gaps(
            {
                track: build_track_gaps(
                    items,
                    track=track,
                )
                for track, items in track_items.items()
            }
        )
        self._hover_gap = None
        self._selected_gap = None
        self._suppressed_hover_gap = None
        self.queue_draw()

    def _set_gap_segments(self, gaps: list[TimelineGap]) -> None:
        self._gap_segments = gaps
        self._gap_next_starts = [gap.next_start_us for gap in gaps]
        self._positive_gaps = [gap for gap in gaps if gap.duration_us > 0]
        self._positive_gap_starts = [gap.previous_end_us for gap in self._positive_gaps]

    def _set_track_gaps(
        self,
        track_gaps: dict[str, list[TimelineGap]],
    ) -> None:
        self._track_gaps = track_gaps
        self._track_next_starts = {
            track: [gap.next_start_us for gap in gaps]
            for track, gaps in track_gaps.items()
        }
        self._track_positive_gaps = {
            track: sorted(
                (gap for gap in gaps if gap.duration_us > 0),
                key=lambda gap: gap.previous_end_us,
            )
            for track, gaps in track_gaps.items()
        }
        self._track_positive_starts = {
            track: [gap.previous_end_us for gap in gaps]
            for track, gaps in self._track_positive_gaps.items()
        }

    def set_move_locked(self, locked: bool) -> None:
        """Update gap interaction state after the Move lock changes."""
        self.clear_gap_selection(suppress_hover=True)
        if locked:
            self._set_gap_segments([])
            self._set_track_gaps({})
        else:
            self._rebuild_gaps()
        if not self._editor._erase_mode:
            self.set_cursor_from_name(None)
        self.queue_draw()

    def _ensure_gap_cache(self) -> None:
        if not self._gap_segments and not self._track_gaps:
            self._rebuild_gaps()

    def clear_gap_selection(self, *, suppress_hover: bool = False) -> None:
        if suppress_hover:
            self._suppressed_hover_gap = self._selected_gap or self._hover_gap
        self._hover_gap = None
        self._selected_gap = None
        if self._gap_popover is not None:
            self._gap_popover.popdown()
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
    def _g_track_h(self) -> int:
        return self._get_track_h(self._g_num_lanes)

    @property
    def _kb_y(self) -> int:
        return self.RULER_HEIGHT

    @property
    def _m_y(self) -> int:
        return self.RULER_HEIGHT + self._kb_track_h

    @property
    def _g_y(self) -> int:
        return self.RULER_HEIGHT + self._kb_track_h + self._m_track_h

    @property
    def _wave_y(self) -> int:
        return self.RULER_HEIGHT + self._kb_track_h + self._m_track_h + self._g_track_h

    @property
    def _total_height(self) -> int:
        return (
            self.RULER_HEIGHT
            + self._kb_track_h
            + self._m_track_h
            + self._g_track_h
            + self.TRACK_HEIGHT
        )

    def _recompute_lanes(self) -> None:
        """Recompute lane assignments from current events and update size request."""
        events = self._editor._events
        self._kb_lanes, self._kb_num_lanes = _assign_lanes(events, "keyboard")
        self._m_lanes, self._m_num_lanes = _assign_lanes(events, "mouse")
        self._g_lanes, self._g_num_lanes = _assign_lanes(events, "gamepad")
        self.set_size_request(-1, self._total_height)

    # ------------------------------------------------------------------
    # Coordinate helpers
    # ------------------------------------------------------------------

    def _time_to_x(self, t_us: int) -> float:
        """Convert a time in microseconds to an x coordinate in the drawing area."""
        return self.LABEL_WIDTH + self.TIMELINE_PAD + t_us / 1e6 * self._pps - self._scroll_offset

    def _x_to_time_us(self, x: float) -> int:
        """Convert a drawing-area x coordinate to microseconds."""
        return int(
            (x - self.LABEL_WIDTH - self.TIMELINE_PAD + self._scroll_offset) / self._pps * 1e6
        )

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def _build_render_state(self) -> timeline_render.TimelineRenderState:
        return timeline_render.TimelineRenderState(
            LABEL_WIDTH=self.LABEL_WIDTH,
            TIMELINE_PAD=self.TIMELINE_PAD,
            RULER_HEIGHT=self.RULER_HEIGHT,
            TRACK_HEIGHT=self.TRACK_HEIGHT,
            MIN_EVENT_WIDTH=self.MIN_EVENT_WIDTH,
            _pps=self._pps,
            _scroll_offset=self._scroll_offset,
            _selected=self._selected,
            _hover_x=self._hover_x,
            _hover_y=self._hover_y,
            _context_menu_x=self._context_menu_x,
            _hover_gap=self._hover_gap,
            _selected_gap=self._selected_gap,
            events=self._editor._events,
            rel_events=self._editor._rel_events,
            passthrough_events=self._editor._passthrough_events,
            synthetic_moves=self._editor._synthetic_moves,
            control_events=self._editor._control_events,
            duration_us=self._editor._duration_us,
            _kb_lanes=self._kb_lanes,
            _m_lanes=self._m_lanes,
            _g_lanes=self._g_lanes,
            _kb_num_lanes=self._kb_num_lanes,
            _m_num_lanes=self._m_num_lanes,
            _g_num_lanes=self._g_num_lanes,
            _kb_track_h=self._kb_track_h,
            _m_track_h=self._m_track_h,
            _g_track_h=self._g_track_h,
            _kb_y=self._kb_y,
            _m_y=self._m_y,
            _g_y=self._g_y,
            _wave_y=self._wave_y,
            _erase_band=(
                (self._erase_track, self._erase_x0, self._erase_x1)
                if self._erase_track is not None
                and self._erase_x0 is not None
                and self._erase_x1 is not None
                else None
            ),
            _erase_pending_ids=frozenset(id(obj) for obj in self._erase_pending),
        )

    def _draw(self, area, cr, width, height, user_data) -> None:
        self._recompute_lanes()
        timeline_render.draw(cr, self._build_render_state(), width, height)

    def _get_passthrough_marker_layouts(
        self,
        track: str,
        width: int,
        y_top: int,
        track_h: int,
    ) -> list[tuple[MacroEvent, float, float, float]]:
        return timeline_render.get_passthrough_marker_layouts(
            self._build_render_state(),
            track,
            width,
            y_top,
            track_h,
        )

    # ------------------------------------------------------------------
    # Hit testing
    # ------------------------------------------------------------------

    def _get_track_at_y(self, y: float) -> str | None:
        """Return 'keyboard', 'mouse', 'gamepad', 'movement', or None (ruler)."""
        if y < self.RULER_HEIGHT:
            return None
        if y < self._m_y:
            return "keyboard"
        if y < self._g_y:
            return "mouse"
        if y < self._wave_y:
            return "gamepad"
        if y < self._wave_y + self.TRACK_HEIGHT:
            return "movement"
        return None

    def _hit_test_move(self, x: float, y: float) -> EditableMove | None:
        base_y = self._wave_y + 14
        for move in reversed(self._editor._synthetic_moves):
            mx = self._time_to_x(move.t_us)
            dx = x - mx
            dy = y - base_y
            if dx * dx + dy * dy <= 14.0 * 14.0:
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
        elif track == "gamepad":
            y_top = self._g_y
            track_h = self._g_track_h
        else:
            y_top = self._wave_y
            track_h = self.TRACK_HEIGHT

        width = self.get_width()
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
        elif track == "gamepad":
            device_type = "gamepad"
            track_y = self._g_y
            track_h = self._g_track_h
            num_lanes = self._g_num_lanes
            lanes_dict = self._g_lanes
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

    def _gap_track_at_y(self, y: float) -> str | None:
        track = self._get_track_at_y(y)
        if track in {"keyboard", "mouse", "gamepad"}:
            return track
        if track == "movement":
            if y < self._wave_y + self.TRACK_HEIGHT / 2:
                return "movement"
            return "control"
        return None

    def _gap_at_position(self, x: float, y: float) -> TimelineGap | None:
        if x < self.LABEL_WIDTH:
            return None
        timestamp_us = max(0, self._x_to_time_us(x))

        if y >= self.RULER_HEIGHT:
            track = self._gap_track_at_y(y)
            if track is None:
                return None
            gaps = self._track_gaps.get(track, ())
            positive_gaps = self._track_positive_gaps.get(track, ())
            positive_starts = self._track_positive_starts.get(track, ())
            positive_index = bisect_right(positive_starts, timestamp_us) - 1
            if positive_index >= 0:
                gap = positive_gaps[positive_index]
                if gap.previous_end_us < timestamp_us < gap.next_start_us:
                    return gap

            tolerance_us = int(6.0 / max(self._pps, 1.0) * 1e6)
            next_starts = self._track_next_starts.get(track, ())
            index = bisect_right(next_starts, timestamp_us) - 1
            for candidate_index in (index, index + 1):
                if 0 <= candidate_index < len(gaps):
                    gap = gaps[candidate_index]
                    if abs(timestamp_us - gap.next_start_us) <= tolerance_us:
                        return gap
            return None

        if not self._gap_segments:
            return None
        positive_index = bisect_right(self._positive_gap_starts, timestamp_us) - 1
        if positive_index >= 0:
            gap = self._positive_gaps[positive_index]
            if gap.previous_end_us < timestamp_us < gap.next_start_us:
                return gap

        index = bisect_right(self._gap_next_starts, timestamp_us) - 1
        if index >= 0:
            gap = self._gap_segments[index]
            if gap.next_start_us <= timestamp_us <= gap.previous_end_us:
                return gap

        tolerance_us = int(6.0 / max(self._pps, 1.0) * 1e6)
        for candidate_index in (index, index + 1):
            if 0 <= candidate_index < len(self._gap_segments):
                gap = self._gap_segments[candidate_index]
                if abs(timestamp_us - gap.next_start_us) <= tolerance_us:
                    return gap
        return None

    def _gap_item_label(self, item: object) -> str:
        if isinstance(item, EditableEvent):
            if item.ev_type == evdev.ecodes.EV_KEY:
                return _get_key_name(item.code)
            return _get_event_name(item.ev_type, item.code)
        if isinstance(item, EditableMove):
            return "Mouse move"
        if isinstance(item, EditableControl):
            return {
                "wait": "Wait",
                "wait_random": "Random wait",
                "exec_sync": "Run command",
                "exec_async": "Run command",
                "compositor_dispatch": "Compositor action",
            }.get(item.mode, item.mode.replace("_", " ").capitalize())
        if isinstance(item, dict):
            if int(item.get("type", -1)) == evdev.ecodes.EV_REL:
                return "Mouse movement"
            title, _detail = _describe_passthrough_event(item)
            return title
        return "Action"

    def _gap_step_label(self, items: tuple[object, ...]) -> str:
        labels = list(dict.fromkeys(self._gap_item_label(item) for item in items))
        if len(labels) <= 2:
            return " + ".join(labels)
        return f"{labels[0]} + {len(labels) - 1} more"

    def _select_gap(self, gap: TimelineGap, x: float, y: float) -> None:
        self._selected = None
        self._selected_gap = gap
        self._hover_gap = gap
        self._suppressed_hover_gap = None
        self._editor._on_selection_changed(None)
        self.queue_draw()
        self._show_gap_editor(gap, x, y)

    def _show_gap_editor(self, gap: TimelineGap, x: float, y: float) -> None:
        if self._gap_popover is not None:
            self._gap_popover.popdown()

        popover = Gtk.Popover()
        self._gap_popover = popover

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(10)
        box.set_margin_end(10)

        previous_label = self._gap_step_label(gap.previous_items)
        next_label = self._gap_step_label(gap.next_items)
        title = Gtk.Label(label=f"{previous_label} → {next_label}")
        title.add_css_class("heading")
        title.set_halign(Gtk.Align.START)
        title.set_max_width_chars(38)
        title.set_ellipsize(3)
        box.append(title)

        value_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        value_row.append(Gtk.Label(label="Gap:"))
        spin = Gtk.SpinButton()
        self._gap_spin = spin
        spin.set_adjustment(
            Gtk.Adjustment(
                value=gap.duration_us / 1000.0,
                lower=gap.minimum_us / 1000.0,
                upper=(2**63 - 1) / 1000.0,
                step_increment=1.0,
                page_increment=10.0,
            )
        )
        spin.set_digits(1)
        spin.set_width_chars(8)
        value_row.append(spin)
        value_row.append(Gtk.Label(label="ms"))
        box.append(value_row)

        hint = Gtk.Label(label="Negative values create an overlap.")
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        hint.set_halign(Gtk.Align.START)
        box.append(hint)

        scope_label = Gtk.Label(label="Move:")
        scope_label.set_halign(Gtk.Align.START)
        box.append(scope_label)

        next_only_check = Gtk.CheckButton(label="Next action only")
        self._gap_next_only_check = next_only_check
        next_only_check.set_active(True)
        box.append(next_only_check)

        track_following_check: Gtk.CheckButton | None = None
        if gap.scope == "track":
            track_check = Gtk.CheckButton(label="Following actions in this track")
            track_check.set_group(next_only_check)
            box.append(track_check)
            track_following_check = track_check
            self._gap_track_following_check = track_check

        timeline_following_check = Gtk.CheckButton(label="Everything after this time")
        self._gap_timeline_following_check = timeline_following_check
        timeline_following_check.set_group(next_only_check)
        box.append(timeline_following_check)

        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_row.set_halign(Gtk.Align.END)
        cancel_button = Gtk.Button(label="Cancel")
        self._gap_cancel_button = cancel_button

        def _cancel_gap(_button: Gtk.Button) -> None:
            self.clear_gap_selection(suppress_hover=True)

        cancel_button.connect("clicked", _cancel_gap)
        button_row.append(cancel_button)
        apply_button = Gtk.Button(label="Apply")
        self._gap_apply_button = apply_button
        apply_button.add_css_class("suggested-action")

        def _apply_gap(_button: Gtk.Button) -> None:
            target_us = int(round(spin.get_value() * 1000))
            if track_following_check is not None and track_following_check.get_active():
                move_scope = "track"
            elif timeline_following_check.get_active():
                move_scope = "timeline"
            else:
                move_scope = "next"
            popover.popdown()
            self._editor._edit_timeline_gap(
                gap,
                target_us,
                move_scope=move_scope,
            )

        apply_button.connect("clicked", _apply_gap)
        spin.connect("activate", _apply_gap)
        button_row.append(apply_button)
        box.append(button_row)

        def _on_popover_closed(closed_popover: Gtk.Popover) -> None:
            if self._gap_popover is closed_popover:
                if self._selected_gap is gap:
                    self._suppressed_hover_gap = gap
                    self._selected_gap = None
                if self._hover_gap is gap:
                    self._hover_gap = None
                self._gap_popover = None
                self._gap_spin = None
                self._gap_next_only_check = None
                self._gap_track_following_check = None
                self._gap_timeline_following_check = None
                self._gap_cancel_button = None
                self._gap_apply_button = None
                self.queue_draw()
            if closed_popover.get_parent() is self:
                closed_popover.unparent()

        popover.connect("closed", _on_popover_closed)
        popover.set_child(box)
        popover.set_parent(self)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        if self.get_realized():
            popover.popup()
            spin.grab_focus()

    # ------------------------------------------------------------------
    # Gesture handlers
    # ------------------------------------------------------------------

    def _collect_erase_hits(self, track: str, x0: float, x1: float) -> list[object]:
        """Collect items in `track` whose drawn shape intersects pixel range [x0, x1].

        Press/release pairs that fully span the band are skipped: a held key
        enclosing the selection should survive deleting what happens under it.
        """
        hits: list[object] = []
        if track in ("keyboard", "mouse", "gamepad"):
            for ev in self._editor._events:
                if ev.device_type != track:
                    continue
                ex1 = self._time_to_x(ev.press_t_us)
                ex2 = max(self._time_to_x(ev.release_t_us), ex1 + self.MIN_EVENT_WIDTH)
                if ex1 <= x1 and ex2 >= x0 and not (ex1 < x0 and ex2 > x1):
                    hits.append(ev)
        elif track == "movement":
            for move in self._editor._synthetic_moves:
                mx = self._time_to_x(move.t_us)
                if x0 - 9.0 <= mx <= x1 + 9.0:
                    hits.append(move)
            for control in self._editor._control_events:
                cx = self._time_to_x(control.t_us)
                if x0 - 7.0 <= cx <= x1 + 7.0:
                    hits.append(control)
            for ev in self._editor._rel_events:
                rx = self._time_to_x(int(ev.get("t_us", 0)))
                if x0 - 1.0 <= rx <= x1 + 1.0:
                    hits.append(ev)
        for ev in self._editor._passthrough_events:
            if _passthrough_track(ev) != track:
                continue
            px = self._time_to_x(int(ev.get("t_us", 0)))
            if x0 - 4.0 <= px <= x1 + 4.0:
                hits.append(ev)
        return hits

    def _collect_ripple_hits(self, t0_us: int, t1_us: int) -> list[object]:
        """Collect items in [t0_us, t1_us] across all tracks (ripple preview).

        Mirrors timing_ops.ripple_delete_range: pairs spanning the range are
        not deleted (the collapse shortens them), so they are not previewed.
        """
        hits: list[object] = []
        for ev in self._editor._events:
            if (
                ev.press_t_us <= t1_us
                and ev.release_t_us >= t0_us
                and not (ev.press_t_us < t0_us and ev.release_t_us > t1_us)
            ):
                hits.append(ev)
        for ev in self._editor._rel_events:
            if t0_us <= int(ev.get("t_us", 0)) <= t1_us:
                hits.append(ev)
        for ev in self._editor._passthrough_events:
            if t0_us <= int(ev.get("t_us", 0)) <= t1_us:
                hits.append(ev)
        for move in self._editor._synthetic_moves:
            if t0_us <= move.t_us <= t1_us:
                hits.append(move)
        for control in self._editor._control_events:
            if t0_us <= control.t_us <= t1_us:
                hits.append(control)
        return hits

    def _reset_erase_drag(self) -> None:
        self._erase_track = None
        self._erase_x0 = None
        self._erase_x1 = None
        self._erase_pending = []

    def _on_gap_click_pressed(
        self,
        _gesture: Gtk.GestureClick,
        press_count: int,
        x: float,
        y: float,
    ) -> None:
        if press_count != 2 or self._editor._erase_mode or self._hit_test(x, y) is not None:
            return
        self._ensure_gap_cache()
        gap = self._gap_at_position(x, y)
        if gap is None:
            return
        self._gap_double_click_handled = True
        self._select_gap(gap, x, y)

    def _on_drag_begin(self, gesture, start_x, start_y) -> None:
        # Always hit-test on press; actual movement only starts after threshold.
        hit = self._hit_test(start_x, start_y)
        self._drag_selected_obj = hit
        self._drag_selected_gap = None
        self._drag_start_x = float(start_x)
        self._drag_start_y = float(start_y)
        self._drag_scroll_origin = self._scroll_offset
        self._drag_last_offset_x = 0.0
        self._stop_autoscroll()
        self._reset_erase_drag()
        if self._editor._erase_mode:
            # In erase mode a drag sweeps a delete band instead of moving events;
            # clicks below the drag threshold still select as usual.
            self._erase_track = self._get_track_at_y(start_y)
            self._erase_anchor_t_us = self._x_to_time_us(start_x)
            self._drag_event = None
            self._drag_move = None
            self._drag_control = None
            self._in_drag = False
            return
        if hit is not None:
            self.clear_gap_selection()
        elif not self._editor._drag_locked and self._gap_popover is None:
            self._drag_selected_gap = self._gap_at_position(start_x, start_y)
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
        if self._erase_track is not None:
            if not self._in_drag:
                if abs(offset_x) < 4:
                    return
                self._in_drag = True
            self._drag_last_offset_x = float(offset_x)
            self._apply_erase_band()
            self._update_autoscroll(self._drag_start_x + float(offset_x))
            return
        if self._drag_selected_gap is not None:
            if abs(offset_x) >= 4 or abs(offset_y) >= 4:
                self._drag_selected_gap = None
                self._in_drag = True
            return
        if (
            self._drag_event is None and self._drag_move is None and self._drag_control is None
        ) or self._editor._drag_locked:
            return
        if not self._in_drag:
            if abs(offset_x) < 4:
                return
            self._in_drag = True

        self._drag_last_offset_x = float(offset_x)
        self._apply_drag_position()
        self._update_autoscroll(self._drag_start_x + float(offset_x))

    def _apply_drag_position(self) -> None:
        """Move the dragged item to match the pointer, folding in any scroll
        movement since drag-begin (edge auto-scroll shifts time under the
        stationary pointer)."""
        scroll_delta_px = self._scroll_offset - self._drag_scroll_origin
        delta_us = int((self._drag_last_offset_x + scroll_delta_px) / self._pps * 1e6)
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

    def _apply_erase_band(self) -> None:
        """Recompute the erase band between the time-space anchor and the
        pointer, so the fixed end stays put while edge auto-scroll shifts
        the visible slice."""
        if self._erase_track is None:
            return
        anchor_x = self._time_to_x(self._erase_anchor_t_us)
        pointer_x = self._drag_start_x + self._drag_last_offset_x
        self._erase_x0 = min(anchor_x, pointer_x)
        self._erase_x1 = max(anchor_x, pointer_x)
        if self._erase_track == "all":
            t0_us = max(0, self._x_to_time_us(self._erase_x0))
            t1_us = max(t0_us, self._x_to_time_us(self._erase_x1))
            self._erase_pending = self._collect_ripple_hits(t0_us, t1_us)
        else:
            self._erase_pending = self._collect_erase_hits(
                self._erase_track,
                self._erase_x0,
                self._erase_x1,
            )
        self.queue_draw()

    def _edge_autoscroll_velocity(self, pointer_x: float) -> float:
        """Scroll velocity in px/s for a pointer position, ramping up as the
        pointer nears (or passes) either edge of the visible slice."""
        width = self.get_width()
        margin = float(self.EDGE_SCROLL_MARGIN)
        if width < self.LABEL_WIDTH + 3 * margin:
            return 0.0
        left_edge = self.LABEL_WIDTH + margin
        right_edge = width - margin
        if pointer_x < left_edge:
            depth = min((left_edge - pointer_x) / margin, 1.0)
            return -depth * self.EDGE_SCROLL_MAX_SPEED
        if pointer_x > right_edge:
            depth = min((pointer_x - right_edge) / margin, 1.0)
            return depth * self.EDGE_SCROLL_MAX_SPEED
        return 0.0

    def _update_autoscroll(self, pointer_x: float) -> None:
        self._autoscroll_velocity = self._edge_autoscroll_velocity(pointer_x)
        if self._autoscroll_velocity == 0.0:
            self._stop_autoscroll()
        elif self._autoscroll_tick_id == 0:
            self._autoscroll_last_time = None
            self._autoscroll_tick_id = self.add_tick_callback(self._on_autoscroll_tick)

    def _stop_autoscroll(self) -> None:
        if self._autoscroll_tick_id != 0:
            self.remove_tick_callback(self._autoscroll_tick_id)
            self._autoscroll_tick_id = 0
        self._autoscroll_velocity = 0.0
        self._autoscroll_last_time = None

    def _on_autoscroll_tick(self, widget, frame_clock) -> bool:
        if not self._in_drag or self._autoscroll_velocity == 0.0:
            self._autoscroll_tick_id = 0
            return GLib.SOURCE_REMOVE
        now = frame_clock.get_frame_time()
        last = self._autoscroll_last_time
        self._autoscroll_last_time = now
        if last is None:
            return GLib.SOURCE_CONTINUE
        dt_s = max(0.0, (now - last) / 1e6)
        before = self._scroll_offset
        self._editor._scroll_timeline_by(self._autoscroll_velocity * dt_s)
        if abs(self._scroll_offset - before) < 0.01:
            # Clamped at a timeline boundary; a later drag-update re-arms us.
            self._autoscroll_tick_id = 0
            self._autoscroll_velocity = 0.0
            return GLib.SOURCE_REMOVE
        if self._erase_track is not None:
            self._apply_erase_band()
        else:
            self._apply_drag_position()
        return GLib.SOURCE_CONTINUE

    def _on_drag_end(self, gesture, offset_x, offset_y) -> None:
        self._stop_autoscroll()
        if self._erase_track is not None and self._in_drag:
            pending = self._erase_pending
            self._reset_erase_drag()
            self._drag_selected_obj = None
            self._in_drag = False
            if pending:
                self._editor._delete_events_bulk(pending)
            self.queue_draw()
            return
        self._reset_erase_drag()
        if self._in_drag and self._drag_event:
            # Commit the move: re-sort and refresh stats.
            self._editor._events.sort(key=lambda e: e.press_t_us)
            self._editor._update_stats()
            self._editor._sync_close_guard()
        elif self._in_drag and self._drag_move:
            self._editor._synthetic_moves.sort(key=lambda m: m.t_us)
            self._editor._update_stats()
            self._editor._sync_close_guard()
        elif self._in_drag and self._drag_control:
            self._editor._refresh_after_timing_edit()
        elif not self._in_drag:
            selected_obj = self._drag_selected_obj
            selected_gap = self._drag_selected_gap
            if self._gap_double_click_handled:
                pass
            elif selected_gap is not None:
                self._select_gap(selected_gap, self._drag_start_x, self._drag_start_y)
            else:
                self.clear_gap_selection()
            if (
                not self._gap_double_click_handled
                and selected_gap is None
                and selected_obj is not self._selected
            ):
                self._selected = selected_obj
                self._editor._on_selection_changed(selected_obj)
        self._drag_event = None
        self._drag_move = None
        self._drag_control = None
        self._drag_selected_obj = None
        self._drag_selected_gap = None
        self._gap_double_click_handled = False
        self._in_drag = False
        self.queue_draw()

    def _on_right_drag_begin(self, gesture, start_x, start_y) -> None:
        if not self._editor._erase_mode:
            return
        self._reset_erase_drag()
        self._stop_autoscroll()
        self._erase_track = "all"
        self._erase_anchor_t_us = self._x_to_time_us(start_x)
        self._drag_start_x = float(start_x)
        self._drag_last_offset_x = 0.0
        self._right_press_x = start_x
        self._right_press_y = start_y
        self._in_drag = False

    def _on_right_drag_update(self, gesture, offset_x, offset_y) -> None:
        if self._erase_track != "all":
            return
        if not self._in_drag:
            if abs(offset_x) < 4:
                return
            self._in_drag = True
        self._drag_last_offset_x = float(offset_x)
        self._apply_erase_band()
        self._update_autoscroll(self._drag_start_x + float(offset_x))

    def _on_right_drag_end(self, gesture, offset_x, offset_y) -> None:
        self._stop_autoscroll()
        if not self._editor._erase_mode or self._erase_track != "all":
            return
        x0 = self._erase_x0
        x1 = self._erase_x1
        was_drag = self._in_drag
        self._reset_erase_drag()
        self._in_drag = False
        self.queue_draw()
        if not was_drag or x0 is None or x1 is None:
            # Plain right-click in erase mode: show the context menu on release.
            self._on_right_click(None, 1, self._right_press_x, self._right_press_y)
            return
        t0_us = max(0, self._x_to_time_us(x0))
        t1_us = max(t0_us, self._x_to_time_us(x1))
        self._editor._ripple_delete_range(t0_us, t1_us)

    def _on_pointer_motion(self, controller, x, y) -> None:
        self._hover_x = x
        self._hover_y = y
        gaps_visible = not self._editor._erase_mode and not self._editor._drag_locked
        candidate = (
            self._gap_at_position(x, y)
            if gaps_visible and self._hit_test(x, y) is None
            else None
        )
        if candidate is not None and candidate == self._suppressed_hover_gap:
            candidate = None
        elif candidate != self._suppressed_hover_gap:
            self._suppressed_hover_gap = None
        self._hover_gap = candidate
        if gaps_visible:
            self.set_cursor_from_name("pointer" if self._hover_gap is not None else None)
        elif not self._editor._erase_mode:
            self.set_cursor_from_name(None)
        self.queue_draw()

    def _on_pointer_leave(self, controller) -> None:
        self._hover_x = None
        self._hover_y = None
        self._hover_gap = None
        self._suppressed_hover_gap = None
        if not self._editor._erase_mode:
            self.set_cursor_from_name(None)
        self.queue_draw()

    def _on_scroll(self, controller, dx, dy) -> bool:
        try:
            state = controller.get_current_event_state()
        except (RuntimeError, TypeError, ValueError):
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
        if gesture is not None and self._editor._erase_mode:
            # In erase mode the right button is owned by the ripple drag; a
            # plain right-click reopens the menu via _on_right_drag_end.
            return
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
                self._editor._present_add_key_dialog(default_t_us=_t, device_type="mouse")

            add_btn.connect("clicked", _add_click)
            box.append(add_btn)

        elif track == "gamepad":
            add_btn = Gtk.Button(label=f"Add Gamepad Button at {t_label}")
            add_btn.add_css_class("flat")

            def _add_gamepad(_b, _t=t_us, _p=popover):
                _p.popdown()
                self._editor._present_add_key_dialog(default_t_us=_t, device_type="gamepad")

            add_btn.connect("clicked", _add_gamepad)
            box.append(add_btn)

        elif track == "movement":
            add_move_btn = Gtk.Button(label=f"Add Mouse Move at {t_label}")
            add_move_btn.add_css_class("flat")

            def _add_move(_b, _t=t_us, _p=popover):
                _p.popdown()
                self._editor._present_mouse_move_dialog(default_t_us=_t)

            add_move_btn.connect("clicked", _add_move)
            box.append(add_move_btn)

        if track in ("keyboard", "mouse", "gamepad", "movement"):
            if box.get_first_child():
                box.append(Gtk.Separator())

            gap_btn = Gtk.Button(label=f"Insert Wait at {t_label}")
            gap_btn.add_css_class("flat")

            def _insert_gap(_b, _t=t_us, _p=popover):
                _p.popdown()
                rect = Gdk.Rectangle()
                rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
                self._editor._show_add_control_popover(
                    self,
                    "wait",
                    default_t_us=_t,
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
                    self,
                    "wait_random",
                    default_t_us=_t,
                    pointing_to=rect,
                )

            wait_random_btn.connect("clicked", _insert_wait_random)
            box.append(wait_random_btn)

            exec_btn = Gtk.Button(label=f"Run Command at {t_label}")
            exec_btn.add_css_class("flat")

            def _insert_exec(_b, _t=t_us, _p=popover):
                _p.popdown()
                control = EditableControl(
                    mode="exec_sync",
                    t_us=int(_t),
                    command="",
                    timeout_ms=min(30000, self._editor._macro_exec_timeout_max_ms),
                    inhibit_mouse=False,
                )
                self._editor._insert_control_event(control)

            exec_btn.connect("clicked", _insert_exec)
            box.append(exec_btn)

            macro_btn = Gtk.Button(label=f"Call Macro at {t_label}")
            macro_btn.add_css_class("flat")

            def _insert_macro(_b, _t=t_us, _p=popover):
                _p.popdown()
                self._editor._present_macro_call_dialog(
                    mode="macro_sync",
                    default_t_us=_t,
                )

            macro_btn.connect("clicked", _insert_macro)
            box.append(macro_btn)

            compositor_btn = Gtk.Button(label=f"Insert Compositor Action at {t_label}")
            compositor_btn.add_css_class("flat")

            def _insert_compositor(_b, _t=t_us, _p=popover):
                _p.popdown()
                self._editor._present_compositor_action_dialog(default_t_us=_t)

            compositor_btn.connect("clicked", _insert_compositor)
            box.append(compositor_btn)

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

        if isinstance(ev, EditableEvent) and track in ("keyboard", "mouse", "gamepad"):
            if box.get_first_child():
                box.append(Gtk.Separator())
            event_name = (
                _get_key_name(ev.code)
                if ev.ev_type == evdev.ecodes.EV_KEY
                else _get_event_name(ev.ev_type, ev.code)
            )
            del_btn = Gtk.Button(label=f"Delete {event_name}")
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
            label = (
                "Compositor Action"
                if ev.mode == "compositor_dispatch"
                else f"Macro Call {ev.macro_name}"
                if ev.mode in {"macro_sync", "macro_parallel"}
                else "Run Command"
                if ev.mode in {"exec_sync", "exec_parallel", "exec_async"}
                else ev.mode.replace("_", " ").title()
            )
            del_control_btn = Gtk.Button(label=f"Delete {label}")
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
