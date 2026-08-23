"""Control-event editor widgets and presentation state."""

from dataclasses import dataclass, replace

import gi

# pyright: reportAttributeAccessIssue=false, reportUnknownLambdaType=false

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.widgets.macro_editor.model import (
    EditableControl,
    _describe_compositor_control,
)


@dataclass(frozen=True)
class ControlEditorState:
    """GTK-independent presentation state for one control event."""

    title: str
    detail: str
    mode_label: str
    change_label: str
    show_change: bool
    title_context: str = ""
    show_ab: bool = False
    a_label: str = "A:"
    a_value_ms: float = 0.0
    show_a: bool = False
    b_label: str = "B:"
    b_value_ms: float = 0.0
    show_b: bool = False
    show_command: bool = False
    command: str = ""
    show_exec_mode: bool = False
    exec_mode: str = "exec_sync"
    show_sync: bool = False
    timeout_ms: int = 1
    inhibit_mouse: bool = False
    show_timeout_hint: bool = False
    timeout_hint: str = ""
    show_macro: bool = False
    macro_wait: bool = True
    macro_loop_mode: str = "none"
    macro_loop_count: int = 1
    macro_loop_stop_behavior: str = "finish_run"
    macro_speed: float = 1.0
    macro_replay_mouse_movement: bool = True
    macro_replay_mouse_clicks: bool = True


def timeout_policy_hint(timeout_ms: int, max_timeout_ms: int) -> str:
    """Describe whether a saved timeout will be clamped by runtime policy."""
    max_timeout = max(1, int(max_timeout_ms))
    if timeout_ms > max_timeout:
        return f"Runtime clamp: {timeout_ms}ms -> {max_timeout}ms"
    return f"Policy max timeout: {max_timeout}ms"


def control_editor_state(
    control: EditableControl,
    max_timeout_ms: int,
) -> ControlEditorState:
    """Resolve all control-editor presentation decisions without GTK."""
    is_compositor = control.mode == "compositor_dispatch"
    base = ControlEditorState(
        title="Compositor Action" if is_compositor else "Control",
        detail=(
            _describe_compositor_control(control)
            if is_compositor
            else control.mode.replace("_", " ").title()
        ),
        mode_label=control.mode.replace("_", " ").title(),
        change_label="Change Action..." if is_compositor else "Change Key...",
        show_change=is_compositor,
    )
    if control.mode == "wait":
        return replace(
            base,
            show_ab=True,
            a_label="Duration (ms):",
            a_value_ms=max(0.0, control.duration_us / 1000.0),
            show_a=True,
        )
    if control.mode == "wait_random":
        return replace(
            base,
            show_ab=True,
            a_label="Min (ms):",
            a_value_ms=max(0.0, control.min_us / 1000.0),
            show_a=True,
            b_label="Max (ms):",
            b_value_ms=max(0.0, control.max_us / 1000.0),
            show_b=True,
        )
    if control.mode in {"exec_sync", "exec_parallel", "exec_async"}:
        tracked = control.mode != "exec_async"
        detail = {
            "exec_sync": "Wait for completion",
            "exec_parallel": "Run in parallel",
            "exec_async": "Run detached",
        }[control.mode]
        return replace(
            base,
            title="Run Command",
            detail=detail,
            mode_label="Shell command",
            show_command=True,
            command=control.command,
            show_exec_mode=True,
            exec_mode=control.mode,
            show_sync=tracked,
            timeout_ms=max(1, int(control.timeout_ms)),
            inhibit_mouse=bool(control.inhibit_mouse),
            show_timeout_hint=tracked,
            timeout_hint=timeout_policy_hint(int(control.timeout_ms), max_timeout_ms),
        )
    if control.mode in {"macro_sync", "macro_parallel"}:
        call_label = "Run and wait" if control.mode == "macro_sync" else "Run in parallel"
        loop_label = {
            "none": "once",
            "count": f"{max(1, control.macro_loop_count)} times",
            "hold": "while held",
        }.get(control.macro_loop_mode, "once")
        return replace(
            base,
            title="Macro Call",
            detail=f"{call_label}, {loop_label}",
            title_context=control.macro_name or "<missing macro>",
            mode_label="Saved macro",
            change_label="Change Macro...",
            show_change=True,
            show_macro=True,
            macro_wait=control.mode == "macro_sync",
            macro_loop_mode=control.macro_loop_mode,
            macro_loop_count=max(1, int(control.macro_loop_count)),
            macro_loop_stop_behavior=control.macro_loop_stop_behavior,
            macro_speed=max(0.01, float(control.macro_speed)),
            macro_replay_mouse_movement=bool(control.macro_replay_mouse_movement),
            macro_replay_mouse_clicks=bool(control.macro_replay_mouse_clicks),
        )
    return base


