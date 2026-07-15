from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from .model import Payload, text


class SuppressionMixin:
    def _sync_status(self: Any, data: Payload) -> None:
        active = bool(data.get("active", True))
        suppressed = bool(data.get("suppressed", False))
        if active:
            state = "suppressed" if suppressed else "monitoring"
            status = f"{self.device.name} - {'Output suppressed' if suppressed else 'Monitoring'}"
        else:
            state = "stopped"
            status = f"{self.device.name} - Stopped"
        self._set_status_title(status, state)
        self._syncing_suppression = True
        try:
            if self._suppression_switch.get_active() != suppressed:
                self._suppression_switch.set_active(suppressed)
        finally:
            self._syncing_suppression = False
        self._suppression_switch.set_sensitive(active and not self._session.closing)
        self._suppression_hint_label.set_visible(active and suppressed)

    def _set_status_title(self: Any, value: str, state: str) -> None:
        self._status_label.set_text(value)
        self._status_label.set_tooltip_text(value)
        for css_class in (
            "inspector-header-monitoring",
            "inspector-header-suppressed",
            "inspector-header-stopped",
        ):
            self._status_label.remove_css_class(css_class)
        self._status_label.add_css_class(
            {
                "suppressed": "inspector-header-suppressed",
                "stopped": "inspector-header-stopped",
            }.get(state, "inspector-header-monitoring")
        )

    def _on_inspector_status(self: Any, event: Payload) -> bool:
        if text(event.get("hardware_id")) != self._hardware_id:
            return False
        self._sync_status(event)
        return False

    def _on_keymasqd_status(self: Any, event: Payload) -> bool:
        if not bool(event.get("connected", False)):
            self._set_status_title(f"{self.device.name} - Daemon disconnected", "stopped")
            self._suppression_switch.set_sensitive(False)
        return False

    def _on_suppression_toggled(self: Any, switch: Gtk.Switch, _param: object) -> None:
        if self._syncing_suppression or self._session.closing:
            return
        switch.set_sensitive(False)
        self._session.set_suppressed(
            switch.get_active(),
            self._on_suppression_response,
            reason="manual",
        )

    def _on_key_pressed(
        self: Any,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if (
            keyval != Gdk.KEY_Escape
            or self._session.closing
            or not self._suppression_switch.get_active()
        ):
            return False
        self._suppression_switch.set_sensitive(False)
        self._session.set_suppressed(
            False,
            self._on_suppression_response,
            reason="key_esc",
        )
        return True

    def _on_suppression_response(self: Any, result: Payload | None) -> bool:
        if self._session.closing:
            return False
        if result and result.get("status") == "ok":
            self._sync_status(result)
        else:
            self._request_snapshot()
        return False
