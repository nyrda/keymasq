# ruff: noqa: E402, I001
"""Shared helpers for macro editor dialog tests."""

import pytest

gi = pytest.importorskip("gi")

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gtk

from keymasq.gui.widgets.macro_editor import dialog as macro_editor_dialog_module
from keymasq.gui.widgets.macro_editor.dialog import MacroEditorDialog

__all__ = [
    "_FakeSlurpCapture",
    "_build_macro_dialog",
]


class _FakeSlurpCapture:
    def __init__(self, available: bool = False) -> None:
        self.available = available
        self.compositor: str | None = None
        self.capture_callback = None

    def set_compositor(self, compositor: str) -> None:
        self.compositor = compositor

    def capture_point(self, callback) -> None:
        self.capture_callback = callback


def _build_macro_dialog(
    monkeypatch,
    *,
    slurp_available: bool = False,
    create_new: bool = False,
) -> MacroEditorDialog:
    fake_slurp = _FakeSlurpCapture(available=slurp_available)
    monkeypatch.setattr(macro_editor_dialog_module, "get_slurp_capture", lambda: fake_slurp)
    monkeypatch.setattr(macro_editor_dialog_module, "session_compositor_id", lambda: "hyprland")
    monkeypatch.setattr(
        macro_editor_dialog_module,
        "_compute_macro_editor_dialog_size",
        lambda parent: (800, 600),
    )
    monkeypatch.setattr(MacroEditorDialog, "_load_initial_state_async", lambda self: None)
    dialog = MacroEditorDialog(Gtk.Window(), "demo_macro", create_new=create_new)
    dialog._test_slurp = fake_slurp  # type: ignore[attr-defined]
    return dialog
