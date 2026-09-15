"""Device masking switches with inline confirmation and recovery feedback."""

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk, Pango  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.masking import MaskPhase, is_active, is_recovering
from keymasq.gui.session_client import session_request_async

Change = Callable[[str, dict], None]


def connect_button(button: Gtk.Button, action: Callable[[], object]) -> None:
    def clicked(_button: Gtk.Button) -> None:
        action()

    button.connect("clicked", clicked)


def failure_message(mask: dict) -> str:
    if mask.get("state") == MaskPhase.RECOVERY_FAILED:
        return "Device access could not be restored yet. Keymasq is retrying."
    if mask.get("state") == MaskPhase.RESTORED and mask.get("reason") == "user_restore":
        return ""
    error = str(mask.get("error", ""))
    if mask.get("error_code") == "device_in_use" or "direct USB or auxiliary HID" in error:
        app = str(mask.get("blocking_application") or "Another app")
        app = {
            "steam": "Steam",
            "steamwebhelper": "Steam",
            "winedevice.exe": "Wine",
            "wine64-preloader": "Wine",
            "wine-preloader": "Wine",
        }.get(app, app)
        return f"{app} is using this device directly. Close it, then retry."
    return (
        "Masking could not be started. Retry, or open device details for more information."
        if error
        else ""
    )


