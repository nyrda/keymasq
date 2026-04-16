import argparse
import os
from importlib import resources

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
from keymasq.gui.session_reload import notify_session_reload_async  # noqa: E402
from keymasq.gui.window import MainWindow  # noqa: E402

APP_VERSION = "0.4.1"
APP_ID = "tools.keymasq.Keymasq"
APP_ICON_NAME = APP_ID


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

        superkeys_action = Gio.SimpleAction.new("superkeys", None)
        superkeys_action.connect("activate", self._on_superkeys)
        self.add_action(superkeys_action)

        macros_action = Gio.SimpleAction.new("macros", None)
        macros_action.connect("activate", self._on_macros)
        self.add_action(macros_action)
        self.set_accels_for_action("app.macros", ["<Control>m"])

        record_macro_action = Gio.SimpleAction.new("record-macro", None)
        record_macro_action.connect("activate", self._on_record_macro)
        self.add_action(record_macro_action)

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

    def _on_superkeys(self, action, param) -> None:
        self._open_superkey_dialog()

    def _open_superkey_dialog(self) -> None:
        if not self.window:
            return

        from keymasq.gui.widgets.superkey_dialog import SuperkeyDialog

        dialog = SuperkeyDialog(self.window, self.window.profile_manager)
        dialog.connect("superkey-saved", self._on_superkey_changed)
        dialog.connect("superkey-deleted", self._on_superkey_changed)
        dialog.present()

    def _on_superkey_changed(self, dialog, name: str) -> None:
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

    def _on_about(self, action, param) -> None:
        if self.window:
            dialog = Adw.AboutDialog()
            dialog.set_application_name("Keymasq")
            dialog.set_application_icon(APP_ICON_NAME)
            dialog.set_version(APP_VERSION)
            dialog.set_comments("A key remapping tool for Linux")
            dialog.set_developer_name("Keymasq Team")
            dialog.set_license_type(Gtk.License.MIT_X11)
            dialog.present(self.window)

    def _on_quit(self, action, param) -> None:
        self.quit()


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
