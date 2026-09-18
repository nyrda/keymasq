"""Macro-level settings panel and its loop presentation state."""

from dataclasses import dataclass

import gi

# pyright: reportAttributeAccessIssue=false, reportUnknownLambdaType=false

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.widgets.macro_editor.panel.pause_timeout import PauseTimeoutControl
from keymasq.gui.widgets.macro_editor.panel.rows import (
    check_row,
    field_row,
    group_box,
    rows_list,
)

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
        title = Gtk.Label(label="Macro")
        title.add_css_class("heading")
        title.set_halign(Gtk.Align.START)
        title.set_hexpand(True)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.append(title)
        timing_btn = Gtk.Button(label="Timing…")
        timing_btn.add_css_class("flat")
        timing_btn.set_valign(Gtk.Align.CENTER)
        timing_btn.set_tooltip_text("Trim silence, insert empty time, or set the total length")
        timing_btn.connect("clicked", self._present_timing_dialog)
        header.append(timing_btn)

        self._name_entry = Gtk.Entry()
        self._name_entry.set_text(self._macro_name)
        self._name_entry.set_hexpand(True)
        self._name_entry.connect("changed", lambda _entry: self._sync_close_guard())
        name_row = field_row("Name", self._name_entry)

        self._macro_loop_mode_combo = _build_option_dropdown(
            _LOOP_MODE_OPTIONS,
            self._macro_loop_mode,
        )
        self._macro_loop_mode_combo.connect(
            "notify::selected",
            self._on_macro_loop_mode_changed,
        )
        loop_row = field_row("Loop", self._macro_loop_mode_combo)

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
        self._macro_loop_count_row = field_row("Count", self._macro_loop_count_spin)

        self._macro_loop_finish_check = Gtk.CheckButton()
        self._macro_loop_finish_check.set_active(self._macro_loop_stop_behavior == "finish_run")
        self._macro_loop_finish_check.connect(
            "toggled",
            self._on_macro_loop_stop_toggled,
        )
        self._macro_loop_finish_row = check_row(
            "Finish current run before stopping",
            self._macro_loop_finish_check,
            tooltip="When disabled, release or toggle stop cancels the macro immediately.",
        )
        self._macro_pause_check = Gtk.CheckButton()
        self._macro_pause_check.set_active(self._macro_loop_stop_behavior == "pause_run")
        self._macro_pause_check.connect("toggled", self._on_macro_pause_toggled)
        self._macro_pause_row = check_row(
            "Pause on release",
            self._macro_pause_check,
            tooltip=(
                "Release held outputs and pause the timeline. Hold the trigger again to resume. "
                "Active waits, commands, and mouse moves continue. Requires a held trigger."
            ),
        )
        self._macro_pause_timeout = PauseTimeoutControl(self._on_macro_pause_timeout_changed)
        self._macro_pause_timeout.set_timeout(self._macro_pause_timeout_s)

        self._macro_block_mouse_check = Gtk.CheckButton()
        self._macro_block_mouse_check.set_active(self._macro_block_mouse_movement)
        self._macro_block_mouse_check.connect(
            "toggled",
            self._on_macro_block_mouse_toggled,
        )
        block_mouse_row = check_row(
            "Block physical mouse movement",
            self._macro_block_mouse_check,
            tooltip="Suppress movement from the physical mouse while this macro plays.",
        )

        self._exec_summary_label = Gtk.Label()
        self._exec_summary_label.add_css_class("dim-label")
        self._exec_summary_label.add_css_class("caption")
        self._exec_summary_label.set_halign(Gtk.Align.START)
        self._exec_summary_label.set_xalign(0.0)
        summary = self._exec_summary_label

        fields = rows_list(name_row, loop_row, self._macro_loop_count_row)
        toggles = rows_list(
            self._macro_loop_finish_row,
            self._macro_pause_row,
            self._macro_pause_timeout,
            block_mouse_row,
        )
        toggles.set_margin_top(8)
        outer = group_box(header, fields, toggles, summary)

        self._update_loop_controls()
        self._update_exec_summary_label()
        return outer

    def _sync_macro_settings_controls(self) -> None:
        loop_mode = self._macro_loop_mode
        loop_count = self._macro_loop_count
        loop_stop_behavior = self._macro_loop_stop_behavior
        pause_timeout_s = self._macro_pause_timeout_s
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
        # The finish checkbox may emit while the old pause checkbox is still off.
        self._macro_loop_stop_behavior = loop_stop_behavior
        self._macro_pause_check.set_active(loop_stop_behavior == "pause_run")
        self._macro_pause_timeout.set_timeout(pause_timeout_s)
        self._macro_block_mouse_check.set_active(block_mouse_movement)
        self._update_loop_controls()

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
        if not self._macro_pause_check.get_active():
            self._macro_loop_stop_behavior = "finish_run" if check.get_active() else "cancel_run"
        self._sync_close_guard()

    def _on_macro_pause_toggled(self, check: Gtk.CheckButton) -> None:
        if (
            check.get_active()
            and self._macro_loop_stop_behavior != "pause_run"
            and self._macro_pause_timeout_s == 0
        ):
            self._macro_pause_timeout_s = 60.0
            self._macro_pause_timeout.set_timeout(60)
        self._macro_loop_stop_behavior = (
            "pause_run"
            if check.get_active()
            else "finish_run"
            if self._macro_loop_finish_check.get_active()
            else "cancel_run"
        )
        self._update_loop_controls()
        self._sync_close_guard()

    def _on_macro_pause_timeout_changed(self, seconds: float) -> None:
        self._macro_pause_timeout_s = seconds
        self._sync_close_guard()

    def _update_loop_controls(self) -> None:
        state = loop_control_state(self._macro_loop_mode)
        self._macro_loop_count_row.set_visible(state.show_count)
        self._macro_loop_count_spin.set_visible(state.show_count)
        self._macro_loop_finish_row.set_visible(state.show_stop_behavior)
        self._macro_loop_finish_check.set_visible(state.show_stop_behavior)
        show_pause = self._macro_loop_mode != "toggle"
        self._macro_pause_row.set_visible(show_pause)
        self._macro_pause_check.set_visible(show_pause)
        self._macro_pause_timeout.set_visible(show_pause and self._macro_pause_check.get_active())
        self._macro_loop_finish_check.set_sensitive(
            self._macro_loop_mode == "toggle" or not self._macro_pause_check.get_active()
        )

    def _on_macro_block_mouse_toggled(self, check: Gtk.CheckButton) -> None:
        self._macro_block_mouse_movement = check.get_active()
        self._sync_close_guard()
