"""Timeline selection gestures and keyboard commands."""

# pyright: reportAttributeAccessIssue=false, reportUnknownLambdaType=false

from collections.abc import Callable

import evdev
import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.widgets.macro_editor import selection, timeline_render
from keymasq.gui.widgets.macro_editor.model import (
    EditableControl,
    EditableEvent,
    EditableMove,
    _passthrough_track,
)


class TimelineSelectionMixin:
    def _init_selection(self) -> None:
        self._selection: list[selection.Item] = []
        self._time_selection: tuple[int, int] | None = None
        self._bulk_original_range: tuple[int, int] | None = None
        self._insertion_us = 0
        self._bulk_drag_active = False
        self._bulk_drag_kind = ""
        self._selection_box: tuple[float, float, float, float] | None = None
        self._bulk_originals: list[tuple[selection.Item, int]] = []
        self._box_base: list[selection.Item] = []
        self._bulk_offset_y = 0.0
        self.set_focusable(True)
        keys = Gtk.EventControllerKey.new()
        keys.connect("key-pressed", self._on_selection_key)
        self.add_controller(keys)

    def selected_items(self) -> list[selection.Item]:
        if self._selected is not None:
            return [self._selected]
        return list(self._selection)

    def set_selection(
        self, selected: list[selection.Item], *, time_range: tuple[int, int] | None = None
    ) -> None:
        self._selection = list({id(item): item for item in selected}.values())
        self._time_selection = time_range
        self._selected = self._selection[0] if len(self._selection) == 1 else None
        self._editor._on_selection_changed(self._selected)
        self._editor._update_selection_summary()
        self.queue_draw()

    def set_time_selection(self, first: int, last: int) -> None:
        selected, time_range = selection.select_time_range(
            selection.items(self._editor._timeline_lists()), first, last
        )
        self.set_selection(
            selected, time_range=time_range if time_range[1] > time_range[0] else None
        )

    def prune_selection(self) -> None:
        live = {id(item) for item in selection.items(self._editor._timeline_lists())}
        selected = self.selected_items()
        kept = [item for item in selected if id(item) in live]
        time_range = self._time_selection
        if time_range is not None and kept:
            first, last = selection.bounds(kept)
            expanded = (min(time_range[0], first), max(time_range[1], last))
            if expanded != time_range:
                self.set_selection(kept, time_range=expanded)
                return
        if len(kept) != len(selected):
            self.set_selection(kept, time_range=time_range if kept else None)

    def _selection_click(self, hit: selection.Item, modifiers: int) -> None:
        current = self.selected_items()
        if modifiers & Gdk.ModifierType.CONTROL_MASK:
            self.set_selection(
                [item for item in current if item is not hit]
                if any(item is hit for item in current)
                else [*current, hit]
            )
        else:
            self.set_selection([hit])

    def _begin_selection_drag(self, gesture, x: float, y: float) -> bool:
        if self._editor._erase_mode or x < self.LABEL_WIDTH:
            self._bulk_drag_kind = ""
            return False
        self.grab_focus()
        self._stop_autoscroll()
        self._recompute_lanes()
        hit = self._hit_test(x, y)
        modifiers = gesture.get_current_event_state() if gesture is not None else 0
        self._bulk_modifiers = modifiers
        self._drag_start_x, self._drag_start_y = float(x), float(y)
        self._drag_scroll_origin = self._scroll_offset
        self._drag_last_offset_x = 0.0
        self._bulk_offset_y = 0.0
        self._in_drag = False
        self._bulk_drag_active = False
        self._bulk_hit = hit
        if modifiers & Gdk.ModifierType.SHIFT_MASK:
            self._bulk_drag_kind = "range"
        elif y < self.RULER_HEIGHT:
            self._bulk_drag_kind = "range"
            self._insertion_us = max(0, self._x_to_time_us(x))
        elif hit is not None:
            self.clear_gap_selection()
            selected = self.selected_items()
            if modifiers & Gdk.ModifierType.CONTROL_MASK:
                self._selection_click(hit, modifiers)
            elif not any(item is hit for item in selected):
                self._selection_click(hit, 0)
            self._bulk_drag_kind = "move"
            self._bulk_originals = [(item, selection.start(item)) for item in self.selected_items()]
            self._bulk_original_range = self._time_selection
        else:
            self._bulk_drag_kind = "box"
            self._box_base = (
                self.selected_items() if modifiers & Gdk.ModifierType.CONTROL_MASK else []
            )
        return True

    def _update_selection_drag(self, offset_x: float, offset_y: float) -> bool:
        if not self._bulk_drag_kind:
            return False
        if not self._in_drag and abs(offset_x) < 4 and abs(offset_y) < 4:
            return True
        if self._bulk_drag_kind == "move" and (
            self._editor._drag_locked
            or self._bulk_modifiers & Gdk.ModifierType.CONTROL_MASK
        ):
            return True
        if not self._in_drag:
            self._editor._record_edit_history()
            self.clear_gap_selection()
        self._in_drag = True
        self._bulk_drag_active = self._bulk_drag_kind == "move"
        self._drag_last_offset_x = float(offset_x)
        self._bulk_offset_y = float(offset_y)
        self._apply_selection_drag()
        self._update_autoscroll(self._drag_start_x + float(offset_x))
        return True

    def _apply_selection_drag(self) -> None:
        scroll_delta = self._scroll_offset - self._drag_scroll_origin
        if self._bulk_drag_kind == "move":
            delta = round((self._drag_last_offset_x + scroll_delta) / self._pps * 1e6)
            delta = max(delta, -min((stamp for _, stamp in self._bulk_originals), default=0))
            if self._bulk_original_range is not None:
                first, last = self._bulk_original_range
                delta = max(delta, -first)
                self._time_selection = (first + delta, last + delta)
            for item, original in self._bulk_originals:
                selection.shift(item, original + delta - selection.start(item))
            self._editor._on_selection_changed(self._selected)
        elif self._bulk_drag_kind == "range":
            first = (
                self._insertion_us
                if self._bulk_modifiers & Gdk.ModifierType.SHIFT_MASK
                else self._x_to_time_us(self._drag_start_x - scroll_delta)
            )
            last = self._x_to_time_us(self._drag_start_x + self._drag_last_offset_x)
            self.set_time_selection(first, last)
        else:
            x0 = self._drag_start_x - scroll_delta
            x1 = self._drag_start_x + self._drag_last_offset_x
            y0, y1 = self._drag_start_y, self._drag_start_y + self._bulk_offset_y
            self._selection_box = (min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
            hits = self._box_hits(*self._selection_box)
            self.set_selection([*self._box_base, *hits])
        self.queue_draw()

    def _end_selection_drag(self) -> bool:
        if not self._bulk_drag_kind:
            return False
        self._stop_autoscroll()
        moved = self._in_drag and self._bulk_drag_kind == "move"
        if not self._in_drag and not self._gap_double_click_handled:
            if (
                self._bulk_drag_kind == "range"
                and self._bulk_modifiers & Gdk.ModifierType.SHIFT_MASK
            ):
                self.clear_gap_selection()
                self._apply_selection_drag()
            elif self._bulk_hit is not None:
                if not self._bulk_modifiers:
                    self._selection_click(self._bulk_hit, 0)
            else:
                self._insertion_us = max(0, self._x_to_time_us(self._drag_start_x))
                if not self._bulk_modifiers:
                    self.set_selection([])
                gap = None
                if not self._editor._drag_locked:
                    gap = self._gap_at_position(self._drag_start_x, self._drag_start_y)
                if gap is not None:
                    self._select_gap(gap, self._drag_start_x, self._drag_start_y)
                else:
                    self.clear_gap_selection()
        self._bulk_drag_kind = ""
        self._bulk_drag_active = False
        self._in_drag = False
        self._selection_box = None
        self._bulk_originals = []
        self._bulk_original_range = None
        self._gap_double_click_handled = False
        if moved:
            self._editor._finish_selection_edit()
        self._editor._update_selection_summary()
        self.queue_draw()
        return True

    def _box_hits(self, x: float, y: float, width: float, height: float) -> list[selection.Item]:
        hits: list[selection.Item] = []
        # Lay out raw markers once per track, not once per event. Include the
        # scrolled-out part of the rectangle when selecting a long recording.
        raw_rects: dict[int, tuple[float, float, float, float]] = {}
        state = self._build_render_state()
        for track, top, track_h in (
            ("keyboard", self._kb_y, self._kb_track_h),
            ("mouse", self._m_y, self._m_track_h),
            ("gamepad", self._g_y, self._g_track_h),
            ("movement", self._wave_y, self.TRACK_HEIGHT),
        ):
            for item, mx, my, size in timeline_render.get_passthrough_marker_layouts(
                state, track, self.get_width(), top, track_h, include_offscreen=True
            ):
                raw_rects[id(item)] = (mx - size, my - size, 2 * size, 2 * size)
        for item in selection.items(self._editor._timeline_lists()):
            rect = raw_rects.get(id(item))
            ix, iy, iw, ih = rect if rect is not None else self._selection_item_rect(item)
            if ix <= x + width and ix + iw >= x and iy <= y + height and iy + ih >= y:
                hits.append(item)
        return hits

    def _selection_item_rect(self, item: selection.Item) -> tuple[float, float, float, float]:
        x = self._time_to_x(selection.start(item))
        if isinstance(item, EditableEvent):
            prefix = {"keyboard": "kb", "mouse": "m", "gamepad": "g"}[item.device_type]
            lanes = getattr(self, f"_{prefix}_lanes")
            lane_h = getattr(self, f"_{prefix}_track_h") / getattr(self, f"_{prefix}_num_lanes")
            y = getattr(self, f"_{prefix}_y") + lanes.get(id(item), 0) * lane_h
            margin = max(1, min(4, int(lane_h * 0.10)))
            return (
                x,
                y + margin,
                max(self.MIN_EVENT_WIDTH, self._time_to_x(selection.end(item)) - x),
                lane_h - 2 * margin,
            )
        if isinstance(item, (EditableMove, EditableControl)):
            y = self._wave_y + (self.TRACK_HEIGHT - 14 if isinstance(item, EditableControl) else 14)
            return x - 7, y - 7, 14, 14
        if item.get("type") == evdev.ecodes.EV_REL:
            return x - 1, self._wave_y + 4, 2, self.TRACK_HEIGHT - 8
        track = _passthrough_track(item)
        prefix = {"keyboard": "kb", "mouse": "m", "gamepad": "g"}.get(track)
        top = getattr(self, f"_{prefix}_y") if prefix else self._wave_y
        track_h = getattr(self, f"_{prefix}_track_h") if prefix else self.TRACK_HEIGHT
        return x - 5, top + track_h - 16, 10, 10

    def _draw_selection_overlay(self, cr, width: int, height: int) -> None:
        cr.save()
        cr.rectangle(self.LABEL_WIDTH, 0, max(0, width - self.LABEL_WIDTH), height)
        cr.clip()
        x = self._time_to_x(self._insertion_us)
        cr.set_source_rgba(0.55, 0.8, 1.0, 0.8)
        cr.set_line_width(1)
        cr.set_dash([3, 3])
        cr.move_to(x, 0)
        cr.line_to(x, height)
        cr.stroke()
        cr.set_dash([])
        if self._time_selection is not None:
            first, last = self._time_selection
            left, right = self._time_to_x(first), self._time_to_x(last)
            cr.set_source_rgba(0.4, 0.7, 1.0, 0.12)
            cr.rectangle(left, 0, right - left, height)
            cr.fill()
            cr.set_source_rgba(0.4, 0.7, 1.0, 0.25)
            cr.rectangle(left, 0, right - left, self.RULER_HEIGHT)
            cr.fill()
            cr.set_source_rgba(0.55, 0.8, 1.0, 0.9)
            for edge in (left, right):
                cr.move_to(edge, 0)
                cr.line_to(edge, height)
            cr.stroke()
        selected_ids = {id(item) for item in self.selected_items()}
        for item in self._editor._rel_events:
            if id(item) in selected_ids:
                cr.set_source_rgba(0.55, 0.8, 1.0, 0.8)
                cr.rectangle(*self._selection_item_rect(item))
                cr.fill()
        if self._selection_box is not None:
            cr.set_source_rgba(0.4, 0.7, 1, 0.15)
            cr.rectangle(*self._selection_box)
            cr.fill_preserve()
            cr.set_source_rgba(0.55, 0.8, 1, 0.9)
            cr.stroke()
        cr.restore()

    def _on_selection_key(self, _controller, keyval: int, _keycode: int, state: int) -> bool:
        if self._bulk_drag_kind:
            return False
        if state & Gdk.ModifierType.CONTROL_MASK:
            key = Gdk.keyval_to_lower(keyval)
            commands: dict[int, Callable[[], None]] = {
                Gdk.KEY_a: self._editor._select_all,
                Gdk.KEY_c: self._editor._copy_selection,
                Gdk.KEY_x: self._editor._cut_selection,
                Gdk.KEY_v: self._editor._paste_selection,
                Gdk.KEY_z: lambda: self._editor._restore_history(
                    redo=bool(state & Gdk.ModifierType.SHIFT_MASK)
                ),
                Gdk.KEY_y: lambda: self._editor._restore_history(redo=True),
            }
            command = commands.get(key)
            if command is not None:
                command()
                return True
        elif keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace):
            self._editor._delete_selection()
            return True
        elif keyval == Gdk.KEY_Escape:
            self.set_selection([])
            self.clear_gap_selection()
            return True
        return False
