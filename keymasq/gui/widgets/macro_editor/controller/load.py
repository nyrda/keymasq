"""Macro editor loading and document hydration."""

# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false

from typing import Any

from keymasq.gui.session_client import GuiTaskResult
from keymasq.gui.widgets.macro_editor.document import MacroDocument, selection_order


class LoadControllerMixin:
    """Load session state and hydrate the editable document."""

    def _resolve_compositor_action_status(
        self,
        status: object | None = None,
    ) -> dict[str, bool | str | None]:
        resolved: dict[str, bool | str | None] = {
            "compositor_id": None,
            "listener_name": None,
            "compositor_dispatch_available": False,
        }
        if isinstance(status, dict):
            for key in resolved:
                value = status.get(key)
                if isinstance(value, (bool, str)) or value is None:
                    resolved[key] = value
            return resolved

        root = self._parent.get_root() if hasattr(self._parent, "get_root") else None
        get_status = getattr(root, "get_compositor_action_status", None)
        if callable(get_status):
            root_status = get_status()
            if isinstance(root_status, dict):
                for key in resolved:
                    value = root_status.get(key)
                    if isinstance(value, (bool, str)) or value is None:
                        resolved[key] = value
        return resolved

    def _load_initial_state_async(self) -> None:
        self._run_gui_task(
            self._load_initial_state,
            self._on_initial_state_loaded,
        )

    def _load_initial_state(self) -> dict[str, object]:
        timeout_max = 30000
        compositor_status: dict[str, object] = {}
        try:
            status = self._session_request({"command": "get_status"}) or {}
            timeout_max = int(status.get("macro_exec_timeout_max_ms", 30000) or 30000)
            compositor_status = dict(status)
        except (OSError, RuntimeError, TypeError, ValueError):
            timeout_max = 30000

        macro: dict[str, Any] | None = None
        try:
            response = (
                self._session_request({"command": "get_macro", "name": self._macro_name}) or {}
            )
            loaded_macro = response.get("macro")
            if response.get("status") == "ok" and isinstance(loaded_macro, dict):
                macro = loaded_macro
        except (OSError, RuntimeError, TypeError, ValueError):
            macro = None

        return {
            "timeout_max": max(1, timeout_max),
            "compositor_status": compositor_status,
            "macro": macro,
        }

    def _on_initial_state_loaded(self, result: GuiTaskResult[dict[str, object]]) -> bool:
        payload = result.value if result.ok and isinstance(result.value, dict) else {}
        timeout_max_raw = payload.get("timeout_max", 30000)
        timeout_max = timeout_max_raw if isinstance(timeout_max_raw, int) else 30000
        self._macro_exec_timeout_max_ms = max(1, timeout_max)
        self._compositor_action_status = self._resolve_compositor_action_status(
            payload.get("compositor_status")
        )

        timeout_adjustment = self._control_timeout_spin.get_adjustment()
        timeout_adjustment.set_upper(self._macro_exec_timeout_max_ms)
        timeout_adjustment.set_value(
            min(timeout_adjustment.get_value(), float(self._macro_exec_timeout_max_ms))
        )

        macro = payload.get("macro")
        if isinstance(macro, dict):
            self._macro_exists = True
            self._apply_macro_state(macro)
            self._sync_macro_settings_controls()
            self._initial_macro_data = self._current_macro_payload()
            self._refresh_loaded_macro_state()
            if self._select_initial_event:
                self._select_first_event()
        else:
            self._initial_macro_data = self._current_macro_payload()
        self._initial_state_loaded = True
        self._sync_close_guard()
        return False

    def _refresh_loaded_macro_state(self) -> None:
        self._update_stats()
        self._timeline.queue_draw()
        self._update_canvas_width()

    def _select_first_event(self) -> None:
        candidates: list[object] = [
            *self._events,
            *self._rel_events,
            *self._passthrough_events,
            *self._synthetic_moves,
            *self._control_events,
        ]
        if not candidates:
            return
        selected = min(candidates, key=selection_order)
        self._timeline._selected = selected
        self._on_selection_changed(selected)
        self._timeline.queue_draw()

    def _apply_macro_state(self, macro: dict[str, Any]) -> None:
        document = MacroDocument.from_payload(macro)
        self._macro_data = document.source
        self._events = document.events
        self._rel_events = document.relative_events
        self._passthrough_events = document.passthrough_events
        self._synthetic_moves = document.moves
        self._control_events = document.controls
        self._duration_us = document.duration_us
        self._macro_has_move_to_start_setting = document.has_move_to_start_setting
        self._macro_move_to_start = document.move_to_start
        self._macro_start_x = document.start_x
        self._macro_start_y = document.start_y
        self._macro_block_mouse_movement = document.block_mouse_movement
        self._macro_loop_mode = document.loop_mode
        self._macro_loop_count = document.loop_count
        self._macro_loop_stop_behavior = document.loop_stop_behavior
