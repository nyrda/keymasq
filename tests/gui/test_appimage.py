# ruff: noqa: E402

from dataclasses import replace
from pathlib import Path

import pytest

from keymasq.gui.appimage import (
    PKEXEC_DISMISSED,
    PKEXEC_FAILED,
    AppImageRuntime,
    InstallStatus,
    SetupNeed,
    UpdateCheck,
    detect_runtime,
    failure_message,
    release_notes_url,
    setup_need,
)

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.appimage_process import run_launcher
from keymasq.gui.widgets.appimage_dialog import AppImageDialog, setup_copy, setup_warnings
from keymasq.gui.window import appimage_setup, connection

INSTALLED = InstallStatus(
    installed=True,
    installed_version="1.2.3",
    loose_appimage=False,
    installed_runtime=True,
    native_package=False,
    systemd=True,
    user="deck",
    user_setup=True,
    daemon_active=True,
    session_active=True,
    password_missing=False,
    missing_install_tools=(),
    missing_update_tools=(),
    architecture="x86_64",
)
NOT_INSTALLED = replace(
    INSTALLED,
    installed=False,
    installed_version="",
    loose_appimage=True,
    installed_runtime=False,
    user_setup=False,
    daemon_active=False,
    session_active=False,
)


def _write_launcher(appdir: Path, script: str) -> AppImageRuntime:
    launcher = appdir / "bin/keymasq"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    launcher.chmod(0o755)
    marker = appdir / "share/keymasq/appimage/gui-integration-version"
    marker.parent.mkdir(parents=True)
    marker.write_text("1\n", encoding="utf-8")
    runtime = detect_runtime({"APPDIR": str(appdir)})
    assert runtime is not None
    return runtime


def test_launcher_commands_only_run_from_a_runtime_that_implements_them(tmp_path: Path) -> None:
    appdir = tmp_path / "AppDir"
    runtime = _write_launcher(appdir, "exit 0")
    appimage = tmp_path / "Keymasq.AppImage"
    appimage.touch()
    (appdir / "share/keymasq/appimage/gui-integration-version").unlink()

    assert detect_runtime({"APPDIR": str(appdir)}) is None
    assert detect_runtime({}) is None

    environment = replace(runtime, appimage=str(appimage)).environment(
        {
            "APPDIR": str(appdir),
            "ARGV0": "keymasq-session",
            "LD_LIBRARY_PATH": f"{appdir}/lib:/usr/lib/host",
            "PYTHONHOME": str(appdir),
        }
    )

    assert environment == {
        "APPDIR": str(appdir),
        "APPIMAGE": str(appimage),
        "LD_LIBRARY_PATH": "/usr/lib/host",
    }


def test_launcher_output_is_streamed_and_ends_with_the_exit_status(tmp_path: Path) -> None:
    runtime = _write_launcher(
        tmp_path / "AppDir",
        "printf '==> first\\nsecond\\n'; sleep 0.1; printf 'error: no newline' >&2; exit 7",
    )
    loop = GLib.MainLoop()
    streamed: list[str] = []
    finished: list[tuple[int, list[str]]] = []

    def on_exit(exit_code: int, lines: list[str]) -> None:
        finished.append((exit_code, lines))
        loop.quit()

    run_launcher(runtime, ["--anything"], streamed.append, on_exit)
    timeout = GLib.timeout_add_seconds(10, loop.quit)
    loop.run()
    GLib.source_remove(timeout)

    assert finished == [(7, ["==> first", "second", "error: no newline"])]
    assert streamed == finished[0][1]


