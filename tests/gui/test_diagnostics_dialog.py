# ruff: noqa: F403, F405, I001, E402
from tests.gui.support import *

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]


def test_diagnostics_format_helpers() -> None:
    from keymasq.gui.widgets.diagnostics_dialog import (
        _diagnostics_docs_url,
        _format_latency,
        _label_title,
        _output_event_export_line,
        _output_event_detail,
        _output_event_title,
    )

    assert _format_latency(22.25) == "22.2 us"
    assert _format_latency(1500.0) == "1.50 ms"
    assert _label_title("action_key") == "Action: key"
    assert _label_title("combo_passthrough") == "Combo candidate passthrough"
    assert (
        _output_event_title(
            {
                "kind": "output",
                "output": "keyboard",
                "code_name": "KEY_A",
                "value": 1,
            }
        )
        == "keyboard: KEY_A=1"
    )
    assert (
        _output_event_title(
            {
                "kind": "output",
                "output": "gamepad",
                "output_id": "virtual-gamepad-1",
                "code_name": "BTN_SOUTH",
                "value": 1,
            }
        )
        == "gamepad virtual-gamepad-1: BTN_SOUTH=1"
    )
    detail = _output_event_detail(
        {
            "sequence": 10,
            "category": "repeat",
            "type_name": "EV_KEY",
            "code_name": "KEY_A",
            "value": 2,
        }
    )
    assert "#10  repeat  EV_KEY KEY_A value=2" == detail
    detail = _output_event_detail(
        {
            "sequence": 12,
            "category": "mousemove",
            "type_name": "EV_REL",
            "code_name": "REL_X",
            "value": 7,
            "hardware_id": "1234:5678",
            "interface_id": "kbd",
        }
    )
    assert "1234:5678" in detail
    assert "kbd" in detail
    assert (
        _output_event_export_line(
            {
                "sequence": 12,
                "output": "mouse",
                "code_name": "REL_WHEEL",
                "type_name": "EV_REL",
                "value": -1,
            }
        )
        == "#12 mouse REL_WHEEL EV_REL value=-1"
    )
    assert _diagnostics_docs_url().endswith("/PERFORMANCE/#diagnostics-labels")


def test_diagnostics_dialog_sends_settings_and_renders_snapshot(monkeypatch) -> None:
    import keymasq.gui.widgets.diagnostics_dialog as dialog_module
    from keymasq.gui.widgets.diagnostics_dialog import DiagnosticsDialog

    sent: list[dict[str, object]] = []
    registered: list[tuple[str, object]] = []
    unregistered: list[tuple[str, object]] = []

    def fake_request_async(payload, callback, timeout=5.0):
        sent.append(payload)
        return callback(
            {
                "status": "ok",
                "data": {
                    "enabled": bool(payload["enabled"]),
                    "interval": payload["interval"],
                    "categories": payload["categories"],
                },
            }
        )

    monkeypatch.setattr(dialog_module, "session_request_async", fake_request_async)
    monkeypatch.setattr(
        dialog_module,
        "register_session_event_callback",
        lambda event, callback: registered.append((event, callback)),
    )
    monkeypatch.setattr(
        dialog_module,
        "unregister_session_event_callback",
        lambda event, callback: unregistered.append((event, callback)),
    )

    dialog = DiagnosticsDialog(Gtk.Window())
    assert registered and registered[0][0] == "diagnostics_snapshot"
    assert dialog._docs_button.get_tooltip_text() == "Open Diagnostics documentation"

    dialog._category_checks["combo"].set_active(True)
    dialog._enable_switch.set_active(True)

    assert sent[-1] == {
        "command": "set_diagnostics",
        "enabled": True,
        "interval": 5.0,
        "categories": ["mainline", "combo"],
    }

    dialog._on_diagnostics_snapshot(
        {
            "event": "diagnostics_snapshot",
            "enabled": True,
            "interval": 5.0,
            "categories": ["mainline", "combo"],
            "samples": {
                "passthrough_mapped": {
                    "n": 2,
                    "p50": 1.0,
                    "p95": 2.0,
                    "p99": 2.0,
                    "max": 3.0,
                }
            },
        }
    )

    assert "passthrough_mapped" in dialog._rows
    row = dialog._rows["passthrough_mapped"]
    cells = row._diagnostics_cells  # pyright: ignore[reportAttributeAccessIssue]
    assert cells["n"].get_text() == "n 2"
    assert cells["max"].get_text() == "peak 3.0 us"
    assert "OS scheduling noise" in (cells["max"].get_tooltip_text() or "")
    assert cells["max"].has_css_class("dim-label")

    sent.clear()
    dialog._on_closed(dialog)
    assert unregistered == [
        ("diagnostics_snapshot", dialog._on_diagnostics_snapshot),
        ("diagnostics_output_event", dialog._on_diagnostics_output_event),
    ]
    assert sent == [
        {
            "command": "set_diagnostics",
            "enabled": False,
            "interval": 5.0,
            "categories": ["mainline", "combo"],
        }
    ]


