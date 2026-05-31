import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import (  # pyright: ignore[reportAttributeAccessIssue]
    Adw,  # pyright: ignore[reportAttributeAccessIssue]
    Gtk,  # pyright: ignore[reportAttributeAccessIssue]
)

from keymasq.common.settings import GlobalSettings
from keymasq.common.virtual_devices import (
    MAX_VIRTUAL_GAMEPADS,
    MIN_VIRTUAL_GAMEPADS,
    clamp_virtual_gamepad_count,
)
from keymasq.gui.session_client import session_request_async
from keymasq.session.settings import load_global_settings, save_global_settings


def _count_value(value: object, default: int) -> int:
    try:
        return int(value if isinstance(value, (int, float, str)) else default)
    except (TypeError, ValueError):
        return default


class SettingsDialog(Adw.Dialog):
    def __init__(self, parent: Gtk.Window | None = None) -> None:
        super().__init__(title="Settings", content_width=460, content_height=380)
        self._parent = parent
        self._settings = load_global_settings()
        self._gamepad_count = self._settings.virtual_gamepad_count
        self._syncing_controls = False
        self._save_seq = 0

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()

        gamepad_group = Adw.PreferencesGroup(title="Virtual Devices")
        page.add(gamepad_group)

        gamepad_row = Adw.ActionRow(title="Virtual gamepads")
        gamepad_row.set_subtitle("Set virtual controller outputs")
        count_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        count_box.set_valign(Gtk.Align.CENTER)
        self._minus_button = Gtk.Button(icon_name="list-remove-symbolic")
        self._minus_button.set_tooltip_text("Remove virtual gamepad")
        self._minus_button.connect("clicked", self._on_decrement_gamepads_clicked)
        self._count_label = Gtk.Label()
        self._count_label.set_width_chars(2)
        self._count_label.set_xalign(0.5)
        self._plus_button = Gtk.Button(icon_name="list-add-symbolic")
        self._plus_button.set_tooltip_text("Add virtual gamepad")
        self._plus_button.connect("clicked", self._on_increment_gamepads_clicked)
        count_box.append(self._minus_button)
        count_box.append(self._count_label)
        count_box.append(self._plus_button)
        gamepad_row.add_suffix(count_box)
        gamepad_group.add(gamepad_row)
        self._sync_gamepad_count_controls()

        macro_group = Adw.PreferencesGroup(title="Macros")
        page.add(macro_group)

        macro_row = Adw.ActionRow(title="Macro recording")
        macro_row.set_subtitle("Recording sources and opt-in state")
        macro_btn = Gtk.Button(icon_name="go-next-symbolic")
        macro_btn.set_tooltip_text("Open macro recording settings")
        macro_btn.set_valign(Gtk.Align.CENTER)
        macro_btn.connect("clicked", self._on_macro_settings_clicked)
        macro_row.add_suffix(macro_btn)
        macro_group.add(macro_row)
        self._macro_settings_btn = macro_btn

        footer = Gtk.ActionBar()
        self._status = Gtk.Label(label="")
        self._status.set_xalign(0)
        self._status.set_hexpand(True)
        self._status.add_css_class("dim-label")
        footer.pack_start(self._status)
        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", self._on_close_clicked)
        footer.pack_end(close_btn)
        toolbar.add_bottom_bar(footer)

        toolbar.set_content(page)
        self.set_child(toolbar)
        session_request_async(
            {"command": "get_settings"},
            self._on_loaded,
            timeout=1.0,
        )

    def _count(self) -> int:
        return clamp_virtual_gamepad_count(self._gamepad_count)

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _on_macro_settings_clicked(self, _button: Gtk.Button) -> None:
        present_settings = getattr(self._parent, "present_recording_settings_dialog", None)
        if callable(present_settings):
            present_settings(reason="settings")
            return
        if self._parent is None:
            self._status.set_text("Macro recording settings are available from the main window")
            return

        from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

        dialog = RecordMacroDialog(self._parent)
        dialog.present(self._parent)

    def _on_loaded(self, response: dict[str, object] | None) -> bool:
        if self._save_seq > 0:
            return False
        if isinstance(response, dict) and response.get("status") == "ok":
            self._syncing_controls = True
            try:
                raw_count = response.get(
                    "virtual_gamepad_count",
                    self._settings.virtual_gamepad_count,
                )
                count = _count_value(raw_count, self._settings.virtual_gamepad_count)
                self._gamepad_count = clamp_virtual_gamepad_count(count)
            except (TypeError, ValueError):
                self._gamepad_count = self._settings.virtual_gamepad_count
            self._sync_gamepad_count_controls()
            self._syncing_controls = False
        return False

    def _on_increment_gamepads_clicked(self, _button: Gtk.Button) -> None:
        self._set_gamepad_count(self._gamepad_count + 1)

    def _on_decrement_gamepads_clicked(self, _button: Gtk.Button) -> None:
        self._set_gamepad_count(self._gamepad_count - 1)

    def _set_gamepad_count(self, count: int) -> None:
        normalized = clamp_virtual_gamepad_count(count)
        if normalized == self._gamepad_count:
            return
        self._gamepad_count = normalized
        self._sync_gamepad_count_controls()
        self._save_settings()

    def _sync_gamepad_count_controls(self) -> None:
        count = self._count()
        self._count_label.set_text(str(count))
        self._minus_button.set_sensitive(count > MIN_VIRTUAL_GAMEPADS)
        self._plus_button.set_sensitive(count < MAX_VIRTUAL_GAMEPADS)

    def _save_settings(self) -> None:
        count = self._count()
        self._gamepad_count = count
        self._sync_gamepad_count_controls()
        self._save_seq += 1
        save_seq = self._save_seq
        self._status.set_text("")

        def on_response(response: dict[str, object] | None) -> bool:
            if save_seq != self._save_seq:
                return False
            if isinstance(response, dict) and response.get("status") == "ok":
                raw_saved = response.get("virtual_gamepad_count", count)
                saved_count = _count_value(raw_saved, count)
                self._syncing_controls = True
                self._gamepad_count = clamp_virtual_gamepad_count(saved_count)
                self._sync_gamepad_count_controls()
                self._syncing_controls = False
                return False
            if isinstance(response, dict):
                message = str(response.get("message") or "Failed to apply settings")
                self._status.set_text(message)
                return False
            saved = save_global_settings(
                GlobalSettings(
                    virtual_gamepad_count=count,
                )
            )
            self._syncing_controls = True
            self._gamepad_count = saved.virtual_gamepad_count
            self._sync_gamepad_count_controls()
            self._syncing_controls = False
            return False

        session_request_async(
            {
                "command": "set_settings",
                "virtual_gamepad_count": count,
            },
            on_response,
            timeout=1.0,
        )
