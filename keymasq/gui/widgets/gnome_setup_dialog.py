import logging
from collections.abc import Callable
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq import __version__
from keymasq.gui.session_client import JsonDict, session_request_async
from keymasq.gui.widgets.docs_links import docs_page_url

GNOME_BRIDGE_UUID = "gnome-bridge@keymasq.tools"
log = logging.getLogger("keymasq.gui.widgets.gnome_setup_dialog")


GNOME_SETUP_DOCS_URL = docs_page_url("GNOME", version=__version__)


@dataclass(frozen=True)
class _DialogCopy:
    title: str
    body: str
    primary_label: str | None
    action: str | None
    close_label: str | None = "Not Now"
    docs_label: str | None = "Setup Guide"
    show_docs: bool = True


def _state_copy(state: str, action: str) -> _DialogCopy:
    bridge_reason = (
        "Key remapping still works. Window-aware profiles, GNOME window actions, "
        "and native pointer positioning need the Keymasq GNOME Shell bridge."
    )
    if state == "missing_files":
        return _DialogCopy(
            title="GNOME Bridge Is Not Installed",
            body=(
                "Keymasq cannot find its GNOME Shell extension files. Reinstall the "
                f"Keymasq package, or follow the setup guide to install {GNOME_BRIDGE_UUID}."
            ),
            primary_label=None,
            action=None,
        )
    if state == "shell_not_rescanned":
        return _DialogCopy(
            title="Finish GNOME Setup",
            body=(
                "Keymasq needs a GNOME extension for window-aware profiles, GNOME window "
                "actions, and native pointer positioning. The extension is installed; log "
                "out and back in once so GNOME can load it."
            ),
            primary_label="Log Out",
            action="logout",
            close_label=None,
        )
    if state == "extensions_disabled":
        return _DialogCopy(
            title="GNOME Shell Extensions Are Disabled",
            body=(
                "GNOME Shell extensions are disabled for this session. Enable extensions, "
                f"then enable {GNOME_BRIDGE_UUID}."
            ),
            primary_label="Enable Extensions",
            action="enable_extensions",
        )
    if state == "bridge_disabled":
        return _DialogCopy(
            title="Enable GNOME Bridge",
            body=(
                "Enable the bridge for window-aware profiles, GNOME window actions, "
                "and native pointer positioning."
            ),
            primary_label="Enable Bridge",
            action="enable_bridge",
            docs_label=None,
            show_docs=False,
        )
    if state == "shell_dbus_unavailable":
        return _DialogCopy(
            title="Refresh GNOME Shell Bridge",
            body=(
                "Keymasq could not query GNOME Shell extension state yet. Refresh after "
                "GNOME Shell has finished loading."
            ),
            primary_label="Refresh",
            action="refresh",
        )
    if state == "protocol_stale":
        return _DialogCopy(
            title="Reload GNOME Shell Bridge",
            body=(
                "GNOME Shell is still running an older Keymasq bridge. Log out and back "
                "in to load the updated extension."
            ),
            primary_label="Log Out...",
            action="logout",
        )
    if state == "protocol_newer":
        return _DialogCopy(
            title="Restart Keymasq Session",
            body=(
                "The GNOME bridge is newer than keymasq-session. Restart the Keymasq "
                "session service after updating Keymasq."
            ),
            primary_label="Restart Session Service",
            action="restart_session",
        )
    if action:
        return _DialogCopy(
            title="Set Up GNOME Shell Bridge",
            body=bridge_reason,
            primary_label="Continue",
            action=action,
        )
    return _DialogCopy(
        title="Set Up GNOME Shell Bridge",
        body=bridge_reason,
        primary_label=None,
        action=None,
    )


