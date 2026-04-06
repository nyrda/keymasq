from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GObject, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keyforge.common.models import ProfileConfig
from keyforge.session.profiles import ProfileManager


class ProfileCreateDialog(Adw.Window):
    __gsignals__ = {
        "profile-created": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(
        self,
        parent,
        profile_manager: ProfileManager,
    ) -> None:
        super().__init__(
            title="Create Profile",
            transient_for=parent,
            modal=True,
            default_width=350,
            default_height=150,
        )

        self.profile_manager = profile_manager

        self._setup_ui()

    def _setup_ui(self) -> None:
        header = Adw.HeaderBar()

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_cancel_clicked)
        header.pack_start(cancel_btn)

        save_btn = Gtk.Button(label="Create")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._on_create)
        header.pack_end(save_btn)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)

        title = Gtk.Label(label="New Profile")
        title.add_css_class("title-2")
        box.append(title)

        name_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        name_label = Gtk.Label(label="Name:")
        name_label.set_size_request(80, -1)
        name_label.set_halign(Gtk.Align.START)
        self.name_entry = Gtk.Entry()
        self.name_entry.set_placeholder_text("e.g., Gaming, Work, FPS")
        self.name_entry.set_hexpand(True)
        self.name_entry.connect("activate", self._on_create)
        name_box.append(name_label)
        name_box.append(self.name_entry)
        box.append(name_box)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(box)

        self.set_content(toolbar)

    def _on_cancel_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _show_error_dialog(self, message: str) -> None:
        dialog = Adw.AlertDialog(
            heading="Cannot Create Profile",
            body=message,
        )
        dialog.add_response("ok", "OK")
        dialog.present(self)

    def _on_create(self, button_or_entry) -> None:
        name = self.name_entry.get_text().strip()
        if not name:
            return

        profile = ProfileConfig(
            name=name,
            enabled=True,
            is_permanent=True,
            priority=self.profile_manager.get_next_priority(),
            notify_on_activation=False,
            created_at=datetime.now(),
        )

        try:
            self.profile_manager.save_profile(profile)
        except ValueError as exc:
            self._show_error_dialog(str(exc))
            return

        self.emit("profile-created", name)
        self.close()
