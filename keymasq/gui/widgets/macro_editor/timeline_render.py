"""Cairo rendering helpers for the macro editor timeline."""

from dataclasses import dataclass

import evdev

from keymasq.gui.widgets.macro_editor.gaps import TimelineGap
from keymasq.gui.widgets.macro_editor.model import (
    EditableControl,
    EditableEvent,
    EditableMove,
    MacroEvent,
    _format_time_us,
    _get_event_name,
    _get_key_name,
    _passthrough_track,
)

_ERASE_PENDING_FILL_RGBA = (0.85, 0.22, 0.22, 0.90)
_ERASE_PENDING_BORDER_RGBA = (1.0, 0.45, 0.45, 0.98)
_ERASE_BAND_FILL_RGBA = (0.95, 0.30, 0.30, 0.14)
_ERASE_BAND_BORDER_RGBA = (1.0, 0.45, 0.45, 0.85)


@dataclass(frozen=True)
class TimelineRenderState:
    LABEL_WIDTH: int
    TIMELINE_PAD: int
    RULER_HEIGHT: int
    TRACK_HEIGHT: int
    MIN_EVENT_WIDTH: int
    _pps: float
    _scroll_offset: float
    _selected: object | None
    _hover_x: float | None
    _hover_y: float | None
    _context_menu_x: float | None
    _hover_gap: TimelineGap | None
    _selected_gap: TimelineGap | None
    events: list[EditableEvent]
    rel_events: list[MacroEvent]
    passthrough_events: list[MacroEvent]
    synthetic_moves: list[EditableMove]
    control_events: list[EditableControl]
    duration_us: int
    _kb_lanes: dict[int, int]
    _m_lanes: dict[int, int]
    _g_lanes: dict[int, int]
    _kb_num_lanes: int
    _m_num_lanes: int
    _g_num_lanes: int
    _kb_track_h: int
    _m_track_h: int
    _g_track_h: int
    _kb_y: int
    _m_y: int
    _g_y: int
    _wave_y: int
    _erase_band: tuple[str, float, float] | None
    _erase_pending_ids: frozenset[int]

    def _time_to_x(self, t_us: int) -> float:
        return self.LABEL_WIDTH + self.TIMELINE_PAD + t_us / 1e6 * self._pps - self._scroll_offset

    def _x_to_time_us(self, x: float) -> int:
        return int(
            (x - self.LABEL_WIDTH - self.TIMELINE_PAD + self._scroll_offset) / self._pps * 1e6
        )

    def _draw_ruler(self, cr, width: int, duration_us: int) -> None:
        _draw_ruler(cr, self, width, duration_us)

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
        _draw_event_rect(
            cr,
            self,
            ev,
            y_top,
            track_h,
            fill_rgba,
            border_rgba,
            sel_fill_rgba,
            sel_border_rgba,
            width,
        )

    def _draw_track_with_lanes(
        self,
        cr,
        width: int,
        events: list[EditableEvent],
        device_type: str,
        y_top: int,
        track_h: int,
        num_lanes: int,
        lanes_dict: dict[int, int],
        bg_rgb,
        fill_rgba,
        border_rgba,
        sel_fill_rgba,
        sel_border_rgba,
    ) -> None:
        _draw_track_with_lanes(
            cr,
            self,
            width,
            events,
            device_type,
            y_top,
            track_h,
            num_lanes,
            lanes_dict,
            bg_rgb,
            fill_rgba,
            border_rgba,
            sel_fill_rgba,
            sel_border_rgba,
        )

    def _draw_keyboard_track(self, cr, width: int, events: list[EditableEvent]) -> None:
        _draw_keyboard_track(cr, self, width, events)

    def _draw_mouse_track(self, cr, width: int, events: list[EditableEvent]) -> None:
        _draw_mouse_track(cr, self, width, events)

    def _draw_gamepad_track(self, cr, width: int, events: list[EditableEvent]) -> None:
        _draw_gamepad_track(cr, self, width, events)

    def _draw_movement_track(self, cr, width: int, rel_events: list[MacroEvent]) -> None:
        _draw_movement_track(cr, self, width, rel_events)

    def _draw_passthrough_markers(self, cr, width: int) -> None:
        _draw_passthrough_markers(cr, self, width)

    def _draw_passthrough_markers_for_track(
        self,
        cr,
        width: int,
        *,
        track: str,
        y_top: int,
        track_h: int,
    ) -> None:
        _draw_passthrough_markers_for_track(
            cr,
            self,
            width,
            track=track,
            y_top=y_top,
            track_h=track_h,
        )

    def _get_passthrough_marker_layouts(
        self,
        track: str,
        width: int,
        y_top: int,
        track_h: int,
    ) -> list[tuple[MacroEvent, float, float, float]]:
        return get_passthrough_marker_layouts(self, track, width, y_top, track_h)

    def _draw_labels(self, cr, height: int) -> None:
        _draw_labels(cr, self, height)

    def _draw_synthetic_move_markers(self, cr, width: int) -> None:
        _draw_synthetic_move_markers(cr, self, width)

    def _draw_control_markers(self, cr, width: int) -> None:
        _draw_control_markers(cr, self, width)

    def _draw_pointer_guide(self, cr, width: int, height: int) -> None:
        _draw_pointer_guide(cr, self, width, height)

    def _draw_vertical_guide(self, cr, width: int, height: int, x: float) -> None:
        _draw_vertical_guide(cr, self, width, height, x)


