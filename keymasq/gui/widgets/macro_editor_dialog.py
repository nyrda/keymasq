"""Compatibility shim for the macro editor dialog package."""

import sys
from typing import TYPE_CHECKING

from keymasq.gui.widgets.macro_editor import dialog as _dialog

if TYPE_CHECKING:
    MacroEditorDialog = _dialog.MacroEditorDialog

sys.modules[__name__] = _dialog
