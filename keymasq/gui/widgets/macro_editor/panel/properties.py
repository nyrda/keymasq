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
from keymasq.gui.widgets.macro_editor.model import (
    EditableControl,
    EditableEvent,
    EditableMove,
    MacroEvent,
    _describe_passthrough_event,
    _get_event_name,
    _get_key_name,
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

    def _build_property_panel(self) -> Gtk.Widget:
        self._revealer = Gtk.Revealer()
        self._revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._revealer.set_reveal_child(False)

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        panel.set_margin_top(8)
        panel.set_margin_bottom(4)
        panel.set_margin_start(8)
        panel.set_margin_end(8)

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._prop_title = Gtk.Label()
        self._prop_title.add_css_class("heading")
        self._prop_title.set_halign(Gtk.Align.START)
        title_row.append(self._prop_title)
        self._prop_context_label = Gtk.Label()
        self._prop_context_label.add_css_class("heading")
        self._prop_context_label.add_css_class("dim-label")
        self._prop_context_label.set_halign(Gtk.Align.START)
        self._prop_context_label.set_hexpand(True)
        self._prop_context_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._prop_context_label.set_visible(False)
        title_row.append(self._prop_context_label)
        panel.append(title_row)

        timing_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        timing_row.set_halign(Gtk.Align.START)

        self._press_label = Gtk.Label(label="Press:")
        timing_row.append(self._press_label)
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
        timing_row.append(self._press_spin)
        self._press_unit_label = Gtk.Label(label="ms")
        timing_row.append(self._press_unit_label)

        self._duration_text_label = Gtk.Label(label="Duration:")
        timing_row.append(self._duration_text_label)
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
        self._duration_spin.set_width_chars(7)
        self._duration_spin.connect("value-changed", self._on_duration_changed)
        timing_row.append(self._duration_spin)
        self._duration_unit_label = Gtk.Label(label="ms")
        timing_row.append(self._duration_unit_label)

        self._release_label = Gtk.Label(label="  Release:")
        timing_row.append(self._release_label)
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
        timing_row.append(self._release_spin)
        self._release_unit_label = Gtk.Label(label="ms")
        timing_row.append(self._release_unit_label)
        panel.append(timing_row)

        move_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        move_row.set_halign(Gtk.Align.START)
        self._move_mode_label = Gtk.Label(label="Mode: REL")
        move_row.append(self._move_mode_label)

        self._move_capture_prefix_label = Gtk.Label(label="")
        move_row.append(self._move_capture_prefix_label)

        self._move_capture_delay_label = Gtk.Label(label="Capture in:")
        self._move_capture_delay_label.add_css_class("dim-label")
        move_row.append(self._move_capture_delay_label)

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
        move_row.append(self._move_capture_delay_spin)

        self._move_capture_delay_unit_label = Gtk.Label(label="s")
        move_row.append(self._move_capture_delay_unit_label)

        self._move_capture_btn = Gtk.Button(label="Capture")
        self._move_capture_btn.connect("clicked", self._on_capture_selected_move_clicked)
        move_row.append(self._move_capture_btn)

        self._move_capture_colon_label = Gtk.Label(label="")
        move_row.append(self._move_capture_colon_label)

        self._move_x_label = Gtk.Label(label="X:")
        move_row.append(self._move_x_label)
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
        self._move_x_spin.set_width_chars(7)
        self._move_x_spin.connect("value-changed", self._on_move_x_changed)
        move_row.append(self._move_x_spin)
        self._move_y_label = Gtk.Label(label="Y:")
        move_row.append(self._move_y_label)
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
        self._move_y_spin.set_width_chars(7)
        self._move_y_spin.connect("value-changed", self._on_move_y_changed)
        move_row.append(self._move_y_spin)

        self._move_capture_status = Gtk.Label(label="")
        self._move_capture_status.add_css_class("dim-label")
        self._move_capture_status.set_halign(Gtk.Align.START)
        self._move_capture_status.set_hexpand(True)
        move_row.append(self._move_capture_status)

        panel.append(move_row)
        self._move_row = move_row
        self._move_capture_widgets = (
            self._move_capture_prefix_label,
            self._move_capture_delay_label,
            self._move_capture_delay_spin,
            self._move_capture_delay_unit_label,
            self._move_capture_btn,
            self._move_capture_colon_label,
            self._move_capture_status,
        )
        self._move_row.set_visible(False)
        self._update_selected_move_capture_controls(None)

        self._build_control_editor(panel)

        action_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
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

    def _on_selection_changed(self, selected_obj: object | None) -> None:
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

        self._control_row.set_visible(False)
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

            self._press_label.set_label("At:")
            self._duration_text_label.set_visible(False)
            self._duration_spin.set_visible(False)
            self._duration_unit_label.set_visible(False)
            self._release_label.set_visible(False)
            self._release_spin.set_visible(False)
            self._release_unit_label.set_visible(False)
            self._change_key_btn.set_visible(move.mode == "natural")
            self._change_key_btn.set_label("Modify Move")
            self._move_row.set_visible(True)
            self._move_mode_label.set_label(f"Mode: {mode_label}")
            self._move_x_label.set_label("X:")
            self._move_y_label.set_label("Y:")
            self._move_y_label.set_visible(True)
            self._move_y_spin.set_visible(True)
            self._move_y_spin.set_sensitive(True)
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
            self._press_label.set_label("At:")
            self._duration_text_label.set_visible(False)
            self._duration_spin.set_visible(False)
            self._duration_unit_label.set_visible(False)
            self._release_label.set_visible(False)
            self._release_spin.set_visible(False)
            self._release_unit_label.set_visible(False)
            self._change_key_btn.set_visible(False)
            self._move_row.set_visible(False)
            self._control_row.set_visible(False)
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
            self._press_label.set_label("At:")
            self._duration_text_label.set_visible(False)
            self._duration_spin.set_visible(False)
            self._duration_unit_label.set_visible(False)
            self._release_label.set_visible(False)
            self._release_spin.set_visible(False)
            self._release_unit_label.set_visible(False)
            self._change_key_btn.set_visible(True)
            self._change_key_btn.set_label("Change Axis...")
            self._move_row.set_visible(True)
            self._move_mode_label.set_label("Gamepad Axis:")
            self._move_x_label.set_label("Value:")
            self._move_y_label.set_visible(False)
            self._move_y_spin.set_visible(False)
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
        self._prop_title.set_label(name)
        output_suffix = f" @ {ev.output_id}" if ev.device_type == "gamepad" and ev.output_id else ""
        self._key_info_label.set_label(f"{name} (code {ev.code}){output_suffix}")
        self._press_label.set_label("Press:")
        self._duration_text_label.set_visible(True)
        self._duration_spin.set_visible(True)
        self._duration_unit_label.set_visible(True)
        self._release_label.set_visible(True)
        self._release_spin.set_visible(True)
        self._release_unit_label.set_visible(True)
        self._change_key_btn.set_visible(True)
        self._change_key_btn.set_label("Change Key...")
        self._move_row.set_visible(False)

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

    def _delete_events_bulk(self, evs: list[object]) -> None:
        pending_ids = {id(ev) for ev in evs}
        if not pending_ids:
            return
        kept_events = [e for e in self._events if id(e) not in pending_ids]
        kept_rel = [e for e in self._rel_events if id(e) not in pending_ids]
        kept_passthrough = [e for e in self._passthrough_events if id(e) not in pending_ids]
        kept_moves = [move for move in self._synthetic_moves if id(move) not in pending_ids]
        kept_controls = [
            control for control in self._control_events if id(control) not in pending_ids
        ]
        deleted = (
            len(kept_events) != len(self._events)
            or len(kept_rel) != len(self._rel_events)
            or len(kept_passthrough) != len(self._passthrough_events)
            or len(kept_moves) != len(self._synthetic_moves)
            or len(kept_controls) != len(self._control_events)
        )
        if not deleted:
            return
        self._events = kept_events
        self._rel_events = kept_rel
        self._passthrough_events = kept_passthrough
        self._synthetic_moves = kept_moves
        self._control_events = kept_controls
        self._clear_selection_if_removed()
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

        dialog = KeySelectorDialog(
            self._parent,
            dialog_label,
            current_action,
            allow_passthrough=False,
            allow_clear_mapping=False,
            allow_suppress=False,
            allow_superkey=False,
            allow_repeat=False,
            allow_rapidfire=False,
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
