import argparse
import os
from importlib import resources

from keymasq import __version__

# Libadwaita applications should not inherit a forced GTK theme override.
# Let Adw.StyleManager and the session settings drive appearance instead.
os.environ.pop("GTK_THEME", None)

import gi  # pyright: ignore[reportMissingImports]

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import (  # pyright: ignore[reportMissingImports, reportAttributeAccessIssue]  # noqa: E402
    Adw,  # pyright: ignore[reportAttributeAccessIssue]
    Gdk,  # pyright: ignore[reportAttributeAccessIssue]
    Gio,  # pyright: ignore[reportAttributeAccessIssue]
    Gtk,  # pyright: ignore[reportAttributeAccessIssue]
)

from keymasq.common.paths import ensure_config_dirs  # noqa: E402
from keymasq.gui.icons import register_icon_search_path, theme_supports_core_icons  # noqa: E402
from keymasq.gui.preferences import (  # noqa: E402
    AppearanceMode,
    load_appearance_mode,
    save_appearance_mode,
)
from keymasq.gui.session_reload import notify_session_reload_async  # noqa: E402
from keymasq.gui.widgets.docs_links import docs_page_url  # noqa: E402
from keymasq.gui.window.core import MainWindow  # noqa: E402

APP_VERSION = __version__
APP_ID = "tools.keymasq.keymasq"
APP_ICON_NAME = APP_ID

COLOR_SCHEME_BY_APPEARANCE: dict[AppearanceMode, Adw.ColorScheme] = {
    "system": Adw.ColorScheme.DEFAULT,
    "light": Adw.ColorScheme.FORCE_LIGHT,
    "dark": Adw.ColorScheme.FORCE_DARK,
}


def _docs_url() -> str:
    return docs_page_url(version=APP_VERSION)