def draw(cr, state: TimelineRenderState, width: int, height: int) -> None:
    events = state.events
    rel_events = state.rel_events
    duration_us = state.duration_us

    # Dark background
    cr.set_source_rgb(0.12, 0.12, 0.12)
    cr.paint()

    state._draw_ruler(cr, width, duration_us)
    state._draw_keyboard_track(cr, width, events)
    state._draw_mouse_track(cr, width, events)
    state._draw_gamepad_track(cr, width, events)
    state._draw_movement_track(cr, width, rel_events)
    state._draw_passthrough_markers(cr, width)
    state._draw_synthetic_move_markers(cr, width)
    state._draw_control_markers(cr, width)
    _draw_gap_feedback(cr, state, width, height)
    _draw_erase_band(cr, state, width)
    state._draw_labels(cr, height)
    state._draw_pointer_guide(cr, width, height)

    # Horizontal separator lines between tracks
    cr.set_source_rgba(0.30, 0.30, 0.30, 0.8)
    cr.set_line_width(1)
    for y in [state._kb_y, state._m_y, state._g_y, state._wave_y]:
        cr.move_to(0, y + 0.5)
        cr.line_to(width, y + 0.5)
        cr.stroke()


def _draw_ruler(cr, state: TimelineRenderState, width: int, duration_us: int) -> None:
    cr.set_source_rgb(0.17, 0.17, 0.17)
    cr.rectangle(0, 0, width, state.RULER_HEIGHT)
    cr.fill()

    if duration_us <= 0:
        return

    visible_s = max((width - state.LABEL_WIDTH) / state._pps, 0.001)
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
        x = state._time_to_x(int(t * 1e6))
        if state.LABEL_WIDTH - 1 <= x <= width:
            cr.set_source_rgba(0.55, 0.55, 0.55, 1.0)
            cr.set_line_width(1)
            cr.move_to(x + 0.5, state.RULER_HEIGHT - 6)
            cr.line_to(x + 0.5, state.RULER_HEIGHT)
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
            if lx >= state.LABEL_WIDTH:
                cr.set_source_rgba(0.60, 0.60, 0.60, 1.0)
                cr.move_to(lx, state.RULER_HEIGHT - 8)
                cr.show_text(label)

        t = round(t + tick_interval, 6)


