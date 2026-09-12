"""Touch-accessible hardware reservation trials, independent of device setup."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.session_client import session_request_async


class HardwareMaskingDialog(Adw.Dialog):
    def __init__(self, parent: Gtk.Window | None = None) -> None:
        super().__init__(title="Hardware masking", content_width=620, content_height=540)
        self._parent = parent
        self._closed = False
        self._loading = False
        self._changing = False
        self._state: dict[str, object] = {}
        self._signature = ""
        self._syncing_persistence = False
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)
        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", self._on_refresh)
        header.pack_end(refresh)
        page = Adw.PreferencesPage()
        self._devices = Adw.PreferencesGroup(
            title="Attached hardware",
            description="Reserve a controller for Keymasq. A 30-second trial restores access "
            "unless you choose Keep. Choose controller output in hardware setup "
            "or Hardware Settings. "
            "The Steam Deck touchscreen remains available.",
        )
        page.add(self._devices)
        self._rows: list[Adw.ActionRow] = []
        startup = Adw.PreferencesGroup(title="Startup")
        self._persist = Adw.SwitchRow(
            title="Mask automatically when Keymasq starts",
            subtitle="Remember confirmed hardware across restarts and reboots. "
            "Restore pauses automatic masking too.",
            active=True,
        )
        self._persist.connect("notify::active", self._on_persistence_changed)
        startup.add(self._persist)
        page.add(startup)
        recovery = Adw.PreferencesGroup(title="Recovery")
        page.add(recovery)
        restore_row = Adw.ActionRow(title="Restore hardware access")
        restore_row.set_subtitle("Release the mask and pause remapping")
        restore = Gtk.Button(label="Restore")
        restore.set_valign(Gtk.Align.CENTER)
        restore.connect("clicked", self._on_restore)
        restore_row.add_suffix(restore)
        recovery.add(restore_row)
        self._resume_row = Adw.ActionRow(title="Remapping is paused")
        self._resume_row.set_subtitle("Resume remapping and any saved automatic mask")
        resume = Gtk.Button(label="Resume")
        resume.set_valign(Gtk.Align.CENTER)
        resume.connect("clicked", self._on_resume)
        self._resume_row.add_suffix(resume)
        recovery.add(self._resume_row)
        self._resume_row.set_visible(False)
        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for setter in (
            footer.set_margin_top,
            footer.set_margin_bottom,
            footer.set_margin_start,
            footer.set_margin_end,
        ):
            setter(12)
        self._status = Gtk.Label(wrap=True, xalign=0)
        footer.append(self._status)
        self._trial_buttons = Gtk.Box(spacing=12, homogeneous=True)
        undo = Gtk.Button(label="Restore")
        undo.connect("clicked", self._on_restore)
        self._keep = Gtk.Button(label="Keep")
        self._keep.add_css_class("suggested-action")
        self._keep.connect("clicked", self._on_keep)
        self._trial_buttons.append(undo)
        self._trial_buttons.append(self._keep)
        self._trial_buttons.set_visible(False)
        footer.append(self._trial_buttons)
        toolbar.add_bottom_bar(footer)
        toolbar.set_content(page)
        self.set_child(toolbar)
        self.connect("closed", self._on_closed)
        self._timer = GLib.timeout_add_seconds(1, self._refresh)
        self._refresh()

    def _on_closed(self, _dialog: Adw.Dialog) -> None:
        self._closed = True
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def _on_refresh(self, _button: Gtk.Button) -> None:
        self._refresh()

    def _on_restore(self, _button: Gtk.Button) -> None:
        self._change("restore_hardware")

    def _on_resume(self, _button: Gtk.Button) -> None:
        self._authorized_change("resume_hardware")

    def _on_keep(self, _button: Gtk.Button) -> None:
        self._authorized_change(
            "keep_hardware_mask",
            {
                "token": self._state.get("token"),
                "persist": self._persist.get_active(),
            },
        )

    def _on_persistence_changed(self, _row: Adw.SwitchRow, _param: object) -> None:
        if self._syncing_persistence or self._state.get("state") in {
            "applying",
            "acquiring",
            "trial",
        }:
            return
        if self._state.get("has_saved_mask") or self._state.get("state") == "masked":
            self._authorized_change(
                "set_hardware_mask_persistence",
                {
                    "id": self._state.get("saved_id"),
                    "token": self._state.get("token"),
                    "persist": self._persist.get_active(),
                },
            )

    def _on_mask(self, _button: Gtk.Button, data: dict[str, object]) -> None:
        self._authorized_change("mask_hardware", data)

    def _refresh(self) -> bool:
        if self._closed:
            return False
        if self._loading or self._changing:
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
            return
        self._state = response
        state = str(response.get("state", "unmasked"))
        trial = state in {"applying", "acquiring", "trial"}
        active = trial or state == "masked"
        automatic = bool(response.get("automatic"))
        self._trial_buttons.set_visible(trial and not automatic)
        if not trial and response.get("has_saved_mask"):
            self._syncing_persistence = True
            self._persist.set_active(bool(response.get("persist")))
            self._syncing_persistence = False
        self._persist.set_sensitive(not self._changing and not (trial and automatic))
        self._keep.set_sensitive(state == "trial" and not self._changing)
        self._resume_row.set_visible(bool(response.get("remapping_suspended")) and not active)
        if trial:
            remaining = response.get("remaining_seconds", 0)
            message = (
                "Try the controller, then choose Keep."
                if state == "trial"
                else "Restoring saved mask."
                if automatic
                else "Preparing reserved input."
            )
            self._status.set_text(f"{message} Access restores in {remaining} seconds.")
        elif state == "masked":
            self._status.set_text(
                "Controller reserved. You can add it to Keymasq and configure mappings."
            )
        elif response.get("error"):
            self._status.set_text(str(response["error"]))
        elif response.get("remapping_suspended"):
            self._status.set_text("Hardware access restored. Remapping remains paused.")
        else:
            self._status.set_text(str(response.get("message", "")))
        devices = response.get("devices", [])
        signature = repr((devices, response.get("id"), active, response.get("available", True)))
        if signature == self._signature:
            return
        self._signature = signature
        for row in self._rows:
            self._devices.remove(row)
        self._rows.clear()
        for device in devices:
            row = Adw.ActionRow(title=str(device.get("name", "Input device")))
            current = active and device.get("id") == response.get("id")
            supported = bool(device.get("supported"))
            description = str(
                device.get("scope") if supported else device.get("unsupported_reason")
            )
            row.set_subtitle(
                f"{device.get('vendor')}:{device.get('product')} · "
                f"{device.get('transport')}\n{description}"
            )
            button = Gtk.Button(label="Restore" if current else "Try masking")
            button.set_valign(Gtk.Align.CENTER)
            button.set_sensitive(
                current or (supported and not active and bool(response.get("available", True)))
            )
            payload = {"id": device.get("id"), "generation": device.get("generation")}
            if current:
                button.connect("clicked", self._on_restore)
            else:
                button.connect("clicked", self._on_mask, payload)
            row.add_suffix(button)
            self._devices.add(row)
            self._rows.append(row)

    def _authorized_change(self, command: str, data: dict | None = None) -> None:
        if bool(getattr(self._parent, "_recording_unlocked", False)):
            self._change(command, data)
            return
        unlock = getattr(self._parent, "present_unlock_dialog", None)
        if callable(unlock):
            unlock(on_success=lambda: self._change(command, data))
        else:
            self._status.set_text("Unlock Keymasq from the main window to reserve hardware")

    def _change(self, command: str, data: dict | None = None) -> None:
        if self._changing or self._closed:
            return
        self._changing = True
        self._keep.set_sensitive(False)
        self._status.set_text("Restoring access…" if command == "restore_hardware" else "Applying…")

        def changed(response: dict | None) -> bool:
            self._changing = False
            if self._closed:
                return False
            if not response or response.get("status") != "ok":
                self._status.set_text(
                    str(
                        (response or {}).get(
                            "message", "Request failed; an unconfirmed trial will restore access"
                        )
                    )
                )
            self._signature = ""
            self._refresh()
            return False

        session_request_async({"command": command, **(data or {})}, changed, timeout=28)
