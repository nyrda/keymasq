"""Position-capture coordination for macro and selected move settings."""

import gi

# pyright: reportAttributeAccessIssue=false, reportUnknownLambdaType=false

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.widgets.macro_editor.model import (
    EditableControl,
    EditableMove,
    _describe_compositor_control,
)


class PositionCaptureMixin:
    """Coordinate reusable capture controllers with the editor widgets."""

    def _update_selected_move_capture_controls(
        self,
        selected_move: EditableMove | None = None,
    ) -> None:
        if not hasattr(self, "_move_capture_widgets"):
            return
        move = selected_move
        selected_obj = self._timeline._selected if hasattr(self, "_timeline") else None
        if move is None:
            move = selected_obj if isinstance(selected_obj, EditableMove) else None
        enabled = bool(move is not None and move.mode in {"abs", "natural"})
        if not enabled and self._compositor_capture_target() is not None:
            enabled = True
        for widget in self._move_capture_widgets:
            widget.set_visible(enabled)
        show_delay = enabled and not self._selected_move_capture.slurp_available
        self._move_capture_delay_spin.set_visible(show_delay)
        self._move_capture_delay_unit_label.set_visible(show_delay)
        if self._selected_move_capture.slurp_available:
            self._move_capture_delay_spin.set_sensitive(False)
        else:
            self._move_capture_delay_spin.set_sensitive(
                enabled and not self._selected_move_capture.pending
            )
        self._move_capture_btn.set_sensitive(enabled and not self._selected_move_capture.pending)

    def _compositor_capture_target(self) -> EditableControl | None:
        """The selected compositor action, when its preset takes a screen position."""
        selected_obj = self._timeline._selected if hasattr(self, "_timeline") else None
        if (
            not isinstance(selected_obj, EditableControl)
            or selected_obj.mode != "compositor_dispatch"
            or not hasattr(self, "_control_compositor_preset_dropdown")
        ):
            return None
        preset = self._compositor_selected_preset()
        if preset is None or not preset.captures_position:
            return None
        return selected_obj

    def _apply_compositor_capture_position(self, control: EditableControl, x: int, y: int) -> bool:
        if control not in self._control_events:
            self._move_capture_status.set_text("Capture target no longer available")
            return False
        control.compositor_args = f"{int(x)} {int(y)}"
        if self._timeline._selected is control:
            self._updating_props = True
            try:
                self._control_compositor_args_entry.set_text(control.compositor_args)
            finally:
                self._updating_props = False
            self._key_info_label.set_label(_describe_compositor_control(control))
        self._timeline.queue_draw()
        self._sync_close_guard()
        return True

    def _on_capture_selected_move_clicked(self, btn: Gtk.Button) -> None:
        compositor_control = self._compositor_capture_target()
        if compositor_control is not None:
            self._selected_move_capture.begin(
                button=self._move_capture_btn,
                status_label=self._move_capture_status,
                delay_seconds=float(self._move_capture_delay_spin.get_value()),
                apply_position=lambda x, y, control=compositor_control: (
                    self._apply_compositor_capture_position(control, x, y)
                ),
            )
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableMove) or selected_obj.mode not in {
            "abs",
            "natural",
        }:
            return
        self._selected_move_capture.begin(
            button=self._move_capture_btn,
            status_label=self._move_capture_status,
            delay_seconds=float(self._move_capture_delay_spin.get_value()),
            apply_position=lambda x, y, move=selected_obj: (
                self._apply_selected_move_capture_position(move, x, y)
            ),
        )

    def _apply_selected_move_capture_position(
        self,
        move: EditableMove,
        x: int,
        y: int,
    ) -> bool:
        if move.mode not in {"abs", "natural"} or move not in self._synthetic_moves:
            self._move_capture_status.set_text("Capture target no longer available")
            return False

        move.x = int(x)
        move.y = int(y)
        if self._timeline._selected is move:
            self._updating_props = True
            try:
                self._move_x_spin.set_value(move.x)
                self._move_y_spin.set_value(move.y)
            finally:
                self._updating_props = False
            self._on_selection_changed(move)
        self._timeline.queue_draw()
        self._sync_close_guard()
        return True

    def _cancel_capture_selected_move(self, status_text: str) -> None:
        self._selected_move_capture.cancel(status_text)