def _draw_event_rect(
    cr,
    state: TimelineRenderState,
    ev: EditableEvent,
    y_top: float,
    track_h: float,
    fill_rgba,
    border_rgba,
    sel_fill_rgba,
    sel_border_rgba,
    width: int,
) -> None:
    x1 = state._time_to_x(ev.press_t_us)
    x2 = state._time_to_x(ev.release_t_us)
    w = max(x2 - x1, state.MIN_EVENT_WIDTH)

    if x1 > width or x1 + w < state.LABEL_WIDTH:
        return  # Completely off screen

    is_sel = ev is state._selected
    is_pending_delete = id(ev) in state._erase_pending_ids
    margin = max(1, min(4, int(track_h * 0.10)))
    rect_y = y_top + margin
    rect_h = track_h - margin * 2

    if is_pending_delete:
        fill = _ERASE_PENDING_FILL_RGBA
        border = _ERASE_PENDING_BORDER_RGBA
    elif is_sel:
        fill = sel_fill_rgba
        border = sel_border_rgba
    else:
        fill = fill_rgba
        border = border_rgba

    # Fill
    cr.set_source_rgba(*fill)
    cr.rectangle(x1, rect_y, w, rect_h)
    cr.fill()

    # Border
    cr.set_source_rgba(*border)
    cr.set_line_width(2.0 if (is_sel or is_pending_delete) else 1.0)
    cr.rectangle(x1 + 0.5, rect_y + 0.5, max(w - 1, 0), max(rect_h - 1, 0))
    cr.stroke()

    # Label (only if wide and tall enough)
    if w > 28 and rect_h > 10:
        name = (
            _get_key_name(ev.code)
            if ev.ev_type == evdev.ecodes.EV_KEY
            else _get_event_name(ev.ev_type, ev.code)
        )
        cr.select_font_face("sans", 0, 0)
        cr.set_font_size(min(9.0, rect_h * 0.65))
        extents = cr.text_extents(name)
        # Center text in the rect (Cairo baseline: ty - height/2 - y_bearing)
        tx = x1 + (w - extents[2]) / 2 - extents[0]
        ty = rect_y + rect_h / 2 - extents[3] / 2 - extents[1]
        if tx >= state.LABEL_WIDTH:
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.9)
            cr.move_to(tx, ty)
            cr.show_text(name)


