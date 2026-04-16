# pyright: reportUnusedImport=false, reportUnusedFunction=false, reportUnusedClass=false
# ruff: noqa: F401, I001
"""Macro editor dialog tests."""

# ruff: noqa: E402, I001

import gi
import sys

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import evdev  # noqa: E402

from keymasq.common.models import ActionType  # noqa: E402
from keymasq.gui.widgets.macro_editor_dialog import EditableEvent  # noqa: E402
from keymasq.gui.widgets.macro_editor_dialog import EditableMove  # noqa: E402
from keymasq.gui.widgets.macro_editor_dialog import MacroEditorDialog  # noqa: E402
from keymasq.gui.widgets.macro_editor_dialog import _passthrough_track  # noqa: E402
from keymasq.gui.widgets.macro_editor_dialog import parse_events  # noqa: E402
from keymasq.gui.widgets.macro_editor_dialog import reconstruct_events  # noqa: E402

macro_editor_dialog_module = sys.modules["keymasq.gui.widgets.macro_editor_dialog"]


class _FakeSlurpCapture:
    def __init__(self, available: bool = False) -> None:
        self.available = available
        self.compositor: str | None = None
        self.capture_callback = None

    def set_compositor(self, compositor: str) -> None:
        self.compositor = compositor

    def capture_point(self, callback) -> None:
        self.capture_callback = callback


def _build_macro_dialog(monkeypatch, *, slurp_available: bool = False) -> MacroEditorDialog:
    from gi.repository import Gtk

    fake_slurp = _FakeSlurpCapture(available=slurp_available)
    monkeypatch.setattr(macro_editor_dialog_module, "get_slurp_capture", lambda: fake_slurp)
    monkeypatch.setattr(macro_editor_dialog_module, "detect_compositor_sync", lambda: "hyprland")
    monkeypatch.setattr(
        macro_editor_dialog_module,
        "_compute_macro_editor_dialog_size",
        lambda parent: (800, 600),
    )
    monkeypatch.setattr(MacroEditorDialog, "_load_initial_state_async", lambda self: None)
    dialog = MacroEditorDialog(Gtk.Window(), "demo_macro")
    dialog._test_slurp = fake_slurp  # type: ignore[attr-defined]
    return dialog

__all__ = [
    'gi',
    'sys',
    'evdev',
    'ActionType',
    'EditableEvent',
    'EditableMove',
    'MacroEditorDialog',
    '_passthrough_track',
    'parse_events',
    'reconstruct_events',
    'macro_editor_dialog_module',
    '_FakeSlurpCapture',
    '_build_macro_dialog',
]
