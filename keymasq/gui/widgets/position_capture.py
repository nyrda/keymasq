# pyright: reportAttributeAccessIssue=false, reportUnknownLambdaType=false
from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.widgets.key_selector.compat import session_request_async


class PositionCaptureMixin:
    """Mouse-position capture behavior for dialog hosts.

    Hosts must initialize these attributes before calling any capture method:
    `_capture_request_id: int`, `_capture_timeout_id: int`,
    `_capture_pending: bool`, `_capture_apply: Callable[[int, int], None] | None`,
    `_capture_status_label: Gtk.Label | None`, `_capture_button: Gtk.Button | None`,
    `_slurp_available: bool`, and `_slurp_capture`, an object exposing
    `capture_point(callback)`.

    When `_slurp_available` is false, the host must also provide
    `mouse_move_capture_delay_spin: Gtk.SpinButton`. The mixin updates GTK
    button/label state directly and calls `session_request_async` with the
    `get_cursor_position` command for delayed fallback capture.
    """

    def _capture_compositor_position(
        self,
        button: Gtk.Button,
        status_label: Gtk.Label,
        callback: Callable[[int, int], None],
    ) -> None:
        self._begin_position_capture(
            button,
            status_label,
            callback,
        )

    def _begin_position_capture(
        self,
        button: Gtk.Button | None,
        status_label: Gtk.Label | None,
        apply_position: Callable[[int, int], None],
    ) -> None:
        self._cancel_capture_position("")
        self._capture_request_id += 1
        request_id = self._capture_request_id
        self._capture_button = button
        self._capture_status_label = status_label
        self._capture_apply = apply_position

        if self._slurp_available:
            self._capture_pending = True
            if button is not None:
                button.set_sensitive(False)
            if status_label is not None:
                status_label.set_text("Click to capture position...")
            self._slurp_capture.capture_point(
                lambda result, expected_id=request_id: self._on_slurp_capture_result(
                    expected_id, result
                )
            )
        else:
            self._capture_delay_seconds = float(self.mouse_move_capture_delay_spin.get_value())
            self._capture_pending = True
            if button is not None:
                button.set_sensitive(False)
            if status_label is not None:
                status_label.set_text(
                    f"Move cursor now... capturing in {self._capture_delay_seconds:.1f}s"
                )
            self._capture_timeout_id = GLib.timeout_add(
                int(self._capture_delay_seconds * 1000),
                lambda expected_id=request_id: self._capture_position_after_delay(expected_id),
            )

    def _on_slurp_capture_result(self, request_id: int, result) -> None:
        if request_id != self._capture_request_id:
            return
        self._capture_pending = False
        if self._capture_button is not None:
            self._capture_button.set_sensitive(True)

        if result is None:
            if self._capture_status_label is not None:
                self._capture_status_label.set_text("Capture cancelled or failed")
            return

        if self._capture_apply is not None:
            self._capture_apply(int(result.x), int(result.y))
        if self._capture_status_label is not None:
            self._capture_status_label.set_text(f"Captured: {result.x}, {result.y}")

    def _capture_position_after_delay(self, request_id: int) -> bool:
        self._capture_timeout_id = 0
        if request_id != self._capture_request_id or not self._capture_pending:
            return False
        if self._capture_status_label is not None:
            self._capture_status_label.set_text("Reading cursor position...")
        session_request_async(
            {"command": "get_cursor_position"},
            lambda response, expected_id=request_id: self._on_capture_position_response(
                expected_id, response
            ),
            timeout=5.0,
        )
        return False

    def _on_capture_position_response(self, request_id: int, response: dict | None) -> bool:
        if request_id != self._capture_request_id:
            return False
        self._capture_pending = False
        if self._capture_button is not None:
            self._capture_button.set_sensitive(True)

        if not response or response.get("status") != "ok":
            message = (
                (response or {}).get("message") or (response or {}).get("error") or "Capture failed"
            )
            if "Unknown command: get_cursor_position" in message:
                message = "Please restart Keymasq Session, then try again"
            if self._capture_status_label is not None:
                self._capture_status_label.set_text(message)
            return False

        x = int(response.get("x", 0))
        y = int(response.get("y", 0))
        if self._capture_apply is not None:
            self._capture_apply(x, y)
        if self._capture_status_label is not None:
            self._capture_status_label.set_text("Captured")
        return False

    def _cancel_capture_position(self, status_text: str) -> None:
        self._capture_request_id += 1
        if self._capture_timeout_id:
            GLib.source_remove(self._capture_timeout_id)
            self._capture_timeout_id = 0
        self._capture_pending = False
        if self._capture_button is not None:
            self._capture_button.set_sensitive(True)
        if self._capture_status_label is not None:
            self._capture_status_label.set_text(status_text)
        self._capture_apply = None
        self._capture_button = None
        self._capture_status_label = None