def _draw_track_with_lanes(
    cr,
    state: TimelineRenderState,
    width: int,
    events: list[EditableEvent],
    device_type: str,
    y_top: int,
    track_h: int,
    num_lanes: int,
    lanes_dict: dict[int, int],
    bg_rgb,
    fill_rgba,
    border_rgba,
    sel_fill_rgba,
    sel_border_rgba,
) -> None:
    """Draw a track with sub-lane support for overlapping events."""
    cr.set_source_rgb(*bg_rgb)
    cr.rectangle(state.LABEL_WIDTH, y_top, width - state.LABEL_WIDTH, track_h)
    cr.fill()

    lane_h = track_h / num_lanes

    # Draw subtle lane dividers when multiple lanes are active
    if num_lanes > 1:
        cr.set_source_rgba(0.30, 0.30, 0.36, 0.45)
        cr.set_line_width(0.5)
        for lane_i in range(1, num_lanes):
            ly = y_top + lane_i * lane_h + 0.25
            cr.move_to(state.LABEL_WIDTH, ly)
            cr.line_to(width, ly)
            cr.stroke()

    for ev in events:
        if ev.device_type != device_type:
            continue
        lane = lanes_dict.get(id(ev), 0)
        ev_y = y_top + lane * lane_h
        state._draw_event_rect(
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


def _draw_keyboard_track(cr, state: TimelineRenderState, width: int, events: list) -> None:
    state._draw_track_with_lanes(
        cr,
        width,
        events,
        device_type="keyboard",
        y_top=state._kb_y,
        track_h=state._kb_track_h,
        num_lanes=state._kb_num_lanes,
        lanes_dict=state._kb_lanes,
        bg_rgb=(0.12, 0.12, 0.15),
        fill_rgba=(0.22, 0.40, 0.80, 0.75),
        border_rgba=(0.35, 0.55, 0.90, 0.9),
        sel_fill_rgba=(0.35, 0.60, 1.00, 0.92),
        sel_border_rgba=(0.60, 0.82, 1.00, 1.0),
    )


def _draw_mouse_track(cr, state: TimelineRenderState, width: int, events: list) -> None:
    state._draw_track_with_lanes(
        cr,
        width,
        events,
        device_type="mouse",
        y_top=state._m_y,
        track_h=state._m_track_h,
        num_lanes=state._m_num_lanes,
        lanes_dict=state._m_lanes,
        bg_rgb=(0.15, 0.12, 0.09),
        fill_rgba=(0.78, 0.50, 0.08, 0.75),
        border_rgba=(0.90, 0.62, 0.20, 0.9),
        sel_fill_rgba=(1.00, 0.70, 0.20, 0.92),
        sel_border_rgba=(1.00, 0.85, 0.40, 1.0),
    )


def _draw_gamepad_track(cr, state: TimelineRenderState, width: int, events: list) -> None:
    state._draw_track_with_lanes(
        cr,
        width,
        events,
        device_type="gamepad",
        y_top=state._g_y,
        track_h=state._g_track_h,
        num_lanes=state._g_num_lanes,
        lanes_dict=state._g_lanes,
        bg_rgb=(0.10, 0.14, 0.15),
        fill_rgba=(0.10, 0.66, 0.72, 0.75),
        border_rgba=(0.28, 0.82, 0.86, 0.9),
        sel_fill_rgba=(0.18, 0.85, 0.92, 0.92),
        sel_border_rgba=(0.60, 1.00, 1.00, 1.0),
    )


def _draw_movement_track(
    cr,
    state: TimelineRenderState,
    width: int,
    rel_events: list[MacroEvent],
) -> None:
    y_top = state._wave_y
    track_h = state.TRACK_HEIGHT

    cr.set_source_rgb(0.11, 0.13, 0.11)
    cr.rectangle(state.LABEL_WIDTH, y_top, width - state.LABEL_WIDTH, track_h)
    cr.fill()

    if not rel_events:
        return

    draw_width = max(width - state.LABEL_WIDTH, 1)
    bins = [0.0] * draw_width

    for ev in rel_events:
        x = state._time_to_x(ev["t_us"])
        idx = int(x - state.LABEL_WIDTH)
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
        x = state.LABEL_WIDTH + i
        y = y_top + track_h - 4 - h
        cr.rectangle(x, y, 1, h)
    cr.fill()


def _draw_passthrough_markers(cr, state: TimelineRenderState, width: int) -> None:
    state._draw_passthrough_markers_for_track(
        cr,
        width,
        track="keyboard",
        y_top=state._kb_y,
        track_h=state._kb_track_h,
    )
    state._draw_passthrough_markers_for_track(
        cr,
        width,
        track="mouse",
        y_top=state._m_y,
        track_h=state._m_track_h,
    )
    state._draw_passthrough_markers_for_track(
        cr,
        width,
        track="gamepad",
        y_top=state._g_y,
        track_h=state._g_track_h,
    )
    state._draw_passthrough_markers_for_track(
        cr,
        width,
        track="movement",
        y_top=state._wave_y,
        track_h=state.TRACK_HEIGHT,
    )


def _draw_passthrough_markers_for_track(
    cr,
    state: TimelineRenderState,
    width: int,
    *,
    track: str,
    y_top: int,
    track_h: int,
) -> None:
    for ev, x, y, size in state._get_passthrough_marker_layouts(track, width, y_top, track_h):
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

        if id(ev) in state._erase_pending_ids:
            cr.set_source_rgba(*_ERASE_PENDING_BORDER_RGBA)
            cr.set_line_width(1.6)
            cr.arc(x, y, size + 3.0, 0, 6.283185307179586)
            cr.stroke()
        elif ev is state._selected:
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.98)
            cr.set_line_width(1.2)
            cr.arc(x, y, size + 3.0, 0, 6.283185307179586)
            cr.stroke()