@pytest.mark.parametrize(
    ("installed_version", "appimage_version", "primary_action", "secondary_action"),
    [
        ("0.21.1", "0.22.0", "install", "open-installed"),
        ("0.22.0rc1", "0.22.0", "install", "open-installed"),
        ("", "0.22.0", "open-installed", "install"),
        ("0.22.0", "0.21.1", "open-installed", "install"),
        ("0.22.0", "0.22.0.dev3", "open-installed", "install"),
    ],
)
def test_version_mismatch_never_offers_a_downgrade_as_the_default_action(
    installed_version: str,
    appimage_version: str,
    primary_action: str,
    secondary_action: str,
) -> None:
    status = replace(
        INSTALLED,
        installed_version=installed_version,
        loose_appimage=True,
        installed_runtime=False,
    )

    assert setup_need(status, appimage_version) is SetupNeed.VERSION_MISMATCH
    copy = setup_copy(SetupNeed.VERSION_MISMATCH, status, appimage_version)

    assert copy.primary is not None
    assert (copy.primary.action, copy.secondary.action) == (primary_action, secondary_action)


def test_install_warns_about_blockers_before_asking_for_a_password() -> None:
    blocked = replace(
        NOT_INSTALLED,
        password_missing=True,
        missing_install_tools=("setfacl",),
        systemd=False,
    )

    warnings = setup_warnings(SetupNeed.INSTALL, blocked)

    assert len(warnings) == 3
    assert "passwd" in warnings[0]
    assert "setfacl" in warnings[1]
    assert "keymasqd" in warnings[2]
    assert setup_warnings(SetupNeed.INSTALL, NOT_INSTALLED) == []
    assert setup_warnings(SetupNeed.SETUP_USER, blocked) == []


def test_failure_message_explains_authorization_problems_and_script_errors() -> None:
    output = ["==> Copying", "error: first problem", "warning: noise", "error: disk is full"]

    assert "cancelled" in failure_message(PKEXEC_DISMISSED, output, "keymasq --install")
    assert failure_message(PKEXEC_FAILED, output, "keymasq --install").endswith(
        "\nkeymasq --install"
    )
    assert failure_message(1, output, "keymasq --install") == "Disk is full"
    assert failure_message(3, ["no error line"], "keymasq --install").endswith("exit code 3.")


def test_release_notes_link_only_targets_published_versions() -> None:
    assert release_notes_url("1.2.3").endswith("/releases/tag/v1.2.3")
    assert release_notes_url("1.2.3.dev4").endswith("/releases")
    assert release_notes_url("1.2.3/../../evil").endswith("/releases")


class _Launcher:
    def __init__(self, status_output: str = "") -> None:
        self.status_output = status_output
        self.calls: list[list[str]] = []
        self.pending: dict[str, tuple] = {}

    def __call__(self, _runtime, arguments, on_line, on_exit) -> None:
        self.calls.append(list(arguments))
        if arguments[0] == "--appimage-status":
            on_exit(0 if self.status_output else 1, self.status_output.splitlines())
            return
        self.pending[arguments[0]] = (on_line, on_exit)

    def finish(self, argument: str, exit_code: int, lines: list[str]) -> None:
        on_line, on_exit = self.pending.pop(argument)
        for line in lines:
            if on_line is not None:
                on_line(line)
        on_exit(exit_code, lines)


def _widgets(root: Gtk.Widget, widget_type: type) -> list:
    found = [root] if isinstance(root, widget_type) else []
    child = root.get_first_child()
    while child is not None:
        found.extend(_widgets(child, widget_type))
        child = child.get_next_sibling()
    return found


def _visible_page(dialog: AppImageDialog) -> Gtk.Widget:
    stack = _widgets(dialog.get_child(), Gtk.Stack)[0]
    return stack.get_visible_child()


def _click(dialog: AppImageDialog, label: str) -> None:
    buttons = [
        button
        for button in _widgets(_visible_page(dialog), Gtk.Button)
        if button.get_label() == label and button.get_visible()
    ]
    assert len(buttons) == 1, label
    buttons[0].emit("clicked")


def _texts(dialog: AppImageDialog) -> list[str]:
    return [label.get_text() for label in _widgets(_visible_page(dialog), Gtk.Label)]


RUNTIME = AppImageRuntime(
    appdir="/tmp/AppDir",
    launcher="/tmp/AppDir/bin/keymasq",
    appimage="/home/deck/Downloads/Keymasq.AppImage",
)


