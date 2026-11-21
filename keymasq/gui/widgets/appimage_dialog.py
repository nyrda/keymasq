from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.appimage import (
    AppImageRuntime,
    InstallStatus,
    Operation,
    SetupNeed,
    UpdateCheck,
    compare_versions,
    failure_message,
    operation_arguments,
    parse_install_status,
    parse_update_check,
    release_notes_url,
    setup_need,
    step_from_line,
    updates_supported,
)
from keymasq.gui.appimage_process import LauncherRunner, run_launcher
from keymasq.gui.preferences import load_update_check_enabled, save_update_check_enabled

SetupAction = Operation | Literal["open-installed", "quit"]


@dataclass(frozen=True)
class SetupButton:
    label: str
    action: SetupAction


@dataclass(frozen=True)
class SetupCopy:
    title: str
    body: str
    primary: SetupButton | None
    secondary: SetupButton


@dataclass(frozen=True)
class _OperationCopy:
    running_title: str
    success_title: str
    failure_title: str


_OPERATION_COPY: dict[Operation, _OperationCopy] = {
    "install": _OperationCopy("Installing Keymasq", "Keymasq Is Installed", "Installation Failed"),
    "setup-user": _OperationCopy("Setting Up Keymasq", "Keymasq Is Ready", "Setup Failed"),
    "update": _OperationCopy("Updating Keymasq", "Keymasq Was Updated", "Update Failed"),
    "repair": _OperationCopy("Repairing Keymasq", "Repair Finished", "Repair Failed"),
    "uninstall": _OperationCopy("Uninstalling Keymasq", "Keymasq Was Removed", "Uninstall Failed"),
}

_QUIT = SetupButton("Quit", "quit")
_OPEN_INSTALLED = SetupButton("Open Installed Version", "open-installed")


def setup_copy(need: SetupNeed, status: InstallStatus, gui_version: str) -> SetupCopy:
    if need is SetupNeed.INSTALL:
        return SetupCopy(
            title="Install Keymasq",
            body=(
                "Keymasq remaps your devices through two background services. Installing "
                "copies Keymasq to /opt/keymasq, starts both services, and adds Keymasq to "
                "your application menu.\n\nYou will be asked for your password."
            ),
            primary=SetupButton("Install", "install"),
            secondary=_QUIT,
        )
    if need is SetupNeed.SETUP_USER:
        return SetupCopy(
            title="Set Up Keymasq for Your Account",
            body=(
                "Keymasq is installed on this system, but its session service is not set "
                "up for your user account yet. Setting it up does not need a password."
            ),
            primary=SetupButton("Set Up", "setup-user"),
            secondary=_QUIT,
        )
    if need is SetupNeed.NATIVE_PACKAGE:
        return SetupCopy(
            title="Keymasq Is Already Installed",
            body=(
                "A Keymasq package from your distribution is installed on this system. "
                "Start that version from your application menu. To use this AppImage "
                "instead, remove the package first."
            ),
            primary=None,
            secondary=_QUIT,
        )
    installed_version = status.installed_version or "an unknown version"
    explanation = "The window and the background services have to run the same version."
    order = compare_versions(status.installed_version, gui_version)
    if order == 1:
        return SetupCopy(
            title=f"Update Keymasq to {gui_version}?",
            body=(
                f"Keymasq {installed_version} is installed and this AppImage is version "
                f"{gui_version}. {explanation}\n\nYou will be asked for your password."
            ),
            primary=SetupButton(f"Update to {gui_version}", "install"),
            secondary=_OPEN_INSTALLED,
        )
    older = order == -1
    return SetupCopy(
        title="A Newer Keymasq Is Installed" if older else "A Different Keymasq Is Installed",
        body=(
            f"Keymasq {installed_version} is installed and this AppImage is "
            f"{'the older version' if older else 'version'} {gui_version}. {explanation}"
        ),
        primary=_OPEN_INSTALLED,
        secondary=SetupButton(
            f"Downgrade to {gui_version}" if older else f"Install {gui_version}", "install"
        ),
    )


