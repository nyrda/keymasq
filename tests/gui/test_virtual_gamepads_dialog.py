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


def test_virtual_gamepads_dialog_shows_session_apply_error(
    monkeypatch,
    temp_config_dir,
) -> None:
    from keymasq.gui.widgets import virtual_gamepads_dialog as dialog_module
    from keymasq.gui.widgets.virtual_gamepads_dialog import VirtualGamepadsDialog

    callbacks = []
    saves = []

    def fake_session_request_async(payload, callback, timeout=5.0):
        callbacks.append((payload, callback))

    monkeypatch.setattr(dialog_module, "session_request_async", fake_session_request_async)
    monkeypatch.setattr(
        dialog_module,
        "save_virtual_gamepad_count",
        lambda count: saves.append(count) or count,
    )

    dialog = VirtualGamepadsDialog()
    dialog._spin.set_value(2)
    dialog._on_apply_clicked(dialog._spin)

    callbacks[-1][1]({"status": "error", "message": "daemon rejected request"})

    assert dialog._status.get_text() == "daemon rejected request"
    assert saves == []


def test_virtual_gamepads_dialog_local_saves_only_without_session_response(
    monkeypatch,
    temp_config_dir,
) -> None:
    from keymasq.gui.widgets import virtual_gamepads_dialog as dialog_module
    from keymasq.gui.widgets.virtual_gamepads_dialog import VirtualGamepadsDialog

    callbacks = []
    saves = []

    def fake_session_request_async(payload, callback, timeout=5.0):
        callbacks.append((payload, callback))

    monkeypatch.setattr(dialog_module, "session_request_async", fake_session_request_async)
    monkeypatch.setattr(
        dialog_module,
        "save_virtual_gamepad_count",
        lambda count: saves.append(count) or count,
    )

    dialog = VirtualGamepadsDialog()
    dialog._spin.set_value(3)
    dialog._on_apply_clicked(dialog._spin)

    callbacks[-1][1](None)

    assert dialog._status.get_text() == "Saved 3 virtual gamepad(s)"
    assert saves == [3]
