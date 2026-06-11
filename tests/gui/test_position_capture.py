from collections.abc import Callable

import pytest

gi = pytest.importorskip("gi")


class _FakeSlurpCapture:
    def capture_point(self, callback: Callable[[object | None], None]) -> None:
        _ = callback


def test_position_capture_invalid_cursor_coordinates_report_status() -> None:
    gi.require_version("Gtk", "4.0")

    from keymasq.gui.widgets.position_capture import PositionCaptureController

    statuses: list[tuple[str, bool]] = []

    def set_status(_label, text: str, error: bool) -> None:
        statuses.append((text, error))

    controller = PositionCaptureController(
        slurp_capture=_FakeSlurpCapture(),
        slurp_available=False,
        set_status=set_status,
    )

    assert controller.on_response(0, {"status": "ok", "x": "bad", "y": 20}) is False
    assert statuses == [("Capture failed: invalid cursor coordinates", True)]