class Application(Adw.Application):
    def __init__(self, demo_mode: bool = False) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.demo_mode = demo_mode
        self.window: MainWindow | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)

        provider = Gtk.CssProvider()
        with resources.as_file(resources.files("keymasq").joinpath("gui/style.css")) as css_path:
            provider.load_from_path(str(css_path))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        # Ensure Adwaita icon theme is available on non-GNOME Wayland compositors
        # where xdg-desktop-portal may not expose GTK settings correctly
        # (e.g. icon-theme resolves to non-existent "gnome" instead of "Adwaita").
        register_icon_search_path()
        if not theme_supports_core_icons():
            settings = Gtk.Settings.get_default()
            if settings:
                settings.set_property("gtk-icon-theme-name", "Adwaita")

        self.apply_appearance_mode(load_appearance_mode(), persist=False)

        superkeys_action = Gio.SimpleAction.new("superkeys", None)
        superkeys_action.connect("activate", self._on_superkeys)
        self.add_action(superkeys_action)

        analog_controls_action = Gio.SimpleAction.new("analog-controls", None)
        analog_controls_action.connect("activate", self._on_analog_controls)
        self.add_action(analog_controls_action)

        macros_action = Gio.SimpleAction.new("macros", None)
        macros_action.connect("activate", self._on_macros)
        self.add_action(macros_action)
        self.set_accels_for_action("app.macros", ["<Control>m"])

        record_macro_action = Gio.SimpleAction.new("record-macro", None)
        record_macro_action.connect("activate", self._on_record_macro)
        self.add_action(record_macro_action)

        diagnostics_action = Gio.SimpleAction.new("diagnostics", None)
        diagnostics_action.connect("activate", self._on_diagnostics)
        self.add_action(diagnostics_action)

        settings_action = Gio.SimpleAction.new("settings", None)
        settings_action.connect("activate", self._on_settings)
        self.add_action(settings_action)

        feedback_action = Gio.SimpleAction.new("feedback", None)
        feedback_action.connect("activate", self._on_feedback)
        self.add_action(feedback_action)

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._on_about)
        self.add_action(about_action)

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", self._on_quit)
        self.add_action(quit_action)

    def do_activate(self) -> None:
        ensure_config_dirs()

        if not self.window:
            self.window = MainWindow(application=self, demo_mode=self.demo_mode)

        self.window.present()

    def do_shutdown(self) -> None:
        from keymasq.gui.session_client import shutdown_gui_runtime

        shutdown_gui_runtime()
        Adw.Application.do_shutdown(self)

    def _on_superkeys(self, action, param) -> None:
        self._open_superkey_dialog()

    def _open_superkey_dialog(self) -> None:
        if not self.window:
            return

        from keymasq.gui.widgets.superkey_editor.dialog import SuperkeyDialog

        dialog = SuperkeyDialog(self.window, self.window.profile_manager)
        dialog.connect("superkey-saved", self._on_superkey_changed)
        dialog.connect("superkey-deleted", self._on_superkey_changed)
        dialog.present(self.window)

    def _on_superkey_changed(self, dialog, name: str) -> None:
        notify_session_reload_async()

    def _on_analog_controls(self, action, param) -> None:
        self._open_analog_control_dialog()

    def _open_analog_control_dialog(self) -> None:
        if not self.window:
            return

        from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

        dialog = AnalogControlDialog(self.window, self.window.profile_manager)
        dialog.connect("analog-control-saved", self._on_analog_control_changed)
        dialog.connect("analog-control-deleted", self._on_analog_control_changed)
        dialog.present(self.window)

    def _on_analog_control_changed(self, dialog, name: str) -> None:
        notify_session_reload_async()

    def _on_macros(self, action, param) -> None:
        if not self.window:
            return
        from keymasq.gui.widgets.macro_manager_dialog import MacroManagerDialog

        window = self.window
        dialog = MacroManagerDialog(window)
        window.set_macro_manager_dialog(dialog)
        dialog.connect("closed", self._on_macro_manager_closed, window)
        dialog.present(window)

    def _on_macro_manager_closed(self, _dialog: Adw.Dialog, window: MainWindow) -> None:
        window.set_macro_manager_dialog(None)

    def _on_record_macro(self, action, param) -> None:
        if not self.window:
            return
        self.window.present_recording_settings_dialog()

    def _on_diagnostics(self, action, param) -> None:
        if not self.window:
            return
        from keymasq.gui.widgets.diagnostics_dialog import DiagnosticsDialog

        dialog = DiagnosticsDialog(self.window)
        dialog.present(self.window)

    def _on_settings(self, action, param) -> None:
        if not self.window:
            return
        from keymasq.gui.widgets.settings_dialog import SettingsDialog

        dialog = SettingsDialog(self.window)
        dialog.present(self.window)

    def _on_feedback(self, action, param) -> None:
        if not self.window:
            return
        from keymasq.gui.widgets.feedback_dialog import FeedbackDialog

        dialog = FeedbackDialog(self.window)
        dialog.present(self.window)

    def _on_about(self, action, param) -> None:
        if self.window:
            dialog = Adw.AboutDialog(
                application_name="Keymasq",
                application_icon=APP_ICON_NAME,
                version=APP_VERSION,
            )
            dialog.add_link("Website", "https://keymasq.tools/")
            dialog.add_link("Documentation", _docs_url())
            dialog.add_link("License", "https://github.com/nyrda/keymasq/blob/main/LICENSE")
            dialog.present(self.window)

    def _on_quit(self, action, param) -> None:
        self.quit()

    def apply_appearance_mode(self, mode: AppearanceMode, *, persist: bool = True) -> None:
        Adw.StyleManager.get_default().set_color_scheme(COLOR_SCHEME_BY_APPEARANCE[mode])
        if persist:
            save_appearance_mode(mode)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="keymasq",
        description="Keymasq - Key remapping tool GUI",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run in demo mode with sample data (no system access needed)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {APP_VERSION}",
    )

    args, _ = parser.parse_known_args()

    app = Application(demo_mode=args.demo)
    app.run()


if __name__ == "__main__":
    main()