def get_passthrough_marker_layouts(
    state: TimelineRenderState,
    track: str,
    width: int,
    y_top: int,
    track_h: int,
) -> list[tuple[MacroEvent, float, float, float]]:
    unknown_events = [ev for ev in state.passthrough_events if _passthrough_track(ev) == track]
    if not unknown_events:
        return []

    stack_per_x: dict[int, int] = {}
    base_y = y_top + track_h - 11
    max_stack = 6
    layouts: list[tuple[MacroEvent, float, float, float]] = []

    for ev in unknown_events:
        x = state._time_to_x(int(ev.get("t_us", 0)))
        if x < state.LABEL_WIDTH - 8 or x > width + 8:
            continue

        x_px = int(x)
        stack_idx = stack_per_x.get(x_px, 0)
        stack_per_x[x_px] = min(stack_idx + 1, max_stack)

        y = base_y - (stack_idx % max_stack) * 7
        is_press = int(ev.get("value", -1)) == 1
        size = 5.5 if is_press else 4.4
        layouts.append((ev, x, y, size))

    return layouts


def _draw_labels(cr, state: TimelineRenderState, height: int) -> None:
    # Background column
    cr.set_source_rgb(0.09, 0.09, 0.09)
    cr.rectangle(0, 0, state.LABEL_WIDTH, height)
    cr.fill()

    # Right border of label column
    cr.set_source_rgba(0.30, 0.30, 0.30, 0.8)
    cr.set_line_width(1)
    cr.move_to(state.LABEL_WIDTH + 0.5, 0)
    cr.line_to(state.LABEL_WIDTH + 0.5, height)
    cr.stroke()

    cr.select_font_face("monospace", 0, 0)
    cr.set_font_size(11)
    cr.set_source_rgba(0.62, 0.62, 0.62, 1.0)

    labels = [
        (state._kb_y + state._kb_track_h * 0.5, "K"),
        (state._m_y + state._m_track_h * 0.5, "M"),
        (state._g_y + state._g_track_h * 0.5, "G"),
        (state._wave_y + state.TRACK_HEIGHT * 0.5, "≈"),
    ]
    for y_center, label in labels:
        extents = cr.text_extents(label)
        x = (state.LABEL_WIDTH - extents[2]) / 2 - extents[0]
        y = y_center - extents[3] / 2 - extents[1]
        cr.move_to(x, y)
        cr.show_text(label)


def _draw_track_time_tick(cr, state: TimelineRenderState, x: float, rgba) -> None:
    """Faint full-height line on the ≈ track marking a point-marker's exact time."""
    cr.set_source_rgba(rgba[0], rgba[1], rgba[2], 0.22)
    cr.set_line_width(1.0)
    xi = int(x) + 0.5
    cr.move_to(xi, state._wave_y + 2)
    cr.line_to(xi, state._wave_y + state.TRACK_HEIGHT - 2)
    cr.stroke()


def _draw_synthetic_move_markers(cr, state: TimelineRenderState, width: int) -> None:
    moves = state.synthetic_moves
    if not moves:
        return

    base_y = state._wave_y + 14
    cr.select_font_face("sans", 0, 0)
    cr.set_font_size(9)

    for move in moves:
        x = state._time_to_x(move.t_us)
        if x < state.LABEL_WIDTH - 8 or x > width + 8:
            continue

        if move.mode == "natural":
            rgba = (0.55, 0.75, 1.00, 0.95)
        elif move.mode == "abs":
            rgba = (0.30, 0.90, 1.00, 0.95)
        else:
            rgba = (1.00, 0.80, 0.20, 0.95)

        _draw_track_time_tick(cr, state, x, rgba)

        size = 7.0
        cr.set_source_rgba(*rgba)
        cr.move_to(x, base_y - size)
        cr.line_to(x + size, base_y)
        cr.line_to(x, base_y + size)
        cr.line_to(x - size, base_y)
        cr.close_path()
        cr.fill()

        if id(move) in state._erase_pending_ids:
            cr.set_source_rgba(*_ERASE_PENDING_BORDER_RGBA)
            cr.set_line_width(1.6)
            cr.arc(x, base_y, size + 3.0, 0, 6.283185307179586)
            cr.stroke()
        elif move is state._selected:
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.98)
            cr.set_line_width(1.2)
            cr.arc(x, base_y, size + 3.0, 0, 6.283185307179586)
            cr.stroke()

        label = "N" if move.mode == "natural" else "A" if move.mode == "abs" else "R"
        extents = cr.text_extents(label)
        cr.set_source_rgba(0.94, 0.94, 0.94, 0.95)
        cr.move_to(x - extents[2] / 2 - extents[0], base_y + 18)
        cr.show_text(label)


