"""Macro editor layout, toolbar, timing tools, and footer."""

import gi

# pyright: reportAttributeAccessIssue=false

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.widgets.macro_editor.timeline import TimelineWidget


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
        root.append(self._build_property_panel())
        root.append(self._build_name_row())
        footer_spacer = Gtk.Box()
        footer_spacer.set_vexpand(True)
        root.append(footer_spacer)
        root.append(self._build_footer())

        frame = Gtk.Frame()
        frame.add_css_class("macro-editor-outline")
        frame.set_child(root)
        self.set_child(frame)
        GLib.idle_add(self._update_canvas_width)

    def _build_toolbar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.set_margin_top(4)
        bar.set_margin_bottom(4)
        bar.set_margin_start(4)
        bar.set_margin_end(4)

        timing_btn = Gtk.MenuButton(label="Timing Tools")
        timing_btn.add_css_class("flat")
        timing_btn.set_popover(self._build_timing_popover())
        bar.append(timing_btn)

        undo_btn = Gtk.Button(label="Undo All")
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

        reset_fit_btn = Gtk.Button(label="Reset Fit")
        reset_fit_btn.add_css_class("flat")
        reset_fit_btn.set_tooltip_text("Fit timeline to visible width")
        reset_fit_btn.connect("clicked", self._on_reset_fit)
        bar.append(reset_fit_btn)

        sep2 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep2.set_margin_start(4)
        sep2.set_margin_end(4)
        bar.append(sep2)

        self._lock_btn = Gtk.ToggleButton(label="Lock Move")
        self._lock_btn.set_active(True)
        self._lock_btn.set_tooltip_text("When locked, events cannot be dragged")
        self._lock_btn.connect("toggled", self._on_move_lock_toggled)
        bar.append(self._lock_btn)

        self._erase_btn = Gtk.ToggleButton(label="Erase")
        self._erase_btn.set_tooltip_text(
            "When on: drag across a lane to delete the events it touches; "
            "right-drag to ripple delete a time span across all lanes and "
            "close the gap"
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

    def _build_timing_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(10)
        box.set_margin_end(10)

        title = Gtk.Label(label="Timing Tools")
        title.add_css_class("heading")
        title.set_halign(Gtk.Align.START)
        box.append(title)

        hint = Gtk.Label(label="Trim silence and shape waiting times")
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        hint.set_halign(Gtk.Align.START)
        box.append(hint)

        trim_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        trim_start_btn = Gtk.Button(label="Trim Start")
        trim_start_btn.connect("clicked", self._on_trim_start_clicked)
        trim_row.append(trim_start_btn)
        trim_end_btn = Gtk.Button(label="Trim End")
        trim_end_btn.connect("clicked", self._on_trim_end_clicked)
        trim_row.append(trim_end_btn)
        box.append(trim_row)

        box.append(Gtk.Separator())

        scale_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        scale_row.append(Gtk.Label(label="Scale:"))
        timing_scale_spin = Gtk.SpinButton()
        self._timing_scale_spin = timing_scale_spin
        timing_scale_spin.set_adjustment(
            Gtk.Adjustment(
                value=1.00,
                lower=0.10,
                upper=10.00,
                step_increment=0.10,
            )
        )
        timing_scale_spin.set_digits(2)
        timing_scale_spin.set_width_chars(5)
        scale_row.append(timing_scale_spin)
        scale_row.append(Gtk.Label(label="x"))
        apply_scale_btn = Gtk.Button(label="Apply")
        apply_scale_btn.connect("clicked", self._on_apply_scale_clicked)
        scale_row.append(apply_scale_btn)
        box.append(scale_row)

        gap_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        gap_row.append(Gtk.Label(label="Min gap (ms):"))
        timing_min_gap_spin = Gtk.SpinButton()
        self._timing_min_gap_spin = timing_min_gap_spin
        timing_min_gap_spin.set_adjustment(
            Gtk.Adjustment(
                value=0.0,
                lower=0.0,
                upper=2000.0,
                step_increment=1.0,
            )
        )
        timing_min_gap_spin.set_digits(0)
        timing_min_gap_spin.set_width_chars(5)
        gap_row.append(timing_min_gap_spin)
        gap_row.append(Gtk.Label(label="Max gap (ms):"))
        timing_max_gap_spin = Gtk.SpinButton()
        self._timing_max_gap_spin = timing_max_gap_spin
        timing_max_gap_spin.set_adjustment(
            Gtk.Adjustment(
                value=250.0,
                lower=0.0,
                upper=10000.0,
                step_increment=10.0,
            )
        )
        timing_max_gap_spin.set_digits(0)
        timing_max_gap_spin.set_width_chars(5)
        gap_row.append(timing_max_gap_spin)
        box.append(gap_row)

        apply_gap_btn = Gtk.Button(label="Apply Gap Limits")
        apply_gap_btn.connect("clicked", self._on_apply_gap_limits_clicked)
        box.append(apply_gap_btn)

        box.append(Gtk.Separator())

        extend_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        extend_row.append(Gtk.Label(label="Time (ms):"))
        timing_extend_ms_spin = Gtk.SpinButton()
        self._timing_extend_ms_spin = timing_extend_ms_spin
        timing_extend_ms_spin.set_adjustment(
            Gtk.Adjustment(
                value=100.0,
                lower=0.0,
                upper=600000.0,
                step_increment=10.0,
            )
        )
        timing_extend_ms_spin.set_digits(0)
        timing_extend_ms_spin.set_width_chars(7)
        extend_row.append(timing_extend_ms_spin)
        box.append(extend_row)

        extend_btn_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        add_start_btn = Gtk.Button(label="Add at Start")
        add_start_btn.connect("clicked", self._on_add_time_start_clicked)
        extend_btn_row.append(add_start_btn)
        add_end_btn = Gtk.Button(label="Add at End")
        add_end_btn.connect("clicked", self._on_add_time_end_clicked)
        extend_btn_row.append(add_end_btn)
        total_time_btn = Gtk.Button(label="Total Time")
        total_time_btn.connect("clicked", self._on_set_total_time_clicked)
        extend_btn_row.append(total_time_btn)
        box.append(extend_btn_row)

        box.append(Gtk.Separator())

        insert_title = Gtk.Label(label="Insert Wait")
        insert_title.add_css_class("heading")
        insert_title.set_halign(Gtk.Align.START)
        box.append(insert_title)

        at_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        at_row.append(Gtk.Label(label="At (ms):"))
        insert_gap_at_spin = Gtk.SpinButton()
        self._insert_gap_at_spin = insert_gap_at_spin
        insert_gap_at_spin.set_adjustment(
            Gtk.Adjustment(
                value=0.0,
                lower=0.0,
                upper=3600000.0,
                step_increment=1.0,
            )
        )
        insert_gap_at_spin.set_digits(0)
        insert_gap_at_spin.set_width_chars(7)
        at_row.append(insert_gap_at_spin)
        box.append(at_row)

        gap_insert_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        gap_insert_row.append(Gtk.Label(label="Wait (ms):"))
        insert_gap_ms_spin = Gtk.SpinButton()
        self._insert_gap_ms_spin = insert_gap_ms_spin
        insert_gap_ms_spin.set_adjustment(
            Gtk.Adjustment(
                value=100.0,
                lower=0.0,
                upper=60000.0,
                step_increment=10.0,
            )
        )
        insert_gap_ms_spin.set_digits(0)
        insert_gap_ms_spin.set_width_chars(7)
        gap_insert_row.append(insert_gap_ms_spin)
        box.append(gap_insert_row)

        insert_btn = Gtk.Button(label="Insert Wait")
        insert_btn.add_css_class("suggested-action")
        insert_btn.connect("clicked", self._on_insert_gap_clicked)
        box.append(insert_btn)

        pop.set_child(box)
        return pop

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
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END)
        footer.set_margin_top(8)
        footer.set_margin_bottom(4)
        footer.set_margin_end(8)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_close_clicked)
        footer.append(cancel_btn)

        copy_btn = Gtk.Button(label="Save as Copy…")
        copy_btn.connect("clicked", self._on_save_as_copy)
        footer.append(copy_btn)

        apply_btn = Gtk.Button(label="Apply")
        apply_btn.connect("clicked", self._on_apply)
        footer.append(apply_btn)

        save_btn = Gtk.Button(label="Save Changes")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._on_save)
        footer.append(save_btn)

        self._footer_action_buttons = [
            cancel_btn,
            copy_btn,
            apply_btn,
            save_btn,
        ]
        return footer

    def _on_move_lock_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._drag_locked = btn.get_active()
        btn.set_label("Lock Move" if self._drag_locked else "Move Unlocked")

    def _on_erase_mode_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._erase_mode = btn.get_active()
        if self._erase_mode:
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

        self._cancel_capture_start_position("")
        self._cancel_capture_selected_move("")
        self._apply_macro_state(self._initial_macro_data)

        self._timeline._selected = None
        self._revealer.set_reveal_child(False)
        self._timeline._context_menu_x = None
        self._timeline._hover_x = None
        self._timeline._hover_y = None

        self._sync_macro_settings_controls()
        self._macro_capture_delay_spin.set_value(self._start_position_capture.delay_seconds)

        self._auto_zoom_enabled = True
        self._set_timeline_scroll(0.0)
        self._update_stats()
        self._update_canvas_width()
        self._timeline.queue_draw()
        self._sync_close_guard()

    def _on_zoom_in(self, btn) -> None:
        self._zoom_timeline(1.25)

    def _on_zoom_out(self, btn) -> None:
        self._zoom_timeline(1.0 / 1.25)
