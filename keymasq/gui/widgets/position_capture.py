from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from typing import Protocol, cast

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import (  # pyright: ignore[reportAttributeAccessIssue]
    GLib,  # pyright: ignore[reportAttributeAccessIssue]
    Gtk,  # pyright: ignore[reportAttributeAccessIssue]
)

from keymasq.gui.session_client import session_request_async

PositionCallback = Callable[[int, int], bool | None]
StatusCallback = Callable[[Gtk.Label | None, str, bool], None]


class CapturedPoint(Protocol):
    x: int
    y: int


class SlurpCapture(Protocol):
    def capture_point(self, callback: Callable[[CapturedPoint | None], None]) -> None: ...


@dataclass(frozen=True, slots=True)
class PositionCaptureMessages:
    click_prompt: str = "Click to capture position..."
    delay_prompt: str = "Move cursor now... capturing in {delay:.1f}s"
    reading: str = "Reading cursor position..."
    failed: str = "Capture cancelled or failed"
    unknown_command: str = "Please restart Keymasq Session, then try again"
    slurp_success: str | Callable[[int, int], str] = "Captured: {x}, {y}"
    response_success: str | Callable[[int, int], str] = "Captured"


def default_set_capture_status(
    status_label: Gtk.Label | None,
    text: str,
    error: bool,
) -> None:
    _ = error
    if status_label is not None:
        status_label.set_text(text)


class PositionCaptureController:
    def __init__(
        self,
        *,
        slurp_capture: SlurpCapture,
        slurp_available: bool,
        set_status: StatusCallback = default_set_capture_status,
        request_async: Callable[..., None] = session_request_async,
        on_state_changed: Callable[[], None] | None = None,
        messages: PositionCaptureMessages | None = None,
    ) -> None:
        self._slurp_capture = slurp_capture
        self._slurp_available = slurp_available
        self._set_status = set_status
        self._request_async = request_async
        self._on_state_changed = on_state_changed
        self._messages = messages or PositionCaptureMessages()
        self.timeout_id = 0
        self.pending = False
        self.request_id = 0
        self.apply: PositionCallback | None = None
        self.status_label: Gtk.Label | None = None
        self.button: Gtk.Button | None = None
        self.delay_seconds = 2.0

    @property
    def slurp_available(self) -> bool:
        return self._slurp_available

    def begin(
        self,
        *,
        button: Gtk.Button | None,
        status_label: Gtk.Label | None,
        delay_seconds: float,
        apply_position: PositionCallback,
        messages: PositionCaptureMessages | None = None,
    ) -> None:
        active_messages = messages or self._messages
        self.cancel("")
        self.request_id += 1
        request_id = self.request_id
        self.button = button
        self.status_label = status_label
        self.apply = apply_position

        if self._slurp_available:
            self.pending = True
            self._set_button_sensitive(False)
            self._set_status(status_label, active_messages.click_prompt, False)
            self._notify_state_changed()

            def on_point(result: CapturedPoint | None, expected_id: int = request_id) -> None:
                self.on_slurp_result(expected_id, result, active_messages)

            self._slurp_capture.capture_point(on_point)
            return

        self.delay_seconds = self._validated_delay_seconds(delay_seconds)
        self.pending = True
        self._set_button_sensitive(False)
        self._set_status(
            status_label,
            active_messages.delay_prompt.format(delay=self.delay_seconds),
            False,
        )
        self._notify_state_changed()
        self.timeout_id = GLib.timeout_add(
            int(self.delay_seconds * 1000),
            lambda expected_id=request_id: self.capture_after_delay(
                expected_id,
                active_messages,
            ),
        )

    def on_slurp_result(
        self,
        request_id: int,
        result: CapturedPoint | None,
        messages: PositionCaptureMessages | None = None,
    ) -> None:
        active_messages = messages or self._messages
        if request_id != self.request_id:
            return
        self.pending = False
        self._set_button_sensitive(True)
        self._notify_state_changed()

        if result is None:
            self._set_status(self.status_label, active_messages.failed, True)
            return

        x = int(result.x)
        y = int(result.y)
        if self.apply is not None and self.apply(x, y) is False:
            return
        self._set_status(
            self.status_label,
            self._success_text(active_messages.slurp_success, x, y),
            False,
        )

    def capture_after_delay(
        self,
        request_id: int,
        messages: PositionCaptureMessages | None = None,
    ) -> bool:
        active_messages = messages or self._messages
        self.timeout_id = 0
        if request_id != self.request_id or not self.pending:
            return False
        self._set_status(self.status_label, active_messages.reading, False)

        def on_cursor_response(
            response: dict | None,
            expected_id: int = request_id,
        ) -> bool:
            return self.on_response(expected_id, response, active_messages)

        self._request_async(
            {"command": "get_cursor_position"},
            cast(Callable[[dict | None], bool], on_cursor_response),
            timeout=5.0,
        )
        return False

    def on_response(
        self,
        request_id: int,
        response: dict | None,
        messages: PositionCaptureMessages | None = None,
    ) -> bool:
        active_messages = messages or self._messages
        if request_id != self.request_id:
            return False
        self.pending = False
        self._set_button_sensitive(True)
        self._notify_state_changed()

        if not response or response.get("status") != "ok":
            message = (
                (response or {}).get("message")
                or (response or {}).get("error")
                or "Capture failed"
            )
            if "Unknown command: get_cursor_position" in message:
                message = active_messages.unknown_command
            self._set_status(self.status_label, str(message), True)
            return False

        if "x" not in response or "y" not in response:
            self._set_status(
                self.status_label,
                "Capture failed: missing cursor coordinates",
                True,
            )
            return False

        x = int(response["x"])
        y = int(response["y"])
        if self.apply is not None and self.apply(x, y) is False:
            return False
        self._set_status(
            self.status_label,
            self._success_text(active_messages.response_success, x, y),
            False,
        )
        return False

    def cancel(self, status_text: str) -> None:
        self.request_id += 1
        if self.timeout_id:
            GLib.source_remove(self.timeout_id)
            self.timeout_id = 0
        self.pending = False
        self._set_button_sensitive(True)
        self._set_status(self.status_label, status_text, False)
        self.apply = None
        self.button = None
        self.status_label = None
        self._notify_state_changed()

    def _set_button_sensitive(self, sensitive: bool) -> None:
        if self.button is not None:
            self.button.set_sensitive(sensitive)

    def _validated_delay_seconds(self, delay_seconds: float) -> float:
        value = float(delay_seconds)
        if not isfinite(value) or value < 0.0 or value > 3600.0:
            raise ValueError("position capture delay must be finite and between 0 and 3600 seconds")
        return value

    def _notify_state_changed(self) -> None:
        if self._on_state_changed is not None:
            self._on_state_changed()

    def _success_text(
        self,
        template: str | Callable[[int, int], str],
        x: int,
        y: int,
    ) -> str:
        if callable(template):
            return template(x, y)
        return template.format(x=x, y=y)
