import pytest

gi = pytest.importorskip("gi")


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
    assert dialog.get_content_height() == 380
    assert dialog._macro_settings_btn.get_tooltip_text() == "Open macro recording settings"


def test_settings_dialog_opens_macro_recording_settings(monkeypatch, temp_config_dir) -> None:
    from gi.repository import Gtk

    from keymasq.gui.widgets import settings_dialog as dialog_module
    from keymasq.gui.widgets.settings_dialog import SettingsDialog

    monkeypatch.setattr(
        dialog_module,
        "session_request_async",
        lambda payload, callback, timeout=5.0: None,
    )
    captured: dict[str, object] = {}

    class Parent(Gtk.Window):
        def present_recording_settings_dialog(self, reason: str = "settings") -> None:
            captured["reason"] = reason

    dialog = SettingsDialog(Parent())
    dialog._on_macro_settings_clicked(dialog._macro_settings_btn)

    assert captured == {"reason": "settings"}


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
    load_callback = callbacks[0][1]
    dialog._set_gamepad_count(2)

    callbacks[-1][1]({"status": "error", "message": "daemon rejected request"})

    assert dialog._status.get_text() == "daemon rejected request"
    assert dialog._gamepad_count == 1
    assert dialog._count_label.get_text() == "1"
    assert saves == []

    load_callback({"status": "ok", "virtual_gamepad_count": 3})

    assert dialog._gamepad_count == 3
    assert dialog._count_label.get_text() == "3"


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


def test_settings_dialog_save_tolerates_malformed_saved_count(
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
    dialog._set_gamepad_count(2)

    callbacks[-1][1]({"status": "ok", "virtual_gamepad_count": "bad"})

    assert dialog._gamepad_count == 2
    assert dialog._count_label.get_text() == "2"


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


def test_settings_dialog_reverts_to_stale_success_when_newest_save_fails(
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
            "virtual_gamepad_count": 1,
        }
    )

    dialog._on_increment_gamepads_clicked(dialog._plus_button)
    first_save_callback = callbacks[-1][1]
    assert callbacks[-1][0] == {
        "command": "set_settings",
        "virtual_gamepad_count": 2,
    }

    dialog._on_increment_gamepads_clicked(dialog._plus_button)
    newest_save_callback = callbacks[-1][1]
    assert callbacks[-1][0] == {
        "command": "set_settings",
        "virtual_gamepad_count": 3,
    }

    first_save_callback({"status": "ok", "virtual_gamepad_count": 2})
    assert dialog._count_label.get_text() == "3"

    newest_save_callback({"status": "error", "message": "failed"})

    assert dialog._status.get_text() == "failed"
    assert dialog._gamepad_count == 2
    assert dialog._applied_gamepad_count == 2
    assert dialog._count_label.get_text() == "2"


def test_settings_dialog_ignores_stale_success_after_newest_save_applies(
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
            "virtual_gamepad_count": 1,
        }
    )

    dialog._on_increment_gamepads_clicked(dialog._plus_button)
    first_save_callback = callbacks[-1][1]
    assert callbacks[-1][0] == {
        "command": "set_settings",
        "virtual_gamepad_count": 2,
    }

    dialog._on_increment_gamepads_clicked(dialog._plus_button)
    newest_save_callback = callbacks[-1][1]
    assert callbacks[-1][0] == {
        "command": "set_settings",
        "virtual_gamepad_count": 3,
    }

    newest_save_callback({"status": "ok", "virtual_gamepad_count": 3})
    first_save_callback({"status": "ok", "virtual_gamepad_count": 2})

    assert dialog._gamepad_count == 3
    assert dialog._applied_gamepad_count == 3
    assert dialog._count_label.get_text() == "3"

    dialog._on_decrement_gamepads_clicked(dialog._minus_button)
    assert callbacks[-1][0] == {
        "command": "set_settings",
        "virtual_gamepad_count": 2,
    }
    callbacks[-1][1]({"status": "error", "message": "failed"})

    assert dialog._status.get_text() == "failed"
    assert dialog._gamepad_count == 3
    assert dialog._applied_gamepad_count == 3
    assert dialog._count_label.get_text() == "3"


def test_settings_dialog_syncs_late_stale_success_after_newest_save_fails(
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
            "virtual_gamepad_count": 1,
        }
    )

    dialog._on_increment_gamepads_clicked(dialog._plus_button)
    first_save_callback = callbacks[-1][1]
    assert callbacks[-1][0] == {
        "command": "set_settings",
        "virtual_gamepad_count": 2,
    }

    dialog._on_increment_gamepads_clicked(dialog._plus_button)
    newest_save_callback = callbacks[-1][1]
    assert callbacks[-1][0] == {
        "command": "set_settings",
        "virtual_gamepad_count": 3,
    }

    newest_save_callback({"status": "error", "message": "failed"})
    assert dialog._count_label.get_text() == "1"

    first_save_callback({"status": "ok", "virtual_gamepad_count": 2})

    assert dialog._status.get_text() == "failed"
    assert dialog._gamepad_count == 2
    assert dialog._applied_gamepad_count == 2
    assert dialog._count_label.get_text() == "2"
