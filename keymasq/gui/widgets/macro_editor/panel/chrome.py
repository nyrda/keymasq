"""Macro editor layout, toolbar, timing tools, and footer."""

import logging

import gi

# pyright: reportAttributeAccessIssue=false

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.widgets.docs_links import docs_page_url
from keymasq.gui.widgets.macro_editor.panel.rows import field_row, rows_list, unit_label
from keymasq.gui.widgets.macro_editor.timeline import TimelineWidget

log = logging.getLogger(__name__)


class EditorChromeMixin:
    """Construct and coordinate the dialog's persistent editor chrome."""

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.set_margin_top(8)
        root.set_margin_bottom(8)
        root.set_margin_start(8)
        root.set_margin_end(8)

        root.append(self._build_toolbar())
        root.append(Gtk.Separator())

        root.append(self._build_timeline_area())
        root.append(Gtk.Separator())
        root.append(self._build_inspector())
        root.append(self._build_footer())

        frame = Gtk.Frame()
        frame.add_css_class("macro-editor-outline")
        frame.set_child(root)
        self._editor_key_controller = Gtk.EventControllerKey()
        self._editor_key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self._editor_key_controller.connect("key-pressed", self._on_editor_key_pressed)
        frame.add_controller(self._editor_key_controller)

        busy_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        busy_content.set_halign(Gtk.Align.CENTER)
        busy_content.set_valign(Gtk.Align.CENTER)
        busy_spinner = Gtk.Spinner()
        busy_spinner.set_size_request(32, 32)
        busy_content.append(busy_spinner)
        busy_label = Gtk.Label(label="Loading macro…")
        busy_label.add_css_class("dim-label")
        busy_content.append(busy_label)

        busy_overlay = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        busy_overlay.add_css_class("macro-editor-busy-overlay")
        busy_overlay.append(busy_content)

        overlay = Gtk.Overlay()
        overlay.set_child(frame)
        overlay.add_overlay(busy_overlay)
        self.set_child(overlay)

        self._editor_content = frame
        self._editor_busy_overlay = busy_overlay
        self._editor_busy_spinner = busy_spinner
        self._editor_busy_label = busy_label
        self._set_editor_busy(True, "Loading macro…")
        GLib.idle_add(self._update_canvas_width)

    def _build_inspector(self) -> Gtk.Widget:
        columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        # Both columns share one width; the separator between them does not.
        column_widths = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        columns.set_margin_top(10)
        columns.set_margin_bottom(6)
        columns.set_margin_start(8)
        columns.set_margin_end(8)

        selection_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        # The property panel creates the shared control width group the selection panel joins.
        property_panel = self._build_property_panel()
        self._inspector_placeholder = self._build_selection_panel()
        selection_column.append(self._inspector_placeholder)
        selection_column.append(property_panel)
        self._revealer.connect("notify::reveal-child", self._on_inspector_reveal_changed)
        selection_column.set_hexpand(True)
        column_widths.add_widget(selection_column)
        columns.append(selection_column)

        columns.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        macro_column = self._build_name_row()
        macro_column.set_valign(Gtk.Align.START)
        macro_column.set_hexpand(True)
        column_widths.add_widget(macro_column)
        columns.append(macro_column)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_propagate_natural_height(True)
        scrolled.add_css_class("macro-inspector")
        scrolled.set_child(columns)
        return scrolled

    def _on_inspector_reveal_changed(self, revealer: Gtk.Revealer, _pspec) -> None:
        self._inspector_placeholder.set_visible(not revealer.get_reveal_child())

    def _set_editor_busy(self, busy: bool, message: str = "") -> None:
        if self._dialog_closed:
            return
        if self._editor_content is not None:
            self._editor_content.set_sensitive(not busy)
        if self._editor_busy_overlay is not None:
            self._editor_busy_overlay.set_visible(busy)
        if message and self._editor_busy_label is not None:
            self._editor_busy_label.set_label(message)
        if self._editor_busy_spinner is not None:
            if busy:
                self._editor_busy_spinner.start()
            else:
                self._editor_busy_spinner.stop()

    def _build_toolbar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.set_margin_top(4)
        bar.set_margin_bottom(4)
        bar.set_margin_start(4)
        bar.set_margin_end(4)

        self._undo_button = Gtk.Button(icon_name="edit-undo-symbolic")
        self._undo_button.add_css_class("flat")
        self._undo_button.set_tooltip_text("Undo (Ctrl+Z)")
        self._undo_button.set_sensitive(False)
        self._undo_button.connect("clicked", self._on_undo_clicked)
        bar.append(self._undo_button)
        self._redo_button = Gtk.Button(icon_name="edit-redo-symbolic")
        self._redo_button.add_css_class("flat")
        self._redo_button.set_tooltip_text("Redo (Ctrl+Shift+Z)")
        self._redo_button.set_sensitive(False)
        self._redo_button.connect("clicked", self._on_redo_clicked)
        bar.append(self._redo_button)
        undo_btn = Gtk.Button(label="Revert")
        undo_btn.add_css_class("flat")
        undo_btn.set_tooltip_text("Restore macro to loaded state")
        undo_btn.connect("clicked", self._on_undo_all_changes)
        bar.append(undo_btn)

        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep.set_margin_start(4)
        sep.set_margin_end(4)
        bar.append(sep)

        zoom_out_btn = Gtk.Button(label="−")
        zoom_out_btn.add_css_class("flat")
        zoom_out_btn.set_tooltip_text("Zoom out")
        zoom_out_btn.connect("clicked", self._on_zoom_out)
        bar.append(zoom_out_btn)

        zoom_in_btn = Gtk.Button(label="+")
        zoom_in_btn.add_css_class("flat")
        zoom_in_btn.set_tooltip_text("Zoom in")
        zoom_in_btn.connect("clicked", self._on_zoom_in)
        bar.append(zoom_in_btn)

        reset_fit_btn = Gtk.Button(label="Fit")
        reset_fit_btn.add_css_class("flat")
        reset_fit_btn.set_tooltip_text("Reset fit: fit timeline to visible width")
        reset_fit_btn.connect("clicked", self._on_reset_fit)
        bar.append(reset_fit_btn)

        sep2 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep2.set_margin_start(4)
        sep2.set_margin_end(4)
        bar.append(sep2)

        self._move_btn = Gtk.ToggleButton(label="Move Actions")
        self._move_btn.set_tooltip_text(
            "Allow dragging selected actions to change their timing. "
            "Also shows gaps for click-to-edit; double-click gaps when off."
        )
        self._move_btn.connect("toggled", self._on_move_actions_toggled)
        bar.append(self._move_btn)

        self._erase_btn = Gtk.ToggleButton(label="Erase")
        self._erase_btn.set_tooltip_text(
            "Drag to erase time across all tracks and shift later actions earlier. "
            "Keys held across the entire span stay held and become shorter. "
            "To delete actions without shifting time, select them and press Delete or Backspace."
        )
        self._erase_btn.connect("toggled", self._on_erase_mode_toggled)
        bar.append(self._erase_btn)

        self._stats_label = Gtk.Label()
        self._stats_label.add_css_class("dim-label")
        self._stats_label.add_css_class("caption")
        self._stats_label.set_hexpand(True)
        self._stats_label.set_halign(Gtk.Align.END)
        bar.append(self._stats_label)
        self._update_stats()
        return bar

    def _build_timing_tools(self) -> Gtk.Widget:
        """Build the Timing Tools content once; the dialog reuses it on every open."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        for margin in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{margin}")(16)

        def spin(value: float, upper: float) -> Gtk.SpinButton:
            widget = Gtk.SpinButton()
            widget.set_adjustment(
                Gtk.Adjustment(value=value, lower=0.0, upper=upper, step_increment=10.0)
            )
            widget.set_digits(0)
            widget.set_width_chars(8)
            return widget

        trim_start_btn = Gtk.Button(label="Trim Start")
        trim_start_btn.set_tooltip_text("Remove silence before the first action")
        trim_start_btn.connect("clicked", self._on_trim_start_clicked)
        trim_end_btn = Gtk.Button(label="Trim End")
        trim_end_btn.set_tooltip_text("Remove silence after the last action")
        trim_end_btn.connect("clicked", self._on_trim_end_clicked)
        trim_row = field_row("Trim", trim_start_btn, trim_end_btn)

        self._timing_extend_ms_spin = spin(100.0, 600000.0)
        insert_row = field_row("Insert time", self._timing_extend_ms_spin, unit_label("ms"))
        at_cursor_btn = Gtk.Button(label="At Cursor")
        at_cursor_btn.set_tooltip_text("Add empty time at the insertion cursor")
        at_cursor_btn.connect("clicked", self._on_insert_time_at_cursor_clicked)
        at_start_btn = Gtk.Button(label="At Start")
        at_start_btn.set_tooltip_text("Add leading silence")
        at_start_btn.connect("clicked", self._on_add_time_start_clicked)
        at_end_btn = Gtk.Button(label="At End")
        at_end_btn.set_tooltip_text("Add trailing silence")
        at_end_btn.connect("clicked", self._on_add_time_end_clicked)
        insert_buttons = field_row("", at_cursor_btn, at_start_btn, at_end_btn)

        self._timing_total_spin = spin(0.0, 3600000.0)
        set_total_btn = Gtk.Button(label="Set")
        set_total_btn.set_tooltip_text("Trailing silence grows or shrinks to reach this length")
        set_total_btn.connect("clicked", self._on_set_total_time_clicked)
        total_row = field_row(
            "Total time",
            self._timing_total_spin,
            unit_label("ms"),
            set_total_btn,
            subtitle="Cannot end before the last action",
        )

        box.append(rows_list(trim_row, insert_row, insert_buttons, total_row))
        self._timing_content = box
        return box

    def _present_timing_dialog(self, _button: Gtk.Widget | None = None) -> None:
        if self._timing_content is None:
            self._build_timing_tools()
        content = self._timing_content
        assert content is not None
        if self._timing_total_spin is not None:
            self._timing_total_spin.set_value(self._duration_us / 1000)
        dialog = Adw.Dialog(title="Timing Tools", content_width=420)
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        wrapper.append(content)
        close_btn = Gtk.Button(label="Close")
        close_btn.set_halign(Gtk.Align.END)
        close_btn.set_margin_end(16)
        close_btn.set_margin_bottom(16)
        close_btn.connect("clicked", self._on_close_dialog_clicked, dialog)
        wrapper.append(close_btn)
        dialog.set_child(wrapper)

        def release_content(closed_dialog: Adw.Dialog) -> None:
            wrapper.remove(content)
            closed_dialog.set_child(None)
            if self._timing_dialog is closed_dialog:
                self._timing_dialog = None

        self._timing_dialog = dialog

        dialog.connect("closed", release_content)
        dialog.present(self._parent)

    def _build_timeline_area(self) -> Gtk.Widget:
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        self._scrolled = Gtk.ScrolledWindow()
        self._scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
        self._scrolled.set_overlay_scrolling(False)
        self._scrolled.set_vexpand(False)
        self._scrolled.connect("notify::width", self._on_timeline_viewport_changed)

        self._timeline = TimelineWidget(self)
        self._scrolled.set_child(self._timeline)
        container.append(self._scrolled)

        timeline_scroll_adj = Gtk.Adjustment(
            value=0.0,
            lower=0.0,
            upper=1.0,
            step_increment=32.0,
            page_increment=160.0,
            page_size=1.0,
        )
        self._timeline_scroll_adj = timeline_scroll_adj
        timeline_scroll_adj.connect("value-changed", self._on_timeline_scroll_adjustment_changed)

        hscroll = Gtk.Scrollbar(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=timeline_scroll_adj,
        )
        hscroll.set_hexpand(True)
        container.append(hscroll)
        return container

    def _on_timeline_viewport_changed(self, _widget, _pspec) -> None:
        GLib.idle_add(self._update_canvas_width)

    def _build_footer(self) -> Gtk.Widget:
        footer = Gtk.CenterBox()
        footer.set_margin_top(8)
        footer.set_margin_bottom(4)
        footer.set_margin_start(8)
        footer.set_margin_end(8)

        docs_btn = Gtk.Button(label="?")
        docs_btn.add_css_class("flat")
        docs_btn.add_css_class("actions-docs-button")
        docs_btn.set_tooltip_text("Open Macro timeline editor documentation")
        docs_btn.connect("clicked", self._on_editor_docs_clicked)
        footer.set_start_widget(docs_btn)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_end_widget(actions)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_close_clicked)
        actions.append(cancel_btn)

        copy_btn = Gtk.Button(label="Save as Copy…")
        copy_btn.connect("clicked", self._on_save_as_copy)
        actions.append(copy_btn)

        apply_btn = Gtk.Button(label="Apply")
        apply_btn.connect("clicked", self._on_apply)
        actions.append(apply_btn)

        save_btn = Gtk.Button(label="Save Changes")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._on_save)
        actions.append(save_btn)

        self._footer_action_buttons = [
            cancel_btn,
            copy_btn,
            apply_btn,
            save_btn,
        ]
        return footer

    def _on_editor_docs_clicked(self, _button: Gtk.Button) -> None:
        url = docs_page_url("MACRO_EDITOR")
        try:
            launcher = Gtk.UriLauncher.new(url)
            launcher.launch(None, None, None)
        except Exception:
            log.exception("Could not open Macro timeline editor documentation %s", url)

    def _on_move_actions_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._drag_locked = not btn.get_active()
        if btn.get_active():
            btn.add_css_class("suggested-action")
        else:
            btn.remove_css_class("suggested-action")
        self._timeline.set_move_locked(self._drag_locked)

    def _on_erase_mode_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._erase_mode = btn.get_active()
        if self._erase_mode:
            self._timeline.clear_gap_selection()
            btn.add_css_class("destructive-action")
            self._timeline.set_cursor_from_name("crosshair")
        else:
            btn.remove_css_class("destructive-action")
            self._timeline.set_cursor_from_name(None)
            self._timeline._reset_erase_drag()
            self._timeline.queue_draw()

    def _on_reset_fit(self, _btn) -> None:
        self._auto_zoom_enabled = True
        self._set_timeline_scroll(0.0)
        self._update_canvas_width()
        self._timeline.queue_draw()

    def _on_undo_all_changes(self, _btn) -> None:
        if not self._initial_macro_data:
            return
        self._history_restoring = True
        try:
            self._revert_to_saved_state()
        finally:
            self._history_restoring = False
        self._sync_close_guard()

    def _revert_to_saved_state(self) -> None:
        self._cancel_capture_selected_move("")
        self._apply_macro_state(self._initial_macro_data)

        self._timeline._selected = None
        self._timeline.set_selection([])
        self._revealer.set_reveal_child(False)
        self._timeline._context_menu_x = None
        self._timeline._hover_x = None
        self._timeline._hover_y = None
        self._timeline.clear_gap_selection()

        self._sync_macro_settings_controls()

        self._auto_zoom_enabled = True
        self._set_timeline_scroll(0.0)
        self._update_stats()
        self._update_canvas_width()
        self._timeline.queue_draw()

    def _on_zoom_in(self, btn) -> None:
        self._zoom_timeline(1.25)

    def _on_undo_clicked(self, _btn) -> None:
        self._restore_history()

    def _on_redo_clicked(self, _btn) -> None:
        self._restore_history(redo=True)

    def _on_zoom_out(self, btn) -> None:
        self._zoom_timeline(1.0 / 1.25)
