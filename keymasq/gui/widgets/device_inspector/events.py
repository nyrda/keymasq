from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, GLib, Gtk, Pango  # pyright: ignore[reportAttributeAccessIssue]

from .model import EVENT_ROW_LIMIT, Payload, text

EVENT_RENDER_THROTTLE_MS = 33


class EventsMixin:
    def _on_inspector_event(self: Any, event: Payload) -> bool:
        if text(event.get("hardware_id")) != self._hardware_id:
            return False
        self._store_event(event)

        control_id = text(event.get("control_id"))
        event_type = text(event.get("type_name")).lower()
        value = int(event.get("value", 0) or 0)
        if control_id:
            if event_type == "ev_key":
                self._set_control_active(control_id, value != 0)
            else:
                self._flash_control(control_id)

        analog_id = text(event.get("analog_id"))
        role = text(event.get("analog_role"))
        if analog_id and role:
            self._update_analog_value(analog_id, role, value)
            self._flash_control(analog_id)
        return False

    def _store_event(self: Any, event: Payload) -> None:
        category = self._event_history.add(event)
        if not self._event_filter_active(category):
            return
        if category == "button":
            self._render_event_rows()
        else:
            self._queue_event_render()

    def _prepend_event_row(self: Any, event: Payload) -> None:
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.add_css_class("inspector-event-row")
        if int(event.get("value", 0) or 0) == 1:
            row.add_css_class("inspector-event-row-pressed")

        grid = Gtk.Grid(column_spacing=10, row_spacing=2)
        grid.set_margin_top(5)
        grid.set_margin_bottom(5)
        grid.set_margin_start(8)
        grid.set_margin_end(8)

        sequence = int(event.get("sequence", 0) or 0)
        event_type = text(event.get("type_name"), text(event.get("type")))
        code_name = text(event.get("code_name"), text(event.get("code")))
        value = text(event.get("value"), "0")
        source = text(event.get("source"))

        sequence_label = Gtk.Label(label=f"#{sequence}" if sequence else "")
        sequence_label.add_css_class("caption")
        sequence_label.add_css_class("dim-label")
        sequence_label.set_xalign(0.0)
        grid.attach(sequence_label, 0, 0, 1, 2)

        code_label = Gtk.Label(label=code_name)
        code_label.add_css_class("heading")
        code_label.set_xalign(0.0)
        code_label.set_hexpand(True)
        code_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        grid.attach(code_label, 1, 0, 1, 1)

        detail_text = f"{event_type} value={value}"
        if source:
            detail_text = f"{detail_text} source={source}"
        details = Gtk.Label(label=detail_text)
        details.add_css_class("caption")
        details.add_css_class("dim-label")
        details.set_xalign(0.0)
        details.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        grid.attach(details, 1, 1, 1, 1)

        row.set_child(grid)
        self._event_list.insert(row, 0)
        self._event_rows.insert(0, row)
        while len(self._event_rows) > EVENT_ROW_LIMIT:
            old = self._event_rows.pop()
            self._event_list.remove(old)

    def _on_event_filter_toggled(self: Any, _button: Gtk.ToggleButton) -> None:
        self._cancel_event_render()
        self._render_event_rows()

    def _on_copy_events_clicked(self: Any, _button: Gtk.Button) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        display.get_clipboard().set(self._visible_event_export_text())

    def _visible_event_export_text(self: Any) -> str:
        return self._event_history.export(self._active_event_categories())

    def _render_event_rows(self: Any) -> None:
        for row in list(self._event_rows):
            self._event_list.remove(row)
        self._event_rows.clear()
        for event in reversed(self._visible_event_history()):
            self._prepend_event_row(event)

    def _visible_event_history(self: Any) -> list[Payload]:
        return self._event_history.visible(self._active_event_categories())

    def _active_event_categories(self: Any) -> set[str]:
        return {
            category
            for category, button in self._event_filter_buttons.items()
            if button.get_active()
        }

    def _event_filter_active(self: Any, category: str) -> bool:
        button = self._event_filter_buttons.get(category)
        return bool(button and button.get_active())

    def _queue_event_render(self: Any) -> None:
        if self._event_render_source_id:
            return
        self._event_render_source_id = GLib.timeout_add(
            EVENT_RENDER_THROTTLE_MS,
            self._flush_event_render,
        )

    def _flush_event_render(self: Any) -> bool:
        self._event_render_source_id = 0
        if not self._session.closing:
            self._render_event_rows()
        return False

    def _cancel_event_render(self: Any) -> None:
        if not self._event_render_source_id:
            return
        GLib.source_remove(self._event_render_source_id)
        self._event_render_source_id = 0
