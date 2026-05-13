import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import (  # pyright: ignore[reportAttributeAccessIssue]
    Adw,  # pyright: ignore[reportAttributeAccessIssue]
    Gtk,  # pyright: ignore[reportAttributeAccessIssue]
)

from keymasq.common.virtual_devices import MAX_VIRTUAL_GAMEPADS, MIN_VIRTUAL_GAMEPADS
from keymasq.gui.session_client import session_request_async
from keymasq.session.virtual_devices import (
    load_virtual_gamepad_count,
    save_virtual_gamepad_count,
)


class VirtualGamepadsDialog(Adw.Dialog):
    def __init__(self, parent: Gtk.Window | None = None) -> None:
        super().__init__(title="Virtual Gamepads")
        self.set_default_size(420, 220)
        self._parent = parent

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()
        page.add(group)

        row = Adw.ActionRow(title="Virtual gamepads")
        row.set_subtitle("Xbox 360 virtual controller outputs")
        adjustment = Gtk.Adjustment(
            value=load_virtual_gamepad_count(),
            lower=MIN_VIRTUAL_GAMEPADS,
            upper=MAX_VIRTUAL_GAMEPADS,
            step_increment=1,
            page_increment=1,
            page_size=0,
        )
        self._spin = Gtk.SpinButton(adjustment=adjustment, climb_rate=1, digits=0)
        self._spin.set_numeric(True)
        self._spin.set_valign(Gtk.Align.CENTER)
        row.add_suffix(self._spin)
        group.add(row)

        self._status = Gtk.Label(label="")
        self._status.set_xalign(0)
        self._status.add_css_class("dim-label")
        group.add(self._status)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        actions.set_halign(Gtk.Align.END)
        actions.set_margin_top(12)
        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", self._on_close_clicked)
        apply_btn = Gtk.Button(label="Apply")
        apply_btn.add_css_class("suggested-action")
        apply_btn.connect("clicked", self._on_apply_clicked)
        actions.append(close_btn)
        actions.append(apply_btn)
        group.add(actions)

        toolbar.set_content(page)
        self.set_child(toolbar)
        session_request_async(
            {"command": "get_virtual_gamepads"},
            self._on_loaded,
            timeout=1.0,
        )

    def _count(self) -> int:
        value = int(round(self._spin.get_value()))
        return max(MIN_VIRTUAL_GAMEPADS, min(MAX_VIRTUAL_GAMEPADS, value))

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _on_loaded(self, response: dict[str, object] | None) -> bool:
        if isinstance(response, dict) and response.get("status") == "ok":
            try:
                raw_count = response.get("count", load_virtual_gamepad_count())
                count = raw_count if isinstance(raw_count, (int, float, str)) else 1
                self._spin.set_value(float(count))
            except (TypeError, ValueError):
                self._spin.set_value(float(load_virtual_gamepad_count()))
        return False

    def _on_apply_clicked(self, _button: Gtk.Button) -> None:
        count = self._count()
        self._spin.set_value(float(count))

        def on_response(response: dict[str, object] | None) -> bool:
            if isinstance(response, dict) and response.get("status") == "ok":
                raw_saved = response.get("count", count)
                saved = int(raw_saved if isinstance(raw_saved, (int, float, str)) else count)
                self._status.set_text(f"Saved {saved} virtual gamepad(s)")
                return False
            saved = save_virtual_gamepad_count(count)
            self._status.set_text(f"Saved {saved} virtual gamepad(s)")
            return False

        session_request_async(
            {"command": "set_virtual_gamepads", "count": count},
            on_response,
            timeout=1.0,
        )
