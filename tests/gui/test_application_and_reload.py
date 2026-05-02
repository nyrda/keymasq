# ruff: noqa: F403, F405, I001
from tests.gui.support import *


def test_application_dialog_actions_route_to_window_helpers(monkeypatch) -> None:
    import keymasq.gui.application as application_module
    import keymasq.gui.widgets.macro_manager_dialog as macro_manager_module
    import keymasq.gui.widgets.superkey_dialog as superkey_module
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
    monkeypatch.setattr(application_module.Adw, "AboutDialog", _AboutDialog)

    app = Application()
    app._open_superkey_dialog()

    window = _Window()
    app.window = window

    app._open_superkey_dialog()
    app._on_superkey_changed(None, "Nav")
    app._on_macros(None, None)
    app._on_record_macro(None, None)
    app._on_about(None, None)
    app._on_macro_manager_closed(None, window)

    assert reloads == [True]
    assert window.recording_settings == 1
    assert isinstance(window.macro_dialogs[0], _Dialog)
    assert window.macro_dialogs[-1] is None


def test_application_activate_and_main_use_configured_entrypoints(monkeypatch) -> None:
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


def test_session_reload_reports_sync_and_async_status(monkeypatch) -> None:
    import keymasq.gui.session_reload as session_reload_module

    monkeypatch.setattr(
        session_reload_module,
        "session_request",
        lambda payload, timeout=5.0: {"status": "ok", "payload": payload},
    )
    assert session_reload_module.notify_session_reload(timeout=1.0) is True

    def raise_request(_payload, timeout=5.0):
        raise RuntimeError("daemon unavailable")

    monkeypatch.setattr(session_reload_module, "session_request", raise_request)
    assert session_reload_module.notify_session_reload() is False

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
