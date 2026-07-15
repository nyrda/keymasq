"""Timeline viewport and timing-edit coordination."""

# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false

import math

import evdev
import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.widgets.macro_editor import timing_ops
from keymasq.gui.widgets.macro_editor.model import (
    EditableControl,
    EditableEvent,
    EditableMove,
)
from keymasq.gui.widgets.macro_editor.timeline import TimelineWidget


class TimelineControllerMixin:
    """Coordinate timeline rendering, scrolling, and timing mutations."""

    def _update_stats(self) -> None:
        duration_s = self._duration_us / 1e6
        editable_event_count = sum(
            2 if event.ev_type == evdev.ecodes.EV_KEY else 1 for event in self._events
        )
        event_count = (
            editable_event_count
            + len(self._rel_events)
            + len(self._passthrough_events)
            + len(self._synthetic_moves)
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
        self._timeline._pps = max(
            self._auto_zoom_min_pps,
            min(self._zoom_max_pps, fit_pps),
        )

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
        return timing_ops.all_timestamps(
            self._events,
            self._rel_events,
            self._passthrough_events,
            self._synthetic_moves,
            self._control_events,
            include_passthrough=include_passthrough,
        )

    def _apply_time_map(self, mapping: dict[int, int]) -> None:
        timing_ops.apply_time_map(
            self._events,
            self._rel_events,
            self._passthrough_events,
            self._synthetic_moves,
            self._control_events,
            mapping,
        )

    def _recompute_duration(self) -> None:
        self._duration_us = timing_ops.compute_duration_us(
            self._events,
            self._rel_events,
            self._passthrough_events,
            self._synthetic_moves,
            self._control_events,
        )

    def _refresh_after_timing_edit(self) -> None:
        selected_obj = self._timeline._selected
        self._recompute_duration()
        self._update_stats()
        self._update_canvas_width()
        self._timeline.queue_draw()
        if selected_obj is not None:
            self._on_selection_changed(selected_obj)
        self._sync_close_guard()

    def _build_time_mapping_with_gap_limits(
        self,
        *,
        scale: float = 1.0,
        min_gap_us: int = 0,
        max_gap_us: int | None = None,
        include_passthrough: bool = True,
    ) -> dict[int, int]:
        return timing_ops.build_time_mapping_with_gap_limits(
            self._events,
            self._rel_events,
            self._passthrough_events,
            self._synthetic_moves,
            self._control_events,
            scale=scale,
            min_gap_us=min_gap_us,
            max_gap_us=max_gap_us,
            include_passthrough=include_passthrough,
        )

    def _on_trim_start_clicked(self, _btn) -> None:
        mapping = timing_ops.build_trim_start_mapping(self._all_timestamps())
        if not mapping:
            return
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

        mapping = timing_ops.build_shift_mapping(
            self._all_timestamps(include_passthrough=True),
            at_us=0,
            delta_us=delta_us,
        )
        if not mapping:
            return

        self._apply_time_map(mapping)
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
        self._sync_close_guard()

    def _on_set_total_time_clicked(self, _btn) -> None:
        if not self._timing_extend_ms_spin:
            return
        target_us = int(float(self._timing_extend_ms_spin.get_value()) * 1000)
        if target_us < 0:
            return

        content_end_us = max(self._all_timestamps(include_passthrough=True), default=0)
        self._duration_us = max(target_us, content_end_us)
        self._update_stats()
        self._update_canvas_width()
        self._timeline.queue_draw()
        self._sync_close_guard()

    def _on_insert_gap_clicked(self, _btn) -> None:
        if not self._insert_gap_at_spin or not self._insert_gap_ms_spin:
            return

        at_us = int(float(self._insert_gap_at_spin.get_value()) * 1000)
        gap_us = int(float(self._insert_gap_ms_spin.get_value()) * 1000)
        control = EditableControl(
            mode="wait",
            t_us=at_us,
            duration_us=max(0, gap_us),
        )
        self._insert_control_event(control)

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
        if (
            isinstance(selected, dict)
            and selected not in self._rel_events
            and selected not in self._passthrough_events
        ):
            self._timeline._selected = None
            self._revealer.set_reveal_child(False)

    def _set_startpoint(self, at_us: int) -> None:
        if max(0, int(at_us)) <= 0:
            return
        (
            self._events,
            self._rel_events,
            self._passthrough_events,
            self._synthetic_moves,
            self._control_events,
        ) = timing_ops.trim_startpoint(
            self._events,
            self._rel_events,
            self._passthrough_events,
            self._synthetic_moves,
            self._control_events,
            at_us,
        )
        self._clear_selection_if_removed()
        self._refresh_after_timing_edit()

    def _set_endpoint(self, at_us: int) -> None:
        (
            self._events,
            self._rel_events,
            self._passthrough_events,
            self._synthetic_moves,
            self._control_events,
        ) = timing_ops.trim_endpoint(
            self._events,
            self._rel_events,
            self._passthrough_events,
            self._synthetic_moves,
            self._control_events,
            at_us,
        )
        self._clear_selection_if_removed()
        self._refresh_after_timing_edit()

    def _ripple_delete_range(self, t0_us: int, t1_us: int) -> None:
        t0_us = max(0, int(t0_us))
        t1_us = int(t1_us)
        if t1_us <= t0_us:
            return
        (
            self._events,
            self._rel_events,
            self._passthrough_events,
            self._synthetic_moves,
            self._control_events,
        ) = timing_ops.ripple_delete_range(
            self._events,
            self._rel_events,
            self._passthrough_events,
            self._synthetic_moves,
            self._control_events,
            t0_us,
            t1_us,
        )
        self._clear_selection_if_removed()
        self._refresh_after_timing_edit()

    def _shift_timeline_for_gap(
        self,
        *,
        at_us: int,
        delta_us: int,
        scope: str,
        exclude_control: EditableControl | None,
    ) -> bool:
        return timing_ops.shift_timeline_for_gap(
            self._events,
            self._rel_events,
            self._passthrough_events,
            self._synthetic_moves,
            self._control_events,
            at_us=at_us,
            delta_us=delta_us,
            scope=scope,
            exclude_control=exclude_control,
        )
