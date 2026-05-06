# ruff: noqa: F403, F405, I001, E402
from tests.gui.support import *

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]


def test_diagnostics_format_helpers() -> None:
    from keymasq.gui.widgets.diagnostics_dialog import (
        _diagnostics_docs_url,
        _format_latency,
        _label_title,
    )

    assert _format_latency(22.25) == "22.2 us"
    assert _format_latency(1500.0) == "1.50 ms"
    assert _label_title("action_key") == "Action: key"
    assert _label_title("combo_passthrough") == "Combo candidate passthrough"
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
    assert unregistered == [("diagnostics_snapshot", dialog._on_diagnostics_snapshot)]
    assert sent == [
        {
            "command": "set_diagnostics",
            "enabled": False,
            "interval": 5.0,
            "categories": ["mainline", "combo"],
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
    monkeypatch.setattr(
        dialog_module, "register_session_event_callback", lambda *a: None
    )
    monkeypatch.setattr(
        dialog_module, "unregister_session_event_callback", lambda *a: None
    )

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
    monkeypatch.setattr(
        dialog_module, "register_session_event_callback", lambda *a: None
    )
    monkeypatch.setattr(
        dialog_module, "unregister_session_event_callback", lambda *a: None
    )

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