def _set_entry_text_if_needed(entry: Gtk.Entry, text: str) -> None:
    """Avoid redundant Gtk.Entry updates that reset caret/focus state."""
    if entry.get_text() != text:
        entry.set_text(text)


class ControlEditorMixin:
    """Build, present, and edit wait, command, and compositor controls."""

    def _build_control_editor(self, panel: Gtk.Box) -> None:
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

        control_exec_mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        control_exec_mode_row.append(Gtk.Label(label="Run:"))
        self._control_exec_mode_dropdown = Gtk.DropDown.new_from_strings(
            ["Wait for completion", "Run in parallel", "Run detached"]
        )
        self._control_exec_mode_dropdown.connect(
            "notify::selected", self._on_control_exec_mode_changed
        )
        control_exec_mode_row.append(self._control_exec_mode_dropdown)
        control_row.append(control_exec_mode_row)

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

        macro_call_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        macro_call_row.append(Gtk.Label(label="Call:"))
        self._control_macro_call_dropdown = Gtk.DropDown.new_from_strings(
            ["Run and wait", "Run in parallel"]
        )
        self._control_macro_call_dropdown.connect(
            "notify::selected", self._on_control_macro_call_changed
        )
        macro_call_row.append(self._control_macro_call_dropdown)
        macro_call_row.append(Gtk.Label(label="Playback:"))
        self._control_macro_loop_dropdown = Gtk.DropDown.new_from_strings(
            ["Once", "Count", "While held"]
        )
        self._control_macro_loop_dropdown.connect(
            "notify::selected", self._on_control_macro_loop_changed
        )
        macro_call_row.append(self._control_macro_loop_dropdown)
        self._control_macro_count_spin = Gtk.SpinButton()
        self._control_macro_count_spin.set_adjustment(
            Gtk.Adjustment(value=1, lower=1, upper=1000000, step_increment=1)
        )
        self._control_macro_count_spin.set_digits(0)
        self._control_macro_count_spin.set_width_chars(7)
        self._control_macro_count_spin.connect(
            "value-changed", self._on_control_macro_count_changed
        )
        macro_call_row.append(self._control_macro_count_spin)
        control_row.append(macro_call_row)

        macro_options_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        macro_options_row.append(Gtk.Label(label="Speed:"))
        self._control_macro_speed_spin = Gtk.SpinButton()
        self._control_macro_speed_spin.set_adjustment(
            Gtk.Adjustment(value=1.0, lower=0.1, upper=10.0, step_increment=0.1)
        )
        self._control_macro_speed_spin.set_digits(1)
        self._control_macro_speed_spin.connect(
            "value-changed", self._on_control_macro_speed_changed
        )
        macro_options_row.append(self._control_macro_speed_spin)
        self._control_macro_movement_check = Gtk.CheckButton(label="Mouse movement")
        self._control_macro_movement_check.connect(
            "toggled", self._on_control_macro_movement_changed
        )
        macro_options_row.append(self._control_macro_movement_check)
        self._control_macro_clicks_check = Gtk.CheckButton(label="Mouse clicks")
        self._control_macro_clicks_check.connect("toggled", self._on_control_macro_clicks_changed)
        macro_options_row.append(self._control_macro_clicks_check)
        control_row.append(macro_options_row)

        macro_stop_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        macro_stop_row.append(Gtk.Label(label="On release:"))
        self._control_macro_stop_dropdown = Gtk.DropDown.new_from_strings(
            ["Finish current run", "Cancel current run"]
        )
        self._control_macro_stop_dropdown.connect(
            "notify::selected", self._on_control_macro_stop_changed
        )
        macro_stop_row.append(self._control_macro_stop_dropdown)
        control_row.append(macro_stop_row)

        panel.append(control_row)
        self._control_row = control_row
        self._control_ab_row = control_ab_row
        self._control_cmd_row = control_cmd_row
        self._control_exec_mode_row = control_exec_mode_row
        self._control_sync_row = control_sync_row
        self._control_timeout_hint_label.set_visible(False)
        self._control_macro_call_row = macro_call_row
        self._control_macro_options_row = macro_options_row
        self._control_macro_stop_row = macro_stop_row
        macro_call_row.set_visible(False)
        macro_options_row.set_visible(False)
        macro_stop_row.set_visible(False)
        self._control_row.set_visible(False)

    def _show_control_properties(self, control: EditableControl) -> None:
        state = control_editor_state(control, self._macro_exec_timeout_max_ms)
        self._prop_title.set_label(f"{state.title}:" if state.title_context else state.title)
        self._prop_context_label.set_label(state.title_context)
        self._prop_context_label.set_tooltip_text(state.title_context or None)
        self._prop_context_label.set_visible(bool(state.title_context))
        self._key_info_label.set_label(state.detail)
        self._press_label.set_label("At:")
        self._duration_text_label.set_visible(False)
        self._duration_spin.set_visible(False)
        self._duration_unit_label.set_visible(False)
        self._release_label.set_visible(False)
        self._release_spin.set_visible(False)
        self._release_unit_label.set_visible(False)
        self._change_key_btn.set_visible(state.show_change)
        self._change_key_btn.set_label(state.change_label)
        self._move_row.set_visible(False)
        self._control_row.set_visible(True)

        self._updating_props = True
        try:
            self._press_spin.set_value(control.t_us / 1000)
            self._control_mode_label.set_label(state.mode_label)
            self._control_mode_label.set_visible(not state.show_macro)
            self._control_a_label.set_label(state.a_label)
            self._control_a_label.set_visible(state.show_a)
            self._control_a_spin.set_visible(state.show_a)
            self._control_a_spin.set_value(state.a_value_ms)
            self._control_b_label.set_label(state.b_label)
            self._control_b_label.set_visible(state.show_b)
            self._control_b_spin.set_visible(state.show_b)
            self._control_b_spin.set_value(state.b_value_ms)
            self._control_ab_row.set_visible(state.show_ab)
            self._control_cmd_row.set_visible(state.show_command)
            self._control_exec_mode_row.set_visible(state.show_exec_mode)
            self._control_sync_row.set_visible(state.show_sync)
            self._control_timeout_hint_label.set_visible(state.show_timeout_hint)
            self._control_macro_call_row.set_visible(state.show_macro)
            self._control_macro_options_row.set_visible(state.show_macro)
            self._control_macro_stop_row.set_visible(
                state.show_macro and state.macro_loop_mode == "hold"
            )
            if state.show_command:
                _set_entry_text_if_needed(self._control_cmd_entry, state.command)
            if state.show_exec_mode:
                self._control_exec_mode_dropdown.set_selected(
                    {"exec_sync": 0, "exec_parallel": 1, "exec_async": 2}.get(
                        state.exec_mode, 0
                    )
                )
            if state.show_sync:
                self._control_timeout_spin.set_value(state.timeout_ms)
                self._control_inhibit_check.set_active(state.inhibit_mouse)
            if state.show_timeout_hint:
                self._control_timeout_hint_label.set_label(state.timeout_hint)
            if state.show_macro:
                self._control_macro_call_dropdown.set_selected(0 if state.macro_wait else 1)
                self._control_macro_loop_dropdown.set_selected(
                    {"none": 0, "count": 1, "hold": 2}.get(state.macro_loop_mode, 0)
                )
                self._control_macro_count_spin.set_visible(state.macro_loop_mode == "count")
                self._control_macro_count_spin.set_value(state.macro_loop_count)
                self._control_macro_stop_dropdown.set_selected(
                    1 if state.macro_loop_stop_behavior == "cancel_run" else 0
                )
                self._control_macro_speed_spin.set_value(state.macro_speed)
                self._control_macro_movement_check.set_active(state.macro_replay_mouse_movement)
                self._control_macro_clicks_check.set_active(state.macro_replay_mouse_clicks)
        finally:
            self._updating_props = False
        self._update_selected_move_capture_controls(None)

    def _refresh_after_control_change(self, control: EditableControl) -> None:
        self._control_events.sort(key=lambda c: c.t_us)
        self._recompute_duration()
        self._update_stats()
        self._update_canvas_width()
        self._timeline.queue_draw()
        self._on_selection_changed(control)
        self._sync_close_guard()

    def _update_timeout_clamp_hint(self, timeout_ms: int) -> None:
        self._control_timeout_hint_label.set_label(
            timeout_policy_hint(timeout_ms, self._macro_exec_timeout_max_ms)
        )

    def _on_control_a_changed(self, spin: Gtk.SpinButton) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableControl):
            return
        if selected_obj.mode == "wait":
            selected_obj.duration_us = max(0, int(spin.get_value() * 1000))
        elif selected_obj.mode == "wait_random":
            selected_obj.min_us = max(0, int(spin.get_value() * 1000))
            if selected_obj.max_us < selected_obj.min_us:
                selected_obj.max_us = selected_obj.min_us
        self._refresh_after_control_change(selected_obj)

    def _on_control_b_changed(self, spin: Gtk.SpinButton) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableControl):
            return
        if selected_obj.mode == "wait_random":
            selected_obj.max_us = max(0, int(spin.get_value() * 1000))
            if selected_obj.max_us < selected_obj.min_us:
                selected_obj.max_us = selected_obj.min_us
            self._refresh_after_control_change(selected_obj)

    def _on_control_command_changed(self, entry: Gtk.Entry) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableControl):
            return
        if selected_obj.mode in {"exec_sync", "exec_parallel", "exec_async"}:
            # Rebuilding the panel here would drop focus from the command entry.
            selected_obj.command = entry.get_text()
            self._sync_close_guard()

    def _on_control_exec_mode_changed(self, dropdown: Gtk.DropDown, _param) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableControl) or selected_obj.mode not in {
            "exec_sync",
            "exec_parallel",
            "exec_async",
        }:
            return
        selected_obj.mode = {0: "exec_sync", 1: "exec_parallel", 2: "exec_async"}.get(
            dropdown.get_selected(), "exec_sync"
        )
        self._refresh_after_control_change(selected_obj)

    def _on_control_timeout_changed(self, spin: Gtk.SpinButton) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableControl):
            return
        if selected_obj.mode in {"exec_sync", "exec_parallel"}:
            selected_obj.timeout_ms = max(1, int(spin.get_value()))
            self._update_timeout_clamp_hint(selected_obj.timeout_ms)
            self._refresh_after_control_change(selected_obj)

    def _on_control_inhibit_toggled(self, check: Gtk.CheckButton) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableControl):
            return
        if selected_obj.mode in {"exec_sync", "exec_parallel"}:
            selected_obj.inhibit_mouse = bool(check.get_active())
            self._refresh_after_control_change(selected_obj)

    def _selected_macro_control(self) -> EditableControl | None:
        selected = self._timeline._selected
        if isinstance(selected, EditableControl) and selected.mode in {
            "macro_sync",
            "macro_parallel",
        }:
            return selected
        return None

    def _on_control_macro_call_changed(self, dropdown: Gtk.DropDown, _param) -> None:
        if self._updating_props or (control := self._selected_macro_control()) is None:
            return
        control.mode = "macro_sync" if dropdown.get_selected() == 0 else "macro_parallel"
        self._refresh_after_control_change(control)

    def _on_control_macro_loop_changed(self, dropdown: Gtk.DropDown, _param) -> None:
        if self._updating_props or (control := self._selected_macro_control()) is None:
            return
        control.macro_loop_mode = {0: "none", 1: "count", 2: "hold"}.get(
            dropdown.get_selected(), "none"
        )
        self._refresh_after_control_change(control)

    def _on_control_macro_count_changed(self, spin: Gtk.SpinButton) -> None:
        if self._updating_props or (control := self._selected_macro_control()) is None:
            return
        control.macro_loop_count = max(1, spin.get_value_as_int())
        self._refresh_after_control_change(control)

    def _on_control_macro_stop_changed(self, dropdown: Gtk.DropDown, _param) -> None:
        if self._updating_props or (control := self._selected_macro_control()) is None:
            return
        control.macro_loop_stop_behavior = (
            "cancel_run" if dropdown.get_selected() == 1 else "finish_run"
        )
        self._refresh_after_control_change(control)

    def _on_control_macro_speed_changed(self, spin: Gtk.SpinButton) -> None:
        if self._updating_props or (control := self._selected_macro_control()) is None:
            return
        control.macro_speed = max(0.01, spin.get_value())
        self._refresh_after_control_change(control)

    def _on_control_macro_movement_changed(self, check: Gtk.CheckButton) -> None:
        if self._updating_props or (control := self._selected_macro_control()) is None:
            return
        control.macro_replay_mouse_movement = bool(check.get_active())
        self._refresh_after_control_change(control)

    def _on_control_macro_clicks_changed(self, check: Gtk.CheckButton) -> None:
        if self._updating_props or (control := self._selected_macro_control()) is None:
            return
        control.macro_replay_mouse_clicks = bool(check.get_active())
        self._refresh_after_control_change(control)
