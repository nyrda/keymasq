# ruff: noqa: I001
import logging
from types import SimpleNamespace

import pytest


def test_application_dialog_actions_route_to_window_helpers(monkeypatch) -> None:
    pytest.importorskip("gi")

    import keymasq.gui.application as application_module
    import keymasq.gui.widgets.diagnostics_dialog as diagnostics_module
    import keymasq.gui.widgets.macro_manager_dialog as macro_manager_module
    import keymasq.gui.widgets.superkey_editor.dialog as superkey_module
    from keymasq.gui.application import Application

    class _Window:
        def __init__(self) -> None:
            self.profile_manager = object()
            self.macro_dialogs: list[object | None] = []
            self.recording_settings = 0

        def set_macro_manager_dialog(self, dialog) -> None:
            self.macro_dialogs.append(dialog)

        def present_recording_settings_dialog(self) -> None:
            self.recording_settings += 1

    class _Dialog:
        def __init__(self, *args) -> None:
            self.args = args
            self.connected: list[tuple[str, object]] = []
            self.presented_with = None

        def connect(self, signal_name: str, callback, *args) -> None:
            self.connected.append((signal_name, callback))

        def present(self, parent) -> None:
            self.presented_with = parent

    class _AboutDialog(_Dialog):
        def __init__(self, **kwargs) -> None:
            super().__init__(kwargs)
            self.links: list[tuple[str, str]] = []

        def add_link(self, label: str, url: str) -> None:
            self.links.append((label, url))

    reloads: list[bool] = []
    monkeypatch.setattr(
        application_module,
        "notify_session_reload_async",
        lambda: reloads.append(True),
    )
    monkeypatch.setattr(superkey_module, "SuperkeyDialog", _Dialog)
    monkeypatch.setattr(macro_manager_module, "MacroManagerDialog", _Dialog)
    monkeypatch.setattr(diagnostics_module, "DiagnosticsDialog", _Dialog)
    monkeypatch.setattr(application_module.Adw, "AboutDialog", _AboutDialog)

    app = Application()
    app._open_superkey_dialog()

    window = _Window()
    app.window = window

    app._open_superkey_dialog()
    app._on_superkey_changed(None, "Nav")
    app._on_macros(None, None)
    app._on_record_macro(None, None)
    app._on_diagnostics(None, None)
    app._on_about(None, None)
    app._on_macro_manager_closed(None, window)

    assert reloads == [True]
    assert window.recording_settings == 1
    assert isinstance(window.macro_dialogs[0], _Dialog)
    assert window.macro_dialogs[-1] is None


def test_application_activate_and_main_use_configured_entrypoints(monkeypatch) -> None:
    pytest.importorskip("gi")

    import keymasq.gui.application as application_module
    from keymasq.gui.application import Application

    calls: list[str] = []

    class _Window:
        def __init__(self, application, demo_mode: bool) -> None:
            calls.append(f"window:{demo_mode}")
            self.application = application
            self.present_count = 0

        def present(self) -> None:
            self.present_count += 1

    monkeypatch.setattr(application_module, "ensure_config_dirs", lambda: calls.append("config"))
    monkeypatch.setattr(application_module, "MainWindow", _Window)

    app = Application(demo_mode=True)
    app.do_activate()
    app.do_activate()

    assert calls == ["config", "window:True", "config"]
    assert app.window.present_count == 2

    class _App:
        def __init__(self, demo_mode: bool = False) -> None:
            calls.append(f"main:{demo_mode}")

        def run(self) -> None:
            calls.append("run")

    monkeypatch.setattr(application_module, "Application", _App)
    monkeypatch.setattr(
        application_module.argparse.ArgumentParser,
        "parse_known_args",
        lambda self: (SimpleNamespace(demo=True), []),
    )

    application_module.main()

    assert calls[-2:] == ["main:True", "run"]


def test_application_uses_unique_instance_by_default(monkeypatch) -> None:
    pytest.importorskip("gi")

    import keymasq.gui.application as application_module
    from keymasq.gui.application import Application

    monkeypatch.delenv("GDK_BACKEND", raising=False)

    app = Application()

    assert not app.get_flags() & application_module.Gio.ApplicationFlags.NON_UNIQUE


