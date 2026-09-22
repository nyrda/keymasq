"""Selection-driven event property panel and event editing actions."""

import gi

# pyright: reportAttributeAccessIssue=false, reportUnknownLambdaType=false

gi.require_version("Gtk", "4.0")

import evdev
from gi.repository import Gtk, Pango  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.gamepad_axes import (
    clamp_gamepad_axis_value,
    gamepad_axis_range,
)
from keymasq.common.macro_rapidfire import plan_macro_rapidfire
from keymasq.gui.widgets.macro_editor.model import (
    EditableControl,
    EditableEvent,
    EditableMove,
    MacroEvent,
    _describe_passthrough_event,
    _get_event_name,
    _get_key_name,
)
from keymasq.gui.widgets.macro_editor.panel.rows import (
    field_row,
    group_box,
    rows_list,
    unit_label,
)
from keymasq.gui.widgets.mouse_move_units import format_natural_move_speed

_REL_MOVE_COORDINATE_RANGE = (-10000, 10000)
_ABS_MOVE_COORDINATE_RANGE = (-100000, 100000)


def _is_gamepad_axis_event(ev: EditableEvent) -> bool:
    return ev.device_type == "gamepad" and ev.ev_type == evdev.ecodes.EV_ABS


def _event_target_name(ev: EditableEvent) -> str:
    return _get_event_name(ev.ev_type, ev.code).lower()


