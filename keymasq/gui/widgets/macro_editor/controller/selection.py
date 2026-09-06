"""Bulk editing commands, application clipboard, and undo coordination."""

# pyright: reportAttributeAccessIssue=false, reportUnknownLambdaType=false

import json
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.session_client import GuiTaskResult
from keymasq.gui.widgets.macro_editor import selection
from keymasq.gui.widgets.macro_editor.clipboard import (
    MACRO_FRAGMENT_MIME,
    has_macro_fragment,
    read_macro_fragment,
)
from keymasq.gui.widgets.macro_editor.model import _format_time_us
from keymasq.gui.widgets.macro_editor.panel.controls import _set_entry_text_if_needed
from keymasq.gui.widgets.macro_editor.timing_ops import TimelineLists, sort_timeline_items

# Keep the fragment alive when its source dialog closes. The native clipboard
# owns a custom format so text fields never paste a macro's implementation data.
_clipboard_fragment: selection.Fragment | None = None
_clipboard_provider: object | None = None
_PERSISTENCE_FIELDS = ("revision", "created_at", "event_count")


class SelectionControllerMixin:
    def _on_editor_key_pressed(
        self, _controller: Gtk.EventControllerKey, keyval: int, _keycode: int, state: int
    ) -> bool:
        if (
            not state & Gdk.ModifierType.CONTROL_MASK
            or state & (Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK)
            or Gdk.keyval_to_lower(keyval) not in (Gdk.KEY_v, Gdk.KEY_z, Gdk.KEY_y)
            or not self._editor_content.is_sensitive()
            or self._timeline._bulk_drag_kind
        ):
            return False
        root = self._editor_content.get_root()
        focus = root.get_focus() if root is not None else None
        while focus is not None and focus is not self._editor_content:
            if isinstance(focus, (Gtk.Editable, Gtk.TextView, Gtk.Popover)):
                return False
            focus = focus.get_parent()
        if focus is None:
            return False
        if Gdk.keyval_to_lower(keyval) == Gdk.KEY_v:
            self._paste_selection()
            return True
        self._restore_history(
            redo=Gdk.keyval_to_lower(keyval) == Gdk.KEY_y
            or bool(state & Gdk.ModifierType.SHIFT_MASK)
        )
        return True

    def _timeline_lists(self) -> TimelineLists:
        return (
            self._events,
            self._rel_events,
            self._passthrough_events,
            self._synthetic_moves,
            self._control_events,
        )

    def _record_edit_history(self) -> None:
        if (
            not self._initial_state_loaded
            or self._history_restoring
            or self._updating_props
            or getattr(self._timeline, "_bulk_drag_active", False)
        ):
            return
        payload = self._current_macro_payload()
        # Saving metadata is not an undoable edit.
        for field in _PERSISTENCE_FIELDS:
            payload.pop(field, None)
        payload["device_types"] = sorted(payload.get("device_types", []))
        self._edit_history.record(payload)
        self._undo_button.set_sensitive(bool(self._edit_history.past))
        self._redo_button.set_sensitive(bool(self._edit_history.future))
        self._timeline.prune_selection()

    def _restore_history(self, *, redo: bool = False) -> None:
        payload = self._edit_history.redo() if redo else self._edit_history.undo()
        if payload is None:
            return
        metadata = {
            field: self._macro_data[field]
            for field in _PERSISTENCE_FIELDS
            if field in self._macro_data
        }
        self._history_restoring = True
        try:
            self._cancel_capture_start_position("")
            self._cancel_capture_selected_move("")
            self._apply_macro_state({**payload, **metadata})
            self._timeline.set_selection([])
            self._timeline.clear_gap_selection()
            _set_entry_text_if_needed(self._name_entry, str(payload.get("name", self._macro_name)))
            self._sync_macro_settings_controls()
            self._refresh_loaded_macro_state()
        finally:
            self._history_restoring = False
        self._sync_close_guard()

    def _finish_selection_edit(self) -> None:
        sort_timeline_items(*self._timeline_lists())
        self._duration_us = max(
            self._duration_us, selection.bounds(selection.items(self._timeline_lists()))[1]
        )
        if self._timeline._time_selection is not None:
            self._duration_us = max(self._duration_us, self._timeline._time_selection[1])
        self._refresh_after_timing_edit(recompute_duration=False)
        self._update_selection_summary()

    def _copy_selection(self) -> None:
        self._copy_to_clipboard()

    def _copy_to_clipboard(self) -> bool:
        global _clipboard_fragment, _clipboard_provider
        fragment = self._capture_selection()
        if fragment is None:
            return False
        provider = Gdk.ContentProvider.new_for_bytes(
            MACRO_FRAGMENT_MIME,
            GLib.Bytes.new(json.dumps(fragment.clipboard_payload()).encode()),
        )
        clipboard = self._timeline.get_clipboard()
        if clipboard.set_content(provider):
            _clipboard_fragment = fragment
            _clipboard_provider = provider
            return True
        return False

    def _capture_selection(self) -> selection.Fragment | None:
        selected = self._timeline.selected_items()
        time_range = self._timeline._time_selection
        if not selected and time_range is None:
            return None
        return selection.Fragment.capture(self._timeline_lists(), selected, time_range=time_range)

    def _available_fragment(self) -> selection.Fragment | None:
        if self._timeline.get_clipboard().get_content() is _clipboard_provider:
            return _clipboard_fragment
        return None

    def _cut_selection(self) -> None:
        if self._copy_to_clipboard():
            self._delete_selection()

    def _delete_selection(self) -> None:
        selected = self._timeline.selected_items()
        if not selected:
            self._timeline.set_selection([])
            return
        ids = {id(item) for item in selected}
        groups = self._timeline_lists()
        selection.remove_ids(groups[0], ids)
        selection.remove_ids(groups[1], ids)
        selection.remove_ids(groups[2], ids)
        selection.remove_ids(groups[3], ids)
        selection.remove_ids(groups[4], ids)
        self._timeline.set_selection([])
        self._finish_selection_edit()

    def _paste_selection(self, *, insert: bool = False, at_us: int | None = None) -> None:
        if self._paste_cancellable is not None or self._save_in_flight or self._dialog_closed:
            return
        destination_us = self._timeline._insertion_us if at_us is None else at_us
        fragment = self._available_fragment()
        if fragment is not None:
            self._paste_fragment(fragment, destination_us, insert=insert)
            return
        clipboard = self._timeline.get_clipboard()
        if not has_macro_fragment(clipboard):
            return
        self._paste_cancellable = Gio.Cancellable()
        self._set_editor_busy(True, "Pasting actions…")

        def finish(fragment: selection.Fragment | None, error: str | None) -> None:
            self._paste_cancellable = None
            if self._dialog_closed:
                return
            self._set_editor_busy(False)
            if fragment is not None:
                self._paste_fragment(fragment, destination_us, insert=insert)
            else:
                dialog = Adw.AlertDialog(heading="Unable to Paste Actions", body=error or "")
                dialog.add_response("ok", "OK")
                dialog.present(self)

        def parsed(result: GuiTaskResult[selection.Fragment]) -> bool:
            finish(result.value if result.ok else None, str(result.error) if result.error else None)
            return False

        def received(data: bytes | None, error: str | None) -> None:
            if data is None or self._dialog_closed:
                finish(None, error)
                return
            self._run_gui_task(lambda: selection.Fragment.from_clipboard(data), parsed)

        read_macro_fragment(clipboard, self._paste_cancellable, received)

    def _paste_fragment(
        self, fragment: selection.Fragment, at_us: int, *, insert: bool = False
    ) -> None:
        selected = fragment.paste(self._timeline_lists(), at_us, insert=insert)
        if insert and at_us <= self._duration_us:
            self._duration_us += fragment.duration_us
        self._duration_us = max(self._duration_us, at_us + fragment.duration_us)
        self._timeline.set_selection(
            selected,
            time_range=(at_us, at_us + fragment.duration_us) if fragment.preserve_range else None,
        )
        self._timeline._insertion_us = at_us + fragment.duration_us
        self._finish_selection_edit()
        # Async clipboard reads temporarily disable the editor and drop focus.
        # Return it after the content and property panel have been refreshed.
        self._timeline.grab_focus()

    def _select_all(self) -> None:
        self._timeline.set_selection(selection.items(self._timeline_lists()))

    def _build_selection_bar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_margin_top(4)
        bar.set_margin_bottom(4)
        menu = Gtk.MenuButton(label="Edit Selection")
        self._selection_menu_button = menu
        popover = Gtk.Popover()
        menu.set_popover(popover)
        popover.connect("show", lambda _p: self._populate_selection_menu(popover))
        bar.append(menu)
        self._selection_summary = Gtk.Label(label="No actions selected")
        self._selection_summary.add_css_class("dim-label")
        self._selection_summary.set_hexpand(True)
        self._selection_summary.set_halign(Gtk.Align.START)
        bar.append(self._selection_summary)
        bar.append(Gtk.Label(label="Paste at:"))
        self._insertion_spin = Gtk.SpinButton.new_with_range(0, 3_600_000, 1)
        self._insertion_spin.set_digits(3)
        self._insertion_spin.set_width_chars(10)
        self._insertion_spin.set_tooltip_text(
            "The dotted line marks where Paste inserts actions. "
            "Right-click the timeline to paste there."
        )
        self._insertion_spin.connect("value-changed", self._on_insertion_changed)
        bar.append(self._insertion_spin)
        bar.append(Gtk.Label(label="ms"))
        return bar

    def _on_insertion_changed(self, spin: Gtk.SpinButton) -> None:
        self._timeline._insertion_us = round(spin.get_value() * 1000)
        self._timeline.queue_draw()

    def _update_selection_summary(self) -> None:
        if not hasattr(self, "_selection_summary"):
            return
        selected = self._timeline.selected_items()
        first, last = selection.bounds(selected)
        if self._timeline._time_selection is not None:
            first, last = self._timeline._time_selection
            label = f"Range {first / 1000:g} to {last / 1000:g} ms · {len(selected)} selected"
        else:
            label = (
                f"{len(selected)} selected · {(last - first) / 1000:g} ms"
                if selected
                else "No actions selected"
            )
        self._selection_summary.set_label(label)
        self._insertion_spin.set_value(self._timeline._insertion_us / 1000)

    def _populate_selection_menu(self, popover: Gtk.Popover) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._append_selection_commands(box, popover)
        popover.set_child(box)

    def _append_selection_commands(
        self,
        box: Gtk.Box,
        popover: Gtk.Popover,
        *,
        paste_at_us: int | None = None,
        timeline_point: tuple[float, float] | None = None,
    ) -> None:
        selected = bool(self._timeline.selected_items())
        copyable = selected or self._timeline._time_selection is not None
        paste = has_macro_fragment(self._timeline.get_clipboard())
        paste_label = (
            f"Paste at {_format_time_us(paste_at_us)}"
            if paste_at_us is not None
            else "Paste  Ctrl+V"
        )
        commands: list[tuple[str, Callable[[], None], bool]] = [
            ("Cut  Ctrl+X", self._cut_selection, copyable),
            ("Copy  Ctrl+C", self._copy_selection, copyable),
            (paste_label, lambda: self._paste_selection(at_us=paste_at_us), paste),
            (
                "Paste and Shift Later Actions",
                lambda: self._paste_selection(insert=True, at_us=paste_at_us),
                paste,
            ),
            ("Delete Selected Actions", self._delete_selection, selected),
            (
                "Selection Timing…",
                lambda: self._show_selection_timing(timeline_point=timeline_point),
                selected,
            ),
        ]
        if timeline_point is None:
            commands.append(("Select All  Ctrl+A", self._select_all, True))
        for label, command, sensitive in commands:
            button = Gtk.Button(label=label)
            button.add_css_class("flat")
            button.set_sensitive(sensitive)

            def activate(_button, callback=command) -> None:
                popover.popdown()
                self._timeline.grab_focus()
                callback()

            button.connect("clicked", activate)
            box.append(button)

    def _show_selection_timing(self, *, timeline_point: tuple[float, float] | None = None) -> None:
        selected = self._timeline.selected_items()
        if not selected:
            return
        popover = Gtk.Popover()
        if timeline_point is not None:
            popover.set_parent(self._timeline)
            rect = Gdk.Rectangle()
            rect.x, rect.y = map(round, timeline_point)
            rect.width = rect.height = 1
            popover.set_pointing_to(rect)
            popover.set_position(Gtk.PositionType.RIGHT)
        else:
            popover.set_parent(self._selection_menu_button)
            popover.set_position(Gtk.PositionType.BOTTOM)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_size_request(360, -1)
        for margin in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{margin}")(16)
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title = Gtk.Label(label="Selection Timing", xalign=0)
        title.add_css_class("heading")
        heading.append(title)
        count = len(selected)
        subtitle = Gtk.Label(label=f"{count} action{'s' if count != 1 else ''} selected", xalign=0)
        subtitle.add_css_class("dim-label")
        heading.append(subtitle)
        box.append(heading)

        stack = Gtk.Stack()
        stack.set_hhomogeneous(True)
        stack.set_vhomogeneous(True)
        switcher = Gtk.StackSwitcher()
        switcher.set_stack(stack)
        switcher.set_halign(Gtk.Align.FILL)
        switcher.set_hexpand(True)
        box.append(switcher)
        box.append(stack)
        inputs: dict[str, Gtk.SpinButton] = {}
        unit_group = Gtk.SizeGroup.new(Gtk.SizeGroupMode.HORIZONTAL)

        def page(
            name: str,
            title: str,
            label: str,
            unit: str,
            low: float,
            high: float,
            value: float,
            help_text: str,
        ) -> Gtk.Box:
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            text = Gtk.Label(label=label, xalign=0)
            text.set_hexpand(True)
            line.append(text)
            spin = Gtk.SpinButton.new_with_range(low, high, 1)
            spin.set_digits(1 if name == "scale" else 3)
            spin.set_numeric(True)
            spin.set_width_chars(9)
            spin.set_value(value)
            spin.set_tooltip_text(label)
            inputs[name] = spin
            line.append(spin)
            unit_label = Gtk.Label(label=unit, xalign=0)
            unit_group.add_widget(unit_label)
            line.append(unit_label)
            content.append(line)
            help_label = Gtk.Label(label=help_text, xalign=0)
            help_label.set_wrap(True)
            help_label.set_max_width_chars(40)
            help_label.add_css_class("dim-label")
            content.append(help_label)
            stack.add_titled(content, name, title)
            return content

        page(
            "move",
            "Move",
            "Move by",
            "ms",
            -3_600_000,
            3_600_000,
            0,
            "Negative values move actions earlier.",
        )
        pauses_available = len(selection.pause_sections(selected)) > 1
        page(
            "pauses",
            "Pauses",
            "Pause between actions",
            "ms",
            0,
            3_600_000,
            100,
            (
                "Sets pauses between actions or overlapping groups. "
                "Keeps holds and overlaps unchanged."
                if pauses_available
                else "There are no pauses to adjust in this selection. "
                "Holds and overlapping actions stay together."
            ),
        )
        inputs["pauses"].set_sensitive(pauses_available)
        scale_page = page(
            "scale",
            "Scale",
            "Duration",
            "%",
            0.1,
            100_000,
            100,
            "50% = twice as fast · 200% = twice as slow",
        )
        wait_check = Gtk.CheckButton(label="Include wait durations")
        wait_check.set_tooltip_text("Also scale Wait and Random Wait durations.")
        scale_page.append(wait_check)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: popover.popdown())
        footer.append(cancel)
        apply_button = Gtk.Button(label="Move")
        apply_button.set_size_request(110, -1)
        apply_button.add_css_class("suggested-action")
        footer.append(apply_button)
        box.append(footer)

        def apply(_button=None) -> None:
            if not apply_button.get_sensitive():
                return
            name = stack.get_visible_child_name() or "move"
            spin = inputs[name]
            spin.update()
            value = spin.get_value()
            self._record_edit_history()
            if name == "move":
                time_range = self._timeline._time_selection
                delta = round(value * 1000)
                if time_range is not None:
                    delta = max(delta, -time_range[0])
                delta = selection.move(selected, delta)
                if time_range is not None:
                    self._timeline._time_selection = (time_range[0] + delta, time_range[1] + delta)
            elif name == "pauses":
                selection.set_pauses(selected, round(value * 1000))
            else:
                selection.scale(selected, value / 100, scale_waits=wait_check.get_active())
            self._finish_selection_edit()
            popover.popdown()

        def focus_input() -> None:
            spin = inputs[stack.get_visible_child_name() or "move"]
            if not spin.get_sensitive():
                cancel.grab_focus()
                return
            spin.grab_focus()
            spin.select_region(0, -1)

        def changed(_stack, _spec) -> None:
            name = stack.get_visible_child_name() or "move"
            apply_button.set_label({"move": "Move", "pauses": "Set Pauses", "scale": "Scale"}[name])
            apply_button.set_sensitive(name != "pauses" or pauses_available)
            focus_input()

        def key_pressed(_controller, keyval: int, _keycode: int, state: int) -> bool:
            if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
                return False
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                # Enter on Cancel or the tabs keeps its normal widget behavior.
                root = popover.get_root()
                focus = root.get_focus() if root is not None else None
                if focus is cancel or (focus is not None and focus.is_ancestor(switcher)):
                    return False
                apply()
                return True
            if keyval == Gdk.KEY_Escape:
                popover.popdown()
                return True
            return False

        def closed(_popover) -> None:
            popover.unparent()
            self._timeline.grab_focus()

        apply_button.connect("clicked", apply)
        stack.connect("notify::visible-child-name", changed)
        keys = Gtk.EventControllerKey.new()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", key_pressed)
        popover.add_controller(keys)
        popover.set_child(box)
        popover.connect("map", lambda _p: focus_input())
        popover.connect("closed", closed)
        popover.popup()