def _draw_control_markers(cr, state: TimelineRenderState, width: int) -> None:
    controls = state.control_events
    if not controls:
        return

    base_y = state._wave_y + state.TRACK_HEIGHT - 14
    cr.select_font_face("sans", 0, 0)
    cr.set_font_size(9)

    for control in controls:
        x = state._time_to_x(control.t_us)
        if x < state.LABEL_WIDTH - 8 or x > width + 8:
            continue

        if control.mode == "wait":
            rgba = (0.25, 0.85, 0.95, 0.95)
            label = "W"
        elif control.mode == "wait_random":
            rgba = (0.35, 0.95, 0.45, 0.95)
            label = "WR"
        elif control.mode == "exec_sync":
            rgba = (1.00, 0.62, 0.12, 0.95)
            label = "XS"
        elif control.mode == "exec_parallel":
            rgba = (0.98, 0.56, 0.13, 0.95)
            label = "XP"
        elif control.mode == "exec_async":
            rgba = (0.95, 0.50, 0.15, 0.95)
            label = "XA"
        elif control.mode == "compositor_dispatch":
            rgba = (0.70, 0.45, 1.00, 0.95)
            label = "C"
        elif control.mode == "macro_sync":
            rgba = (0.30, 0.72, 1.00, 0.95)
            label = "MW"
        elif control.mode == "macro_parallel":
            rgba = (0.25, 0.88, 0.75, 0.95)
            label = "MP"
        else:
            rgba = (0.65, 0.65, 0.65, 0.95)
            label = "?"

        _draw_track_time_tick(cr, state, x, rgba)

        size = 7.0
        cr.set_source_rgba(*rgba)
        cr.move_to(x, base_y - size)
        cr.line_to(x + size, base_y)
        cr.line_to(x, base_y + size)
        cr.line_to(x - size, base_y)
        cr.close_path()
        cr.fill()

        if id(control) in state._erase_pending_ids:
            cr.set_source_rgba(*_ERASE_PENDING_BORDER_RGBA)
            cr.set_line_width(1.6)
            cr.arc(x, base_y, size + 3.0, 0, 6.283185307179586)
            cr.stroke()
        elif control is state._selected:
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.98)
            cr.set_line_width(1.2)
            cr.arc(x, base_y, size + 3.0, 0, 6.283185307179586)
            cr.stroke()

        extents = cr.text_extents(label)
        cr.set_source_rgba(0.94, 0.94, 0.94, 0.95)
        cr.move_to(x - extents[2] / 2 - extents[0], base_y - 11)
        cr.show_text(label)


def _erase_band_track_bounds(state: TimelineRenderState, track: str) -> tuple[int, int]:
    if track == "all":
        return state.RULER_HEIGHT, state._wave_y + state.TRACK_HEIGHT - state.RULER_HEIGHT
    if track == "keyboard":
        return state._kb_y, state._kb_track_h
    if track == "mouse":
        return state._m_y, state._m_track_h
    if track == "gamepad":
        return state._g_y, state._g_track_h
    return state._wave_y, state.TRACK_HEIGHT


