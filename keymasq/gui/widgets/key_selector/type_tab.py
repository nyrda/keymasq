# pyright: reportAttributeAccessIssue=false
from __future__ import annotations

from datetime import datetime
from secrets import token_hex

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.coercion import coerce_int
from keymasq.common.macro_compile import (
    DEFAULT_TYPE_MACRO_DOWN_MS,
    DEFAULT_TYPE_MACRO_PAUSE_MS,
    build_type_macro_events,
    can_type_directly,
    macro_definition_from_events,
    normalize_type_macro_binding_text,
    normalize_type_macro_text,
    normalize_unicode_type_macro_text,
)
from keymasq.common.model.core import ActionType
from keymasq.common.types import JsonObject
from keymasq.gui.session_client import session_request_async


class TypeTabMixin:
    def _build_type_tab(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        outer.set_margin_start(12)
        outer.set_margin_end(12)

        text_label = Gtk.Label(label="Text to type")
        text_label.add_css_class("dim-label")
        text_label.set_halign(Gtk.Align.START)
        outer.append(text_label)

        text_scrolled = Gtk.ScrolledWindow()
        text_scrolled.set_min_content_height(150)
        text_scrolled.set_max_content_height(260)
        text_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.type_text_view = Gtk.TextView()
        self.type_text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.type_text_view.get_buffer().connect("changed", self._on_type_text_changed)
        text_scrolled.set_child(self.type_text_view)
        outer.append(text_scrolled)

        self.type_unicode_check = Gtk.CheckButton(
            label="Use Ctrl+Shift+U for detected Unicode characters"
        )
        self.type_unicode_check.set_tooltip_text(
            "Best-effort Linux Unicode input. Works in many text fields, but not every app."
        )
        self.type_unicode_check.set_visible(False)
        self.type_unicode_check.connect("toggled", self._on_type_unicode_toggled)
        outer.append(self.type_unicode_check)

        timing = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        timing.set_halign(Gtk.Align.START)

        timing.append(Gtk.Label(label="Key down (ms):"))

        self.type_down_spin = Gtk.SpinButton()
        self.type_down_spin.set_adjustment(
            Gtk.Adjustment(
                value=DEFAULT_TYPE_MACRO_DOWN_MS,
                lower=0,
                upper=1000,
                step_increment=1,
            )
        )
        self.type_down_spin.connect("value-changed", self._on_type_control_changed)
        timing.append(self.type_down_spin)

        timing.append(Gtk.Label(label="Pause between keys (ms):"))

        self.type_pause_spin = Gtk.SpinButton()
        self.type_pause_spin.set_adjustment(
            Gtk.Adjustment(
                value=DEFAULT_TYPE_MACRO_PAUSE_MS,
                lower=0,
                upper=1000,
                step_increment=1,
            )
        )
        self.type_pause_spin.connect("value-changed", self._on_type_control_changed)
        timing.append(self.type_pause_spin)

        outer.append(timing)

        self.type_error_label = Gtk.Label()
        self.type_error_label.add_css_class("error")
        self.type_error_label.add_css_class("caption")
        self.type_error_label.set_halign(Gtk.Align.START)
        self.type_error_label.set_visible(False)
        outer.append(self.type_error_label)

        self._sync_type_unicode_option()
        self._sync_type_map_button()
        return outer

    def _maybe_load_type_macro_details(self) -> None:
        if getattr(self, "_type_macro_details_loaded", False):
            return
        if getattr(self, "_type_macro_details_loading", False):
            return
        if not getattr(self, "_selected_type_macro", None):
            return
        self._load_type_macro_details()

    def _load_type_macro_details(self) -> bool:
        name = getattr(self, "_selected_type_macro", None)
        if not name:
            return False
        self._type_macro_details_loading = True
        session_request_async(
            {"command": "get_macro", "name": name},
            self._on_type_macro_details_loaded,
        )
        return False

    def _on_type_macro_details_loaded(self, result: JsonObject | None) -> bool:
        self._type_macro_details_loading = False
        self._type_macro_details_loaded = True
        macro = result.get("macro") if isinstance(result, dict) else None
        if not isinstance(macro, dict) or not bool(macro.get("type_binding", False)):
            return False
        if self._type_controls_modified:
            return False

        self._type_details_applying = True
        try:
            self.type_text_view.get_buffer().set_text(str(macro.get("type_text", "") or ""))
            self.type_down_spin.set_value(
                float(coerce_int(macro.get("type_down_ms"), DEFAULT_TYPE_MACRO_DOWN_MS))
            )
            self.type_pause_spin.set_value(
                float(coerce_int(macro.get("type_pause_ms"), DEFAULT_TYPE_MACRO_PAUSE_MS))
            )
            self.type_unicode_check.set_active(bool(macro.get("type_use_unicode_input", False)))
            self._sync_type_unicode_option()
        finally:
            self._type_details_applying = False
        self._sync_type_map_button()
        return False

    def _on_type_text_changed(self, _buffer: Gtk.TextBuffer) -> None:
        if not self._type_details_applying:
            self._type_controls_modified = True
        self._sync_type_unicode_option()
        self._clear_type_error()
        self._sync_type_map_button()

    def _on_type_unicode_toggled(self, _check: Gtk.CheckButton) -> None:
        if not self._type_details_applying:
            self._type_controls_modified = True
        self._clear_type_error()
        self._sync_type_map_button()

    def _on_type_control_changed(self, _spin: Gtk.SpinButton) -> None:
        if not self._type_details_applying:
            self._type_controls_modified = True

    def _type_buffer_text(self) -> str:
        buffer = self.type_text_view.get_buffer()
        start = buffer.get_start_iter()
        end = buffer.get_end_iter()
        return buffer.get_text(start, end, False)

    def _type_use_unicode_input(self) -> bool:
        return self.type_unicode_check.get_visible() and self.type_unicode_check.get_active()

    def _normalized_type_buffer_text(self) -> str:
        return normalize_type_macro_binding_text(
            self._type_buffer_text(),
            use_unicode_input=self._type_use_unicode_input(),
        )

    def _sync_type_unicode_option(self) -> None:
        text = self._type_buffer_text()
        needs_unicode = self._type_text_needs_unicode_option(text)
        was_visible = self.type_unicode_check.get_visible()
        self.type_unicode_check.set_visible(needs_unicode)
        if needs_unicode and not was_visible:
            self.type_unicode_check.set_active(True)
        elif not needs_unicode:
            self.type_unicode_check.set_active(False)

    def _type_text_needs_unicode_option(self, text: str) -> bool:
        exact_text = normalize_unicode_type_macro_text(text)
        direct_text = normalize_type_macro_text(text)
        if exact_text != direct_text:
            return True
        return any(not can_type_directly(ch) for ch in direct_text)

    def _sync_type_map_button(self) -> None:
        map_btn = getattr(self, "map_btn", None)
        stack = getattr(self, "stack", None)
        if map_btn is None or stack is None or stack.get_visible_child_name() != "type":
            return
        pending = bool(getattr(self, "_type_create_pending", False))
        map_btn.set_sensitive(bool(self._normalized_type_buffer_text()) and not pending)

    def _on_type_map_clicked(self, _btn: Gtk.Button) -> None:
        use_unicode_input = self._type_use_unicode_input()
        text = self._normalized_type_buffer_text()
        if not text:
            self._show_type_error("Please enter text to type")
            return

        down_ms = int(self.type_down_spin.get_value())
        pause_ms = int(self.type_pause_spin.get_value())
        try:
            events = build_type_macro_events(
                text,
                down_ms,
                pause_ms,
                use_unicode_input=use_unicode_input,
            )
        except ValueError as exc:
            self._show_type_error(str(exc))
            return

        self._type_create_pending = True
        self._sync_type_map_button()

        def on_created(result: JsonObject | None) -> bool:
            try:
                return self._on_type_macro_created(result)
            finally:
                self._on_type_macro_create_done()

        name = self._type_macro_name()
        macro = macro_definition_from_events(events, name=name)
        macro["created_at"] = datetime.now().isoformat()
        macro["type_binding"] = True
        macro["type_text"] = text
        macro["type_down_ms"] = down_ms
        macro["type_pause_ms"] = pause_ms
        macro["type_use_unicode_input"] = bool(use_unicode_input)

        session_request_async({"command": "create_macro", "macro": macro}, on_created)

    def _type_macro_name(self) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"type_text_{timestamp}_{token_hex(3)}"

    def _on_type_macro_created(self, result: JsonObject | None) -> bool:
        result = result or {}
        if result.get("status") != "ok":
            self._show_type_error(str(result.get("message", "Failed to create type macro")))
            return False
        macro = result.get("macro")
        if not isinstance(macro, dict):
            self._show_type_error("Type macro did not return a macro")
            return False
        macro_name = str(macro.get("name", "") or "")
        if not macro_name:
            self._show_type_error("Type macro returned an invalid macro name")
            return False

        self._warn_and_clear_unsupported_rapidfire(ActionType.MACRO)
        action = self._build_selected_action(
            ActionType.MACRO,
            macro_name=macro_name,
            macro_replay_mouse_movement=bool(self._macro_replay_movement),
            macro_replay_mouse_clicks=bool(self._macro_replay_clicks),
            macro_speed=float(self._macro_speed),
        )
        self._emit_selected_action(action)
        return False

    def _on_type_macro_create_done(self) -> None:
        self._type_create_pending = False
        self._sync_type_map_button()

    def _show_type_error(self, message: str) -> None:
        self.type_error_label.set_label(message)
        self.type_error_label.set_visible(True)

    def _clear_type_error(self) -> None:
        self.type_error_label.set_label("")
        self.type_error_label.set_visible(False)
