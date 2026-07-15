"""Macro-level settings panel and its loop presentation state."""

from dataclasses import dataclass

import gi

# pyright: reportAttributeAccessIssue=false, reportUnknownLambdaType=false

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

_LOOP_MODE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("none", "Once"),
    ("count", "Count"),
    ("hold", "While Held"),
    ("toggle", "Toggle"),
)


@dataclass(frozen=True)
class LoopControlState:
    """Visibility state for the controls associated with a loop mode."""

    show_count: bool
    show_stop_behavior: bool


def loop_control_state(mode: str) -> LoopControlState:
    """Resolve loop-control visibility without requiring GTK widgets."""
    return LoopControlState(
        show_count=mode == "count",
        show_stop_behavior=mode in {"hold", "toggle"},
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


class MacroSettingsMixin:
    """Construct and coordinate macro name, loop, and playback settings."""

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
        self._name_entry.connect("changed", lambda _entry: self._sync_close_guard())
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
            Gtk.Adjustment(
                value=self._macro_loop_count,
                lower=1,
                upper=10000,
                step_increment=1,
            )
        )
        self._macro_loop_count_spin.set_digits(0)
        self._macro_loop_count_spin.set_width_chars(6)
        self._macro_loop_count_spin.connect("value-changed", self._on_macro_loop_count_changed)
        loop_row.append(self._macro_loop_count_spin)

        self._macro_loop_finish_check = Gtk.CheckButton(label="Finish current run before stopping")
        self._macro_loop_finish_check.set_active(self._macro_loop_stop_behavior == "finish_run")
        self._macro_loop_finish_check.set_tooltip_text(
            "When disabled, release or toggle stop cancels the macro immediately."
        )
        self._macro_loop_finish_check.connect(
            "toggled",
            self._on_macro_loop_stop_toggled,
        )
        loop_row.append(self._macro_loop_finish_check)
        outer.append(loop_row)

        self._exec_summary_label = Gtk.Label()
        self._exec_summary_label.add_css_class("dim-label")
        self._exec_summary_label.set_halign(Gtk.Align.START)
        outer.append(self._exec_summary_label)

        start_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._move_to_start_row = start_row
        self._macro_move_to_start_check = Gtk.CheckButton(label="Move mouse to:")
        self._macro_move_to_start_check.set_active(self._macro_move_to_start)
        self._macro_move_to_start_check.connect("toggled", self._on_macro_move_to_start_toggled)
        start_row.append(self._macro_move_to_start_check)

        self._macro_start_x_spin = Gtk.SpinButton()
        self._macro_start_x_spin.set_adjustment(
            Gtk.Adjustment(
                value=self._macro_start_x,
                lower=-100000,
                upper=100000,
                step_increment=1,
            )
        )
        self._macro_start_x_spin.set_digits(0)
        self._macro_start_x_spin.set_width_chars(7)
        self._macro_start_x_spin.connect("value-changed", self._on_macro_start_pos_changed)
        start_row.append(self._macro_start_x_spin)

        self._macro_start_y_spin = Gtk.SpinButton()
        self._macro_start_y_spin.set_adjustment(
            Gtk.Adjustment(
                value=self._macro_start_y,
                lower=-100000,
                upper=100000,
                step_increment=1,
            )
        )
        self._macro_start_y_spin.set_digits(0)
        self._macro_start_y_spin.set_width_chars(7)
        self._macro_start_y_spin.connect("value-changed", self._on_macro_start_pos_changed)
        start_row.append(self._macro_start_y_spin)

        start_row.append(Gtk.Label(label="at the start of the macro"))
        outer.append(start_row)

        capture_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._move_to_start_capture_row = capture_row
        capture_row.set_margin_start(24)

        if not self._start_position_capture.slurp_available:
            capture_label = Gtk.Label(label="Capture new position in:")
            capture_label.add_css_class("dim-label")
            capture_row.append(capture_label)

        self._macro_capture_delay_spin = Gtk.SpinButton()
        self._macro_capture_delay_spin.set_adjustment(
            Gtk.Adjustment(
                value=self._start_position_capture.delay_seconds,
                lower=0.2,
                upper=15.0,
                step_increment=0.2,
            )
        )
        self._macro_capture_delay_spin.set_digits(1)
        self._macro_capture_delay_spin.set_width_chars(4)
        self._macro_capture_delay_spin.set_visible(not self._start_position_capture.slurp_available)
        capture_row.append(self._macro_capture_delay_spin)

        if not self._start_position_capture.slurp_available:
            capture_row.append(Gtk.Label(label="s"))

        self._macro_capture_btn = Gtk.Button(label="Capture")
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
        self._macro_block_mouse_check.connect(
            "toggled",
            self._on_macro_block_mouse_toggled,
        )
        outer.append(self._macro_block_mouse_check)

        self._update_loop_controls()
        self._update_macro_move_start_controls()
        self._update_exec_summary_label()
        return outer

    def _sync_macro_settings_controls(self) -> None:
        loop_mode = self._macro_loop_mode
        loop_count = self._macro_loop_count
        loop_stop_behavior = self._macro_loop_stop_behavior
        move_to_start = self._macro_move_to_start
        start_x = self._macro_start_x
        start_y = self._macro_start_y
        block_mouse_movement = self._macro_block_mouse_movement
        name = str(self._macro_data.get("name", self._macro_name) or self._macro_name)
        self._name_entry.set_text(name)
        _set_dropdown_selected_id(
            self._macro_loop_mode_combo,
            _LOOP_MODE_OPTIONS,
            loop_mode,
        )
        self._macro_loop_count_spin.set_value(loop_count)
        self._macro_loop_finish_check.set_active(loop_stop_behavior == "finish_run")
        self._macro_move_to_start_check.set_active(move_to_start)
        self._macro_start_x_spin.set_value(start_x)
        self._macro_start_y_spin.set_value(start_y)
        self._macro_block_mouse_check.set_active(block_mouse_movement)
        self._update_loop_controls()
        self._update_macro_move_start_controls()

    def _on_macro_loop_mode_changed(
        self,
        combo: Gtk.DropDown,
        _pspec=None,
    ) -> None:
        self._macro_loop_mode = _get_dropdown_selected_id(combo, _LOOP_MODE_OPTIONS, "none")
        self._update_loop_controls()
        self._sync_close_guard()

    def _on_macro_loop_count_changed(self, spin: Gtk.SpinButton) -> None:
        self._macro_loop_count = max(1, int(spin.get_value()))
        self._sync_close_guard()

    def _on_macro_loop_stop_toggled(self, check: Gtk.CheckButton) -> None:
        self._macro_loop_stop_behavior = "finish_run" if check.get_active() else "cancel_run"
        self._sync_close_guard()

    def _update_loop_controls(self) -> None:
        state = loop_control_state(self._macro_loop_mode)
        self._macro_loop_count_label.set_visible(state.show_count)
        self._macro_loop_count_spin.set_visible(state.show_count)
        self._macro_loop_finish_check.set_visible(state.show_stop_behavior)

    def _on_macro_move_to_start_toggled(self, check: Gtk.CheckButton) -> None:
        self._macro_move_to_start = check.get_active()
        self._update_macro_move_start_controls()
        self._sync_close_guard()

    def _on_macro_start_pos_changed(self, spin: Gtk.SpinButton) -> None:
        self._macro_start_x = int(self._macro_start_x_spin.get_value())
        self._macro_start_y = int(self._macro_start_y_spin.get_value())
        self._sync_close_guard()

    def _on_macro_block_mouse_toggled(self, check: Gtk.CheckButton) -> None:
        self._macro_block_mouse_movement = check.get_active()
        self._sync_close_guard()

    def _update_macro_move_start_controls(self) -> None:
        visible = self._macro_has_move_to_start_setting
        self._move_to_start_row.set_visible(visible)
        self._move_to_start_capture_row.set_visible(visible)
        enabled = self._macro_move_to_start
        self._macro_start_x_spin.set_sensitive(enabled)
        self._macro_start_y_spin.set_sensitive(enabled)
        if self._start_position_capture.slurp_available:
            self._macro_capture_delay_spin.set_sensitive(False)
        else:
            self._macro_capture_delay_spin.set_sensitive(
                enabled and not self._start_position_capture.pending
            )
        self._macro_capture_btn.set_sensitive(enabled and not self._start_position_capture.pending)
