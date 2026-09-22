"""Track the keyboard layout currently applied by keymasq-session."""

from collections.abc import Callable

from keymasq.common.keyboard_layouts import normalize_keyboard_layout_id
from keymasq.gui.session_client import (
    JsonDict,
    register_session_event_callback,
    session_request_async,
    unregister_session_event_callback,
)


class TypeMacroLayout:
    def __init__(self, on_changed: Callable[[], None]) -> None:
        self.layout_id: str | None = None
        self.loading = True
        self._on_changed = on_changed
        self._generation = 0
        self._closed = False
        # The async get_settings request connects the socket. Registering the
        # event handler must not attempt a socket connection on the GTK thread.
        register_session_event_callback(
            "settings_changed", self._on_settings_changed, connect=False
        )

    def refresh(self) -> None:
        if self._closed:
            return
        self._generation += 1
        generation = self._generation
        self.loading = True
        self._on_changed()
        session_request_async(
            {"command": "get_settings"},
            lambda response: self._on_loaded(response, generation),
            timeout=1.0,
        )

    def _on_loaded(self, response: JsonDict | None, generation: int) -> bool:
        if self._closed or generation != self._generation:
            return False
        self.loading = False
        layout = (
            response.get("keyboard_layout")
            if isinstance(response, dict) and response.get("status") == "ok"
            else None
        )
        self.layout_id = (
            normalize_keyboard_layout_id(layout)
            if isinstance(layout, str) and layout.strip()
            else None
        )
        self._on_changed()
        return False

    def _on_settings_changed(self, event: JsonDict) -> None:
        if self._closed:
            return
        layout = event.get("keyboard_layout")
        if not isinstance(layout, str) or not layout.strip():
            return
        # A settings event is newer than any outstanding get_settings reply.
        self._generation += 1
        self.loading = False
        self.layout_id = normalize_keyboard_layout_id(layout)
        self._on_changed()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        unregister_session_event_callback("settings_changed", self._on_settings_changed)
