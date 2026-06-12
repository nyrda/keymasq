import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import copy
import math
import re
from collections.abc import Callable

import evdev
from gi.repository import (  # pyright: ignore[reportAttributeAccessIssue]
    Adw,  # pyright: ignore[reportAttributeAccessIssue]
    Gdk,  # pyright: ignore[reportAttributeAccessIssue]
    GLib,  # noqa: F401  # pyright: ignore[reportAttributeAccessIssue, reportUnusedImport]
    Gtk,  # pyright: ignore[reportAttributeAccessIssue]
)

from keymasq.common.models import (
    DEFAULT_MACRO_LOOP_STOP_BEHAVIOR,
    normalize_macro_loop_stop_behavior,
)
from keymasq.common.slurp import get_slurp_capture
from keymasq.gui.session_client import (
    GuiTaskResult,
    JsonDict,
    run_gui_task,
    session_request,
    session_request_async,
)
from keymasq.gui.session_reload import notify_session_reload_async
from keymasq.gui.widgets.macro_editor import timing_ops
from keymasq.gui.widgets.macro_editor.add_popovers import MacroEditorAddPopoversMixin
from keymasq.gui.widgets.macro_editor.model import (  # noqa: F401
    EditableControl,
    EditableEvent,
    EditableMove,
    MacroEvent,
    _describe_compositor_control,
    _describe_passthrough_event,
    _get_key_name,
    _passthrough_track,
    parse_events,
    reconstruct_events,
)
from keymasq.gui.widgets.macro_editor.panels import (  # noqa: F401
    _LOOP_MODE_OPTIONS,
    MacroEditorPanelsMixin,
    _build_option_dropdown,
    _get_dropdown_selected_id,
    _set_dropdown_selected_id,
    _set_entry_text_if_needed,
)
from keymasq.gui.widgets.macro_editor.timeline import TimelineWidget
from keymasq.gui.widgets.position_capture import PositionCaptureController
from keymasq.session.compositor import detect_compositor_sync

__all__ = [
    "GLib",
    "MacroEditorDialog",
    "EditableControl",
    "EditableEvent",
    "EditableMove",
    "MacroEvent",
    "_LOOP_MODE_OPTIONS",
    "_build_option_dropdown",
    "_describe_compositor_control",
    "_describe_passthrough_event",
    "_get_dropdown_selected_id",
    "_get_key_name",
    "_passthrough_track",
    "_set_dropdown_selected_id",
    "_set_entry_text_if_needed",
    "parse_events",
    "reconstruct_events",
]


def _compute_macro_editor_dialog_size(parent: Gtk.Window) -> tuple[int, int]:
    width = 760
    height = 680

    parent_width = parent.get_width()
    parent_height = parent.get_height()
    if parent_width > 1:
        width = int(max(760, min(1500, parent_width * 0.9)))
    if parent_height > 1:
        height = int(max(620, min(1000, parent_height * 0.9)))

    return width, height


# ---------------------------------------------------------------------------
# Main editor dialog
# ---------------------------------------------------------------------------