def setup_warnings(need: SetupNeed, status: InstallStatus) -> list[str]:
    if need in {SetupNeed.NONE, SetupNeed.NATIVE_PACKAGE, SetupNeed.SETUP_USER}:
        return []
    warnings: list[str] = []
    if status.password_missing:
        warnings.append(
            "Your user account has no password, and the installer needs one to get "
            "administrator rights. Open a terminal, run passwd, then come back."
        )
    if status.missing_install_tools:
        warnings.append(
            "These system tools are missing: "
            f"{', '.join(status.missing_install_tools)}. Install them with your "
            "distribution's package manager first."
        )
    if need is SetupNeed.INSTALL and not status.systemd:
        warnings.append(
            "This system does not use systemd. Keymasq installs its files, but you have "
            "to add a service for the keymasqd daemon yourself. The installer shows "
            "the details when it finishes."
        )
    return warnings


def services_summary(status: InstallStatus) -> str:
    if status.daemon_active is None or status.session_active is None:
        return "Not managed by systemd"
    stopped = [
        name
        for name, active in (
            ("keymasqd", status.daemon_active),
            ("keymasq-session", status.session_active),
        )
        if not active
    ]
    if not stopped:
        return "Running"
    verb = "is" if len(stopped) == 1 else "are"
    return f"{' and '.join(stopped)} {verb} not running"


def success_message(operation: Operation, status: InstallStatus | None, upgraded: bool) -> str:
    if operation == "install":
        if status is not None and not status.systemd:
            return (
                "Keymasq is installed, but the keymasqd daemon is not running yet. Add a "
                "service for it as described under Details."
            )
        if upgraded:
            return "The background services now run this version."
        return (
            "The background services are running. From now on, start Keymasq from your "
            "application menu. You can delete the downloaded AppImage file."
        )
    if operation == "setup-user":
        return "The session service is set up for your user account."
    if operation == "update":
        return "Restart Keymasq to finish the update."
    if operation == "repair":
        return "The installation files were written again and the services were restarted."
    return "Your profiles, macros, and settings were kept."