class MaskDeviceRow(Adw.PreferencesRow):
    def __init__(self, change: Change, details_group: Adw.ExpanderRow | None = None) -> None:
        super().__init__()
        self._change = change
        self._syncing = False
        self.device: dict = {}
        self.mask: dict = {}
        self.enabled = False
        self._details_group = details_group
        self.diagnostics = ""
        self.set_activatable(False)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.set_margin_top(8)
        content.set_margin_bottom(8)
        content.set_margin_start(12)
        content.set_margin_end(12)
        self.set_child(content)
        header = Gtk.Box(spacing=12)
        self.name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, hexpand=True)
        attributes = Pango.AttrList()
        attributes.insert(Pango.attr_weight_new(Pango.Weight.NORMAL))
        self.name.set_attributes(attributes)
        self.expand = Gtk.Button(hexpand=True, valign=Gtk.Align.CENTER)
        self.expand.add_css_class("flat")
        title = Gtk.Box(spacing=12)
        title.append(self.name)
        self.chevron = Gtk.Image(icon_name="pan-end-symbolic")
        title.append(self.chevron)
        self.expand.set_child(title)
        connect_button(
            self.expand, lambda: self.set_details_expanded(not self.details.get_visible())
        )
        if details_group is None:
            header.append(self.expand)
        else:
            title.remove(self.name)
            header.append(self.name)
        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("dim-label")
        header.append(self.status)

        self.switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.switch.connect("notify::active", self._toggled)
        header.append(self.switch)
        content.append(header)

        self.failure = Gtk.Box(spacing=16)
        self.error = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self.failure.append(self.error)
        self.retry = Gtk.Button(label="Retry", valign=Gtk.Align.CENTER)
        connect_button(self.retry, self.turn_on)
        self.failure.append(self.retry)
        content.append(self.failure)

        self.confirmation = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.confirmation.append(
            Gtk.Label(label="Does your input still work?", xalign=0, wrap=True)
        )
        actions = Gtk.Box(spacing=8)
        self.keep = Gtk.Button(label="Keep masking")
        self.keep.add_css_class("suggested-action")
        connect_button(self.keep, self.confirm)
        self.undo = Gtk.Button(label="Undo")
        connect_button(self.undo, self.turn_off)
        actions.append(self.keep)
        actions.append(self.undo)
        self.confirmation.append(actions)
        content.append(self.confirmation)

        self.details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.technical = Gtk.Label(xalign=0, wrap=True)
        self.technical.add_css_class("dim-label")
        self.details.append(self.technical)
        self.copy = Gtk.Button(label="Copy diagnostics", halign=Gtk.Align.START)
        connect_button(self.copy, self._copy_diagnostics)
        self.details.append(self.copy)
        self.details_row = Adw.PreferencesRow()
        if details_group is None:
            self.details.set_visible(False)
            content.append(self.details)
        else:
            for setter in (
                self.details.set_margin_top,
                self.details.set_margin_bottom,
                self.details.set_margin_start,
                self.details.set_margin_end,
            ):
                setter(12)
            self.details_row.set_child(self.details)
            details_group.add_row(self.details_row)
        self.set_details_expanded(False)

    def set_details_expanded(self, expanded: bool) -> None:
        if self._details_group is None:
            self.details.set_visible(expanded)
        self.chevron.set_from_icon_name("pan-down-symbolic" if expanded else "pan-end-symbolic")
        self.expand.update_state([Gtk.AccessibleState.EXPANDED], [expanded])

    def _copy_diagnostics(self) -> None:
        self.get_clipboard().set(self.diagnostics)
        self.copy.set_label("Copied")

    def remove_details(self) -> None:
        if self._details_group is not None:
            self._details_group.remove(self.details_row)

    def _toggled(self, _switch: Gtk.Switch, _param: object) -> None:
        if self._syncing:
            return
        desired = self.switch.get_active()
        if desired == self.enabled:
            return
        # Until authentication and the helper accept the request, retain the
        # observed state. Cancelling the unlock dialog must not leave a false ON.
        self._syncing = True
        self.switch.set_active(self.enabled)
        self._syncing = False
        if desired:
            self.turn_on()
        else:
            self.turn_off()

    def turn_on(self) -> None:
        data: dict = {"id": self.device["id"], "persist": True}
        if self.device.get("supported"):
            data["generation"] = self.device.get("generation")
        self._change("mask_hardware", data)

    def turn_off(self) -> None:
        self._change(
            "restore_hardware",
            {"id": self.device["id"], "token": self.mask.get("token"), "persist": False},
        )

    def confirm(self) -> None:
        self._change(
            "keep_hardware_mask",
            {"id": self.device["id"], "token": self.mask.get("token"), "persist": True},
        )

    def update(
        self,
        device: dict,
        mask: dict,
        *,
        available: bool,
        paused: bool,
        busy: bool,
        request_error: str = "",
    ) -> None:
        self.device, self.mask = device, mask
        state = mask.get("state", MaskPhase.UNMASKED)
        connected = bool(device.get("supported"))
        active = is_active(state)
        recovering = is_recovering(state)
        enabled = bool(
            mask.get(
                "enabled",
                active
                or (
                    mask.get("has_saved_mask")
                    and mask.get("persist")
                    and not mask.get("remapping_suspended")
                ),
            )
        )
        self.enabled = enabled and not paused
        self._syncing = True
        self.switch.set_active(self.enabled)
        self._syncing = False
        can_enable = connected or bool(mask.get("has_saved_mask"))
        self.switch.set_sensitive(
            available
            and not busy
            and not recovering
            and not paused
            and (active or enabled or can_enable)
        )
        self.name.set_text(str(device.get("name", "Input device")))
        self.name.set_tooltip_text(self.name.get_text())
        self.expand.update_property(
            [Gtk.AccessibleProperty.LABEL], [f"Details for {self.name.get_text()}"]
        )
        self.switch.update_property(
            [Gtk.AccessibleProperty.LABEL], [f"Mask {device.get('name', 'device')}"]
        )
        error = request_error or failure_message(mask)
        # Losing the radio/USB connection is an expected state, not a failure
        # dialog. The saved switch remains on until the user turns it off.
        if not connected and enabled and not recovering and not request_error:
            error = ""
            status = "Waiting for device"
        elif busy:
            status = "Updating…"
        elif state == MaskPhase.RESTORING:
            status = "Turning off…"
        elif error:
            status = (
                "Couldn’t restore access" if state == MaskPhase.RECOVERY_FAILED else "Couldn’t mask"
            )
        elif state in {MaskPhase.APPLYING, MaskPhase.ACQUIRING}:
            status = "Reconnecting device…"
        elif state == MaskPhase.TRIAL:
            status = "Masked · awaiting confirmation"
        elif state == MaskPhase.MASKED:
            status = "Masked"
        elif paused:
            status = "Off · remapping stopped by recovery"
        elif not connected:
            status = "Not connected"
        elif enabled:
            status = "Not masked"
        elif mask.get("reason") == "trial_expired" and not mask.get("automatic"):
            status = "Off · confirmation timed out"
        else:
            status = "Off"
        self.status.set_text(status)
        self.status.set_visible(
            not error
            and not paused
            and status not in {"Masked", "Off", "Masked · awaiting confirmation"}
        )
        self.switch.set_tooltip_text(status)
        self.switch.update_property([Gtk.AccessibleProperty.DESCRIPTION], [status])
        self.error.set_text(error)
        self.failure.set_visible(bool(error))
        self.retry.set_visible(state != MaskPhase.RECOVERY_FAILED)
        self.retry.set_sensitive(
            available and connected and not busy and not recovering and not active and not paused
        )
        confirming = state == MaskPhase.TRIAL and not mask.get("automatic")
        self.confirmation.set_visible(bool(confirming))
        self.keep.set_sensitive(state == MaskPhase.TRIAL and not busy)
        self.undo.set_sensitive(not busy)
        self.undo.set_label(f"Undo · {mask.get('remaining_seconds', 0)}s")
        technical = [
            str(device.get("name", "Input device")),
            status,
            f"{str(device.get('transport', '')).upper()} · "
            f"{device.get('vendor')}:{device.get('product')} · {device.get('connection', '')}",
            str(device.get("scope") or device.get("unsupported_reason", "")),
        ]
        if mask.get("error"):
            technical.append(str(mask["error"]))
        if request_error:
            technical.append(request_error)
        diagnostics = "\n\n".join(technical)
        if diagnostics != self.diagnostics:
            self.copy.set_label("Copy diagnostics")
        self.diagnostics = diagnostics
        self.copy.set_visible(bool(error))
        transport = str(device.get("transport") or "").upper()
        connection = str(device.get("connection") or "")
        summary = f"{transport} · Port {connection}" if transport == "USB" else transport.title()
        details = [summary, str(device.get("scope") or device.get("unsupported_reason") or "")]
        if self._details_group is not None:
            details.insert(0, self.name.get_text())
        self.technical.set_text("\n".join(part for part in details if part))


