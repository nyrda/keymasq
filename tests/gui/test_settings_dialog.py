import pytest

gi = pytest.importorskip("gi")


@pytest.fixture
def toasts(monkeypatch):
    from keymasq.gui.widgets.settings_dialog import SettingsDialog

    submitted = []
    monkeypatch.setattr(SettingsDialog, "add_toast", lambda _dialog, toast: submitted.append(toast))
    return submitted


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

    assert isinstance(dialog, Adw.PreferencesDialog)
    assert dialog.get_search_enabled()
    assert dialog._masking_row.get_activatable()
    assert dialog._layout_row.get_activatable()
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
    toasts,
) -> None:
    from gi.repository import Adw

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

    assert [toast.get_title() for toast in toasts] == ["daemon rejected request"]
    assert isinstance(toasts[0], Adw.Toast)
    assert toasts[0].get_priority() == Adw.ToastPriority.HIGH
    assert toasts[0].get_timeout() == 3
    assert dialog._gamepad_count == 1
    assert dialog._count_label.get_text() == "1"
    assert saves == []

    load_callback({"status": "ok", "virtual_gamepad_count": 3})

    assert dialog._gamepad_count == 3
    assert dialog._count_label.get_text() == "3"


def test_settings_dialog_local_saves_only_without_session_response(
    monkeypatch,
    temp_config_dir,
    toasts,
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

    assert toasts == []
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
        "keyboard_layout": "us",
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
        "keyboard_layout": "us",
    }

    callbacks[-1][1]({"status": "ok", "virtual_gamepad_count": 2})
    assert dialog._count_label.get_text() == "2"

    dialog._on_decrement_gamepads_clicked(dialog._minus_button)
    assert callbacks[-1][0]["virtual_gamepad_count"] == 1


def test_settings_dialog_shows_persistence_warning_without_reverting(
    monkeypatch,
    temp_config_dir,
    toasts,
) -> None:
    from keymasq.gui.widgets import settings_dialog as dialog_module
    from keymasq.gui.widgets.settings_dialog import SettingsDialog

    callbacks = []

    def fake_session_request_async(payload, callback, timeout=5.0):
        callbacks.append((payload, callback))

    monkeypatch.setattr(dialog_module, "session_request_async", fake_session_request_async)

    dialog = SettingsDialog()
    callbacks[0][1]({"status": "ok", "virtual_gamepad_count": 1})
    dialog._on_increment_gamepads_clicked(dialog._plus_button)

    callbacks[-1][1](
        {
            "status": "ok",
            "virtual_gamepad_count": 2,
            "persisted": False,
            "warning": "Applied for this session but could not be saved.",
        }
    )

    assert dialog._gamepad_count == 2
    assert dialog._count_label.get_text() == "2"
    assert [toast.get_title() for toast in toasts] == [
        "Applied for this session but could not be saved."
    ]


def test_settings_dialog_reverts_to_stale_success_when_newest_save_fails(
    monkeypatch,
    temp_config_dir,
    toasts,
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
        "keyboard_layout": "us",
    }

    dialog._on_increment_gamepads_clicked(dialog._plus_button)
    newest_save_callback = callbacks[-1][1]
    assert callbacks[-1][0] == {
        "command": "set_settings",
        "virtual_gamepad_count": 3,
        "keyboard_layout": "us",
    }

    first_save_callback({"status": "ok", "virtual_gamepad_count": 2})
    assert dialog._count_label.get_text() == "3"

    newest_save_callback({"status": "error", "message": "failed"})

    assert [toast.get_title() for toast in toasts] == ["failed"]
    assert dialog._gamepad_count == 2
    assert dialog._applied_gamepad_count == 2
    assert dialog._count_label.get_text() == "2"


