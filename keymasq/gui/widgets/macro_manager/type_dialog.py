"""Type-macro dialog widgets, validation, and payload creation."""

# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false

import re
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.keyboard_layouts import (
    TypedKey,
    keyboard_layout_error,
    keyboard_layout_name,
)
from keymasq.common.macro_compile import (
    DEFAULT_TYPE_MACRO_DOWN_MS,
    DEFAULT_TYPE_MACRO_PAUSE_MS,
    build_type_macro_events,
    can_type_directly,
    char_to_key,
    macro_definition_from_events,
    normalize_type_macro_text,
    normalize_unicode_type_macro_text,
    unicode_input_capability_error,
)
from keymasq.gui.session_client import JsonDict
from keymasq.gui.widgets.settings_dialog import present_keyboard_layout_settings
from keymasq.session.settings import load_keyboard_layout


def _layout_caption(layout_id: str, error: str | None) -> str:
    if error is not None:
        return f"Keyboard layout {layout_id!r} cannot be used: {error}"
    return f"Typed for keyboard layout: {keyboard_layout_name(layout_id)}"


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

        layout_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.layout_label = Gtk.Label(label=self._keyboard_layout_caption())
        self.layout_label.add_css_class("dim-label")
        self.layout_label.add_css_class("caption")
        self.layout_label.set_halign(Gtk.Align.START)
        self.layout_label.set_valign(Gtk.Align.CENTER)
        layout_box.append(self.layout_label)
        self.layout_settings_link = Gtk.LinkButton.new_with_label("", "Change in Settings")
        self.layout_settings_link.add_css_class("caption")
        self.layout_settings_link.set_tooltip_text("Open the keyboard layout setting")
        self.layout_settings_link.connect("activate-link", self._on_layout_link)
        layout_box.append(self.layout_settings_link)
        main.append(layout_box)

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

    def _on_layout_link(self, link: Gtk.LinkButton) -> bool:
        present_keyboard_layout_settings(link, on_closed=self._sync_unicode_warning)
        return True

    def _sync_unicode_warning(self) -> None:
        layout_id = self._keyboard_layout_id()
        layout_error = keyboard_layout_error(layout_id)
        self.layout_label.set_label(_layout_caption(layout_id, layout_error))
        if layout_error is not None:
            self.unicode_check.set_visible(False)
            self.unicode_check.set_active(False)
            return
        text = self._text_buffer_text()
        needs_unicode = self._text_needs_unicode_option(text, layout_id)
        capability_error = unicode_input_capability_error(layout_id)
        self.unicode_check.set_sensitive(capability_error is None)
        if capability_error is None:
            self.unicode_check.set_label("Use Ctrl+Shift+U for detected Unicode characters")
            self.unicode_check.set_tooltip_text(
                "Best-effort Linux Unicode input. Works in many text fields, but not every app."
            )
        else:
            self.unicode_check.set_label("Ctrl+Shift+U is unavailable for this keyboard layout")
            self.unicode_check.set_tooltip_text(capability_error)
        was_visible = self.unicode_check.get_visible()
        self.unicode_check.set_visible(needs_unicode)
        if needs_unicode and not was_visible and capability_error is None:
            self.unicode_check.set_active(True)
        elif not needs_unicode or capability_error is not None:
            self.unicode_check.set_active(False)

    def _text_buffer_text(self) -> str:
        buffer = self.text_view.get_buffer()
        start = buffer.get_start_iter()
        end = buffer.get_end_iter()
        return buffer.get_text(start, end, False)

    def _text_needs_unicode_option(self, text: str, layout_id: str | None = None) -> bool:
        exact_text = normalize_unicode_type_macro_text(text)
        direct_text = normalize_type_macro_text(text)
        if exact_text != direct_text:
            return True
        # Resolve the layout once: it comes from settings.toml, not per character.
        if layout_id is None:
            layout_id = self._keyboard_layout_id()
        return any(not can_type_directly(ch, layout_id) for ch in direct_text)

    def _on_create(self, _btn: Gtk.Button) -> None:
        layout_id = self._keyboard_layout_id()
        layout_error = keyboard_layout_error(layout_id)
        self.layout_label.set_label(_layout_caption(layout_id, layout_error))
        if layout_error is not None:
            self._show_error(f"Keyboard layout {layout_id!r} cannot be used: {layout_error}")
            return
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
                "type_layout": layout_id,
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
                layout=self._keyboard_layout_id(),
            )
        )

    def _keyboard_layout_id(self) -> str:
        return load_keyboard_layout()

    def _keyboard_layout_caption(self) -> str:
        layout_id = self._keyboard_layout_id()
        return _layout_caption(layout_id, keyboard_layout_error(layout_id))

    def _can_type_directly(self, ch: str, layout_id: str | None = None) -> bool:
        return can_type_directly(ch, layout_id or self._keyboard_layout_id())

    def _char_to_key(self, ch: str, layout_id: str | None = None) -> TypedKey:
        return char_to_key(ch, layout_id or self._keyboard_layout_id())