def test_install_failure_shows_the_terminal_fallback_and_returns_to_the_offer() -> None:
    launcher = _Launcher()
    dialog = AppImageDialog(RUNTIME, NOT_INSTALLED, gui_version="1.2.3", run=launcher)

    _click(dialog, "Install")

    assert launcher.calls[-1] == ["--install"]
    assert not dialog.get_can_close()

    launcher.finish("--install", PKEXEC_FAILED, ["warning: authorization failed"])

    assert "Installation Failed" in _texts(dialog)
    assert any(
        text.endswith("\n/home/deck/Downloads/Keymasq.AppImage --install")
        for text in _texts(dialog)
    )
    assert dialog.get_can_close()

    _click(dialog, "Back")

    assert "Install Keymasq" in _texts(dialog)


def test_finished_install_reports_progress_then_closes_the_setup_dialog(monkeypatch) -> None:
    launcher = _Launcher()
    statuses: list[InstallStatus] = []
    closed: list[bool] = []
    dialog = AppImageDialog(
        RUNTIME,
        NOT_INSTALLED,
        gui_version="1.2.3",
        on_status_changed=statuses.append,
        run=launcher,
    )
    monkeypatch.setattr(dialog, "close", lambda: closed.append(True))
    _click(dialog, "Install")
    launcher.status_output = (
        "installed=yes\ninstalled_version=1.2.3\nlaunch=appimage\nservice_manager=systemd\n"
        "user_setup=yes\ndaemon_active=yes\nsession_active=yes\narchitecture=x86_64\n"
    )

    on_line, _on_exit = launcher.pending["--install"]
    on_line("==> Starting the background services")

    assert "Starting the background services…" in _texts(dialog)

    launcher.finish("--install", 0, [])

    assert "Keymasq Is Installed" in _texts(dialog)
    assert [status.installed for status in statuses] == [True]

    _click(dialog, "Done")

    assert closed == [True]


def test_finished_update_restarts_into_the_installed_version() -> None:
    launcher = _Launcher()
    restarts: list[bool] = []
    statuses: list[InstallStatus] = []
    dialog = AppImageDialog(
        replace(RUNTIME, appimage=None),
        INSTALLED,
        gui_version="1.2.3",
        on_status_changed=statuses.append,
        on_open_installed=lambda: restarts.append(True),
        run=launcher,
    )

    _click(dialog, "Check Now")
    launcher.finish(
        "--check-update",
        0,
        ["installed_version=1.2.3", "available_version=1.3.0", "update_available=yes"],
    )
    _click(dialog, "Update")
    launcher.finish("--self-update", 0, ["Keymasq updated to 1.3.0"])
    _click(dialog, "Restart Keymasq")

    assert restarts == [True]
    assert [status.installed_version for status in statuses] == ["1.3.0"]


def test_uninstall_asks_first_and_leaves_only_quit_afterwards(monkeypatch) -> None:
    from keymasq.gui.widgets import appimage_dialog as module

    confirmations: list = []
    monkeypatch.setattr(
        module.Adw.AlertDialog,
        "present",
        lambda confirmation, _parent: confirmations.append(confirmation),
    )
    launcher = _Launcher()
    quits: list[bool] = []
    unsaved_work_resolved: list = []
    dialog = AppImageDialog(
        replace(RUNTIME, appimage=None),
        INSTALLED,
        gui_version="1.2.3",
        on_quit=quits.append,
        before_uninstall=unsaved_work_resolved.append,
        run=launcher,
    )

    _click(dialog, "Uninstall…")
    confirmations[0].emit("response", "uninstall")

    assert ["--uninstall"] not in launcher.calls

    unsaved_work_resolved[0]()
    launcher.finish("--uninstall", 0, [])

    assert not dialog.get_can_close()

    _click(dialog, "Quit")

    assert quits == [True]