def test_settings_dialog_ignores_stale_success_after_newest_save_applies(
    monkeypatch,
    temp_config_dir,
    toasts,
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
        "keyboard_layout": "us",
    }

    dialog._on_increment_gamepads_clicked(dialog._plus_button)
    newest_save_callback = callbacks[-1][1]
    assert callbacks[-1][0] == {
        "command": "set_settings",
        "virtual_gamepad_count": 3,
        "keyboard_layout": "us",
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
        "keyboard_layout": "us",
    }
    callbacks[-1][1]({"status": "error", "message": "failed"})

    assert [toast.get_title() for toast in toasts] == ["failed"]
    assert dialog._gamepad_count == 3
    assert dialog._applied_gamepad_count == 3
    assert dialog._count_label.get_text() == "3"


def test_settings_dialog_syncs_late_stale_success_after_newest_save_fails(
    monkeypatch,
    temp_config_dir,
    toasts,
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
        "keyboard_layout": "us",
    }

    dialog._on_increment_gamepads_clicked(dialog._plus_button)
    newest_save_callback = callbacks[-1][1]
    assert callbacks[-1][0] == {
        "command": "set_settings",
        "virtual_gamepad_count": 3,
        "keyboard_layout": "us",
    }

    newest_save_callback({"status": "error", "message": "failed"})
    assert dialog._count_label.get_text() == "1"

    first_save_callback({"status": "ok", "virtual_gamepad_count": 2})

    assert [toast.get_title() for toast in toasts] == ["failed"]
    assert dialog._gamepad_count == 2
    assert dialog._applied_gamepad_count == 2
    assert dialog._count_label.get_text() == "2"


def test_settings_dialog_keyboard_layout_row_saves_selection(
    monkeypatch,
    temp_config_dir,
    toasts,
) -> None:
    from keymasq.common import xkb
    from keymasq.gui.widgets import settings_dialog as dialog_module
    from keymasq.gui.widgets.settings_dialog import SettingsDialog

    if not xkb.is_available():
        pytest.skip("libxkbcommon unavailable")
    callbacks = []

    def fake_session_request_async(payload, callback, timeout=5.0):
        callbacks.append((payload, callback))

    monkeypatch.setattr(dialog_module, "session_request_async", fake_session_request_async)

    dialog = SettingsDialog()
    assert dialog._layout_row.get_subtitle() == "English (US) (us)"

    callbacks[0][1](
        {"status": "ok", "virtual_gamepad_count": 1, "keyboard_layout": "de(nodeadkeys)"}
    )
    assert dialog._keyboard_layout == "de(nodeadkeys)"
    assert dialog._layout_row.get_subtitle() == "German (no dead keys) (de(nodeadkeys))"
    assert len(callbacks) == 1

    dialog._set_keyboard_layout("fr")
    payload, callback = callbacks[-1]
    assert payload == {
        "command": "set_settings",
        "virtual_gamepad_count": 1,
        "keyboard_layout": "fr",
    }

    callback({"status": "ok", "virtual_gamepad_count": 1, "keyboard_layout": "fr"})
    assert dialog._applied_keyboard_layout == "fr"
    assert toasts == []

    dialog._set_keyboard_layout("de")
    callbacks[-1][1]({"status": "error", "message": "daemon rejected request"})
    assert dialog._keyboard_layout == "fr"
    assert dialog._layout_row.get_subtitle() == "French (fr)"
    assert [toast.get_title() for toast in toasts] == ["daemon rejected request"]


def test_settings_dialog_keyboard_layout_page_filters_and_selects(
    monkeypatch,
    temp_config_dir,
) -> None:
    from keymasq.common import xkb
    from keymasq.gui.widgets import settings_dialog as dialog_module
    from keymasq.gui.widgets.settings_dialog import SettingsDialog

    if not xkb.is_available():
        pytest.skip("libxkbcommon unavailable")
    callbacks = []
    monkeypatch.setattr(
        dialog_module,
        "session_request_async",
        lambda payload, callback, timeout=5.0: callbacks.append((payload, callback)),
    )

    dialog = SettingsDialog()
    page = dialog._open_keyboard_layout_page()
    assert dialog._layout_page is page

    page.search_entry.set_text("de us")
    visible = page.visible_layout_ids()
    assert visible[0] == "de(us)"
    assert "al" not in visible

    page.search_entry.set_text("german dead")
    assert "de(nodeadkeys)" in page.visible_layout_ids()

    page.search_entry.set_text("de(T3)")
    assert page.visible_layout_ids()[0] == "de(T3)"
    page.search_entry.emit("activate")

    assert dialog._keyboard_layout == "de(T3)"
    assert dialog._layout_row.get_subtitle() == "German (T3) (de(T3))"
    assert callbacks[-1][0]["keyboard_layout"] == "de(T3)"