class GnomeSetupDialog(Adw.Dialog):
    def __init__(
        self,
        parent: Gtk.Window,
        support_details: dict[str, object],
        on_action_completed: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(title="GNOME Setup", content_width=520, content_height=-1)
        if hasattr(self, "set_modal"):
            self.set_modal(True)
        self._parent = parent
        self._support_details = support_details
        self._on_action_completed = on_action_completed
        self._primary_button: Gtk.Button | None = None
        self._status_label: Gtk.Label | None = None
        self._pending_action = ""
        self._build_ui()

    def _build_ui(self) -> None:
        state = str(self._support_details.get("gnome_bridge_state", "") or "")
        action = str(self._support_details.get("gnome_bridge_action", "") or "")
        copy = _state_copy(state, action)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(20)
        box.set_margin_end(20)

        title = Gtk.Label(label=copy.title)
        title.add_css_class("title-2")
        title.set_halign(Gtk.Align.START)
        title.set_wrap(True)
        box.append(title)

        body = Gtk.Label(label=copy.body)
        body.set_halign(Gtk.Align.START)
        body.set_wrap(True)
        body.set_xalign(0.0)
        box.append(body)

        status = Gtk.Label()
        status.add_css_class("caption")
        status.set_halign(Gtk.Align.START)
        status.set_wrap(True)
        status.set_visible(False)
        box.append(status)
        self._status_label = status

        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        button_row.set_halign(Gtk.Align.END)

        if copy.show_docs:
            docs_btn = Gtk.Button(label=copy.docs_label or "Setup Guide")
            docs_btn.connect("clicked", self._on_open_docs_clicked)
            button_row.append(docs_btn)

        if copy.close_label:
            close_btn = Gtk.Button(label=copy.close_label)
            close_btn.connect("clicked", self._on_close_clicked)
            button_row.append(close_btn)

        if copy.primary_label and copy.action:
            primary = Gtk.Button(label=copy.primary_label)
            primary.add_css_class("suggested-action")
            primary.connect("clicked", self._on_primary_clicked, copy.action)
            button_row.append(primary)
            self._primary_button = primary

        box.append(button_row)
        self.set_child(box)

    def _set_status(self, message: str, error: bool = False) -> None:
        if self._status_label is None:
            return
        self._status_label.remove_css_class("error")
        self._status_label.remove_css_class("success")
        if error:
            self._status_label.add_css_class("error")
        else:
            self._status_label.add_css_class("success")
        self._status_label.set_text(message)
        self._status_label.set_visible(True)

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _on_open_docs_clicked(self, _button: Gtk.Button) -> None:
        try:
            launcher = Gtk.UriLauncher.new(GNOME_SETUP_DOCS_URL)
            launcher.launch(self._parent, None, None)
        except Exception:
            log.exception("Could not open GNOME setup guide %s", GNOME_SETUP_DOCS_URL)
            self._set_status(
                f"Could not open the setup guide. Visit {GNOME_SETUP_DOCS_URL}",
                error=True,
            )

    def _on_primary_clicked(self, _button: Gtk.Button, action: str) -> None:
        if self._primary_button is not None:
            self._primary_button.set_sensitive(False)
        self._pending_action = action
        self._set_status("Working...")
        session_request_async(
            {
                "command": "run_compositor_setup_action",
                "compositor": "gnome",
                "action": action,
            },
            self._on_action_response,
            timeout=5.0,
        )

    def _on_action_response(self, response: JsonDict | None) -> bool:
        if self._primary_button is not None:
            self._primary_button.set_sensitive(True)

        if response is None:
            if self._pending_action == "restart_session":
                self._set_status("keymasq-session is restarting...")
                if self._on_action_completed is not None:
                    self._on_action_completed(self._pending_action)
                return False
            self._set_status("keymasq-session did not respond.", error=True)
            return False

        ok = str(response.get("status", "") or "") == "ok"
        message = str(response.get("message", "") or "")
        if not message:
            message = "Done." if ok else "Action failed."
        self._set_status(message, error=not ok)
        if ok and self._on_action_completed is not None:
            self._on_action_completed(self._pending_action)
        return False
