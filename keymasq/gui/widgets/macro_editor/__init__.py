"""Macro editor package exports."""

from typing import TYPE_CHECKING

from keymasq.gui.widgets.macro_editor.model import (
    parse_events,
    reconstruct_events,
)

if TYPE_CHECKING:
    from keymasq.gui.widgets.macro_editor.dialog import MacroEditorDialog

__all__ = [
    "MacroEditorDialog",
    "parse_events",
    "reconstruct_events",
]


def __getattr__(name: str) -> object:
    if name == "MacroEditorDialog":
        from keymasq.gui.widgets.macro_editor.dialog import MacroEditorDialog

        return MacroEditorDialog
    raise AttributeError(name)