class MacroEditorDialog(Adw.Dialog, MacroEditorPanelsMixin, MacroEditorAddPopoversMixin):
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
        self._rel_events: list[MacroEvent] = []
        self._passthrough_events: list[MacroEvent] = []
        self._synthetic_moves: list[EditableMove] = []
        self._control_events: list[EditableControl] = []
        self._duration_us: int = 0
        self._macro_loop_mode: str = "none"
        self._macro_loop_count: int = 1
        self._macro_loop_stop_behavior: str = DEFAULT_MACRO_LOOP_STOP_BEHAVIOR
        self._macro_move_to_start: bool = False
        self._macro_start_x: int = 0
        self._macro_start_y: int = 0
        self._macro_block_mouse_movement: bool = False
        self._capture_delay_seconds: float = 2.0
        self._capture_timeout_id: int = 0
        self._capture_pending: bool = False
        self._capture_request_id: int = 0
        self._move_capture_timeout_id: int = 0
        self._move_capture_pending: bool = False
        self._move_capture_request_id: int = 0
        self._slurp_capture = get_slurp_capture()
        self._slurp_capture.set_compositor(detect_compositor_sync())
        self._slurp_available = self._slurp_capture.available
        self._start_position_capture = PositionCaptureController(
            slurp_capture=self._slurp_capture,
            slurp_available=self._slurp_available,
            request_async=session_request_async,
            on_state_changed=self._sync_start_position_capture_legacy_state,
        )
        self._selected_move_capture = PositionCaptureController(
            slurp_capture=self._slurp_capture,
            slurp_available=self._slurp_available,
            request_async=session_request_async,
            on_state_changed=self._sync_selected_move_capture_legacy_state,
        )
        self._timing_scale_spin: Gtk.SpinButton | None = None
        self._timing_min_gap_spin: Gtk.SpinButton | None = None
        self._timing_max_gap_spin: Gtk.SpinButton | None = None
        self._timing_extend_ms_spin: Gtk.SpinButton | None = None
        self._insert_gap_at_spin: Gtk.SpinButton | None = None
        self._insert_gap_ms_spin: Gtk.SpinButton | None = None
        self._timeline_scroll_x: float = 0.0
        self._timeline_scroll_max: float = 0.0
        self._timeline_scroll_adj: Gtk.Adjustment | None = None
        self._auto_zoom_enabled: bool = True
        self._auto_zoom_min_pps: float = 90.0
        self._zoom_min_pps: float = 50.0
        self._zoom_max_pps: float = 4000.0
        self._macro_exec_timeout_max_ms: int = 30000
        self._compositor_action_status: dict[str, bool | str | None] = {
            "compositor_id": None,
            "listener_name": None,
            "compositor_dispatch_available": False,
        }
        self._initial_macro_data: dict = {}
        self._initial_state_loaded = False
        self._macro_exists = False
        self._close_warning_dialog: Adw.AlertDialog | None = None
        self._save_in_flight = False
        self._footer_action_buttons: list[Gtk.Button] = []

        # Suppress property-panel spin callbacks during programmatic updates
        self._updating_props = False

        # Drag-to-move is locked by default; user must explicitly unlock it.
        self._drag_locked: bool = True

        # When on, dragging across a timeline track deletes the events it touches.
        self._erase_mode: bool = False

        self._install_css()
        self._build_ui()
        self.set_can_close(False)
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

    def _resolve_compositor_action_status(
        self,
        status: object | None = None,
    ) -> dict[str, bool | str | None]:
        resolved: dict[str, bool | str | None] = {
            "compositor_id": None,
            "listener_name": None,
            "compositor_dispatch_available": False,
        }
        if isinstance(status, dict):
            for key in resolved:
                value = status.get(key)
                if isinstance(value, (bool, str)) or value is None:
                    resolved[key] = value
            return resolved

        root = self._parent.get_root() if hasattr(self._parent, "get_root") else None
        get_status = getattr(root, "get_compositor_action_status", None)
        if callable(get_status):
            root_status = get_status()
            if isinstance(root_status, dict):
                for key in resolved:
                    value = root_status.get(key)
                    if isinstance(value, (bool, str)) or value is None:
                        resolved[key] = value
        return resolved

    def _load_initial_state_async(self) -> None:
        run_gui_task(
            self._load_initial_state,
            self._on_initial_state_loaded,
        )

    def _session_request_async(
        self,
        payload: JsonDict,
        callback: Callable[[JsonDict | None], bool | None],
        timeout: float = 5.0,
    ) -> None:
        session_request_async(payload, callback, timeout=timeout)

    def _load_initial_state(self) -> dict[str, object]:
        timeout_max = 30000
        compositor_status: dict[str, object] = {}
        try:
            status = session_request({"command": "get_status"}) or {}
            timeout_max = int(status.get("macro_exec_timeout_max_ms", 30000) or 30000)
            compositor_status = dict(status)
        except (OSError, RuntimeError, TypeError, ValueError):
            timeout_max = 30000

        macro: dict | None = None
        try:
            response = session_request({"command": "get_macro", "name": self._macro_name}) or {}
            loaded_macro = response.get("macro")
            if response.get("status") == "ok" and isinstance(loaded_macro, dict):
                macro = loaded_macro
        except (OSError, RuntimeError, TypeError, ValueError):
            macro = None

        return {
            "timeout_max": max(1, timeout_max),
            "compositor_status": compositor_status,
            "macro": macro,
        }

    def _on_initial_state_loaded(self, result: GuiTaskResult[dict[str, object]]) -> bool:
        payload = result.value if result.ok and isinstance(result.value, dict) else {}
        timeout_max_raw = payload.get("timeout_max", 30000)
        timeout_max = timeout_max_raw if isinstance(timeout_max_raw, int) else 30000
        self._macro_exec_timeout_max_ms = max(1, timeout_max)
        self._compositor_action_status = self._resolve_compositor_action_status(
            payload.get("compositor_status")
        )

        timeout_adjustment = self._control_timeout_spin.get_adjustment()
        timeout_adjustment.set_upper(self._macro_exec_timeout_max_ms)
        timeout_adjustment.set_value(
            min(timeout_adjustment.get_value(), float(self._macro_exec_timeout_max_ms))
        )

        macro = payload.get("macro")
        if isinstance(macro, dict):
            self._macro_exists = True
            self._apply_macro_state(macro)
            self._sync_macro_settings_controls()
            self._initial_macro_data = self._current_macro_payload()
            self._refresh_loaded_macro_state()
        else:
            self._initial_macro_data = self._current_macro_payload()
        self._initial_state_loaded = True
        self._sync_close_guard()
        return False

    def _refresh_loaded_macro_state(self) -> None:
        self._update_stats()
        self._timeline.queue_draw()
        self._update_canvas_width()

    def _current_macro_payload(self) -> dict:
        name = (
            self._name_entry.get_text().strip()
            if hasattr(self, "_name_entry")
            else self._macro_name
        )
        return self._build_macro_payload(name)

    def _macro_payload_for_dirty_compare(self, payload: dict) -> dict:
        data = copy.deepcopy(payload)
        device_types = data.get("device_types")
        if isinstance(device_types, list):
            data["device_types"] = sorted(str(item) for item in device_types)
        return data

    def _has_pending_changes(self) -> bool:
        if not self._initial_state_loaded:
            return False
        if not self._initial_macro_data:
            return False
        current = self._macro_payload_for_dirty_compare(self._current_macro_payload())
        initial = self._macro_payload_for_dirty_compare(self._initial_macro_data)
        return current != initial

    def _sync_close_guard(self) -> None:
        if not hasattr(self, "_name_entry"):
            return
        self.set_can_close(not self._save_in_flight and not self._has_pending_changes())

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
        self._duration_us = int(self._macro_data.get("duration_us", 0) or 0)
        self._macro_move_to_start = bool(self._macro_data.get("move_to_start", False))
        self._macro_start_x = int(self._macro_data.get("start_x", 0) or 0)
        self._macro_start_y = int(self._macro_data.get("start_y", 0) or 0)
        self._macro_block_mouse_movement = bool(self._macro_data.get("block_mouse_movement", False))
        self._macro_loop_mode = str(self._macro_data.get("loop_mode", "none") or "none")
        self._macro_loop_count = max(1, int(self._macro_data.get("loop_count", 1) or 1))
        self._macro_loop_stop_behavior = normalize_macro_loop_stop_behavior(
            self._macro_data.get("loop_stop_behavior")
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
                max(m.t_us for m in self._synthetic_moves),
            )
        if self._control_events:
            self._duration_us = max(
                self._duration_us,
                max(c.t_us for c in self._control_events),
            )

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

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
        if isinstance(selected, dict) and selected not in self._passthrough_events:
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

    # ------------------------------------------------------------------
    # Save / Save as copy
    # ------------------------------------------------------------------

    def _on_save(self, btn: Gtk.Button) -> None:
        self._save_current_macro(btn, close_after_save=True)

    def _on_apply(self, btn: Gtk.Button) -> None:
        self._save_current_macro(btn, close_after_save=False)

    def _save_current_macro(self, btn: Gtk.Button | None, *, close_after_save: bool) -> None:
        if self._save_in_flight:
            return

        new_name = self._name_entry.get_text().strip()
        if not self._validate_name_for_save(new_name):
            return

        macro_payload = self._build_macro_payload(new_name)
        revision = int(self._macro_data.get("revision", 1))

        def save_request() -> JsonDict | None:
            return self._save_macro_request(new_name, macro_payload, revision)

        def on_save_finished(result: GuiTaskResult[JsonDict | None]) -> bool:
            return self._on_save_finished(
                result,
                new_name,
                macro_payload,
                close_after_save=close_after_save,
            )

        def on_save_start() -> None:
            self._set_save_controls_sensitive(False, extra_button=btn)

        def on_save_done() -> None:
            self._finish_save_request(extra_button=btn)

        self._save_in_flight = True
        self._sync_close_guard()
        run_gui_task(
            save_request,
            on_save_finished,
            on_start=on_save_start,
            on_done=on_save_done,
        )

    def _set_save_controls_sensitive(
        self,
        sensitive: bool,
        *,
        extra_button: Gtk.Button | None = None,
    ) -> None:
        for button in self._footer_action_buttons:
            button.set_sensitive(sensitive)
        if extra_button is not None and not any(
            button is extra_button for button in self._footer_action_buttons
        ):
            extra_button.set_sensitive(sensitive)

    def _finish_save_request(self, *, extra_button: Gtk.Button | None = None) -> None:
        self._save_in_flight = False
        self._set_save_controls_sensitive(True, extra_button=extra_button)
        self._sync_close_guard()

    def _save_macro_request(
        self,
        new_name: str,
        macro_payload: dict,
        revision: int,
    ) -> JsonDict | None:
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

    def _on_save_finished(
        self,
        result: GuiTaskResult[JsonDict | None],
        requested_name: str,
        requested_payload: dict,
        *,
        close_after_save: bool,
    ) -> bool:
        try:
            payload = result.value if result.ok and isinstance(result.value, dict) else {}
            if payload.get("status") != "ok":
                if self._is_name_conflict_response(payload, requested_name):
                    self._show_name_conflict(requested_name)
                else:
                    self._show_save_error(self._save_error_message(result, payload))
                return False

            self._apply_saved_macro_state(payload, requested_name, requested_payload)
            notify_session_reload_async()
            if close_after_save:
                self._force_close_without_warning()
            return False
        finally:
            self._finish_save_request()

    def _is_name_conflict_response(self, payload: JsonDict, requested_name: str) -> bool:
        status = str(payload.get("status", "") or "")
        if status in {"name-conflict", "name_conflict"}:
            return True
        message = str(payload.get("message", "") or "")
        return status == "error" and message == f"Macro '{requested_name}' already exists"

    def _save_error_message(
        self,
        result: GuiTaskResult[JsonDict | None],
        payload: JsonDict,
    ) -> str:
        if isinstance(payload.get("message"), str) and payload["message"].strip():
            return str(payload["message"])
        if result.error is not None:
            return str(result.error).strip() or result.error.__class__.__name__
        return "Failed to save macro"

    def _apply_saved_macro_state(
        self,
        save_response: JsonDict,
        requested_name: str,
        requested_payload: dict,
    ) -> None:
        saved_macro = save_response.get("macro")
        if not isinstance(saved_macro, dict):
            saved_macro = dict(requested_payload)

        self._macro_data = copy.deepcopy(saved_macro)
        saved_name = str(saved_macro.get("name", requested_name) or requested_name)
        self._macro_name = saved_name
        self._macro_exists = True
        _set_entry_text_if_needed(self._name_entry, saved_name)
        self.set_title(f"Edit macro ({saved_name})")
        self._initial_state_loaded = True
        self._initial_macro_data = self._current_macro_payload()
        self._sync_close_guard()

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
        cancel.connect("clicked", self._on_close_dialog_clicked, dialog)
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

            def create_copy_request() -> JsonDict | None:
                return session_request({"command": "create_macro", "macro": copy_payload}) or {}

            def on_copy_finished(result: GuiTaskResult[JsonDict | None]) -> bool:
                return self._on_save_copy_finished(
                    result,
                    name,
                    error_lbl,
                    dialog,
                )

            def on_copy_start() -> None:
                save.set_sensitive(False)

            def on_copy_done() -> None:
                save.set_sensitive(True)

            run_gui_task(
                create_copy_request,
                on_copy_finished,
                on_start=on_copy_start,
                on_done=on_copy_done,
            )

        save.connect("clicked", on_save_copy)
        entry.connect("activate", on_save_copy)
        btn_row.append(save)
        box.append(btn_row)

        dialog.set_child(box)
        dialog.present(self._parent)

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self._request_close()

    def do_close_attempt(self) -> None:
        self._request_close()

    def _request_close(self) -> None:
        if not self._has_pending_changes():
            self._force_close_without_warning()
            return
        self._show_unsaved_close_warning()

    def _force_close_without_warning(self) -> None:
        self._cancel_capture_start_position("")
        self._cancel_capture_selected_move("")
        self.set_can_close(True)
        self.force_close()

    def _show_unsaved_close_warning(self) -> None:
        if self._close_warning_dialog is not None:
            return

        dialog = Adw.AlertDialog()
        dialog.set_heading("Unsaved Macro Changes")
        dialog.set_body("Save your changes before closing, or discard them?")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("discard", "Discard")
        dialog.add_response("save", "Save")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._on_unsaved_close_response)
        self._close_warning_dialog = dialog
        dialog.present(self)

    def _on_unsaved_close_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        self._close_warning_dialog = None
        if response == "discard":
            self._force_close_without_warning()
            return
        if response == "save":
            self._save_current_macro(None, close_after_save=True)

    def _on_close_dialog_clicked(self, _button: Gtk.Button, dialog: Adw.Dialog) -> None:
        dialog.close()

    def _on_popover_cancel_clicked(self, _button: Gtk.Button, popover: Gtk.Popover) -> None:
        popover.popdown()

    def _on_save_copy_finished(
        self,
        result: GuiTaskResult[JsonDict | None],
        requested_name: str,
        error_label: Gtk.Label,
        dialog: Adw.Dialog,
    ) -> bool:
        payload = result.value if result.ok and isinstance(result.value, dict) else {}
        if payload.get("status") != "ok":
            error_label.set_label(
                payload.get("message", f"'{requested_name}' already exists")
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

    def _show_save_error(self, message: str) -> None:
        d = Adw.AlertDialog()
        d.set_heading("Unable To Save Macro")
        d.set_body(message)
        d.add_response("ok", "OK")
        d.set_default_response("ok")
        d.set_close_response("ok")
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
        data["duration_us"] = duration_us
        data["device_types"] = device_types
        data["loop_mode"] = _get_dropdown_selected_id(
            self._macro_loop_mode_combo,
            _LOOP_MODE_OPTIONS,
            "none",
        )
        data["loop_count"] = max(1, int(self._macro_loop_count_spin.get_value()))
        data["loop_stop_behavior"] = (
            "finish_run" if self._macro_loop_finish_check.get_active() else "cancel_run"
        )
        data["move_to_start"] = bool(self._macro_move_to_start_check.get_active())
        data["start_x"] = int(self._macro_start_x_spin.get_value())
        data["start_y"] = int(self._macro_start_y_spin.get_value())
        data["block_mouse_movement"] = bool(self._macro_block_mouse_check.get_active())
        return data

    def close(self) -> None:
        self._request_close()
