# pyright: reportUnusedFunction=false

from __future__ import annotations

import time
from collections.abc import Callable

from keymasq import __version__
from keymasq.gui.appimage import (
    INSTALLED_COMMAND,
    InstallStatus,
    SetupNeed,
    UpdateCheck,
    detect_runtime,
    parse_install_status,
    parse_update_check,
    setup_need,
    update_check_due,
    updates_supported,
)
from keymasq.gui.appimage_process import run_launcher
from keymasq.gui.preferences import (
    load_last_update_check,
    load_update_check_enabled,
    save_last_update_check,
)
from keymasq.gui.widgets.appimage_dialog import AppImageDialog

from . import _runtime, connection

_MISSING_SETUP = frozenset({SetupNeed.INSTALL, SetupNeed.SETUP_USER})


def start(window) -> None:
    if window.demo_mode:
        return
    window._appimage_runtime = detect_runtime()
    if window._appimage_runtime is None:
        return
    window._appimage_menu_button.set_visible(True)
    _query_status(window)


def _query_status(window) -> None:
    def on_exit(exit_code: int, lines: list[str]) -> None:
        if window._destroyed:
            return
        status = parse_install_status("\n".join(lines)) if exit_code == 0 else None
        window._appimage_status_loaded = True
        if status is None:
            _reevaluate_session_issue(window)
            return
        _apply_status(window, status)
        _check_for_updates_if_due(window, status)

    run_launcher(window._appimage_runtime, ["--appimage-status"], None, on_exit)


def _apply_status(window, status: InstallStatus) -> None:
    if window._destroyed:
        return
    window._appimage_status = status
    update = window._appimage_update
    if update is not None and update.available_version == status.installed_version:
        window._appimage_update = None
    need = setup_need(status, __version__)
    if need is SetupNeed.VERSION_MISMATCH and not window._appimage_mismatch_prompted:
        window._appimage_mismatch_prompted = True
        present_dialog(window)
    _reevaluate_session_issue(window)
    update_banner(window)


def _reevaluate_session_issue(window) -> None:
    if window._connection_issue == "session":
        connection._set_connection_issue(window, "session")


def _check_for_updates_if_due(window, status: InstallStatus) -> None:
    if not updates_supported(status) or status.missing_update_tools:
        return
    if not load_update_check_enabled():
        return
    if not update_check_due(load_last_update_check(), time.time()):
        return

    def on_exit(exit_code: int, lines: list[str]) -> None:
        if window._destroyed or exit_code != 0:
            return
        check = parse_update_check("\n".join(lines))
        if check is not None:
            _on_update_checked(window, check)

    run_launcher(window._appimage_runtime, ["--check-update"], None, on_exit)


def _on_update_checked(window, check: UpdateCheck) -> None:
    if window._destroyed:
        return
    save_last_update_check(time.time())
    window._appimage_update = check
    update_banner(window)


def present_dialog(window) -> None:
    runtime = window._appimage_runtime
    status = window._appimage_status
    if runtime is None or status is None:
        return
    if window._appimage_dialog is None:

        def on_status_changed(new_status: InstallStatus) -> None:
            _apply_status(window, new_status)

        def on_update_checked(check: UpdateCheck) -> None:
            _on_update_checked(window, check)

        def on_quit(removed: bool) -> None:
            _quit(window, removed)

        dialog = AppImageDialog(
            runtime,
            status,
            gui_version=__version__,
            update=window._appimage_update,
            on_status_changed=on_status_changed,
            on_update_checked=on_update_checked,
            on_open_installed=lambda: open_installed(window),
            on_quit=on_quit,
            before_uninstall=lambda proceed: _before_uninstall(window, proceed),
        )

        def on_closed(closed: _runtime.Adw.Dialog) -> None:
            if closed is window._appimage_dialog:
                window._appimage_dialog = None

        dialog.connect("closed", on_closed)
        window._appimage_dialog = dialog
    window._appimage_dialog.present(window)


def present_for_session_issue(window) -> bool:
    if window._appimage_runtime is None:
        return False
    if not window._appimage_status_loaded:
        return True
    dialog = window._appimage_dialog
    if dialog is not None and dialog.busy:
        return True
    status = window._appimage_status
    if status is None or setup_need(status, __version__) not in _MISSING_SETUP:
        return False
    if dialog is None:
        present_dialog(window)
    return True


def on_session_reachable(window) -> None:
    dialog = window._appimage_dialog
    if dialog is None or not dialog.showing_setup:
        return
    status = window._appimage_status
    if status is not None and setup_need(status, __version__) is SetupNeed.VERSION_MISMATCH:
        return
    dialog.close()
    _query_status(window)


def update_session_version(window, data: dict | None) -> None:
    version = data.get("session_version") if isinstance(data, dict) else None
    session_version = version if isinstance(version, str) else ""
    if session_version != window._session_version:
        window._session_version = session_version
        update_banner(window)


def _banner_copy(window) -> tuple[str, str, str]:
    runtime = window._appimage_runtime
    status = window._appimage_status
    if (
        runtime is not None
        and status is not None
        and setup_need(status, __version__) is SetupNeed.VERSION_MISMATCH
    ):
        return (
            f"This AppImage is Keymasq {__version__}, but version "
            f"{status.installed_version or 'unknown'} is installed.",
            "Resolve…",
            "dialog",
        )
    session_version = window._session_version
    if (
        not session_version
        and status is not None
        and status.installed_runtime
        and status.installed_version
    ):
        session_version = status.installed_version
    if session_version and session_version != __version__:
        if runtime is None:
            return (
                f"The Keymasq services run version {session_version} and this window runs "
                f"{__version__}. Restart the services and this window to finish the update.",
                "",
                "",
            )
        if runtime.appimage is None:
            return (
                f"Keymasq was updated to {session_version}. Restart to finish the update.",
                "Restart",
                "restart",
            )
    update = window._appimage_update
    if runtime is not None and update is not None and update.update_available:
        return f"Keymasq {update.available_version} is available.", "Update…", "dialog"
    return "", "", ""


def update_banner(window) -> None:
    title, button_label, action = _banner_copy(window)
    window._appimage_banner_action = action
    banner = window.appimage_banner
    banner.set_title(title)
    banner.set_button_label(button_label or None)
    banner.set_visible(bool(title))
    banner.set_revealed(bool(title))


def on_banner_button_clicked(window) -> None:
    if window._appimage_banner_action == "restart":
        open_installed(window)
    elif window._appimage_banner_action == "dialog":
        present_dialog(window)


def open_installed(window) -> None:
    runtime = window._appimage_runtime
    app = window.get_application()
    if runtime is None or app is None:
        return
    environment = runtime.environment()
    del environment["APPDIR"]
    environment.pop("APPIMAGE", None)
    app.restart_into([INSTALLED_COMMAND], environment)


def _before_uninstall(window, proceed: Callable[[], None]) -> None:
    app = window.get_application()
    if app is None:
        proceed()
    else:
        app.close_macro_editors_then(proceed)


def _quit(window, removed: bool) -> None:
    app = window.get_application()
    if app is None:
        return
    if removed:
        # The uninstaller deleted the runtime, so nothing may be imported from here on.
        app.quit()
    else:
        app.activate_action("quit", None)
