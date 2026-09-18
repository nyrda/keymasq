"""Control-event editor widgets and presentation state."""

from dataclasses import dataclass, replace

import gi

# pyright: reportAttributeAccessIssue=false, reportUnknownLambdaType=false

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.widgets.compositor_actions.compositors import COMPOSITOR_ACTION_DEFINITIONS
from keymasq.gui.widgets.compositor_actions.core import (
    CompositorActionDefinition,
    CompositorActionPreset,
)
from keymasq.gui.widgets.macro_editor.model import (
    EditableControl,
    _control_to_compositor_action,
    _describe_compositor_control,
)
from keymasq.gui.widgets.macro_editor.panel.pause_timeout import PauseTimeoutControl
from keymasq.gui.widgets.macro_editor.panel.rows import check_row, field_row, unit_label

_CONTROL_TITLES = {
    "wait": "Wait",
    "wait_random": "Random Wait",
}
_CONTROL_DETAILS = {
    "wait": "Fixed pause before the next action",
    "wait_random": "Pause for a random duration between min and max",
}


def _release_options(mode: str) -> tuple[tuple[str, str], ...]:
    if mode == "hold":
        return (
            ("finish_run", "Finish current run"),
            ("cancel_run", "Cancel current run"),
            ("pause_run", "Pause and resume"),
        )
    return (("finish_run", "Continue playback"), ("pause_run", "Pause and resume"))


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
    macro_pause_timeout_s: float = 0.0
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
        title="Compositor Action"
        if is_compositor
        else _CONTROL_TITLES.get(control.mode, "Control"),
        detail=(
            _describe_compositor_control(control)
            if is_compositor
            else _CONTROL_DETAILS.get(control.mode, control.mode.replace("_", " ").title())
        ),
        mode_label=control.mode.replace("_", " ").title(),
        change_label="Change Key...",
        show_change=False,
    )
    if control.mode == "wait":
        return replace(
            base,
            show_ab=True,
            a_label="Duration",
            a_value_ms=max(0.0, control.duration_us / 1000.0),
            show_a=True,
        )
    if control.mode == "wait_random":
        return replace(
            base,
            show_ab=True,
            a_label="Min",
            a_value_ms=max(0.0, control.min_us / 1000.0),
            show_a=True,
            b_label="Max",
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
            macro_pause_timeout_s=control.macro_pause_timeout_s,
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

    def _build_control_editor(self) -> None:
        self._control_a_spin = Gtk.SpinButton()
        self._control_a_spin.set_adjustment(
            Gtk.Adjustment(value=0, lower=0, upper=600000, step_increment=1)
        )
        self._control_a_spin.set_digits(0)
        self._control_a_spin.set_width_chars(7)
        self._control_a_spin.connect("value-changed", self._on_control_a_changed)
        self._control_a_row = field_row("Duration", self._control_a_spin, unit_label("ms"))

        self._control_b_spin = Gtk.SpinButton()
        self._control_b_spin.set_adjustment(
            Gtk.Adjustment(value=0, lower=0, upper=600000, step_increment=1)
        )
        self._control_b_spin.set_digits(0)
        self._control_b_spin.set_width_chars(7)
        self._control_b_spin.connect("value-changed", self._on_control_b_changed)
        self._control_b_row = field_row("Max", self._control_b_spin, unit_label("ms"))

        self._control_cmd_entry = Gtk.Entry()
        self._control_cmd_entry.set_hexpand(True)
        self._control_cmd_entry.connect("changed", self._on_control_command_changed)
        self._control_cmd_row = field_row("Command", self._control_cmd_entry)

        self._control_exec_mode_dropdown = Gtk.DropDown.new_from_strings(
            ["Wait for completion", "Run in parallel", "Run detached"]
        )
        self._control_exec_mode_dropdown.connect(
            "notify::selected", self._on_control_exec_mode_changed
        )
        self._control_exec_mode_row = field_row("Run", self._control_exec_mode_dropdown)

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
        self._control_sync_row = field_row("Timeout", self._control_timeout_spin, unit_label("ms"))
        self._control_inhibit_check = Gtk.CheckButton()
        self._control_inhibit_check.connect("toggled", self._on_control_inhibit_toggled)
        self._control_inhibit_row = check_row(
            "Inhibit mouse while waiting", self._control_inhibit_check
        )

        self._control_macro_call_dropdown = Gtk.DropDown.new_from_strings(
            ["Run and wait", "Run in parallel"]
        )
        self._control_macro_call_dropdown.connect(
            "notify::selected", self._on_control_macro_call_changed
        )
        self._control_macro_call_row = field_row("Call", self._control_macro_call_dropdown)

        self._control_macro_loop_dropdown = Gtk.DropDown.new_from_strings(
            ["Once", "Count", "While held"]
        )
        self._control_macro_loop_dropdown.connect(
            "notify::selected", self._on_control_macro_loop_changed
        )
        self._control_macro_loop_row = field_row("Playback", self._control_macro_loop_dropdown)

        self._control_macro_count_spin = Gtk.SpinButton()
        self._control_macro_count_spin.set_adjustment(
            Gtk.Adjustment(value=1, lower=1, upper=1000000, step_increment=1)
        )
        self._control_macro_count_spin.set_digits(0)
        self._control_macro_count_spin.set_width_chars(7)
        self._control_macro_count_spin.connect(
            "value-changed", self._on_control_macro_count_changed
        )
        self._control_macro_count_row = field_row("Count", self._control_macro_count_spin)

        self._control_macro_speed_spin = Gtk.SpinButton()
        self._control_macro_speed_spin.set_adjustment(
            Gtk.Adjustment(value=1.0, lower=0.1, upper=10.0, step_increment=0.1)
        )
        self._control_macro_speed_spin.set_digits(1)
        self._control_macro_speed_spin.connect(
            "value-changed", self._on_control_macro_speed_changed
        )
        self._control_macro_speed_row = field_row("Speed", self._control_macro_speed_spin)

        self._control_macro_movement_check = Gtk.CheckButton()
        self._control_macro_movement_check.connect(
            "toggled", self._on_control_macro_movement_changed
        )
        self._control_macro_movement_row = check_row(
            "Replay mouse movement", self._control_macro_movement_check
        )
        self._control_macro_clicks_check = Gtk.CheckButton()
        self._control_macro_clicks_check.connect("toggled", self._on_control_macro_clicks_changed)
        self._control_macro_clicks_row = check_row(
            "Replay mouse clicks", self._control_macro_clicks_check
        )

        self._control_macro_stop_dropdown = Gtk.DropDown.new_from_strings(
            ["Finish current run", "Cancel current run", "Pause and resume"]
        )
        self._control_macro_stop_dropdown.connect(
            "notify::selected", self._on_control_macro_stop_changed
        )
        self._control_macro_stop_row = field_row("On release", self._control_macro_stop_dropdown)
        self._control_macro_pause_timeout = PauseTimeoutControl(
            self._on_control_macro_pause_timeout_changed
        )

        # Compositor actions edit inline: preset, then the raw dispatcher fields.
        self._control_compositor_definition: CompositorActionDefinition | None = None
        # The action whose preset was explicitly set to Custom; rebuilds keep that choice.
        self._control_compositor_custom_for: EditableControl | None = None
        self._control_compositor_preset_dropdown = Gtk.DropDown.new_from_strings([])
        self._control_compositor_preset_dropdown.connect(
            "notify::selected", self._on_control_compositor_preset_changed
        )
        self._control_compositor_preset_row = field_row(
            "Preset", self._control_compositor_preset_dropdown
        )
        self._control_compositor_dispatcher_entry = Gtk.Entry()
        self._control_compositor_dispatcher_entry.set_hexpand(True)
        self._control_compositor_dispatcher_entry.connect(
            "changed", self._on_control_compositor_field_changed
        )
        self._control_compositor_dispatcher_row = field_row(
            "Dispatcher", self._control_compositor_dispatcher_entry
        )
        self._control_compositor_args_entry = Gtk.Entry()
        self._control_compositor_args_entry.set_hexpand(True)
        self._control_compositor_args_entry.connect(
            "changed", self._on_control_compositor_field_changed
        )
        self._control_compositor_args_row = field_row(
            "Arguments", self._control_compositor_args_entry
        )

        self._control_rows: tuple[Gtk.Widget, ...] = (
            self._control_a_row,
            self._control_b_row,
            self._control_cmd_row,
            self._control_exec_mode_row,
            self._control_sync_row,
            self._control_inhibit_row,
            self._control_macro_call_row,
            self._control_macro_loop_row,
            self._control_macro_count_row,
            self._control_macro_speed_row,
            self._control_macro_movement_row,
            self._control_macro_clicks_row,
            self._control_macro_stop_row,
            self._control_macro_pause_timeout,
            self._control_compositor_preset_row,
            self._control_compositor_dispatcher_row,
            self._control_compositor_args_row,
        )
        self._set_control_rows_visible(False)

    def _set_control_rows_visible(self, visible: bool) -> None:
        for row in self._control_rows:
            row.set_visible(visible)

    def _show_control_properties(self, control: EditableControl) -> None:
        state = control_editor_state(control, self._macro_exec_timeout_max_ms)
        self._prop_title.set_label(f"{state.title}:" if state.title_context else state.title)
        self._prop_context_label.set_label(state.title_context)
        self._prop_context_label.set_tooltip_text(state.title_context or None)
        self._prop_context_label.set_visible(bool(state.title_context))
        self._edit_child_macro_btn.set_visible(state.show_macro)
        self._edit_child_macro_btn.set_sensitive(bool(control.macro_name))
        self._key_info_label.set_label(state.detail)
        self._press_row.set_title("At")
        self._set_key_timing_rows_visible(False)
        self._change_key_btn.set_visible(state.show_change)
        self._change_key_btn.set_label(state.change_label)
        self._set_move_rows_visible(False)

        self._updating_props = True
        try:
            self._press_spin.set_value(control.t_us / 1000)
            self._control_a_row.set_title(state.a_label)
            self._control_a_row.set_visible(state.show_ab and state.show_a)
            self._control_a_spin.set_value(state.a_value_ms)
            self._control_b_row.set_title(state.b_label)
            self._control_b_row.set_visible(state.show_ab and state.show_b)
            self._control_b_spin.set_value(state.b_value_ms)
            self._control_cmd_row.set_visible(state.show_command)
            self._control_exec_mode_row.set_visible(state.show_exec_mode)
            self._control_sync_row.set_visible(state.show_sync)
            self._control_inhibit_row.set_visible(state.show_sync)
            self._control_sync_row.set_subtitle(
                state.timeout_hint if state.show_timeout_hint else ""
            )
            self._control_macro_call_row.set_visible(state.show_macro)
            self._control_macro_loop_row.set_visible(state.show_macro)
            self._control_macro_count_row.set_visible(
                state.show_macro and state.macro_loop_mode == "count"
            )
            self._control_macro_speed_row.set_visible(state.show_macro)
            self._control_macro_movement_row.set_visible(state.show_macro)
            self._control_macro_clicks_row.set_visible(state.show_macro)
            self._control_macro_stop_row.set_visible(state.show_macro)
            self._control_macro_pause_timeout.set_visible(
                state.show_macro and state.macro_loop_stop_behavior == "pause_run"
            )
            self._control_macro_pause_timeout.set_timeout(state.macro_pause_timeout_s)
            self._show_compositor_rows(control if control.mode == "compositor_dispatch" else None)
            if state.show_command:
                _set_entry_text_if_needed(self._control_cmd_entry, state.command)
            if state.show_exec_mode:
                self._control_exec_mode_dropdown.set_selected(
                    {"exec_sync": 0, "exec_parallel": 1, "exec_async": 2}.get(state.exec_mode, 0)
                )
            if state.show_sync:
                self._control_timeout_spin.set_value(state.timeout_ms)
                self._control_inhibit_check.set_active(state.inhibit_mouse)
            if state.show_macro:
                self._control_macro_call_dropdown.set_selected(0 if state.macro_wait else 1)
                self._control_macro_loop_dropdown.set_selected(
                    {"none": 0, "count": 1, "hold": 2}.get(state.macro_loop_mode, 0)
                )
                self._control_macro_count_spin.set_value(state.macro_loop_count)
                release_options = _release_options(state.macro_loop_mode)
                release_labels = [label for _, label in release_options]
                release_model = self._control_macro_stop_dropdown.get_model()
                if (
                    not isinstance(release_model, Gtk.StringList)
                    or [release_model.get_string(i) for i in range(release_model.get_n_items())]
                    != release_labels
                ):
                    self._control_macro_stop_dropdown.set_model(Gtk.StringList.new(release_labels))
                self._control_macro_stop_dropdown.set_selected(
                    next(
                        (
                            i
                            for i, (value, _) in enumerate(release_options)
                            if value == state.macro_loop_stop_behavior
                        ),
                        0,
                    )
                )
                self._control_macro_speed_spin.set_value(state.macro_speed)
                self._control_macro_movement_check.set_active(state.macro_replay_mouse_movement)
                self._control_macro_clicks_check.set_active(state.macro_replay_mouse_clicks)
        finally:
            self._updating_props = False
        self._update_selected_move_capture_controls(None)

    # -- compositor actions -------------------------------------------------

    def _compositor_definition_for(
        self, control: EditableControl
    ) -> CompositorActionDefinition | None:
        action = _control_to_compositor_action(control)
        compositor_id = str(control.compositor_id or "").strip()
        for definition in COMPOSITOR_ACTION_DEFINITIONS:
            if compositor_id and definition.compositor_id == compositor_id:
                return definition
        status = self._resolve_compositor_action_status()
        if not status.get("compositor_dispatch_available"):
            status = self._resolve_compositor_action_status(self._compositor_action_status)
        for definition in COMPOSITOR_ACTION_DEFINITIONS:
            if definition.is_available(action, dict(status)):
                return definition
        return None

    def _compositor_selected_preset(self) -> CompositorActionPreset | None:
        definition = self._control_compositor_definition
        if definition is None:
            return None
        index = int(self._control_compositor_preset_dropdown.get_selected())
        if definition.allow_custom:
            index -= 1
        if index < 0 or index >= len(definition.presets):
            return None
        return definition.presets[index]

    def _show_compositor_rows(self, control: EditableControl | None) -> None:
        """Fill the compositor rows for ``control``; hide them when it is ``None``."""
        preset_row = self._control_compositor_preset_row
        dispatcher_row = self._control_compositor_dispatcher_row
        args_row = self._control_compositor_args_row
        if control is None:
            self._control_compositor_definition = None
            self._control_compositor_custom_for = None
            for row in (preset_row, dispatcher_row, args_row):
                row.set_visible(False)
            return
        if self._control_compositor_custom_for is not control:
            self._control_compositor_custom_for = None

        definition = self._compositor_definition_for(control)
        self._control_compositor_definition = definition
        dispatcher = str(control.compositor_dispatcher or "")
        args = str(control.compositor_args or "")
        _set_entry_text_if_needed(self._control_compositor_dispatcher_entry, dispatcher)
        _set_entry_text_if_needed(self._control_compositor_args_entry, args)
        self._control_compositor_dispatcher_entry.set_placeholder_text(
            definition.dispatcher_placeholder if definition is not None else "dispatcher"
        )
        self._control_compositor_args_entry.set_placeholder_text(
            definition.args_placeholder if definition is not None else ""
        )
        if definition is None:
            # Unknown compositor: expose the raw fields so the action stays editable.
            preset_row.set_visible(False)
            for entry in (
                self._control_compositor_dispatcher_entry,
                self._control_compositor_args_entry,
            ):
                entry.set_editable(True)
            dispatcher_row.set_visible(True)
            dispatcher_row.set_subtitle("")
            args_row.set_visible(True)
            return

        labels = (["Custom"] if definition.allow_custom else []) + [
            preset.label for preset in definition.presets
        ]
        model = self._control_compositor_preset_dropdown.get_model()
        if (
            not isinstance(model, Gtk.StringList)
            or [model.get_string(i) for i in range(model.get_n_items())] != labels
        ):
            self._control_compositor_preset_dropdown.set_model(Gtk.StringList.new(labels))
        selected = 0
        keep_custom = definition.allow_custom and self._control_compositor_custom_for is control
        for index, preset in enumerate(definition.presets):
            if keep_custom or preset.dispatcher != dispatcher:
                continue
            if preset.args != args and not preset.captures_position:
                continue
            selected = index + 1 if definition.allow_custom else index
            break
        self._control_compositor_preset_dropdown.set_selected(selected)
        for entry in (
            self._control_compositor_dispatcher_entry,
            self._control_compositor_args_entry,
        ):
            entry.set_editable(definition.allow_custom)
        preset_row.set_visible(True)
        self._update_compositor_row_state()

    def _update_compositor_row_state(self) -> None:
        definition = self._control_compositor_definition
        if definition is None:
            return
        preset = self._compositor_selected_preset()
        show_raw = definition.show_fields_for_presets or preset is None
        self._control_compositor_dispatcher_row.set_visible(show_raw)
        self._control_compositor_args_row.set_visible(
            (definition.args_visible and show_raw)
            or (preset is not None and preset.captures_position)
        )
        dispatcher = self._control_compositor_dispatcher_entry.get_text().strip()
        args = self._control_compositor_args_entry.get_text().strip()
        if preset is not None:
            hint = preset.hint
        elif not dispatcher:
            hint = "Choose a preset or enter a dispatcher manually."
        elif not definition.args_visible:
            hint = f"Dispatch this Lua expression through {definition.title}."
        else:
            hint = (
                f"Dispatch '{dispatcher}{' ' + args if args else ''}' through {definition.title}."
            )
        self._control_compositor_preset_row.set_subtitle(hint)

    def _on_control_compositor_preset_changed(self, _dropdown: Gtk.DropDown, _param) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if (
            not isinstance(selected_obj, EditableControl)
            or selected_obj.mode != "compositor_dispatch"
        ):
            return
        preset = self._compositor_selected_preset()
        if preset is None:
            # Custom keeps the current fields and simply exposes them for editing.
            self._control_compositor_custom_for = selected_obj
            self._update_compositor_row_state()
            self._control_compositor_dispatcher_entry.grab_focus()
            return
        self._control_compositor_custom_for = None
        selected_obj.compositor_dispatcher = preset.dispatcher
        selected_obj.compositor_args = preset.args
        self._refresh_after_control_change(selected_obj)

    def _on_control_compositor_field_changed(self, _entry: Gtk.Entry) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if (
            not isinstance(selected_obj, EditableControl)
            or selected_obj.mode != "compositor_dispatch"
        ):
            return
        # Like the command entry, avoid rebuilding the panel so typing keeps focus.
        selected_obj.compositor_dispatcher = (
            self._control_compositor_dispatcher_entry.get_text().strip()
        )
        selected_obj.compositor_args = self._control_compositor_args_entry.get_text().strip()
        self._key_info_label.set_label(_describe_compositor_control(selected_obj))
        self._update_compositor_row_state()
        self._timeline.queue_draw()
        self._sync_close_guard()

    def _refresh_after_control_change(self, control: EditableControl) -> None:
        self._control_events.sort(key=lambda c: c.t_us)
        self._recompute_duration()
        self._update_stats()
        self._update_canvas_width()
        self._timeline.queue_draw()
        self._on_selection_changed(control)
        self._sync_close_guard()

    def _on_edit_child_macro(self, _button: Gtk.Button) -> None:
        from keymasq.gui.widgets.macro_editor.dialog import get_macro_editor

        control = self._timeline._selected
        if (
            not isinstance(control, EditableControl)
            or control.mode not in {"macro_sync", "macro_parallel"}
            or not control.macro_name
        ):
            return
        parent = self.get_root()
        if isinstance(parent, Gtk.Window):
            editor = get_macro_editor(parent, control.macro_name, standalone=True)
            editor.present(parent)

    def _update_timeout_clamp_hint(self, timeout_ms: int) -> None:
        self._control_sync_row.set_subtitle(
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
        supported = {value for value, _label in _release_options(control.macro_loop_mode)}
        if control.macro_loop_stop_behavior not in supported:
            control.macro_loop_stop_behavior = "finish_run"
        self._refresh_after_control_change(control)

    def _on_control_macro_count_changed(self, spin: Gtk.SpinButton) -> None:
        if self._updating_props or (control := self._selected_macro_control()) is None:
            return
        control.macro_loop_count = max(1, spin.get_value_as_int())
        self._refresh_after_control_change(control)

    def _on_control_macro_stop_changed(self, dropdown: Gtk.DropDown, _param) -> None:
        if self._updating_props or (control := self._selected_macro_control()) is None:
            return
        options = _release_options(control.macro_loop_mode)
        selected = dropdown.get_selected()
        previous = control.macro_loop_stop_behavior
        control.macro_loop_stop_behavior = (
            options[selected][0] if selected < len(options) else "finish_run"
        )
        if (
            previous != "pause_run"
            and control.macro_loop_stop_behavior == "pause_run"
            and control.macro_pause_timeout_s == 0
        ):
            control.macro_pause_timeout_s = 60.0
        self._refresh_after_control_change(control)

    def _on_control_macro_pause_timeout_changed(self, seconds: float) -> None:
        if self._updating_props or (control := self._selected_macro_control()) is None:
            return
        control.macro_pause_timeout_s = seconds
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