class AppImageDialog(Adw.Dialog):
    def __init__(
        self,
        runtime: AppImageRuntime,
        status: InstallStatus,
        *,
        gui_version: str,
        update: UpdateCheck | None = None,
        on_status_changed: Callable[[InstallStatus], None] | None = None,
        on_update_checked: Callable[[UpdateCheck], None] | None = None,
        on_open_installed: Callable[[], None] | None = None,
        on_quit: Callable[[bool], None] | None = None,
        before_uninstall: Callable[[Callable[[], None]], None] | None = None,
        run: LauncherRunner = run_launcher,
    ) -> None:
        super().__init__(content_width=560, content_height=-1)
        self._runtime = runtime
        self._status = status
        self._gui_version = gui_version
        self._update = update
        self._update_error = ""
        self._checking_update = False
        self._on_status_changed = on_status_changed
        self._on_update_checked = on_update_checked
        self._on_open_installed = on_open_installed
        self._on_quit = on_quit
        self._before_uninstall = before_uninstall
        self._run = run
        self._closed = False
        self.connect("closed", self._on_closed)
        self._opened_for_setup = setup_need(status, gui_version) is not SetupNeed.NONE
        self._operation: Operation | None = None
        self._operation_running = False
        self._operation_upgraded = False
        self._finish_action: Literal["back", "done", "restart", "quit"] = "back"

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        self._overview = Adw.Bin()
        self._stack = Gtk.Stack()
        self._stack.set_vhomogeneous(False)
        self._stack.add_named(self._overview, "overview")
        self._stack.add_named(self._build_progress_page(), "progress")
        toolbar.set_content(self._stack)
        self.set_child(toolbar)

        self._rebuild_overview()
        self._refresh_status()

    @property
    def operation_running(self) -> bool:
        return self._operation_running

    @property
    def busy(self) -> bool:
        return self._stack.get_visible_child_name() == "progress"

    def _on_closed(self, _dialog: Adw.Dialog) -> None:
        self._closed = True

    @property
    def showing_setup(self) -> bool:
        return (
            self._opened_for_setup
            and not self._operation_running
            and self._stack.get_visible_child_name() == "overview"
        )

    def _need(self) -> SetupNeed:
        return setup_need(self._status, self._gui_version)

    def _rebuild_overview(self) -> None:
        need = self._need()
        if need is SetupNeed.NONE:
            self.set_title("Install & Updates")
            self._overview.set_child(self._build_manage_page())
        else:
            self.set_title("Keymasq Setup")
            self._overview.set_child(self._build_setup_page(need))

    def _page_box(self, spacing: int) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
        box.set_margin_top(12)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)
        return box

    def _wrapped_label(self, text: str, *css_classes: str) -> Gtk.Label:
        label = Gtk.Label(label=text)
        label.set_halign(Gtk.Align.START)
        label.set_xalign(0.0)
        label.set_wrap(True)
        for css_class in css_classes:
            label.add_css_class(css_class)
        return label

    def _build_setup_page(self, need: SetupNeed) -> Gtk.Widget:
        copy = setup_copy(need, self._status, self._gui_version)
        box = self._page_box(14)
        box.append(self._wrapped_label(copy.title, "title-2"))
        box.append(self._wrapped_label(copy.body))
        for warning in setup_warnings(need, self._status):
            box.append(self._wrapped_label(f"⚠️ {warning}", "warning"))

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        buttons.set_margin_top(6)
        secondary = Gtk.Button(label=copy.secondary.label)
        secondary.connect("clicked", self._on_setup_button_clicked, copy.secondary.action)
        buttons.append(secondary)
        if copy.primary is not None:
            primary = Gtk.Button(label=copy.primary.label)
            primary.add_css_class("suggested-action")
            primary.connect("clicked", self._on_setup_button_clicked, copy.primary.action)
            buttons.append(primary)
        box.append(buttons)
        return box

    def _build_manage_page(self) -> Gtk.Widget:
        box = self._page_box(18)

        installation = Adw.PreferencesGroup(title="Installation")
        version_row = Adw.ActionRow(title="Installed version", use_markup=False)
        version_row.set_subtitle(self._status.installed_version or "Unknown")
        installation.add(version_row)
        services_row = Adw.ActionRow(title="Background services")
        services_row.set_subtitle(services_summary(self._status))
        installation.add(services_row)
        box.append(installation)

        updates = Adw.PreferencesGroup(title="Updates")
        updates.add(self._build_update_row())
        automatic_row = Adw.SwitchRow(title="Check automatically")
        automatic_row.set_subtitle("Looks for a new version when Keymasq starts, once a day")
        automatic_row.set_active(load_update_check_enabled())
        automatic_row.connect("notify::active", self._on_automatic_check_toggled)
        updates.add(automatic_row)
        box.append(updates)

        maintenance = Adw.PreferencesGroup(title="Maintenance")
        repair_row = Adw.ActionRow(title="Repair installation")
        repair_row.set_subtitle("Writes the services, device rules, and commands again")
        repair_row.add_suffix(self._row_button("Repair", self._on_operation_clicked, "repair"))
        maintenance.add(repair_row)
        uninstall_row = Adw.ActionRow(title="Uninstall Keymasq")
        uninstall_row.set_subtitle("Keeps your profiles, macros, and settings")
        uninstall_button = self._row_button("Uninstall…", self._on_uninstall_clicked)
        uninstall_button.add_css_class("destructive-action")
        uninstall_row.add_suffix(uninstall_button)
        maintenance.add(uninstall_row)
        box.append(maintenance)
        return box

    def _row_button(
        self, label: str, handler: Callable[..., None], *arguments: object
    ) -> Gtk.Button:
        button = Gtk.Button(label=label)
        button.set_valign(Gtk.Align.CENTER)
        button.connect("clicked", handler, *arguments)
        return button

    def _build_update_row(self) -> Adw.ActionRow:
        row = Adw.ActionRow(use_markup=False)
        status = self._status
        if not updates_supported(status):
            row.set_title("Updates are unavailable here")
            if status.loose_appimage:
                row.set_subtitle("Open the installed Keymasq from your application menu to update")
            else:
                row.set_subtitle("The updater supports x86_64 systems only")
        elif status.missing_update_tools:
            row.set_title("Updates need missing system tools")
            row.set_subtitle(f"Install {', '.join(status.missing_update_tools)} first")
        elif self._checking_update:
            row.set_title("Checking for updates…")
        elif self._update is not None and self._update.update_available:
            version = self._update.available_version
            row.set_title(f"Keymasq {version} is available")
            row.set_subtitle(f"Installed: {self._update.installed_version}")
            notes = Gtk.LinkButton(uri=release_notes_url(version), label="Release Notes")
            notes.set_valign(Gtk.Align.CENTER)
            row.add_suffix(notes)
            update_button = self._row_button("Update", self._on_operation_clicked, "update")
            update_button.add_css_class("suggested-action")
            row.add_suffix(update_button)
        elif self._update is not None:
            row.set_title("Keymasq is up to date")
            row.add_suffix(self._row_button("Check Again", self._on_check_update_clicked))
        elif self._update_error:
            row.set_title("Could not check for updates")
            row.set_subtitle(self._update_error)
            row.add_suffix(self._row_button("Try Again", self._on_check_update_clicked))
        else:
            row.set_title("Check for a new version")
            row.add_suffix(self._row_button("Check Now", self._on_check_update_clicked))
        return row

    def _build_progress_page(self) -> Gtk.Widget:
        box = self._page_box(14)
        self._progress_title = self._wrapped_label("", "title-2")
        box.append(self._progress_title)

        step_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self._spinner = Gtk.Spinner()
        step_row.append(self._spinner)
        self._step_label = self._wrapped_label("")
        step_row.append(self._step_label)
        box.append(step_row)

        self._log_buffer = Gtk.TextBuffer()
        log_view = Gtk.TextView(buffer=self._log_buffer)
        log_view.set_editable(False)
        log_view.set_cursor_visible(False)
        log_view.set_monospace(True)
        log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        log_scroller = Gtk.ScrolledWindow()
        log_scroller.set_min_content_height(180)
        log_scroller.set_child(log_view)
        self._log_expander = Gtk.Expander(label="Details")
        self._log_expander.set_child(log_scroller)
        box.append(self._log_expander)

        self._progress_button = Gtk.Button()
        self._progress_button.set_halign(Gtk.Align.END)
        self._progress_button.add_css_class("suggested-action")
        self._progress_button.connect("clicked", self._on_progress_button_clicked)
        box.append(self._progress_button)
        return box

    def _on_setup_button_clicked(self, _button: Gtk.Button, action: SetupAction) -> None:
        if action == "quit":
            if self._on_quit is not None:
                self._on_quit(False)
        elif action == "open-installed":
            if self._on_open_installed is not None:
                self._on_open_installed()
        else:
            self._start_operation(action)

    def _on_operation_clicked(self, _button: Gtk.Button, operation: Operation) -> None:
        self._start_operation(operation)

    def _on_uninstall_clicked(self, _button: Gtk.Button) -> None:
        confirmation = Adw.AlertDialog(
            heading="Uninstall Keymasq?",
            body=(
                "This stops all remapping and removes Keymasq and its background services "
                "from this system. Your profiles, macros, and settings are kept."
            ),
        )
        confirmation.add_response("cancel", "Cancel")
        confirmation.add_response("uninstall", "Uninstall")
        confirmation.set_response_appearance("uninstall", Adw.ResponseAppearance.DESTRUCTIVE)
        confirmation.set_default_response("cancel")
        confirmation.set_close_response("cancel")
        confirmation.connect("response", self._on_uninstall_response)
        confirmation.present(self)

    def _on_uninstall_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        if response != "uninstall":
            return
        if self._before_uninstall is None:
            self._start_operation("uninstall")
        else:
            # The uninstaller stops the session that unsaved macro edits are saved through.
            self._before_uninstall(lambda: self._start_operation("uninstall"))

    def _on_automatic_check_toggled(self, row: Adw.SwitchRow, _pspec: object) -> None:
        save_update_check_enabled(row.get_active())

    def _on_check_update_clicked(self, _button: Gtk.Button) -> None:
        self.check_for_updates()

    def check_for_updates(self) -> None:
        if self._checking_update or self._operation_running:
            return
        self._checking_update = True
        self._update_error = ""
        self._rebuild_overview()
        self._run(self._runtime, ["--check-update"], None, self._on_update_check_finished)

    def _on_update_check_finished(self, exit_code: int, lines: list[str]) -> None:
        if self._closed:
            return
        self._checking_update = False
        check = parse_update_check("\n".join(lines)) if exit_code == 0 else None
        if check is None:
            self._update_error = failure_message(exit_code, lines, "keymasq --check-update")
        else:
            self._update = check
            if self._on_update_checked is not None:
                self._on_update_checked(check)
        if not self._operation_running and self._stack.get_visible_child_name() == "overview":
            self._rebuild_overview()

    def _refresh_status(self, then: Callable[[], None] | None = None) -> None:
        def on_exit(exit_code: int, lines: list[str]) -> None:
            if self._closed:
                return
            status = parse_install_status("\n".join(lines)) if exit_code == 0 else None
            if status is not None:
                self._set_status(status)
            if then is not None:
                then()

        self._run(self._runtime, ["--appimage-status"], None, on_exit)

    def _set_status(self, status: InstallStatus) -> None:
        if status == self._status:
            return
        self._status = status
        if self._on_status_changed is not None:
            self._on_status_changed(status)
        if not self._operation_running and self._stack.get_visible_child_name() == "overview":
            self._rebuild_overview()

    def _start_operation(self, operation: Operation) -> None:
        if self._operation_running:
            return
        copy = _OPERATION_COPY[operation]
        self._operation = operation
        self._operation_running = True
        self._operation_upgraded = self._status.installed
        self.set_can_close(False)
        self._progress_title.set_text(copy.running_title)
        self._step_label.remove_css_class("error")
        self._step_label.set_text("Starting…")
        self._log_buffer.set_text("")
        self._log_expander.set_expanded(False)
        self._progress_button.set_visible(False)
        self._spinner.set_visible(True)
        self._spinner.start()
        self._stack.set_visible_child_name("progress")
        self._run(
            self._runtime,
            operation_arguments(operation),
            self._on_operation_line,
            self._on_operation_exit,
        )

    def _on_operation_line(self, line: str) -> None:
        self._log_buffer.insert(self._log_buffer.get_end_iter(), f"{line}\n")
        step = step_from_line(line)
        if step is not None:
            self._step_label.set_text(f"{step}…")

    def _on_operation_exit(self, exit_code: int, lines: list[str]) -> None:
        operation = self._operation
        if operation is None:
            return
        if exit_code != 0:
            self._show_result(
                operation,
                failure_message(exit_code, lines, self._runtime.terminal_command(operation)),
                failed=True,
            )
            return
        if operation == "uninstall":
            # The uninstaller removed the runtime this process runs from.
            self._show_result(operation, success_message(operation, None, False), failed=False)
            return

        def finish() -> None:
            if operation == "update" and self._update is not None:
                # The launcher of the release just installed may not report a status.
                self._set_status(
                    replace(self._status, installed_version=self._update.available_version)
                )
            self._show_result(
                operation,
                success_message(operation, self._status, self._operation_upgraded),
                failed=False,
            )

        self._refresh_status(finish)

    def _show_result(self, operation: Operation, message: str, *, failed: bool) -> None:
        copy = _OPERATION_COPY[operation]
        self._operation_running = False
        self._spinner.stop()
        self._spinner.set_visible(False)
        self._progress_title.set_text(copy.failure_title if failed else copy.success_title)
        self._step_label.set_text(message)
        if failed:
            self._step_label.add_css_class("error")
            self._log_expander.set_expanded(True)
            self._finish_action = "back"
            label = "Back"
        elif operation == "uninstall":
            self._finish_action = "quit"
            label = "Quit"
        elif operation == "update":
            self._finish_action = "restart"
            label = "Restart Keymasq"
        else:
            self._log_expander.set_expanded(not self._status.systemd)
            self._finish_action = "done"
            label = "Done"
        self.set_can_close(self._finish_action != "quit")
        self._progress_button.set_label(label)
        self._progress_button.set_visible(True)

    def _on_progress_button_clicked(self, _button: Gtk.Button) -> None:
        if self._finish_action == "quit":
            if self._on_quit is not None:
                self._on_quit(True)
            return
        if self._finish_action == "restart":
            if self._on_open_installed is not None:
                self._on_open_installed()
            return
        if self._finish_action == "done" and self._opened_for_setup:
            if self._need() is SetupNeed.NONE:
                self.close()
                return
        self._rebuild_overview()
        self._stack.set_visible_child_name("overview")
