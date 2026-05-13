# ruff: noqa: F403, F405, I001
from tests.gui.support import *


def test_virtual_gamepads_dialog_constructs(monkeypatch, temp_config_dir) -> None:
    gi.require_version("Adw", "1")
    from gi.repository import Adw

    from keymasq.gui.widgets import virtual_gamepads_dialog as dialog_module
    from keymasq.gui.widgets.virtual_gamepads_dialog import VirtualGamepadsDialog

    monkeypatch.setattr(
        dialog_module,
        "session_request_async",
        lambda payload, callback, timeout=5.0: None,
    )

    dialog = VirtualGamepadsDialog()

    assert isinstance(dialog, Adw.Dialog)
    assert dialog.get_child() is not None
    assert dialog.get_content_width() == 420
    assert dialog.get_content_height() == 220
