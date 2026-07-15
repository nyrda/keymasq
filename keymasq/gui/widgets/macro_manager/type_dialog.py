"""Type-macro dialog widgets, validation, and payload creation."""

# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false

import re
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.macro_compile import (
    DEFAULT_TYPE_MACRO_DOWN_MS,
    DEFAULT_TYPE_MACRO_PAUSE_MS,
    build_type_macro_events,
    can_type_directly,
    char_to_key,
    macro_definition_from_events,
    normalize_type_macro_text,
    normalize_unicode_type_macro_text,
)
from keymasq.gui.session_client import JsonDict


class TypeMacroDialogMixin:
    """Build and coordinate a macro that types entered text."""

    def _build_ui(self) -> None:
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        main.set_margin_top(12)
        main.set_margin_bottom(12)
        main.set_margin_start(12)
        main.set_margin_end(12)

        name_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name_label = Gtk.Label(label="Macro name:")
        name_label.set_halign(Gtk.Align.START)
        name_row.append(name_label)

        self.name_entry = Gtk.Entry()
        self.name_entry.set_hexpand(True)
        self.name_entry.set_placeholder_text("e.g., type_hello")
        self.name_entry.set_text(f"type_{datetime.now().strftime('%H%M%S')}")
        name_row.append(self.name_entry)
        main.append(name_row)

        text_label = Gtk.Label(label="Text to type:")
        text_label.set_halign(Gtk.Align.START)
        main.append(text_label)

        text_scrolled = Gtk.ScrolledWindow()
        text_scrolled.set_min_content_height(130)
        text_scrolled.set_max_content_height(220)
        text_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.text_view = Gtk.TextView()
        self.text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.text_view.get_buffer().connect("changed", self._on_text_changed)
        text_scrolled.set_child(self.text_view)
        main.append(text_scrolled)

        self.unicode_check = Gtk.CheckButton(
            label="Use Ctrl+Shift+U for detected Unicode characters"
        )
        self.unicode_check.set_tooltip_text(
            "Best-effort Linux Unicode input. Works in many text fields, but not every app."
        )
        self.unicode_check.set_visible(False)
        main.append(self.unicode_check)

        timing = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        timing.set_halign(Gtk.Align.START)
        timing.append(Gtk.Label(label="Key down (ms):"))

        self.down_spin = Gtk.SpinButton()
        self.down_spin.set_adjustment(
            Gtk.Adjustment(
                value=DEFAULT_TYPE_MACRO_DOWN_MS,
                lower=0,
                upper=1000,
                step_increment=1,
            )
        )
        timing.append(self.down_spin)
        timing.append(Gtk.Label(label="Pause between keys (ms):"))

        self.pause_spin = Gtk.SpinButton()
        self.pause_spin.set_adjustment(
            Gtk.Adjustment(
                value=DEFAULT_TYPE_MACRO_PAUSE_MS,
                lower=0,
                upper=1000,
                step_increment=1,
            )
        )
        timing.append(self.pause_spin)
        main.append(timing)

        self.error_label = Gtk.Label()
        self.error_label.add_css_class("error")
        self.error_label.add_css_class("caption")
        self.error_label.set_halign(Gtk.Align.START)
        self.error_label.set_visible(False)
        main.append(self.error_label)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.END)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_close_clicked)
        btn_row.append(cancel_btn)

        self._create_btn = Gtk.Button(label="Create")
        self._create_btn.add_css_class("suggested-action")
        self._create_btn.connect("clicked", self._on_create)
        btn_row.append(self._create_btn)

        main.append(btn_row)
        self.set_child(main)

    def _on_text_changed(self, _buffer: Gtk.TextBuffer) -> None:
        self._sync_unicode_warning()

    def _sync_unicode_warning(self) -> None:
        text = self._text_buffer_text()
        needs_unicode = self._text_needs_unicode_option(text)
        was_visible = self.unicode_check.get_visible()
        self.unicode_check.set_visible(needs_unicode)
        if needs_unicode and not was_visible:
            self.unicode_check.set_active(True)
        elif not needs_unicode:
            self.unicode_check.set_active(False)

    def _text_buffer_text(self) -> str:
        buffer = self.text_view.get_buffer()
        start = buffer.get_start_iter()
        end = buffer.get_end_iter()
        return buffer.get_text(start, end, False)

    def _text_needs_unicode_option(self, text: str) -> bool:
        exact_text = normalize_unicode_type_macro_text(text)
        direct_text = normalize_type_macro_text(text)
        if exact_text != direct_text:
            return True
        return any(not self._can_type_directly(ch) for ch in direct_text)

    def _on_create(self, _btn: Gtk.Button) -> None:
        name = self.name_entry.get_text().strip()
        if not name:
            self._show_error("Macro name is required")
            return
        if not re.match(r"^[a-zA-Z0-9_\-]+$", name):
            self._show_error("Only letters, numbers, underscores and hyphens")
            return

        text = self._text_buffer_text()
        use_unicode_input = self.unicode_check.get_visible() and self.unicode_check.get_active()
        text = (
            normalize_unicode_type_macro_text(text)
            if use_unicode_input
            else normalize_type_macro_text(text)
        )
        if not text:
            self._show_error("Please enter text to type")
            return

        down_ms = int(self.down_spin.get_value())
        pause_ms = int(self.pause_spin.get_value())
        try:
            events = self._build_type_events(
                text,
                down_ms,
                pause_ms,
                use_unicode_input=use_unicode_input,
            )
        except ValueError as error:
            self._show_error(str(error))
            return

        data = macro_definition_from_events(events, name=name)
        data.update(
            {
                "created_at": datetime.now().isoformat(),
                "type_binding": True,
                "type_text": text,
                "type_down_ms": down_ms,
                "type_pause_ms": pause_ms,
                "type_use_unicode_input": bool(use_unicode_input),
            }
        )

        def on_create_start() -> None:
            self._create_btn.set_sensitive(False)

        def on_create_done() -> None:
            self._create_btn.set_sensitive(True)

        self._session_request_async(
            {"command": "create_macro", "macro": data},
            self._on_create_finished,
            on_start=on_create_start,
            on_done=on_create_done,
        )

    def _on_create_finished(self, result: JsonDict | None) -> bool:
        result = result or {}
        if result.get("status") != "ok":
            self._show_error(result.get("message", "Failed to create macro"))
            return False

        if self._on_created:
            self._on_created()
        self.close()
        return False

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _show_error(self, message: str) -> None:
        self.error_label.set_label(message)
        self.error_label.set_visible(True)

    def _build_type_events(
        self,
        text: str,
        down_ms: int,
        pause_ms: int,
        *,
        use_unicode_input: bool = False,
    ) -> list[dict]:
        return list(
            build_type_macro_events(
                text,
                down_ms,
                pause_ms,
                use_unicode_input=use_unicode_input,
            )
        )

    def _can_type_directly(self, ch: str) -> bool:
        return can_type_directly(ch)

    def _char_to_key(self, ch: str) -> tuple[int, bool]:
        return char_to_key(ch)