def test_application_uses_non_unique_instance_for_broadway(monkeypatch) -> None:
    pytest.importorskip("gi")

    import keymasq.gui.application as application_module
    from keymasq.gui.application import Application

    monkeypatch.setenv("GDK_BACKEND", "broadway")

    app = Application()

    assert app.get_flags() & application_module.Gio.ApplicationFlags.NON_UNIQUE


def test_application_remains_unique_when_broadway_is_only_a_fallback(monkeypatch) -> None:
    pytest.importorskip("gi")

    import keymasq.gui.application as application_module
    from keymasq.gui.application import Application

    monkeypatch.setenv("GDK_BACKEND", "wayland,broadway")

    app = Application()

    assert not app.get_flags() & application_module.Gio.ApplicationFlags.NON_UNIQUE


def test_gui_appearance_preference_round_trips(temp_config_dir) -> None:
    from keymasq.gui.preferences import load_appearance_mode, save_appearance_mode

    assert load_appearance_mode() == "system"

    save_appearance_mode("dark")

    assert load_appearance_mode() == "dark"
    assert (temp_config_dir / "gui_settings.toml").read_text(encoding="utf-8").strip() == (
        'appearance = "dark"'
    )


def test_gui_preferences_warns_for_invalid_settings_toml(temp_config_dir, caplog) -> None:
    from keymasq.gui.preferences import load_appearance_mode

    (temp_config_dir / "gui_settings.toml").write_text("appearance = ", encoding="utf-8")
    caplog.set_level(logging.WARNING, logger="keymasq.gui.preferences")

    assert load_appearance_mode() == "system"
    assert "Failed to load GUI settings" in caplog.text


def test_gui_tab_layout_preference_round_trips_with_appearance(temp_config_dir) -> None:
    from keymasq.gui.preferences import (
        load_appearance_mode,
        load_hidden_tabs,
        load_selected_profile,
        load_selected_tab,
        load_tab_order,
        save_appearance_mode,
        save_selected_profile,
        save_selected_tab,
        save_tab_layout,
    )

    assert load_tab_order() == []
    assert load_hidden_tabs() == set()
    assert load_selected_profile() == ""
    assert load_selected_tab() == ""

    save_appearance_mode("dark")
    save_selected_profile(" Gaming ")
    save_selected_tab(" 1234:5678 ")
    save_tab_layout(
        [" device:2222:0002 ", "combos", "device:1111:0001", "combos", ""],
        {"combos", " "},
    )

    assert load_appearance_mode() == "dark"
    assert load_selected_profile() == "Gaming"
    assert load_selected_tab() == "1234:5678"
    assert load_tab_order() == ["device:2222:0002", "combos", "device:1111:0001"]
    assert load_hidden_tabs() == {"combos"}


def test_session_reload_reports_sync_and_async_status(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keymasq.gui.session_reload as session_reload_module

    monkeypatch.setattr(
        session_reload_module,
        "session_request",
        lambda payload, timeout=5.0: {"status": "ok", "payload": payload},
    )
    assert session_reload_module.notify_session_reload(timeout=1.0) is True

    def raise_request(_payload, timeout=5.0):
        raise RuntimeError("daemon unavailable")

    caplog.set_level(logging.ERROR, logger="keymasq.gui.session_reload")
    monkeypatch.setattr(session_reload_module, "session_request", raise_request)
    assert session_reload_module.notify_session_reload() is False
    assert "Unexpected failure notifying session reload" in caplog.text

    callbacks: list[bool] = []

    def fake_session_request_async(payload, callback, timeout=5.0):
        callbacks.append(callback({"status": "ok"}))
        callbacks.append(callback({"status": "error"}))

    monkeypatch.setattr(
        session_reload_module,
        "session_request_async",
        fake_session_request_async,
    )
    seen: list[bool] = []

    session_reload_module.notify_session_reload_async(seen.append, timeout=2.0)

    assert callbacks == [False, False]
    assert seen == [True, False]
