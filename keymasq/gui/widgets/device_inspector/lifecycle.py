from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib  # pyright: ignore[reportAttributeAccessIssue]

from .model import Payload, text


class LifecycleMixin:
    def _on_start_response(self: Any, result: Payload | None) -> bool:
        if self._session.closing:
            return False
        if not result or result.get("status") != "ok":
            self._set_status_title(
                text((result or {}).get("message"), "Inspector could not start"),
                "stopped",
            )
            self._suppression_switch.set_sensitive(False)
            return False
        self._apply_snapshot(result)
        return False

    def _request_snapshot(self: Any) -> None:
        self._session.request_snapshot(self._on_snapshot_response)

    def _on_snapshot_response(self: Any, result: Payload | None) -> bool:
        if self._session.closing:
            return False
        if result and result.get("status") == "ok":
            self._apply_snapshot(result)
        return False

    def _apply_snapshot(self: Any, snapshot: Payload) -> None:
        self._snapshot = dict(snapshot)
        self._sync_status(snapshot)
        self._render_mapping(snapshot)
        self._render_axes(snapshot)

    def _on_profiles_changed(self: Any, _event: Payload) -> bool:
        self._request_snapshot()
        return False

    def _on_runtime_reset(self: Any, _event: Payload) -> bool:
        self._request_snapshot()
        return False

    def _on_close_request(self: Any, *_args: object) -> bool:
        self._finalize()
        return False

    def _on_destroy(self: Any, *_args: object) -> None:
        self._finalize()

    def _finalize(self: Any) -> None:
        if self._session.finalized:
            return
        self._cancel_event_render()
        for source_id in list(self._flash_timeout_ids.values()):
            GLib.source_remove(source_id)
        self._flash_timeout_ids.clear()
        self._session.finalize()
