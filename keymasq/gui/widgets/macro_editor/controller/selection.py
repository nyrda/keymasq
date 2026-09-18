"""Bulk editing commands, application clipboard, and undo coordination."""

# pyright: reportAttributeAccessIssue=false, reportUnknownLambdaType=false

import json
from collections.abc import Callable
from dataclasses import dataclass

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
from keymasq.gui.widgets.macro_editor.panel.rows import field_row, rows_list, unit_label
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

    _SELECTION_HINT = "Right-click the timeline to cut, copy, or delete the selection"

    def _build_selection_panel(self) -> Gtk.Widget:
        """Build the left inspector column shown when no single action is selected."""
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._selection_summary = Gtk.Label(label="")
        self._selection_summary.add_css_class("heading")
        self._selection_summary.set_wrap(True)
        self._selection_summary.set_xalign(0.0)
        self._selection_summary.set_halign(Gtk.Align.START)
        self._selection_summary.set_visible(False)
        panel.append(self._selection_summary)
        self._selection_hint = Gtk.Label(label=self._SELECTION_HINT)
        self._selection_hint.add_css_class("dim-label")
        self._selection_hint.add_css_class("caption")
        self._selection_hint.set_wrap(True)
        self._selection_hint.set_xalign(0.0)
        self._selection_hint.set_halign(Gtk.Align.START)
        self._selection_hint.set_visible(False)
        panel.append(self._selection_hint)

        # Selection Timing renders inline here while a range or group is selected.
        self._selection_timing_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._selection_timing_box.set_margin_bottom(8)
        self._selection_timing_box.set_visible(False)
        self._selection_timing_key: object = None
        panel.append(self._selection_timing_box)

        # The insertion cursor: where Add and Paste put new actions.
        self._insertion_spin = Gtk.SpinButton.new_with_range(0, 3_600_000, 1)
        self._insertion_spin.set_digits(3)
        self._insertion_spin.set_width_chars(10)
        self._insertion_spin.set_tooltip_text(
            "The dotted line marks where new and pasted actions go. Click the timeline to move it."
        )
        self._insertion_spin.connect("value-changed", self._on_insertion_changed)
        insert_row = field_row("Insert at", self._insertion_spin, unit_label("ms"))
        control_group: Gtk.SizeGroup | None = getattr(self, "_control_width_group", None)
        if control_group is not None:
            control_group.add_widget(self._insertion_spin)
        self._insert_row = rows_list(insert_row)
        panel.append(self._insert_row)

        # With nothing selected the column offers everything the right-click menu adds.
        self._insert_grid = Gtk.Grid(column_spacing=6, row_spacing=6)
        self._insert_grid.set_column_homogeneous(True)
        add_actions: list[tuple[str, str, Callable[[], None]]] = [
            (
                "Key",
                "Adds a key, held for a set time or pulsed with rapidfire",
                lambda: self._add_input_at_cursor("keyboard"),
            ),
            (
                "Mouse Button",
                "Adds a mouse button, held for a set time or pulsed with rapidfire",
                lambda: self._add_input_at_cursor("mouse"),
            ),
            (
                "Gamepad Button",
                "Adds a gamepad button, held or pulsed with rapidfire, or an axis value",
                lambda: self._add_input_at_cursor("gamepad"),
            ),
            (
                "Mouse Move",
                "Adds a mouse move to a position",
                self._add_move_at_cursor,
            ),
            (
                "Wait",
                "Halts playback for a set time that speed changes do not affect",
                lambda: self._insert_wait_at(self._timeline._insertion_us),
            ),
            (
                "Run Command",
                "Adds a shell command to fill in",
                lambda: self._insert_exec_at(self._timeline._insertion_us),
            ),
            (
                "Call Macro",
                "Runs another saved macro",
                self._add_macro_call_at_cursor,
            ),
            (
                "Compositor Action",
                "Adds an action for the running compositor",
                lambda: self._insert_compositor_action(self._timeline._insertion_us),
            ),
        ]
        for index, (label, tooltip, callback) in enumerate(add_actions):
            button = Gtk.Button(label=label)
            button.set_tooltip_text(tooltip)
            button.set_hexpand(True)
            button.connect("clicked", lambda _b, cb=callback: cb())
            self._insert_grid.attach(button, index % 2, index // 2, 1, 1)
        panel.append(self._insert_grid)

        # Paste buttons are shown only while the clipboard holds a macro fragment.
        paste_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._paste_buttons = paste_buttons
        self._paste_button = Gtk.Button(label="Paste")
        self._paste_button.set_tooltip_text("Paste copied actions at the insertion cursor (Ctrl+V)")
        self._paste_button.connect("clicked", lambda _b: self._paste_selection())
        paste_buttons.append(self._paste_button)
        self._paste_shift_button = Gtk.Button(label="Paste and Shift Later Actions")
        self._paste_shift_button.set_tooltip_text(
            "Insert copied actions and move everything after the cursor later"
        )
        self._paste_shift_button.connect("clicked", lambda _b: self._paste_selection(insert=True))
        paste_buttons.append(self._paste_shift_button)
        panel.append(paste_buttons)

        self._timeline.get_clipboard().connect("changed", lambda _c: self._refresh_paste_buttons())
        self._refresh_paste_buttons()
        return panel

    def _add_input_at_cursor(self, device_type: str) -> None:
        self._present_add_key_dialog(
            default_t_us=self._timeline._insertion_us, device_type=device_type
        )

    def _add_move_at_cursor(self) -> None:
        self._present_mouse_move_dialog(default_t_us=self._timeline._insertion_us)

    def _add_macro_call_at_cursor(self) -> None:
        self._present_macro_call_dialog(
            mode="macro_sync", default_t_us=self._timeline._insertion_us
        )

    def _refresh_paste_buttons(self) -> None:
        if not hasattr(self, "_paste_button"):
            return
        available = has_macro_fragment(self._timeline.get_clipboard())
        self._paste_buttons.set_visible(available)
        self._sync_insert_row_visibility()

    def _sync_insert_row_visibility(self) -> None:
        # The cursor field only matters when adding or pasting is on offer.
        self._insert_row.set_visible(
            self._insert_grid.get_visible() or self._paste_buttons.get_visible()
        )

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
        elif selected:
            label = f"{len(selected)} selected · {(last - first) / 1000:g} ms"
        else:
            label = ""
        self._selection_summary.set_label(label)
        has_selection = bool(label)
        self._selection_summary.set_visible(has_selection)
        self._selection_hint.set_visible(has_selection)
        self._insert_grid.set_visible(not has_selection)
        self._sync_insert_row_visibility()
        self._sync_inline_selection_timing(selected)
        self._insertion_spin.set_value(self._timeline._insertion_us / 1000)
        self._refresh_paste_buttons()

    def _sync_inline_selection_timing(self, selected: list[selection.Item]) -> None:
        box = self._selection_timing_box
        key = (tuple(id(item) for item in selected), self._timeline._time_selection)
        if not selected:
            self._selection_timing_key = None
            box.set_visible(False)
            while (child := box.get_first_child()) is not None:
                box.remove(child)
            return
        if key == self._selection_timing_key and box.get_first_child() is not None:
            return
        self._selection_timing_key = key
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        ui = self._build_selection_timing(selected, on_done=None)
        box.append(ui.box)
        box.set_visible(True)

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
        commands.append(("Select All  Ctrl+A", self._select_all, True))
        # Only commands that apply right now are listed; the rest stay out of the way.
        for label, command, sensitive in commands:
            if not sensitive:
                continue
            button = Gtk.Button(label=label)
            button.add_css_class("flat")

            def activate(_button, callback=command) -> None:
                popover.popdown()
                self._timeline.grab_focus()
                callback()

            button.connect("clicked", activate)
            box.append(button)

    def _build_selection_timing(
        self,
        selected: list[selection.Item],
        *,
        on_done: Callable[[], None] | None,
    ) -> "_SelectionTimingUi":
        """Build the Move / Pauses / Scale tabs for the given selection.

        ``on_done`` runs after an apply or cancel. When it is ``None`` there is no Cancel
        button, which is the inline inspector form.
        """
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        if on_done is not None:
            box.set_size_request(360, -1)
            for margin in ("top", "bottom", "start", "end"):
                getattr(box, f"set_margin_{margin}")(16)
            heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            title = Gtk.Label(label="Selection Timing", xalign=0)
            title.add_css_class("heading")
            heading.append(title)
            count = len(selected)
            subtitle = Gtk.Label(
                label=f"{count} action{'s' if count != 1 else ''} selected", xalign=0
            )
            subtitle.add_css_class("dim-label")
            heading.append(subtitle)
            box.append(heading)

        stack = Gtk.Stack()
        stack.set_hhomogeneous(True)
        stack.set_vhomogeneous(True)
        switcher = Gtk.StackSwitcher()
        switcher.add_css_class("macro-selection-tabs")
        switcher.set_stack(stack)
        switcher.set_halign(Gtk.Align.FILL)
        switcher.set_hexpand(True)
        box.append(switcher)
        box.append(stack)
        inputs: dict[str, Gtk.SpinButton] = {}
        # Labels share one column across the tabs; spins share the inspector's control width.
        label_group = Gtk.SizeGroup.new(Gtk.SizeGroupMode.HORIZONTAL)
        control_group: Gtk.SizeGroup | None = getattr(self, "_control_width_group", None)

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
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            spin = Gtk.SpinButton.new_with_range(low, high, 1)
            spin.set_digits(1 if name == "scale" else 3)
            spin.set_numeric(True)
            spin.set_width_chars(8)
            spin.set_value(value)
            spin.set_tooltip_text(label)
            spin.connect("activate", lambda _s: apply())
            inputs[name] = spin
            if control_group is not None:
                control_group.add_widget(spin)
            row = field_row(label, spin, unit_label(unit), subtitle=help_text)
            row.bind_label_column(label_group)
            content.append(row)
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
            "Pause",
            "ms",
            0,
            3_600_000,
            100,
            (
                "Sets pauses between actions. Holds and overlaps stay together."
                if pauses_available
                else "No pauses to adjust here. Holds and overlaps stay together."
            ),
        )
        inputs["pauses"].set_sensitive(pauses_available)
        page(
            "scale",
            "Scale",
            "Duration",
            "%",
            0.1,
            100_000,
            100,
            "50% = twice as fast · 200% = twice as slow",
        )
        # Kept outside the stack so every page has the same height; shown on the Scale tab.
        wait_check = Gtk.CheckButton(label="Include wait durations")
        wait_check.set_tooltip_text("Also scale Wait and Random Wait durations.")
        wait_check.set_visible(False)
        box.append(wait_check)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END if on_done is not None else Gtk.Align.START)
        if on_done is None:
            footer.set_margin_top(8)
        cancel: Gtk.Button | None = None
        if on_done is not None:
            cancel_button = Gtk.Button(label="Cancel")
            cancel_button.connect("clicked", lambda _b: on_done())
            footer.append(cancel_button)
            cancel = cancel_button
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
            if on_done is not None:
                on_done()

        def focus_input() -> None:
            spin = inputs[stack.get_visible_child_name() or "move"]
            if not spin.get_sensitive():
                if cancel is not None:
                    cancel.grab_focus()
                return
            spin.grab_focus()
            spin.select_region(0, -1)

        def changed(_stack, _spec) -> None:
            name = stack.get_visible_child_name() or "move"
            apply_button.set_label({"move": "Move", "pauses": "Set Pauses", "scale": "Scale"}[name])
            apply_button.set_sensitive(name != "pauses" or pauses_available)
            wait_check.set_visible(name == "scale")
            if on_done is not None:
                focus_input()

        apply_button.connect("clicked", apply)
        stack.connect("notify::visible-child-name", changed)
        return _SelectionTimingUi(
            box=box,
            switcher=switcher,
            cancel=cancel,
            apply=apply,
            focus_input=focus_input,
        )

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
            popover.set_parent(self._timeline)
            popover.set_position(Gtk.PositionType.BOTTOM)

        ui = self._build_selection_timing(selected, on_done=popover.popdown)

        def key_pressed(_controller, keyval: int, _keycode: int, state: int) -> bool:
            if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
                return False
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                # Enter on Cancel or the tabs keeps its normal widget behavior.
                root = popover.get_root()
                focus = root.get_focus() if root is not None else None
                if focus is ui.cancel or (focus is not None and focus.is_ancestor(ui.switcher)):
                    return False
                ui.apply()
                return True
            if keyval == Gdk.KEY_Escape:
                popover.popdown()
                return True
            return False

        def closed(_popover) -> None:
            popover.unparent()
            self._timeline.grab_focus()

        keys = Gtk.EventControllerKey.new()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", key_pressed)
        popover.add_controller(keys)
        popover.set_child(ui.box)
        popover.connect("map", lambda _p: ui.focus_input())
        popover.connect("closed", closed)
        popover.popup()


@dataclass(frozen=True)
class _SelectionTimingUi:
    box: Gtk.Box
    switcher: Gtk.StackSwitcher
    cancel: Gtk.Button | None
    apply: Callable[[], None]
    focus_input: Callable[[], None]