def test_diagnostics_dialog_output_stream_page(monkeypatch) -> None:
    import keymasq.gui.widgets.diagnostics_dialog as dialog_module
    from keymasq.gui.widgets.diagnostics_dialog import DiagnosticsDialog

    sent: list[dict[str, object]] = []
    callbacks: dict[str, object] = {}
    unregistered: list[tuple[str, object]] = []

    def fake_request_async(payload, callback, timeout=5.0):
        sent.append(payload)
        if payload["command"] == "set_diagnostics_output_stream":
            return callback(
                {
                    "status": "ok",
                    "data": {
                        "enabled": bool(payload["enabled"]),
                        "filters": payload["filters"],
                    },
                }
            )
        return callback(
            {
                "status": "ok",
                "data": {
                    "enabled": bool(payload["enabled"]),
                    "interval": payload["interval"],
                    "categories": payload["categories"],
                },
            }
        )

    monkeypatch.setattr(dialog_module, "session_request_async", fake_request_async)
    monkeypatch.setattr(
        dialog_module,
        "register_session_event_callback",
        lambda event, callback: callbacks.setdefault(event, callback),
    )
    monkeypatch.setattr(
        dialog_module,
        "unregister_session_event_callback",
        lambda event, callback: unregistered.append((event, callback)),
    )

    dialog = DiagnosticsDialog(Gtk.Window())
    assert "diagnostics_output_event" in callbacks
    assert dialog._reset_button.get_tooltip_text() == "Reset collected samples"

    dialog._output_enable_switch.set_active(True)

    assert sent[-1] == {
        "command": "set_diagnostics_output_stream",
        "enabled": True,
        "filters": ["button"],
    }
    assert dialog._output_status_label.get_text() == "Waiting for output events..."
    assert not dialog._output_filter_checks["repeat"].get_active()
    assert not dialog._output_filter_checks["mousemove"].get_active()
    assert not dialog._output_filter_checks["axis"].get_active()

    dialog._output_filter_checks["mousemove"].set_active(True)
    assert sent[-1] == {
        "command": "set_diagnostics_output_stream",
        "enabled": True,
        "filters": ["button", "mousemove"],
    }

    dialog._on_diagnostics_output_event(
        {
            "event": "diagnostics_output_event",
            "enabled": True,
            "filters": ["button", "mousemove"],
            "events": [
                {
                    "kind": "output",
                    "sequence": 1,
                    "category": "button",
                    "output": "keyboard",
                    "output_id": "keyboard",
                    "type_name": "EV_KEY",
                    "code_name": "KEY_A",
                    "value": 1,
                },
            ],
            "dropped": 0,
        }
    )

    assert len(dialog._output_rows) == 1
    assert not dialog._output_empty_label.get_visible()
    first_row_box = dialog._output_rows[0].get_child()
    assert isinstance(first_row_box, Gtk.Box)
    first_title = first_row_box.get_first_child()
    assert isinstance(first_title, Gtk.Label)
    assert first_title.get_text() == "keyboard: KEY_A=1"
    assert dialog._visible_output_event_export_text() == "#1 keyboard KEY_A EV_KEY value=1"

    copied: list[str] = []

    class Clipboard:
        def set(self, text: str) -> None:
            copied.append(text)

    class Display:
        def get_clipboard(self) -> Clipboard:
            return Clipboard()

    monkeypatch.setattr(dialog_module.Gdk.Display, "get_default", lambda: Display())
    dialog._on_copy_output_events_clicked(dialog._copy_output_button)
    assert copied == ["#1 keyboard KEY_A EV_KEY value=1"]

    dialog._stack.set_visible_child_name("output")
    assert dialog._reset_button.get_tooltip_text() == "Reset collected output events"

    sent.clear()
    dialog._on_reset_clicked(dialog._reset_button)

    assert sent == []
    assert dialog._output_rows == []
    assert dialog._output_empty_label.get_visible()
    assert dialog._output_status_label.get_text() == "Waiting for output events..."

    dialog._stack.set_visible_child_name("latency")
    assert dialog._reset_button.get_tooltip_text() == "Reset collected samples"

    sent.clear()
    dialog._on_closed(dialog)

    assert (
        "diagnostics_output_event",
        dialog._on_diagnostics_output_event,
    ) in unregistered
    assert sent == [
        {
            "command": "set_diagnostics_output_stream",
            "enabled": False,
            "filters": ["button", "mousemove"],
        }
    ]


