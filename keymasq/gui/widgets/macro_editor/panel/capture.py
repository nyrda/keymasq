"""Position-capture coordination for macro and selected move settings."""

import gi

# pyright: reportAttributeAccessIssue=false, reportUnknownLambdaType=false

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.widgets.macro_editor.model import EditableMove


class PositionCaptureMixin:
    """Coordinate reusable capture controllers with the editor widgets."""

    def _update_selected_move_capture_controls(
        self,
        selected_move: EditableMove | None = None,
    ) -> None:
        if not hasattr(self, "_move_capture_widgets"):
            return
        move = selected_move
        if move is None:
            selected_obj = self._timeline._selected if hasattr(self, "_timeline") else None
            move = selected_obj if isinstance(selected_obj, EditableMove) else None
        enabled = bool(move is not None and move.mode in {"abs", "natural"})
        for widget in self._move_capture_widgets:
            widget.set_visible(enabled)
        show_delay = enabled and not self._selected_move_capture.slurp_available
        self._move_capture_delay_label.set_visible(show_delay)
        self._move_capture_delay_spin.set_visible(show_delay)
        self._move_capture_delay_unit_label.set_visible(show_delay)
        if self._selected_move_capture.slurp_available:
            self._move_capture_delay_spin.set_sensitive(False)
        else:
            self._move_capture_delay_spin.set_sensitive(
                enabled and not self._selected_move_capture.pending
            )
        self._move_capture_btn.set_sensitive(enabled and not self._selected_move_capture.pending)

    def _on_capture_start_position_clicked(self, btn: Gtk.Button) -> None:
        self._start_position_capture.begin(
            button=self._macro_capture_btn,
            status_label=self._macro_capture_status,
            delay_seconds=float(self._macro_capture_delay_spin.get_value()),
            apply_position=self._apply_start_capture_position,
        )

    def _on_capture_selected_move_clicked(self, btn: Gtk.Button) -> None:
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

    def _apply_start_capture_position(self, x: int, y: int) -> None:
        self._macro_start_x_spin.set_value(x)
        self._macro_start_y_spin.set_value(y)
        self._macro_move_to_start_check.set_active(True)
        self._sync_close_guard()

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

    def _on_slurp_capture_result(self, request_id: int, result) -> None:
        self._start_position_capture.on_slurp_result(request_id, result)

    def _on_move_slurp_capture_result(
        self,
        request_id: int,
        move: EditableMove,
        result,
    ) -> None:
        if self._selected_move_capture.apply is None:
            self._selected_move_capture.apply = lambda x, y: (
                self._apply_selected_move_capture_position(move, x, y)
            )
        self._selected_move_capture.on_slurp_result(request_id, result)

    def _capture_start_position_after_delay(self, request_id: int) -> bool:
        result = self._start_position_capture.capture_after_delay(request_id)
        return result

    def _capture_selected_move_after_delay(
        self,
        request_id: int,
        move: EditableMove,
    ) -> bool:
        if self._selected_move_capture.apply is None:
            self._selected_move_capture.apply = lambda x, y: (
                self._apply_selected_move_capture_position(move, x, y)
            )
        result = self._selected_move_capture.capture_after_delay(request_id)
        return result

    def _on_capture_start_position_response(
        self,
        request_id: int,
        response: dict | None,
    ) -> bool:
        result = self._start_position_capture.on_response(request_id, response)
        return result

    def _on_capture_selected_move_response(
        self,
        request_id: int,
        move: EditableMove,
        response: dict | None,
    ) -> bool:
        if self._selected_move_capture.apply is None:
            self._selected_move_capture.apply = lambda x, y: (
                self._apply_selected_move_capture_position(move, x, y)
            )
        result = self._selected_move_capture.on_response(request_id, response)
        return result

    def _cancel_capture_start_position(self, status_text: str) -> None:
        self._start_position_capture.cancel(status_text)

    def _cancel_capture_selected_move(self, status_text: str) -> None:
        self._selected_move_capture.cancel(status_text)