def _draw_erase_band(cr, state: TimelineRenderState, width: int) -> None:
    if state._erase_band is None:
        return
    track, band_x0, band_x1 = state._erase_band
    y_top, track_h = _erase_band_track_bounds(state, track)

    x0 = max(band_x0, float(state.LABEL_WIDTH))
    x1 = min(band_x1, float(width))
    if x1 <= x0:
        return

    cr.set_source_rgba(*_ERASE_BAND_FILL_RGBA)
    cr.rectangle(x0, y_top, x1 - x0, track_h)
    cr.fill()

    cr.set_source_rgba(*_ERASE_BAND_BORDER_RGBA)
    cr.set_line_width(1.0)
    cr.rectangle(x0 + 0.5, y_top + 0.5, max(x1 - x0 - 1, 0), max(track_h - 1, 0))
    cr.stroke()

    pending_count = len(state._erase_pending_ids)
    if track == "all":
        span_us = int((band_x1 - band_x0) / state._pps * 1e6)
        label = f"Ripple delete {pending_count} · -{_format_time_us(span_us)}"
    elif pending_count == 0:
        return
    else:
        label = f"Delete {pending_count}"
    cr.select_font_face("monospace", 0, 0)
    cr.set_font_size(10)
    extents = cr.text_extents(label)
    pad_x = 6.0
    pad_y = 3.0
    bubble_w = extents[2] + pad_x * 2
    bubble_h = extents[3] + pad_y * 2

    bx = (x0 + x1 - bubble_w) / 2
    bx = min(max(bx, state.LABEL_WIDTH + 4.0), width - bubble_w - 4.0)
    by = y_top + 4.0

    cr.set_source_rgba(0.08, 0.08, 0.08, 0.90)
    cr.rectangle(bx, by, bubble_w, bubble_h)
    cr.fill()

    cr.set_source_rgba(*_ERASE_BAND_BORDER_RGBA)
    cr.rectangle(bx + 0.5, by + 0.5, bubble_w - 1.0, bubble_h - 1.0)
    cr.stroke()

    cr.set_source_rgba(1.0, 0.75, 0.75, 0.98)
    cr.move_to(bx + pad_x - extents[0], by + pad_y - extents[1])
    cr.show_text(label)


def _draw_gap_feedback(
    cr,
    state: TimelineRenderState,
    width: int,
    height: int,
) -> None:
    gap = state._selected_gap or state._hover_gap
    if gap is None:
        return

    boundary_x = state._time_to_x(gap.previous_end_us)
    next_x = state._time_to_x(gap.next_start_us)
    x0 = max(min(boundary_x, next_x), float(state.LABEL_WIDTH))
    x1 = min(max(boundary_x, next_x), float(width))
    if x1 < state.LABEL_WIDTH or x0 > width:
        return

    selected = gap is state._selected_gap
    overlap = gap.duration_us < 0
    color = (1.0, 0.56, 0.24) if overlap else (0.48, 0.72, 1.0)
    span_width = max(x1 - x0, 1.0)
    feedback_y, feedback_h = _gap_feedback_bounds(state, gap, height)
    feedback_bottom = feedback_y + feedback_h

    cr.set_source_rgba(*color, 0.13 if selected else 0.08)
    cr.rectangle(x0, feedback_y, span_width, feedback_h)
    cr.fill()

    cr.set_source_rgba(*color, 0.92 if selected else 0.68)
    cr.set_line_width(1.5 if selected else 1.0)
    for x in (boundary_x, next_x):
        if state.LABEL_WIDTH <= x <= width:
            cr.move_to(x + 0.5, feedback_y)
            cr.line_to(x + 0.5, feedback_bottom)
            cr.stroke()

    hover_y = state._hover_y
    bracket_y = (
        float(hover_y)
        if hover_y is not None and feedback_y <= hover_y <= feedback_bottom
        else feedback_y + feedback_h / 2
    )
    bracket_y = min(max(bracket_y, feedback_y + 8.0), feedback_bottom - 8.0)
    cr.move_to(x0, bracket_y + 0.5)
    cr.line_to(x1, bracket_y + 0.5)
    cr.stroke()
    for x in (x0, x1):
        cr.move_to(x + 0.5, bracket_y - 4.0)
        cr.line_to(x + 0.5, bracket_y + 5.0)
        cr.stroke()

    label = _format_time_us(gap.duration_us)

    cr.select_font_face("monospace", 0, 0)
    cr.set_font_size(10)
    extents = cr.text_extents(label)
    pad_x = 6.0
    pad_y = 3.0
    bubble_w = extents[2] + pad_x * 2
    bubble_h = extents[3] + pad_y * 2
    bubble_x = (boundary_x + next_x - bubble_w) / 2
    bubble_x = min(max(bubble_x, state.LABEL_WIDTH + 4.0), width - bubble_w - 4.0)
    bubble_y = min(
        max(bracket_y - bubble_h - 7.0, feedback_y + 2.0),
        feedback_bottom - bubble_h - 2.0,
    )

    cr.set_source_rgba(0.08, 0.08, 0.08, 0.94)
    cr.rectangle(bubble_x, bubble_y, bubble_w, bubble_h)
    cr.fill()
    cr.set_source_rgba(*color, 0.98)
    cr.rectangle(bubble_x + 0.5, bubble_y + 0.5, bubble_w - 1.0, bubble_h - 1.0)
    cr.stroke()
    cr.move_to(bubble_x + pad_x - extents[0], bubble_y + pad_y - extents[1])
    cr.show_text(label)


