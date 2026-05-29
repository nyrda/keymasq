# ruff: noqa: F403, F405, I001
from tests.gui.support import *


def test_settings_dialog_constructs(monkeypatch, temp_config_dir) -> None:
    gi.require_version("Adw", "1")
    from gi.repository import Adw

    from keymasq.gui.widgets import settings_dialog as dialog_module
    from keymasq.gui.widgets.settings_dialog import SettingsDialog

    monkeypatch.setattr(
        dialog_module,
        "session_request_async",
        lambda payload, callback, timeout=5.0: None,
    )

    dialog = SettingsDialog()

    assert isinstance(dialog, Adw.Dialog)
    assert dialog.get_child() is not None
    assert dialog.get_content_width() == 460
    assert dialog.get_content_height() == 240


def test_settings_dialog_shows_session_apply_error(
    monkeypatch,
    temp_config_dir,
) -> None:
    from keymasq.gui.widgets import settings_dialog as dialog_module
    from keymasq.gui.widgets.settings_dialog import SettingsDialog

    callbacks = []
    saves = []

    def fake_session_request_async(payload, callback, timeout=5.0):
        callbacks.append((payload, callback))

    monkeypatch.setattr(dialog_module, "session_request_async", fake_session_request_async)
    monkeypatch.setattr(
        dialog_module,
        "save_global_settings",
        lambda settings: saves.append(settings) or settings,
    )

    dialog = SettingsDialog()
    dialog._set_gamepad_count(2)

    callbacks[-1][1]({"status": "error", "message": "daemon rejected request"})

    assert dialog._status.get_text() == "daemon rejected request"
    assert saves == []


def test_settings_dialog_local_saves_only_without_session_response(
    monkeypatch,
    temp_config_dir,
) -> None:
    from keymasq.gui.widgets import settings_dialog as dialog_module
    from keymasq.gui.widgets.settings_dialog import SettingsDialog

    callbacks = []
    saves = []

    def fake_session_request_async(payload, callback, timeout=5.0):
        callbacks.append((payload, callback))

    monkeypatch.setattr(dialog_module, "session_request_async", fake_session_request_async)
    monkeypatch.setattr(
        dialog_module,
        "save_global_settings",
        lambda settings: saves.append(settings) or settings,
    )

    dialog = SettingsDialog()
    dialog._set_gamepad_count(3)

    callbacks[-1][1](None)

    assert dialog._status.get_text() == ""
    assert saves[0].virtual_gamepad_count == 3


def test_settings_dialog_loads_virtual_gamepad_count_from_session(
    monkeypatch,
    temp_config_dir,
) -> None:
    from keymasq.gui.widgets import settings_dialog as dialog_module
    from keymasq.gui.widgets.settings_dialog import SettingsDialog

    callbacks = []

    def fake_session_request_async(payload, callback, timeout=5.0):
        callbacks.append((payload, callback))

    monkeypatch.setattr(dialog_module, "session_request_async", fake_session_request_async)

    dialog = SettingsDialog()
    callbacks[0][1](
        {
            "status": "ok",
            "virtual_gamepad_count": 2,
        }
    )

    assert dialog._gamepad_count == 2
    assert dialog._count_label.get_text() == "2"


def test_settings_dialog_loads_string_virtual_gamepad_count_from_session(
    monkeypatch,
    temp_config_dir,
) -> None:
    from keymasq.gui.widgets import settings_dialog as dialog_module
    from keymasq.gui.widgets.settings_dialog import SettingsDialog

    callbacks = []

    def fake_session_request_async(payload, callback, timeout=5.0):
        callbacks.append((payload, callback))

    monkeypatch.setattr(dialog_module, "session_request_async", fake_session_request_async)

    dialog = SettingsDialog()
    callbacks[0][1](
        {
            "status": "ok",
            "virtual_gamepad_count": "2",
        }
    )

    assert dialog._gamepad_count == 2
    assert dialog._count_label.get_text() == "2"


def test_settings_dialog_ignores_stale_initial_load_after_save(
    monkeypatch,
    temp_config_dir,
) -> None:
    from keymasq.gui.widgets import settings_dialog as dialog_module
    from keymasq.gui.widgets.settings_dialog import SettingsDialog

    callbacks = []

    def fake_session_request_async(payload, callback, timeout=5.0):
        callbacks.append((payload, callback))

    monkeypatch.setattr(dialog_module, "session_request_async", fake_session_request_async)

    dialog = SettingsDialog()
    load_callback = callbacks[0][1]

    dialog._on_increment_gamepads_clicked(dialog._plus_button)
    assert callbacks[-1][0] == {
        "command": "set_settings",
        "virtual_gamepad_count": 2,
    }

    callbacks[-1][1]({"status": "ok", "virtual_gamepad_count": 2})
    load_callback({"status": "ok", "virtual_gamepad_count": 1})

    assert dialog._gamepad_count == 2
    assert dialog._count_label.get_text() == "2"


def test_settings_dialog_plus_minus_auto_applies(monkeypatch, temp_config_dir) -> None:
    from keymasq.gui.widgets import settings_dialog as dialog_module
    from keymasq.gui.widgets.settings_dialog import SettingsDialog

    callbacks = []

    def fake_session_request_async(payload, callback, timeout=5.0):
        callbacks.append((payload, callback))

    monkeypatch.setattr(dialog_module, "session_request_async", fake_session_request_async)

    dialog = SettingsDialog()
    callbacks[0][1](
        {
            "status": "ok",
            "virtual_gamepad_count": 1,
        }
    )

    dialog._on_increment_gamepads_clicked(dialog._plus_button)
    assert callbacks[-1][0] == {
        "command": "set_settings",
        "virtual_gamepad_count": 2,
    }

    callbacks[-1][1]({"status": "ok", "virtual_gamepad_count": 2})
    assert dialog._count_label.get_text() == "2"

    dialog._on_decrement_gamepads_clicked(dialog._minus_button)
    assert callbacks[-1][0]["virtual_gamepad_count"] == 1