@pytest.fixture
def window(temp_config_dir, monkeypatch):
    from keymasq.gui.window.core import MainWindow

    presented: list = []

    class Dialog:
        showing_setup = True
        busy = False

        def __init__(self, _runtime, status, **_kwargs) -> None:
            self.status = status
            self.closed = False

        def connect(self, *_args) -> None:
            pass

        def present(self, _parent) -> None:
            presented.append(self)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(appimage_setup, "AppImageDialog", Dialog)
    monkeypatch.setattr(appimage_setup, "run_launcher", lambda *_args: None)
    window = MainWindow(demo_mode=True)
    window.demo_mode = False
    window._appimage_runtime = RUNTIME
    window.presented_dialogs = presented
    yield window
    window.demo_mode = True
    window.destroy()


def test_unreachable_session_offers_the_install_instead_of_systemctl_advice(window) -> None:
    connection._set_connection_issue(window, "session")

    assert window._connection_dialog is None
    assert window.presented_dialogs == []

    window._appimage_status_loaded = True
    appimage_setup._apply_status(window, NOT_INSTALLED)

    assert window._connection_dialog is None
    assert [dialog.status for dialog in window.presented_dialogs] == [NOT_INSTALLED]

    connection._set_connection_issue(window, None)

    assert window.presented_dialogs[0].closed


@pytest.mark.parametrize(
    "status",
    [
        INSTALLED,
        replace(INSTALLED, installed_version="0.0.1", loose_appimage=True, installed_runtime=False),
    ],
    ids=["matching", "other-version"],
)
def test_unreachable_session_keeps_the_connection_error_for_a_complete_install(
    window, status: InstallStatus
) -> None:
    window._appimage_status_loaded = True
    appimage_setup._apply_status(window, status)
    presented_before = len(window.presented_dialogs)

    connection._set_connection_issue(window, "session")

    assert window._connection_dialog is not None
    assert len(window.presented_dialogs) == presented_before


def test_native_install_keeps_its_compositor_warning_while_the_session_is_down(window) -> None:
    from keymasq.gui.window import compositor

    window._startup_probe_done = True
    window._appimage_status_loaded = True
    window._appimage_status = NOT_INSTALLED
    window._connection_issue = "session"

    compositor._update_compositor_warning_banner(window)
    hidden_during_onboarding = not window.warning_banner.get_revealed()
    window._appimage_runtime = None
    compositor._update_compositor_warning_banner(window)

    assert hidden_during_onboarding
    assert window.warning_banner.get_revealed()


def test_closed_dialog_ignores_a_late_update_check() -> None:
    launcher = _Launcher()
    checks: list[UpdateCheck] = []
    dialog = AppImageDialog(
        replace(RUNTIME, appimage=None),
        INSTALLED,
        gui_version="1.2.3",
        on_update_checked=checks.append,
        run=launcher,
    )
    _click(dialog, "Check Now")

    dialog.emit("closed")
    launcher.finish(
        "--check-update",
        0,
        ["installed_version=1.2.3", "available_version=1.3.0", "update_available=yes"],
    )

    assert checks == []


def test_services_on_another_version_ask_for_a_restart(window) -> None:
    window.demo_mode = True
    window._appimage_runtime = replace(RUNTIME, appimage=None)
    window._status_query_id = 1

    connection._on_status_response(
        window, {"status": "ok", "keymasqd_connected": True, "session_version": "99.0"}, 1
    )

    assert window.appimage_banner.get_revealed()
    assert "99.0" in window.appimage_banner.get_title()
    assert window.appimage_banner.get_button_label() == "Restart"

    window._status_query_id = 2
    connection._on_status_response(
        window,
        {
            "status": "ok",
            "keymasqd_connected": True,
            "session_version": appimage_setup.__version__,
        },
        2,
    )

    assert not window.appimage_banner.get_revealed()


def test_window_left_open_across_an_update_asks_for_a_restart(window) -> None:
    window._appimage_runtime = replace(RUNTIME, appimage=None)
    window._appimage_update = UpdateCheck("1.2.3", "99.0", update_available=True)

    appimage_setup._apply_status(window, replace(INSTALLED, installed_version="99.0"))

    assert "99.0" in window.appimage_banner.get_title()
    assert window.appimage_banner.get_button_label() == "Restart"