def test_diagnostics_sort_by_priority(monkeypatch) -> None:
    import keymasq.gui.widgets.diagnostics_dialog as dialog_module
    from keymasq.gui.widgets.diagnostics_dialog import DiagnosticsDialog, _label_sort_key

    def fake_request_async(payload, callback, timeout=5.0):
        return callback(
            {
                "status": "ok",
                "data": {
                    "enabled": bool(payload["enabled"]),
                    "interval": payload["interval"],
                    "categories": payload["categories"],
                },
            }
        )

    monkeypatch.setattr(dialog_module, "session_request_async", fake_request_async)
    monkeypatch.setattr(dialog_module, "register_session_event_callback", lambda *a: None)
    monkeypatch.setattr(dialog_module, "unregister_session_event_callback", lambda *a: None)

    assert _label_sort_key("passthrough_mapped") < _label_sort_key("passthrough_fast")
    assert _label_sort_key("passthrough_fast") < _label_sort_key("combo_passthrough")
    assert _label_sort_key("combo_passthrough") < _label_sort_key("action_key")
    assert _label_sort_key("action_key") < _label_sort_key("syn")

    dialog = DiagnosticsDialog(Gtk.Window())
    dialog._enable_switch.set_active(True)

    dialog._on_diagnostics_snapshot(
        {
            "event": "diagnostics_snapshot",
            "enabled": True,
            "interval": 5.0,
            "categories": ["mainline"],
            "samples": {
                "passthrough_fast": {"n": 100, "p50": 5, "p95": 10, "p99": 15, "max": 20},
                "passthrough_mapped": {"n": 50, "p50": 50, "p95": 100, "p99": 200, "max": 300},
                "action_key": {"n": 75, "p50": 25, "p95": 50, "p99": 80, "max": 100},
            },
        }
    )

    assert dialog._rows["passthrough_mapped"]._sort_key < dialog._rows["passthrough_fast"]._sort_key  # pyright: ignore[reportAttributeAccessIssue]
    assert dialog._rows["passthrough_fast"]._sort_key < dialog._rows["action_key"]._sort_key  # pyright: ignore[reportAttributeAccessIssue]

    dialog._on_closed(dialog)


def test_diagnostics_reset_clears_rows(monkeypatch) -> None:
    import keymasq.gui.widgets.diagnostics_dialog as dialog_module
    from keymasq.gui.widgets.diagnostics_dialog import DiagnosticsDialog

    sent: list[dict[str, object]] = []

    def fake_request_async(payload, callback, timeout=5.0):
        sent.append(payload)
        return callback(
            {
                "status": "ok",
                "data": {
                    "enabled": bool(payload["enabled"]),
                    "interval": payload["interval"],
                    "categories": payload["categories"],
                },
            }
        )

    monkeypatch.setattr(dialog_module, "session_request_async", fake_request_async)
    monkeypatch.setattr(dialog_module, "register_session_event_callback", lambda *a: None)
    monkeypatch.setattr(dialog_module, "unregister_session_event_callback", lambda *a: None)

    dialog = DiagnosticsDialog(Gtk.Window())
    dialog._enable_switch.set_active(True)

    dialog._on_diagnostics_snapshot(
        {
            "event": "diagnostics_snapshot",
            "enabled": True,
            "interval": 5.0,
            "categories": ["mainline"],
            "samples": {
                "passthrough_mapped": {
                    "n": 100,
                    "p50": 10,
                    "p95": 20,
                    "p99": 30,
                    "max": 40,
                }
            },
        }
    )
    assert len(dialog._rows) == 1

    sent.clear()
    dialog._on_reset_clicked(dialog._reset_button)

    assert len(dialog._rows) == 0
    assert dialog._last_snapshot_time is None
    assert any(s["command"] == "set_diagnostics" for s in sent)

    dialog._enabled = False
    dialog._on_closed(dialog)
