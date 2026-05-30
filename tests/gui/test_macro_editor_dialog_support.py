import re
from pathlib import Path


def test_macro_editor_support_uses_gui_import_guard() -> None:
    source = Path(__file__).with_name("macro_editor_dialog_support.py").read_text(encoding="utf-8")

    guarded_import = "from tests.gui.support import gi"
    assert guarded_import in source
    assert not any(re.match(r"^\s*import\s+gi\b", line) for line in source.splitlines())
    assert source.index(guarded_import) < source.index('gi.require_version("Gtk", "4.0")')
