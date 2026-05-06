# ruff: noqa: F403, F405, I001, E402
from tests.gui.support import *

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]


def test_diagnostics_format_helpers() -> None:
    from keymasq.gui.widgets.diagnostics_dialog import _format_latency, _label_title

    assert _format_latency(22.25) == "22.2 us"
    assert _format_latency(1500.0) == "1.50 ms"
    assert _label_title("action_key") == "Action: key"
    assert _label_title("combo_passthrough") == "Combo candidate passthrough"


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
    assert "single slowest sample" in (cells["max"].get_tooltip_text() or "")

    dialog._on_closed(dialog)
    assert unregistered == [("diagnostics_snapshot", dialog._on_diagnostics_snapshot)]