def _gap_feedback_bounds(
    state: TimelineRenderState,
    gap: TimelineGap,
    height: int,
) -> tuple[float, float]:
    if gap.scope != "track" or gap.track is None:
        return float(state.RULER_HEIGHT), float(height - state.RULER_HEIGHT)

    if gap.track == "keyboard":
        return float(state._kb_y), float(state._kb_track_h)
    if gap.track == "mouse":
        return float(state._m_y), float(state._m_track_h)
    if gap.track == "gamepad":
        return float(state._g_y), float(state._g_track_h)
    if gap.track == "movement":
        return float(state._wave_y), state.TRACK_HEIGHT / 2
    if gap.track == "control":
        return state._wave_y + state.TRACK_HEIGHT / 2, state.TRACK_HEIGHT / 2
    return float(state.RULER_HEIGHT), float(height - state.RULER_HEIGHT)


def _draw_pointer_guide(cr, state: TimelineRenderState, width: int, height: int) -> None:
    if state._context_menu_x is not None:
        state._draw_vertical_guide(cr, width, height, float(state._context_menu_x))

    if state._hover_x is None or state._hover_y is None:
        return
    if state._hover_gap is not None:
        return

    x = float(state._hover_x)
    y = float(state._hover_y)
    if y < state.RULER_HEIGHT or y > height:
        return

    cr.set_source_rgba(1.0, 1.0, 1.0, 0.35)
    cr.set_line_width(1.0)
    cr.move_to(state.LABEL_WIDTH, y + 0.5)
    cr.line_to(width, y + 0.5)
    cr.stroke()

    state._draw_vertical_guide(cr, width, height, x)

    t_us = max(0, state._x_to_time_us(x))
    label = _format_time_us(t_us)

    cr.select_font_face("monospace", 0, 0)
    cr.set_font_size(10)
    extents = cr.text_extents(label)
    pad_x = 6.0
    pad_y = 3.0
    bubble_w = extents[2] + pad_x * 2
    bubble_h = extents[3] + pad_y * 2

    bx = min(max(x + 14.0, state.LABEL_WIDTH + 4.0), width - bubble_w - 4.0)
    by = min(max(y - bubble_h - 12.0, state.RULER_HEIGHT + 2.0), height - bubble_h - 2.0)

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


def _draw_vertical_guide(cr, state: TimelineRenderState, width: int, height: int, x: float) -> None:
    if not (state.LABEL_WIDTH <= x <= width):
        return
    cr.set_source_rgba(1.0, 1.0, 1.0, 0.35)
    cr.set_line_width(1.0)
    cr.move_to(x + 0.5, state.RULER_HEIGHT)
    cr.line_to(x + 0.5, height)
    cr.stroke()