class HardwareMaskingPanel(Gtk.Box):
    def __init__(
        self,
        parent: Gtk.Window | None = None,
        *,
        identities: set[str] | None = None,
        deferred: bool = False,
        remember: Callable[[str, Callable[[], None]], None] | None = None,
        details_group: Adw.ExpanderRow | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.identities = identities
        self.deferred = deferred
        self.choices: dict[str, bool] = {}
        self._remember = remember
        self._details_group = details_group
        self._parent = parent
        self._closed = False
        self._loading = False
        self._pending: set[str] = set()
        self._errors: dict[str, str] = {}
        self._rows: dict[str, MaskDeviceRow] = {}
        self._state: dict = {}
        self._devices = Adw.PreferencesGroup(
            title="Devices" if identities is None else "Device masking",
            description="Stop apps reading the original device while Keymasq keeps access for "
            "remapping. Your choice is remembered across reconnects and restarts.",
        )
        self.append(self._devices)
        self._recovery = Adw.PreferencesGroup(title="Remapping was stopped by recovery")
        recovery_row = Adw.ActionRow(title="Enable remapping")
        recovery_row.set_subtitle("An administrative recovery stopped the input daemon")
        enable = Gtk.Button(label="Enable", valign=Gtk.Align.CENTER)
        connect_button(enable, lambda: self._authorized_change("resume_hardware", {}))
        recovery_row.add_suffix(enable)
        self._recovery.add(recovery_row)
        self._recovery.set_visible(False)
        self.append(self._recovery)
        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.footer = footer
        for setter in (
            footer.set_margin_top,
            footer.set_margin_bottom,
            footer.set_margin_start,
            footer.set_margin_end,
        ):
            setter(12)
        self._status = Gtk.Label(wrap=True, xalign=0)
        self._status.set_visible(False)
        footer.append(self._status)
        self._unmask_all = Gtk.Button(label="Unmask all devices", halign=Gtk.Align.END)
        connect_button(
            self._unmask_all,
            lambda: self._authorized_change("restore_hardware", {"persist": False}),
        )
        footer.append(self._unmask_all)
        self._unmask_all.set_visible(identities is None)
        footer.set_visible(identities is None)
        self.append(footer)
        self._timer = 0
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_closed)

    def _on_map(self, _widget: Gtk.Widget) -> None:
        self._closed = False
        if not self._timer:
            self._timer = GLib.timeout_add_seconds(1, self._refresh)
        self._refresh()

    def _on_closed(self, _widget: Gtk.Widget) -> None:
        self._closed = True
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def _refresh(self) -> bool:
        if self._closed:
            return False
        if self._loading:
            return True
        self._loading = True

        def loaded(response: dict | None) -> bool:
            self._loading = False
            if not self._closed:
                self._render(response)
            return False

        session_request_async({"command": "hardware_inventory"}, loaded, timeout=4)
        return True

    def _render(self, response: dict | None) -> None:
        if not response or response.get("status") != "ok":
            self._status.set_text(
                str((response or {}).get("message", "Hardware service unavailable"))
            )
            self._status.set_visible(True)
            self.footer.set_visible(True)
            for row in self._rows.values():
                row.switch.set_sensitive(False)
                row.retry.set_sensitive(False)
                row.keep.set_sensitive(False)
            return
        self._state = response
        masks = {str(item["id"]): item for item in response.get("masks", []) if item.get("id")}
        paused = bool(response.get("remapping_suspended"))
        self._recovery.set_visible(paused)
        devices = [
            device
            for device in response.get("devices", [])
            if self.identities is None or str(device["id"]) in self.identities
        ]
        identities = {str(device["id"]) for device in devices}
        for identity in list(self._rows):
            if identity not in identities:
                row = self._rows.pop(identity)
                row.remove_details()
                self._devices.remove(row)
        for device in devices:
            identity = str(device["id"])
            if identity not in self._rows:
                self._rows[identity] = row = MaskDeviceRow(
                    self._authorized_change, self._details_group
                )
                self._devices.add(row)
            self._rows[identity].update(
                device,
                masks.get(identity, {}),
                available=bool(response.get("available", True)),
                paused=paused,
                busy=identity in self._pending or "all" in self._pending,
                request_error=self._errors.get(identity, ""),
            )
            if self.deferred and identity in self.choices:
                row = self._rows[identity]
                row.enabled = self.choices[identity]
                row._syncing = True
                row.switch.set_active(row.enabled)
                row._syncing = False
                row.switch.set_tooltip_text("Applied after saving hardware")
        self._unmask_all.set_sensitive(
            not self._pending and any(row.enabled for row in self._rows.values())
        )
        if self._details_group is not None:
            self._details_group.set_visible(bool(self._rows))
        message = self._errors.get("all", "") or str(response.get("message", ""))
        if self.identities is not None and not devices and not message:
            message = "Connect this device, or select a specific interface to mask."
        self._status.set_text(message)
        self._status.set_visible(bool(message))
        self.footer.set_visible(self.identities is None or bool(message))

    def apply_choices(self) -> bool:
        choices, self.choices = self.choices, {}
        self.deferred = False
        self._render(self._state)

        def apply() -> None:
            if self._closed:
                return
            for identity, enabled in choices.items():
                row = self._rows.get(identity)
                if row is not None and row.enabled != enabled:
                    if enabled:
                        row.turn_on()
                    else:
                        row.turn_off()

        unlock = getattr(self._parent, "present_unlock_dialog", None)
        if any(choices.values()) and not getattr(self._parent, "_recording_unlocked", False):
            if callable(unlock):
                unlock(on_success=apply)
            else:
                self._errors["all"] = "Unlock Keymasq from the main window to mask this device."
                self._render(self._state)
        else:
            apply()
        return bool(choices)

    def _authorized_change(self, command: str, data: dict) -> None:
        if self.deferred and command in {"mask_hardware", "restore_hardware"}:
            self.choices[str(data["id"])] = command == "mask_hardware"
            self._render(self._state)
            return
        identity = str(data.get("id", "all"))
        if (
            self._closed
            or identity in self._pending
            or "all" in self._pending
            or (identity == "all" and self._pending)
        ):
            return
        if command == "restore_hardware" or bool(
            getattr(self._parent, "_recording_unlocked", False)
        ):
            self._change(command, data)
            return
        unlock = getattr(self._parent, "present_unlock_dialog", None)
        if callable(unlock):
            unlock(on_success=lambda: self._change(command, data))
        else:
            self._errors[str(data.get("id", "all"))] = (
                "Unlock Keymasq from the main window to mask this device."
            )
            self._render(self._state)

    def _change(self, command: str, data: dict) -> None:
        identity = str(data.get("id", "all"))
        if (
            self._closed
            or identity in self._pending
            or "all" in self._pending
            or (identity == "all" and self._pending)
        ):
            return
        self._pending.add(identity)
        self._errors.pop(identity, None)
        self._render(self._state)

        def changed(response: dict | None) -> bool:
            self._pending.discard(identity)
            if self._closed:
                return False
            if not response or response.get("status") != "ok":
                self._errors[identity] = str(
                    (response or {}).get("message", "Request failed. Retry masking.")
                )
            elif "masks" in response:
                self._state = {**self._state, **response}
            self._render(self._state)
            self._refresh()
            return False

        def send() -> None:
            if self._closed:
                self._pending.discard(identity)
                return
            session_request_async({"command": command, **data}, changed, timeout=28)

        if self._remember is not None and identity != "all":
            self._remember(identity, send)
        else:
            send()


class HardwareMaskingDialog(Adw.Dialog):
    def __init__(self, parent: Gtk.Window | None = None) -> None:
        super().__init__(title="Device masking", content_width=660, content_height=620)
        self.panel = HardwareMaskingPanel(parent)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        scrolled = Gtk.ScrolledWindow(vexpand=True)
        for setter in (
            self.panel.set_margin_top,
            self.panel.set_margin_bottom,
            self.panel.set_margin_start,
            self.panel.set_margin_end,
        ):
            setter(24)
        scrolled.set_child(self.panel)
        toolbar.set_content(scrolled)
        self.panel.remove(self.panel.footer)
        toolbar.add_bottom_bar(self.panel.footer)
        self.set_child(toolbar)
        self.connect("closed", self.panel._on_closed)