class EventPropertiesMixin:
    """Construct and update the editor for the current timeline selection."""

    def _key_detail(self, ev: EditableEvent) -> str:
        output_suffix = f" @ {ev.output_id}" if ev.device_type == "gamepad" and ev.output_id else ""
        detail = f"Code {ev.code}{output_suffix}"
        if ev.device_type == "keyboard" and ev.ev_type == evdev.ecodes.EV_KEY:
            layout_id = self._layout_output_id
            output = self._layout_key_outputs.get(ev.code)
            if layout_id is not None and output is not None:
                detail += f" · {layout_id}: {output}"
        if ev.rapidfire_enabled:
            plan = plan_macro_rapidfire(
                ev.release_t_us - ev.press_t_us, ev.rapidfire_hold_ms, ev.rapidfire_wait_ms
            )
            detail += (
                f" · {plan.count} pulses · Hold {plan.hold_us / 1000:g} ms"
                f" · Fitted wait {plan.wait_us / 1000:.3f} ms"
                if plan.count > 1
                else " · One hold spanning the duration"
            )
        return detail

    def _refresh_selected_key_detail(self) -> None:
        selected = self._timeline._selected
        if isinstance(selected, EditableEvent) and not _is_gamepad_axis_event(selected):
            self._key_info_label.set_label(self._key_detail(selected))

    def _build_property_panel(self) -> Gtk.Widget:
        self._revealer = Gtk.Revealer()
        self._revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._revealer.set_reveal_child(False)

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._prop_title = Gtk.Label()
        self._prop_title.add_css_class("heading")
        self._prop_title.set_halign(Gtk.Align.START)
        self._prop_title.set_ellipsize(Pango.EllipsizeMode.END)
        title_row.append(self._prop_title)
        self._prop_context_label = Gtk.Label()
        self._prop_context_label.add_css_class("heading")
        self._prop_context_label.add_css_class("dim-label")
        self._prop_context_label.set_halign(Gtk.Align.START)
        self._prop_context_label.set_hexpand(True)
        self._prop_context_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._prop_context_label.set_visible(False)
        title_row.append(self._prop_context_label)
        title_spacer = Gtk.Box()
        title_spacer.set_hexpand(True)
        title_row.append(title_spacer)

        header_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        header_actions.set_valign(Gtk.Align.CENTER)
        self._edit_child_macro_btn = Gtk.Button(icon_name="document-edit-symbolic")
        self._edit_child_macro_btn.add_css_class("flat")
        self._edit_child_macro_btn.set_tooltip_text("Edit child macro in a new window")
        self._edit_child_macro_btn.set_visible(False)
        self._edit_child_macro_btn.connect("clicked", self._on_edit_child_macro)
        header_actions.append(self._edit_child_macro_btn)

        change_key_btn = Gtk.Button(label="Change Key…")
        change_key_btn.add_css_class("flat")
        change_key_btn.connect("clicked", self._on_change_key_clicked)
        header_actions.append(change_key_btn)
        self._change_key_btn = change_key_btn

        delete_btn = Gtk.Button(icon_name="user-trash-symbolic")
        delete_btn.add_css_class("destructive-action")
        delete_btn.add_css_class("flat")
        delete_btn.set_tooltip_text("Delete this action (Delete)")
        delete_btn.connect("clicked", self._on_delete_event)
        header_actions.append(delete_btn)
        self._delete_event_btn = delete_btn
        title_row.append(header_actions)

        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        header.append(title_row)
        self._key_info_label = Gtk.Label()
        self._key_info_label.add_css_class("dim-label")
        self._key_info_label.add_css_class("caption")
        self._key_info_label.set_halign(Gtk.Align.START)
        self._key_info_label.set_xalign(0.0)
        self._key_info_label.set_wrap(True)
        self._key_info_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        header.append(self._key_info_label)

        self._press_spin = Gtk.SpinButton()
        self._press_spin.set_adjustment(
            Gtk.Adjustment(
                value=0,
                lower=0,
                upper=3600000,
                step_increment=1,
                page_increment=10,
            )
        )
        self._press_spin.set_digits(0)
        self._press_spin.set_width_chars(8)
        self._press_spin.connect("value-changed", self._on_press_changed)
        self._press_row = field_row("At", self._press_spin, unit_label("ms"))

        self._duration_spin = Gtk.SpinButton()
        self._duration_spin.set_adjustment(
            Gtk.Adjustment(
                value=1,
                lower=1,
                upper=3600000,
                step_increment=1,
                page_increment=10,
            )
        )
        self._duration_spin.set_digits(0)
        self._duration_spin.set_width_chars(8)
        self._duration_spin.connect("value-changed", self._on_duration_changed)
        self._duration_row = field_row("Duration", self._duration_spin, unit_label("ms"))

        self._release_spin = Gtk.SpinButton()
        self._release_spin.set_adjustment(
            Gtk.Adjustment(
                value=0,
                lower=0,
                upper=3600000,
                step_increment=1,
                page_increment=10,
            )
        )
        self._release_spin.set_digits(0)
        self._release_spin.set_width_chars(8)
        self._release_spin.connect("value-changed", self._on_release_changed)
        self._release_row = field_row("Release", self._release_spin, unit_label("ms"))

        self._move_x_spin = Gtk.SpinButton()
        self._move_x_spin.set_adjustment(
            Gtk.Adjustment(
                value=0,
                lower=-10000,
                upper=10000,
                step_increment=1,
            )
        )
        self._move_x_spin.set_digits(0)
        self._move_x_spin.set_width_chars(8)
        self._move_x_spin.connect("value-changed", self._on_move_x_changed)
        self._move_x_row = field_row("X", self._move_x_spin)

        self._move_y_spin = Gtk.SpinButton()
        self._move_y_spin.set_adjustment(
            Gtk.Adjustment(
                value=0,
                lower=-10000,
                upper=10000,
                step_increment=1,
            )
        )
        self._move_y_spin.set_digits(0)
        self._move_y_spin.set_width_chars(8)
        self._move_y_spin.connect("value-changed", self._on_move_y_changed)
        self._move_y_row = field_row("Y", self._move_y_spin)

        self._move_capture_delay_spin = Gtk.SpinButton()
        self._move_capture_delay_spin.set_adjustment(
            Gtk.Adjustment(
                value=self._selected_move_capture.delay_seconds,
                lower=0.2,
                upper=15.0,
                step_increment=0.2,
            )
        )
        self._move_capture_delay_spin.set_digits(1)
        self._move_capture_delay_spin.set_width_chars(4)
        self._move_capture_delay_unit_label = unit_label("s")
        self._move_capture_btn = Gtk.Button(label="Capture")
        self._move_capture_btn.connect("clicked", self._on_capture_selected_move_clicked)
        self._move_capture_row = field_row(
            "",
            self._move_capture_delay_spin,
            self._move_capture_delay_unit_label,
            self._move_capture_btn,
        )
        self._move_capture_status = Gtk.Label(label="")
        self._move_capture_status.add_css_class("dim-label")
        self._move_capture_status.add_css_class("caption")
        self._move_capture_status.set_halign(Gtk.Align.START)
        self._move_capture_status.set_xalign(0.0)
        self._move_capture_status.set_wrap(True)
        self._move_capture_status.connect("notify::label", self._on_move_capture_status_changed)

        self._move_capture_widgets = (
            self._move_capture_row,
            self._move_capture_btn,
        )
        self._move_capture_row.set_visible(False)

        self._build_control_editor()
        # Primary inputs share one width so spins and dropdowns line up down the column.
        self._control_width_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        for control in (
            self._press_spin,
            self._duration_spin,
            self._release_spin,
            self._move_x_spin,
            self._move_y_spin,
            self._control_a_spin,
            self._control_b_spin,
            self._control_timeout_spin,
            self._control_exec_mode_dropdown,
            self._control_macro_call_dropdown,
            self._control_macro_loop_dropdown,
            self._control_macro_count_spin,
            self._control_macro_stop_dropdown,
        ):
            self._control_width_group.add_widget(control)

        rows = rows_list(
            self._press_row,
            self._duration_row,
            self._release_row,
            self._move_x_row,
            self._move_y_row,
            *self._control_rows,
            self._move_capture_row,
        )

        panel = group_box(header, rows, self._move_capture_status)
        self._move_x_row.set_visible(False)
        self._move_y_row.set_visible(False)
        self._update_selected_move_capture_controls(None)

        self._revealer.set_child(panel)
        return self._revealer

    def _on_move_capture_status_changed(self, label: Gtk.Label, _pspec) -> None:
        label.set_visible(bool(label.get_label()))

    def _set_key_timing_rows_visible(self, visible: bool) -> None:
        self._duration_row.set_visible(visible)
        self._release_row.set_visible(visible)

    def _set_move_rows_visible(self, visible: bool) -> None:
        self._move_x_row.set_visible(visible)
        self._move_y_row.set_visible(visible)

    def _on_selection_changed(self, selected_obj: object | None) -> None:
        self._edit_child_macro_btn.set_visible(False)
        if hasattr(self, "_selection_summary"):
            if selected_obj is not None and selected_obj is self._timeline._selected:
                if not any(item is selected_obj for item in self._timeline._selection):
                    self._timeline._time_selection = None
                self._timeline._selection = [selected_obj]
            self._update_selection_summary()
        if selected_obj is None:
            self._prop_context_label.set_visible(False)
            self._cancel_capture_selected_move("")
            self._revealer.set_reveal_child(False)
            self._update_selected_move_capture_controls(None)
            return

        self._revealer.set_reveal_child(True)
        self._prop_context_label.set_visible(False)
        if not isinstance(selected_obj, EditableMove) or selected_obj.mode not in {
            "abs",
            "natural",
        }:
            self._cancel_capture_selected_move("")
        if isinstance(selected_obj, EditableControl):
            self._show_control_properties(selected_obj)
            return

        self._set_control_rows_visible(False)
        if isinstance(selected_obj, EditableMove):
            move = selected_obj
            mode_label = "NATURAL" if move.mode == "natural" else move.mode.upper()
            self._prop_title.set_label(f"Mouse Move ({mode_label})")
            detail = f"Move {mode_label} (x={move.x}, y={move.y})"
            if move.mode == "natural":
                stop_suffix = ", stop on failure" if move.stop_on_failure else ""
                detail = (
                    f"{detail} @ {format_natural_move_speed(move.speed)}, "
                    f"{move.curve}, timeout {move.max_duration_ms}ms{stop_suffix}"
                )
            self._key_info_label.set_label(detail)

            self._press_row.set_title("At")
            self._set_key_timing_rows_visible(False)
            self._change_key_btn.set_visible(move.mode == "natural")
            self._change_key_btn.set_label("Modify Move")
            self._move_x_row.set_title("X")
            self._move_x_row.set_visible(True)
            self._move_y_row.set_visible(True)
            coord_min, coord_max = (
                _ABS_MOVE_COORDINATE_RANGE
                if move.mode in {"abs", "natural"}
                else _REL_MOVE_COORDINATE_RANGE
            )
            self._move_x_spin.set_range(coord_min, coord_max)
            self._move_y_spin.set_range(coord_min, coord_max)

            self._updating_props = True
            try:
                self._press_spin.set_value(move.t_us / 1000)
                self._move_x_spin.set_value(move.x)
                self._move_y_spin.set_value(move.y)
            finally:
                self._updating_props = False
            self._update_selected_move_capture_controls(move)
            return

        if isinstance(selected_obj, dict):
            title, detail = _describe_passthrough_event(selected_obj)
            self._prop_title.set_label(title)
            self._key_info_label.set_label(detail)
            self._press_row.set_title("At")
            self._set_key_timing_rows_visible(False)
            self._change_key_btn.set_visible(False)
            self._set_move_rows_visible(False)
            self._set_control_rows_visible(False)
            self._updating_props = True
            try:
                self._press_spin.set_value(int(selected_obj.get("t_us", 0)) / 1000)
            finally:
                self._updating_props = False
            self._update_selected_move_capture_controls(None)
            return

        assert isinstance(selected_obj, EditableEvent)
        ev = selected_obj
        if _is_gamepad_axis_event(ev):
            name = _get_event_name(ev.ev_type, ev.code)
            axis_range = gamepad_axis_range(name.lower())
            title = axis_range.label if axis_range is not None else name
            output_suffix = f" @ {ev.output_id}" if ev.output_id else ""
            self._prop_title.set_label(title)
            self._key_info_label.set_label(
                f"{name} value {ev.value} (code {ev.code}){output_suffix}"
            )
            self._press_row.set_title("At")
            self._set_key_timing_rows_visible(False)
            self._change_key_btn.set_visible(True)
            self._change_key_btn.set_label("Change Axis...")
            self._move_x_row.set_title("Value")
            self._move_x_row.set_visible(True)
            self._move_y_row.set_visible(False)
            if axis_range is None:
                self._move_x_spin.set_range(-32768, 32767)
            else:
                self._move_x_spin.set_range(
                    axis_range.minimum,
                    axis_range.maximum,
                )

            self._updating_props = True
            try:
                self._press_spin.set_value(ev.press_t_us / 1000)
                self._move_x_spin.set_value(ev.value)
            finally:
                self._updating_props = False
            self._update_selected_move_capture_controls(None)
            return

        name = _get_key_name(ev.code)
        self._prop_title.set_label(f"{name} Rapidfire" if ev.rapidfire_enabled else name)
        self._key_info_label.set_label(self._key_detail(ev))
        self._press_row.set_title("Press")
        self._set_key_timing_rows_visible(True)
        self._change_key_btn.set_visible(True)
        self._change_key_btn.set_label(
            "Edit Rapidfire..." if ev.rapidfire_enabled else "Change Key..."
        )
        self._set_move_rows_visible(False)

        self._updating_props = True
        try:
            self._press_spin.set_value(ev.press_t_us / 1000)
            self._duration_spin.set_value(max(1, round((ev.release_t_us - ev.press_t_us) / 1000)))
            self._release_spin.set_value(ev.release_t_us / 1000)
        finally:
            self._updating_props = False
        self._update_selected_move_capture_controls(None)

    def _refresh_after_key_timing_change(self, ev: EditableEvent) -> None:
        self._events.sort(key=lambda e: e.press_t_us)
        self._recompute_duration()
        self._update_stats()
        self._update_canvas_width()
        self._timeline.queue_draw()
        self._on_selection_changed(ev)
        self._sync_close_guard()

    def _refresh_after_passthrough_timing_change(
        self,
        ev: MacroEvent,
    ) -> None:
        self._rel_events.sort(key=lambda e: int(e.get("t_us", 0)))
        self._passthrough_events.sort(key=lambda e: int(e.get("t_us", 0)))
        self._recompute_duration()
        self._update_stats()
        self._update_canvas_width()
        self._timeline.queue_draw()
        self._on_selection_changed(ev)
        self._sync_close_guard()

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
            selected_obj.t_us = max(0, new_t)
            self._synthetic_moves.sort(key=lambda m: m.t_us)
            self._on_selection_changed(selected_obj)
            self._update_stats()
            self._timeline.queue_draw()
            self._sync_close_guard()
            return
        if isinstance(selected_obj, dict):
            selected_obj["t_us"] = max(0, new_t)
            self._refresh_after_passthrough_timing_change(selected_obj)
            return

        assert isinstance(selected_obj, EditableEvent)
        ev = selected_obj
        duration = ev.release_t_us - ev.press_t_us
        ev.press_t_us = new_t
        ev.release_t_us = new_t + duration
        self._refresh_after_key_timing_change(ev)

    def _on_release_changed(self, spin) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if selected_obj is None or isinstance(
            selected_obj,
            (EditableMove, EditableControl, dict),
        ):
            return
        assert isinstance(selected_obj, EditableEvent)
        ev = selected_obj
        if ev.ev_type != evdev.ecodes.EV_KEY:
            return
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
        if selected_obj is None or isinstance(
            selected_obj,
            (EditableMove, EditableControl, dict),
        ):
            return
        assert isinstance(selected_obj, EditableEvent)
        ev = selected_obj
        if ev.ev_type != evdev.ecodes.EV_KEY:
            return
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
        elif isinstance(ev, dict) and ev in self._rel_events:
            self._rel_events.remove(ev)
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

    def _on_change_key_clicked(self, btn) -> None:
        ev = self._timeline._selected
        if isinstance(ev, EditableControl) and ev.mode == "compositor_dispatch":
            self._present_compositor_action_dialog(control=ev)
            return
        if isinstance(ev, EditableControl) and ev.mode in {"macro_sync", "macro_parallel"}:
            self._present_macro_call_dialog(control=ev)
            return
        if isinstance(ev, EditableMove):
            self._present_mouse_move_dialog(move=ev)
            return
        if ev is None or isinstance(ev, (EditableControl, dict)):
            return
        assert isinstance(ev, EditableEvent)

        from keymasq.common.model.actions import MappingAction
        from keymasq.common.model.core import ActionType
        from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog

        if _is_gamepad_axis_event(ev):
            target = _event_target_name(ev)
            current_action = MappingAction(
                action_type=ActionType.GAMEPAD_AXIS,
                target=target,
                axis_value=ev.value,
                output_id=ev.output_id,
            )
            dialog_label = _get_event_name(ev.ev_type, ev.code)
        else:
            key_name_lower = _get_key_name(ev.code).lower()
            dialog_label = _get_key_name(ev.code)
            if ev.device_type == "keyboard":
                current_action = MappingAction(
                    action_type=ActionType.KEYBOARD,
                    target=key_name_lower,
                )
            elif ev.device_type == "mouse":
                current_action = MappingAction(
                    action_type=ActionType.MOUSE,
                    target=key_name_lower,
                )
            else:
                current_action = MappingAction(
                    action_type=ActionType.GAMEPAD,
                    target=key_name_lower,
                    output_id=ev.output_id,
                )

        current_action.rapidfire_enabled = ev.rapidfire_enabled
        current_action.rapidfire_hold_ms = ev.rapidfire_hold_ms
        current_action.rapidfire_wait_ms = ev.rapidfire_wait_ms

        dialog = KeySelectorDialog(
            self._parent,
            dialog_label,
            current_action,
            allow_passthrough=False,
            allow_clear_mapping=False,
            allow_suppress=False,
            allow_superkey=False,
            allow_repeat=False,
            allow_rapidfire=True,
            allow_gamepad_axis_rapidfire=False,
            allow_tap=False,
            allowed_tabs={
                "gamepad"
                if ev.device_type == "gamepad"
                else "mouse"
                if ev.device_type == "mouse"
                else "keyboard",
                *(() if ev.device_type != "keyboard" else ("navigation", "media")),
            },
            initial_tab=(
                "gamepad"
                if ev.device_type == "gamepad"
                else "mouse"
                if ev.device_type == "mouse"
                else "keyboard"
            ),
            include_mpris_controls=False,
            include_mouse_move_controls=False,
            include_mouse_scroll_controls=(False if ev.device_type == "mouse" else True),
        )
        dialog.rapidfire_check.set_tooltip_text(
            "Pulse this input within its macro duration. The gaps adjust so the last release "
            "lands at the configured end."
        )
        dialog.wait_spin.set_tooltip_text(
            "Preferred gap between pulses; adjusted to fit the duration."
        )
        dialog.connect("key-selected", self._on_key_selected_for_edit)
        dialog.present(self._parent)

    def _on_key_selected_for_edit(self, dialog, action) -> None:
        from keymasq.common.model.core import ActionType

        ev = self._timeline._selected
        if (
            ev is None
            or action is None
            or isinstance(
                ev,
                (EditableMove, EditableControl, dict),
            )
        ):
            return
        assert isinstance(ev, EditableEvent)

        target = getattr(action, "target", None)
        if action.action_type == ActionType.KEYBOARD and target:
            code = getattr(evdev.ecodes, target.upper(), None)
            if code is not None:
                ev.code = code
                ev.device_type = "keyboard"
                ev.ev_type = evdev.ecodes.EV_KEY
                ev.value = 0
                ev.output_id = None
        elif action.action_type == ActionType.MOUSE and target:
            code = getattr(evdev.ecodes, target.upper(), None)
            if code is not None:
                ev.code = code
                ev.device_type = "mouse"
                ev.ev_type = evdev.ecodes.EV_KEY
                ev.value = 0
                ev.output_id = None
        elif action.action_type == ActionType.GAMEPAD and target:
            code = getattr(evdev.ecodes, target.upper(), None)
            if code is not None:
                ev.code = code
                ev.device_type = "gamepad"
                ev.ev_type = evdev.ecodes.EV_KEY
                ev.value = 0
                ev.output_id = getattr(action, "output_id", None)
        elif action.action_type == ActionType.GAMEPAD_AXIS and target:
            code = getattr(evdev.ecodes, target.upper(), None)
            if code is not None:
                ev.code = code
                ev.device_type = "gamepad"
                ev.ev_type = evdev.ecodes.EV_ABS
                ev.value = int(getattr(action, "axis_value", 0) or 0)
                ev.release_t_us = ev.press_t_us + 1
                ev.output_id = getattr(action, "output_id", None)
        else:
            return

        if ev.ev_type == evdev.ecodes.EV_KEY and ev.release_t_us <= ev.press_t_us + 1:
            ev.release_t_us = ev.press_t_us + 50000
        ev.apply_rapidfire(action)
        self._on_selection_changed(ev)
        self._timeline.queue_draw()
        self._sync_close_guard()

    def _on_move_x_changed(self, spin) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableMove):
            if not isinstance(
                selected_obj,
                EditableEvent,
            ) or not _is_gamepad_axis_event(selected_obj):
                return
            selected_obj.value = clamp_gamepad_axis_value(
                _event_target_name(selected_obj),
                int(spin.get_value()),
            )
            self._on_selection_changed(selected_obj)
            self._timeline.queue_draw()
            self._sync_close_guard()
            return
        selected_obj.x = int(spin.get_value())
        self._on_selection_changed(selected_obj)
        self._timeline.queue_draw()
        self._sync_close_guard()

    def _on_move_y_changed(self, spin) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableMove):
            return
        selected_obj.y = int(spin.get_value())
        self._on_selection_changed(selected_obj)
        self._timeline.queue_draw()
        self._sync_close_guard()