def test_settings_dialog_uses_session_layout_list(monkeypatch, temp_config_dir, toasts) -> None:
    from keymasq.gui.widgets import settings_dialog as dialog_module
    from keymasq.gui.widgets.settings_dialog import SettingsDialog

    callbacks = []
    monkeypatch.setattr(
        dialog_module,
        "session_request_async",
        lambda payload, callback, timeout=5.0: callbacks.append((payload, callback)),
    )

    dialog = SettingsDialog()
    callbacks[0][1](
        {
            "status": "ok",
            "virtual_gamepad_count": 1,
            "keyboard_layout": "us",
            "keyboard_layouts": [
                {"id": "us", "name": "English (US)"},
                {"id": "xx", "name": "Test"},
            ],
        }
    )

    assert dialog._layout_names == {"us": "English (US)", "xx": "Test"}
    assert dialog._layout_row.get_sensitive()
    page = dialog._open_keyboard_layout_page()
    assert page.visible_layout_ids() == ["us", "xx"]
    assert toasts == []

    empty = SettingsDialog()
    callbacks[-1][1](
        {
            "status": "ok",
            "virtual_gamepad_count": 1,
            "keyboard_layout": "us",
            "keyboard_layouts": [],
        }
    )
    assert not empty._layout_row.get_sensitive()
    assert [toast.get_title() for toast in toasts] == [
        "Keyboard layouts are unavailable: keymasq-session cannot load libxkbcommon"
    ]


def test_present_keyboard_layout_settings_opens_on_layout_page(monkeypatch) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets import settings_dialog as dialog_module

    monkeypatch.setattr(dialog_module, "session_request_async", lambda *a, **k: None)
    window = Gtk.Window()
    closed = []

    dialog = dialog_module.present_keyboard_layout_settings(
        window, on_closed=lambda: closed.append(True)
    )

    assert dialog._parent is window
    assert dialog._layout_page is not None
    assert dialog._layout_page.get_title() == "Keyboard layout"
    # The window is never mapped here, so fire the signal the dialog would emit.
    dialog.emit("closed")
    assert closed == [True]


def test_open_layout_page_is_dropped_when_session_list_differs(monkeypatch) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets import settings_dialog as dialog_module

    callbacks = []

    def fake_session_request_async(payload, callback, timeout=5.0):
        callbacks.append((payload, callback))

    monkeypatch.setattr(dialog_module, "session_request_async", fake_session_request_async)
    dialog = dialog_module.present_keyboard_layout_settings(Gtk.Window())
    page = dialog._layout_page
    assert page is not None
    assert "de" in page.layout_ids()

    # The session cannot load libxkbcommon: its list is empty, so the page built
    # from the GUI's own list goes away and the row is disabled.
    callbacks[0][1](
        {
            "status": "ok",
            "virtual_gamepad_count": 1,
            "keyboard_layout": "us",
            "keyboard_layouts": [],
        }
    )
    assert dialog._layout_page is None
    assert dialog._layout_row.get_sensitive() is False

    # A matching list leaves an open page alone.
    dialog2 = dialog_module.present_keyboard_layout_settings(Gtk.Window())
    page2 = dialog2._layout_page
    assert page2 is not None
    callbacks[1][1](
        {
            "status": "ok",
            "virtual_gamepad_count": 1,
            "keyboard_layout": "us",
            "keyboard_layouts": [{"id": i, "name": n} for i, n in dialog2._layout_names.items()],
        }
    )
    assert dialog2._layout_page is page2
