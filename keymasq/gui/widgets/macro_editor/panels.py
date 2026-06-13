import gi

# pyright: reportAttributeAccessIssue=false, reportUnknownLambdaType=false

gi.require_version("Gtk", "4.0")

import evdev
from gi.repository import GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.gamepad_axes import clamp_gamepad_axis_value, gamepad_axis_range
from keymasq.gui.widgets.macro_editor.model import (
    EditableControl,
    EditableEvent,
    EditableMove,
    MacroEvent,
    _describe_compositor_control,
    _describe_passthrough_event,
    _get_event_name,
    _get_key_name,
)
from keymasq.gui.widgets.macro_editor.timeline import TimelineWidget

_LOOP_MODE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("none", "Once"),
    ("count", "Count"),
    ("hold", "While Held"),
    ("toggle", "Toggle"),
)


def _build_option_dropdown(
    options: tuple[tuple[str, str], ...],
    active_id: str,
) -> Gtk.DropDown:
    dropdown = Gtk.DropDown.new_from_strings([label for _, label in options])
    dropdown.set_enable_search(False)
    _set_dropdown_selected_id(dropdown, options, active_id)
    return dropdown


def _set_dropdown_selected_id(
    dropdown: Gtk.DropDown,
    options: tuple[tuple[str, str], ...],
    option_id: str,
    default_id: str | None = None,
) -> None:
    fallback_id = default_id or options[0][0]
    selected_index = 0
    for index, (current_id, _) in enumerate(options):
        if current_id == fallback_id:
            selected_index = index
            break
    for index, (current_id, _) in enumerate(options):
        if current_id == option_id:
            selected_index = index
            break
    dropdown.set_selected(selected_index)


def _get_dropdown_selected_id(
    dropdown: Gtk.DropDown,
    options: tuple[tuple[str, str], ...],
    default_id: str,
) -> str:
    index = int(dropdown.get_selected())
    if 0 <= index < len(options):
        return options[index][0]
    return default_id


def _set_entry_text_if_needed(entry: Gtk.Entry, text: str) -> None:
    """Avoid redundant Gtk.Entry updates that reset caret/focus state."""
    if entry.get_text() != text:
        entry.set_text(text)


def _is_gamepad_axis_event(ev: EditableEvent) -> bool:
    return ev.device_type == "gamepad" and ev.ev_type == evdev.ecodes.EV_ABS


def _event_target_name(ev: EditableEvent) -> str:
    return _get_event_name(ev.ev_type, ev.code).lower()


class MacroEditorPanelsMixin:
    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

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
        self._lock_btn.set_active(True)  # locked by default
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
            Gtk.Adjustment(value=1.00, lower=0.10, upper=10.00, step_increment=0.10)
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
            Gtk.Adjustment(value=0.0, lower=0.0, upper=2000.0, step_increment=1.0)
        )
        timing_min_gap_spin.set_digits(0)
        timing_min_gap_spin.set_width_chars(5)
        gap_row.append(timing_min_gap_spin)
        gap_row.append(Gtk.Label(label="Max gap (ms):"))
        timing_max_gap_spin = Gtk.SpinButton()
        self._timing_max_gap_spin = timing_max_gap_spin
        timing_max_gap_spin.set_adjustment(
            Gtk.Adjustment(value=250.0, lower=0.0, upper=10000.0, step_increment=10.0)
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
            Gtk.Adjustment(value=100.0, lower=0.0, upper=600000.0, step_increment=10.0)
        )
        timing_extend_ms_spin.set_digits(0)
        timing_extend_ms_spin.set_width_chars(7)
        extend_row.append(timing_extend_ms_spin)
        box.append(extend_row)

        extend_btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
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
            Gtk.Adjustment(value=0.0, lower=0.0, upper=3600000.0, step_increment=1.0)
        )
        insert_gap_at_spin.set_digits(0)
        insert_gap_at_spin.set_width_chars(7)
        at_row.append(insert_gap_at_spin)
        box.append(at_row)

        gap_insert_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        gap_insert_row.append(Gtk.Label(label="Wait (ms):"))
        insert_gap_ms_spin = Gtk.SpinButton()
        self._insert_gap_ms_spin = insert_gap_ms_spin
        insert_gap_ms_spin.set_adjustment(
            Gtk.Adjustment(value=100.0, lower=0.0, upper=60000.0, step_increment=10.0)
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
        timeline_scroll_adj.connect(
            "value-changed", self._on_timeline_scroll_adjustment_changed
        )

        hscroll = Gtk.Scrollbar(
            orientation=Gtk.Orientation.HORIZONTAL, adjustment=timeline_scroll_adj
        )
        hscroll.set_hexpand(True)
        container.append(hscroll)

        return container

    def _on_timeline_viewport_changed(self, _widget, _pspec) -> None:
        GLib.idle_add(self._update_canvas_width)

    def _build_property_panel(self) -> Gtk.Widget:
        self._revealer = Gtk.Revealer()
        self._revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._revealer.set_reveal_child(False)

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        panel.set_margin_top(8)
        panel.set_margin_bottom(4)
        panel.set_margin_start(8)
        panel.set_margin_end(8)

        # Title row
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._prop_title = Gtk.Label()
        self._prop_title.add_css_class("heading")
        self._prop_title.set_halign(Gtk.Align.START)
        self._prop_title.set_hexpand(True)
        title_row.append(self._prop_title)
        panel.append(title_row)

        # Timing row
        timing_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        timing_row.set_halign(Gtk.Align.START)

        self._press_label = Gtk.Label(label="Press:")
        timing_row.append(self._press_label)
        self._press_spin = Gtk.SpinButton()
        self._press_spin.set_adjustment(
            Gtk.Adjustment(value=0, lower=0, upper=3600000, step_increment=1, page_increment=10)
        )
        self._press_spin.set_digits(0)
        self._press_spin.set_width_chars(8)
        self._press_spin.connect("value-changed", self._on_press_changed)
        timing_row.append(self._press_spin)
        self._press_unit_label = Gtk.Label(label="ms")
        timing_row.append(self._press_unit_label)

        self._duration_text_label = Gtk.Label(label="Duration:")
        timing_row.append(self._duration_text_label)
        self._duration_spin = Gtk.SpinButton()
        self._duration_spin.set_adjustment(
            Gtk.Adjustment(value=1, lower=1, upper=3600000, step_increment=1, page_increment=10)
        )
        self._duration_spin.set_digits(0)
        self._duration_spin.set_width_chars(7)
        self._duration_spin.connect("value-changed", self._on_duration_changed)
        timing_row.append(self._duration_spin)
        self._duration_unit_label = Gtk.Label(label="ms")
        timing_row.append(self._duration_unit_label)

        self._release_label = Gtk.Label(label="  Release:")
        timing_row.append(self._release_label)
        self._release_spin = Gtk.SpinButton()
        self._release_spin.set_adjustment(
            Gtk.Adjustment(value=0, lower=0, upper=3600000, step_increment=1, page_increment=10)
        )
        self._release_spin.set_digits(0)
        self._release_spin.set_width_chars(8)
        self._release_spin.connect("value-changed", self._on_release_changed)
        timing_row.append(self._release_spin)
        self._release_unit_label = Gtk.Label(label="ms")
        timing_row.append(self._release_unit_label)

        panel.append(timing_row)

        move_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        move_row.set_halign(Gtk.Align.START)
        self._move_mode_label = Gtk.Label(label="Mode: REL")
        move_row.append(self._move_mode_label)

        self._move_capture_prefix_label = Gtk.Label(label="")
        move_row.append(self._move_capture_prefix_label)

        self._move_capture_delay_label = Gtk.Label(label="Capture in:")
        self._move_capture_delay_label.add_css_class("dim-label")
        move_row.append(self._move_capture_delay_label)

        self._move_capture_delay_spin = Gtk.SpinButton()
        self._move_capture_delay_spin.set_adjustment(
            Gtk.Adjustment(
                value=self._capture_delay_seconds, lower=0.2, upper=15.0, step_increment=0.2
            )
        )
        self._move_capture_delay_spin.set_digits(1)
        self._move_capture_delay_spin.set_width_chars(4)
        move_row.append(self._move_capture_delay_spin)

        self._move_capture_delay_unit_label = Gtk.Label(label="s")
        move_row.append(self._move_capture_delay_unit_label)

        self._move_capture_btn = Gtk.Button(label="Capture")
        self._move_capture_btn.connect("clicked", self._on_capture_selected_move_clicked)
        move_row.append(self._move_capture_btn)

        self._move_capture_colon_label = Gtk.Label(label="")
        move_row.append(self._move_capture_colon_label)

        self._move_x_label = Gtk.Label(label="X:")
        move_row.append(self._move_x_label)
        self._move_x_spin = Gtk.SpinButton()
        self._move_x_spin.set_adjustment(
            Gtk.Adjustment(value=0, lower=-10000, upper=10000, step_increment=1)
        )
        self._move_x_spin.set_digits(0)
        self._move_x_spin.set_width_chars(7)
        self._move_x_spin.connect("value-changed", self._on_move_x_changed)
        move_row.append(self._move_x_spin)
        self._move_y_label = Gtk.Label(label="Y:")
        move_row.append(self._move_y_label)
        self._move_y_spin = Gtk.SpinButton()
        self._move_y_spin.set_adjustment(
            Gtk.Adjustment(value=0, lower=-10000, upper=10000, step_increment=1)
        )
        self._move_y_spin.set_digits(0)
        self._move_y_spin.set_width_chars(7)
        self._move_y_spin.connect("value-changed", self._on_move_y_changed)
        move_row.append(self._move_y_spin)

        self._move_capture_status = Gtk.Label(label="")
        self._move_capture_status.add_css_class("dim-label")
        self._move_capture_status.set_halign(Gtk.Align.START)
        self._move_capture_status.set_hexpand(True)
        move_row.append(self._move_capture_status)

        panel.append(move_row)
        self._move_row = move_row
        self._move_capture_widgets = (
            self._move_capture_prefix_label,
            self._move_capture_delay_label,
            self._move_capture_delay_spin,
            self._move_capture_delay_unit_label,
            self._move_capture_btn,
            self._move_capture_colon_label,
            self._move_capture_status,
        )
        self._move_row.set_visible(False)
        self._update_selected_move_capture_controls(None)

        control_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._control_mode_label = Gtk.Label()
        self._control_mode_label.add_css_class("dim-label")
        self._control_mode_label.set_halign(Gtk.Align.START)
        control_row.append(self._control_mode_label)

        control_ab_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._control_a_label = Gtk.Label(label="A:")
        control_ab_row.append(self._control_a_label)
        self._control_a_spin = Gtk.SpinButton()
        self._control_a_spin.set_adjustment(
            Gtk.Adjustment(value=0, lower=0, upper=600000, step_increment=1)
        )
        self._control_a_spin.set_digits(0)
        self._control_a_spin.set_width_chars(7)
        self._control_a_spin.connect("value-changed", self._on_control_a_changed)
        control_ab_row.append(self._control_a_spin)
        self._control_b_label = Gtk.Label(label="B:")
        control_ab_row.append(self._control_b_label)
        self._control_b_spin = Gtk.SpinButton()
        self._control_b_spin.set_adjustment(
            Gtk.Adjustment(value=0, lower=0, upper=600000, step_increment=1)
        )
        self._control_b_spin.set_digits(0)
        self._control_b_spin.set_width_chars(7)
        self._control_b_spin.connect("value-changed", self._on_control_b_changed)
        control_ab_row.append(self._control_b_spin)
        control_row.append(control_ab_row)

        control_cmd_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        control_cmd_row.append(Gtk.Label(label="Command:"))
        self._control_cmd_entry = Gtk.Entry()
        self._control_cmd_entry.set_hexpand(True)
        self._control_cmd_entry.connect("changed", self._on_control_command_changed)
        control_cmd_row.append(self._control_cmd_entry)
        control_row.append(control_cmd_row)

        control_sync_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        control_sync_row.append(Gtk.Label(label="Timeout (ms):"))
        self._control_timeout_spin = Gtk.SpinButton()
        self._control_timeout_spin.set_adjustment(
            Gtk.Adjustment(
                value=min(30000, self._macro_exec_timeout_max_ms),
                lower=1,
                upper=self._macro_exec_timeout_max_ms,
                step_increment=100,
            )
        )
        self._control_timeout_spin.set_digits(0)
        self._control_timeout_spin.set_width_chars(8)
        self._control_timeout_spin.connect("value-changed", self._on_control_timeout_changed)
        control_sync_row.append(self._control_timeout_spin)
        self._control_inhibit_check = Gtk.CheckButton(label="Inhibit mouse")
        self._control_inhibit_check.connect("toggled", self._on_control_inhibit_toggled)
        control_sync_row.append(self._control_inhibit_check)
        control_row.append(control_sync_row)

        self._control_timeout_hint_label = Gtk.Label()
        self._control_timeout_hint_label.add_css_class("dim-label")
        self._control_timeout_hint_label.set_halign(Gtk.Align.START)
        control_row.append(self._control_timeout_hint_label)

        panel.append(control_row)
        self._control_row = control_row
        self._control_ab_row = control_ab_row
        self._control_cmd_row = control_cmd_row
        self._control_sync_row = control_sync_row
        self._control_timeout_hint_label.set_visible(False)
        self._control_row.set_visible(False)

        # Action row
        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._key_info_label = Gtk.Label()
        self._key_info_label.add_css_class("dim-label")
        self._key_info_label.set_hexpand(True)
        self._key_info_label.set_halign(Gtk.Align.START)
        action_row.append(self._key_info_label)

        change_key_btn = Gtk.Button(label="Change Key…")
        change_key_btn.add_css_class("flat")
        change_key_btn.connect("clicked", self._on_change_key_clicked)
        action_row.append(change_key_btn)
        self._change_key_btn = change_key_btn

        delete_btn = Gtk.Button(label="Delete Event")
        delete_btn.add_css_class("destructive-action")
        delete_btn.add_css_class("flat")
        delete_btn.connect("clicked", self._on_delete_event)
        action_row.append(delete_btn)

        panel.append(action_row)
        panel.append(Gtk.Separator())

        self._revealer.set_child(panel)
        return self._revealer

    def _build_name_row(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_top(8)
        outer.set_margin_start(8)
        outer.set_margin_end(8)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name_label = Gtk.Label(label="Name:")
        row.append(name_label)

        self._name_entry = Gtk.Entry()
        self._name_entry.set_text(self._macro_name)
        self._name_entry.set_hexpand(True)
        self._name_entry.connect("changed", lambda _entry: self._sync_close_guard())
        row.append(self._name_entry)
        outer.append(row)

        loop_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        loop_row.append(Gtk.Label(label="Loop:"))
        self._macro_loop_mode_combo = _build_option_dropdown(
            _LOOP_MODE_OPTIONS,
            self._macro_loop_mode,
        )
        self._macro_loop_mode_combo.connect(
            "notify::selected",
            self._on_macro_loop_mode_changed,
        )
        loop_row.append(self._macro_loop_mode_combo)

        self._macro_loop_count_label = Gtk.Label(label="Count:")
        loop_row.append(self._macro_loop_count_label)
        self._macro_loop_count_spin = Gtk.SpinButton()
        self._macro_loop_count_spin.set_adjustment(
            Gtk.Adjustment(value=self._macro_loop_count, lower=1, upper=10000, step_increment=1)
        )
        self._macro_loop_count_spin.set_digits(0)
        self._macro_loop_count_spin.set_width_chars(6)
        self._macro_loop_count_spin.connect("value-changed", self._on_macro_loop_count_changed)
        loop_row.append(self._macro_loop_count_spin)

        self._macro_loop_finish_check = Gtk.CheckButton(
            label="Finish current run before stopping"
        )
        self._macro_loop_finish_check.set_active(
            self._macro_loop_stop_behavior == "finish_run"
        )
        self._macro_loop_finish_check.set_tooltip_text(
            "When disabled, release or toggle stop cancels the macro immediately."
        )
        self._macro_loop_finish_check.connect(
            "toggled",
            self._on_macro_loop_stop_toggled,
        )
        loop_row.append(self._macro_loop_finish_check)
        outer.append(loop_row)

        self._exec_summary_label = Gtk.Label()
        self._exec_summary_label.add_css_class("dim-label")
        self._exec_summary_label.set_halign(Gtk.Align.START)
        outer.append(self._exec_summary_label)

        start_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._macro_move_to_start_check = Gtk.CheckButton(label="Move mouse to:")
        self._macro_move_to_start_check.set_active(self._macro_move_to_start)
        self._macro_move_to_start_check.connect("toggled", self._on_macro_move_to_start_toggled)
        start_row.append(self._macro_move_to_start_check)

        self._macro_start_x_spin = Gtk.SpinButton()
        self._macro_start_x_spin.set_adjustment(
            Gtk.Adjustment(value=self._macro_start_x, lower=-100000, upper=100000, step_increment=1)
        )
        self._macro_start_x_spin.set_digits(0)
        self._macro_start_x_spin.set_width_chars(7)
        self._macro_start_x_spin.connect("value-changed", self._on_macro_start_pos_changed)
        start_row.append(self._macro_start_x_spin)

        self._macro_start_y_spin = Gtk.SpinButton()
        self._macro_start_y_spin.set_adjustment(
            Gtk.Adjustment(value=self._macro_start_y, lower=-100000, upper=100000, step_increment=1)
        )
        self._macro_start_y_spin.set_digits(0)
        self._macro_start_y_spin.set_width_chars(7)
        self._macro_start_y_spin.connect("value-changed", self._on_macro_start_pos_changed)
        start_row.append(self._macro_start_y_spin)

        start_row.append(Gtk.Label(label="at the start of the macro"))

        outer.append(start_row)

        capture_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        capture_row.set_margin_start(24)

        if not self._slurp_available:
            capture_label = Gtk.Label(label="Capture new position in:")
            capture_label.add_css_class("dim-label")
            capture_row.append(capture_label)

        self._macro_capture_delay_spin = Gtk.SpinButton()
        self._macro_capture_delay_spin.set_adjustment(
            Gtk.Adjustment(
                value=self._capture_delay_seconds, lower=0.2, upper=15.0, step_increment=0.2
            )
        )
        self._macro_capture_delay_spin.set_digits(1)
        self._macro_capture_delay_spin.set_width_chars(4)
        self._macro_capture_delay_spin.set_visible(not self._slurp_available)
        capture_row.append(self._macro_capture_delay_spin)

        if not self._slurp_available:
            capture_row.append(Gtk.Label(label="s"))

        btn_label = "Capture" if self._slurp_available else "Capture"
        self._macro_capture_btn = Gtk.Button(label=btn_label)
        self._macro_capture_btn.connect("clicked", self._on_capture_start_position_clicked)
        capture_row.append(self._macro_capture_btn)

        self._macro_capture_status = Gtk.Label(label="")
        self._macro_capture_status.add_css_class("dim-label")
        self._macro_capture_status.set_halign(Gtk.Align.START)
        self._macro_capture_status.set_hexpand(True)
        capture_row.append(self._macro_capture_status)

        outer.append(capture_row)

        self._macro_block_mouse_check = Gtk.CheckButton(
            label="Block physical mouse movement during playback"
        )
        self._macro_block_mouse_check.set_active(self._macro_block_mouse_movement)
        self._macro_block_mouse_check.connect(
            "toggled",
            self._on_macro_block_mouse_toggled,
        )
        outer.append(self._macro_block_mouse_check)

        self._update_loop_controls()
        self._update_macro_move_start_controls()
        self._update_exec_summary_label()
        return outer

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

        self._footer_action_buttons = [cancel_btn, copy_btn, apply_btn, save_btn]
        return footer

    # ------------------------------------------------------------------
    # Property panel updates
    # ------------------------------------------------------------------

    def _on_selection_changed(
        self,
        selected_obj: object | None,
    ) -> None:
        if selected_obj is None:
            self._cancel_capture_selected_move("")
            self._revealer.set_reveal_child(False)
            self._update_selected_move_capture_controls(None)
            return

        self._revealer.set_reveal_child(True)
        if not isinstance(selected_obj, EditableMove) or selected_obj.mode not in {
            "abs",
            "natural",
        }:
            self._cancel_capture_selected_move("")
        if isinstance(selected_obj, EditableControl):
            control = selected_obj
            is_compositor = control.mode == "compositor_dispatch"
            self._prop_title.set_label("Compositor Action" if is_compositor else "Control")
            self._key_info_label.set_label(
                _describe_compositor_control(control)
                if is_compositor
                else control.mode.replace("_", " ").title()
            )
            self._press_label.set_label("At:")
            self._duration_text_label.set_visible(False)
            self._duration_spin.set_visible(False)
            self._duration_unit_label.set_visible(False)
            self._release_label.set_visible(False)
            self._release_spin.set_visible(False)
            self._release_unit_label.set_visible(False)
            self._change_key_btn.set_visible(is_compositor)
            self._change_key_btn.set_label("Change Action..." if is_compositor else "Change Key...")
            self._move_row.set_visible(False)
            self._control_row.set_visible(True)

            self._updating_props = True
            try:
                self._press_spin.set_value(control.t_us / 1000)
                self._control_mode_label.set_label(control.mode.replace("_", " ").title())
                self._control_a_label.set_visible(False)
                self._control_a_spin.set_visible(False)
                self._control_b_label.set_visible(False)
                self._control_b_spin.set_visible(False)
                self._control_ab_row.set_visible(False)
                self._control_cmd_row.set_visible(False)
                self._control_sync_row.set_visible(False)
                self._control_timeout_hint_label.set_visible(False)

                if control.mode == "wait":
                    self._control_ab_row.set_visible(True)
                    self._control_a_label.set_label("Duration (ms):")
                    self._control_a_label.set_visible(True)
                    self._control_a_spin.set_visible(True)
                    self._control_a_spin.set_value(max(0.0, control.duration_us / 1000.0))
                elif control.mode == "wait_random":
                    self._control_ab_row.set_visible(True)
                    self._control_a_label.set_label("Min (ms):")
                    self._control_b_label.set_label("Max (ms):")
                    self._control_a_label.set_visible(True)
                    self._control_a_spin.set_visible(True)
                    self._control_b_label.set_visible(True)
                    self._control_b_spin.set_visible(True)
                    self._control_a_spin.set_value(max(0.0, control.min_us / 1000.0))
                    self._control_b_spin.set_value(max(0.0, control.max_us / 1000.0))
                elif control.mode == "exec_async":
                    self._control_cmd_row.set_visible(True)
                    _set_entry_text_if_needed(self._control_cmd_entry, control.command)
                elif control.mode == "exec_sync":
                    self._control_cmd_row.set_visible(True)
                    self._control_sync_row.set_visible(True)
                    self._control_timeout_hint_label.set_visible(True)
                    _set_entry_text_if_needed(self._control_cmd_entry, control.command)
                    self._control_timeout_spin.set_value(max(1, int(control.timeout_ms)))
                    self._control_inhibit_check.set_active(bool(control.inhibit_mouse))
                    self._update_timeout_clamp_hint(int(control.timeout_ms))
            finally:
                self._updating_props = False
            self._update_selected_move_capture_controls(None)
            return

        self._control_row.set_visible(False)
        if isinstance(selected_obj, EditableMove):
            move = selected_obj
            mode_label = "NATURAL" if move.mode == "natural" else move.mode.upper()
            self._prop_title.set_label(f"Mouse Move ({mode_label})")
            detail = f"Move {mode_label} (x={move.x}, y={move.y})"
            if move.mode == "natural":
                stop_suffix = ", stop on failure" if move.stop_on_failure else ""
                detail = (
                    f"{detail} @ {move.speed:g}px/s, {move.curve}, "
                    f"timeout {move.max_duration_ms}ms{stop_suffix}"
                )
            self._key_info_label.set_label(detail)

            self._press_label.set_label("At:")
            self._duration_text_label.set_visible(False)
            self._duration_spin.set_visible(False)
            self._duration_unit_label.set_visible(False)
            self._release_label.set_visible(False)
            self._release_spin.set_visible(False)
            self._release_unit_label.set_visible(False)
            self._change_key_btn.set_visible(move.mode == "natural")
            self._change_key_btn.set_label("Modify Move")
            self._move_row.set_visible(True)
            self._move_mode_label.set_label(f"Mode: {mode_label}")
            self._move_x_label.set_label("X:")
            self._move_y_label.set_label("Y:")
            self._move_y_label.set_visible(True)
            self._move_y_spin.set_visible(True)
            self._move_y_spin.set_sensitive(True)
            self._move_x_spin.set_range(-10000, 10000)
            self._move_y_spin.set_range(-10000, 10000)

            self._updating_props = True
            try:
                self._press_spin.set_value(move.t_us / 1000)
                self._move_x_spin.set_value(move.x)
                self._move_y_spin.set_value(move.y)
            finally:
                self._updating_props = False
            self._update_selected_move_capture_controls(move)
            return

        if isinstance(selected_obj, dict):
            title, detail = _describe_passthrough_event(selected_obj)
            self._prop_title.set_label(title)
            self._key_info_label.set_label(detail)
            self._press_label.set_label("At:")
            self._duration_text_label.set_visible(False)
            self._duration_spin.set_visible(False)
            self._duration_unit_label.set_visible(False)
            self._release_label.set_visible(False)
            self._release_spin.set_visible(False)
            self._release_unit_label.set_visible(False)
            self._change_key_btn.set_visible(False)
            self._move_row.set_visible(False)
            self._control_row.set_visible(False)
            self._updating_props = True
            try:
                self._press_spin.set_value(int(selected_obj.get("t_us", 0)) / 1000)
            finally:
                self._updating_props = False
            self._update_selected_move_capture_controls(None)
            return

        assert isinstance(selected_obj, EditableEvent)
        ev = selected_obj
        if _is_gamepad_axis_event(ev):
            name = _get_event_name(ev.ev_type, ev.code)
            axis_range = gamepad_axis_range(name.lower())
            title = axis_range.label if axis_range is not None else name
            output_suffix = f" @ {ev.output_id}" if ev.output_id else ""
            self._prop_title.set_label(title)
            self._key_info_label.set_label(
                f"{name} value {ev.value} (code {ev.code}){output_suffix}"
            )
            self._press_label.set_label("At:")
            self._duration_text_label.set_visible(False)
            self._duration_spin.set_visible(False)
            self._duration_unit_label.set_visible(False)
            self._release_label.set_visible(False)
            self._release_spin.set_visible(False)
            self._release_unit_label.set_visible(False)
            self._change_key_btn.set_visible(True)
            self._change_key_btn.set_label("Change Axis...")
            self._move_row.set_visible(True)
            self._move_mode_label.set_label("Gamepad Axis:")
            self._move_x_label.set_label("Value:")
            self._move_y_label.set_visible(False)
            self._move_y_spin.set_visible(False)
            if axis_range is None:
                self._move_x_spin.set_range(-32768, 32767)
            else:
                self._move_x_spin.set_range(axis_range.minimum, axis_range.maximum)

            self._updating_props = True
            try:
                self._press_spin.set_value(ev.press_t_us / 1000)
                self._move_x_spin.set_value(ev.value)
            finally:
                self._updating_props = False
            self._update_selected_move_capture_controls(None)
            return

        name = _get_key_name(ev.code)
        self._prop_title.set_label(name)
        output_suffix = f" @ {ev.output_id}" if ev.device_type == "gamepad" and ev.output_id else ""
        self._key_info_label.set_label(f"{name} (code {ev.code}){output_suffix}")
        self._press_label.set_label("Press:")
        self._duration_text_label.set_visible(True)
        self._duration_spin.set_visible(True)
        self._duration_unit_label.set_visible(True)
        self._release_label.set_visible(True)
        self._release_spin.set_visible(True)
        self._release_unit_label.set_visible(True)
        self._change_key_btn.set_visible(True)
        self._change_key_btn.set_label("Change Key...")
        self._move_row.set_visible(False)

        self._updating_props = True
        try:
            self._press_spin.set_value(ev.press_t_us / 1000)
            self._duration_spin.set_value(max(1, round((ev.release_t_us - ev.press_t_us) / 1000)))
            self._release_spin.set_value(ev.release_t_us / 1000)
        finally:
            self._updating_props = False
        self._update_selected_move_capture_controls(None)

    def _refresh_after_key_timing_change(self, ev: EditableEvent) -> None:
        self._events.sort(key=lambda e: e.press_t_us)
        self._recompute_duration()
        self._update_stats()
        self._update_canvas_width()
        self._timeline.queue_draw()
        self._on_selection_changed(ev)
        self._sync_close_guard()

    def _refresh_after_passthrough_timing_change(self, ev: MacroEvent) -> None:
        self._passthrough_events.sort(key=lambda e: int(e.get("t_us", 0)))
        self._recompute_duration()
        self._update_stats()
        self._update_canvas_width()
        self._timeline.queue_draw()
        self._on_selection_changed(ev)
        self._sync_close_guard()

    def _on_press_changed(self, spin) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if selected_obj is None:
            return
        new_t = int(spin.get_value() * 1000)
        if isinstance(selected_obj, EditableControl):
            selected_obj.t_us = max(0, new_t)
            self._refresh_after_control_change(selected_obj)
            return
        if isinstance(selected_obj, EditableMove):
            selected_obj.t_us = max(0, new_t)
            self._synthetic_moves.sort(key=lambda m: m.t_us)
            self._on_selection_changed(selected_obj)
            self._update_stats()
            self._timeline.queue_draw()
            self._sync_close_guard()
            return
        if isinstance(selected_obj, dict):
            selected_obj["t_us"] = max(0, new_t)
            self._refresh_after_passthrough_timing_change(selected_obj)
            return

        assert isinstance(selected_obj, EditableEvent)
        ev = selected_obj
        dur = ev.release_t_us - ev.press_t_us
        ev.press_t_us = new_t
        ev.release_t_us = new_t + dur
        self._refresh_after_key_timing_change(ev)

    def _on_release_changed(self, spin) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if selected_obj is None or isinstance(selected_obj, (EditableMove, EditableControl, dict)):
            return
        assert isinstance(selected_obj, EditableEvent)
        ev = selected_obj
        if ev.ev_type != evdev.ecodes.EV_KEY:
            return
        new_t = int(spin.get_value() * 1000)
        min_release = ev.press_t_us + 1000
        if new_t < min_release:
            new_t = min_release
        ev.release_t_us = new_t
        self._refresh_after_key_timing_change(ev)

    def _on_duration_changed(self, spin) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if selected_obj is None or isinstance(selected_obj, (EditableMove, EditableControl, dict)):
            return
        assert isinstance(selected_obj, EditableEvent)
        ev = selected_obj
        if ev.ev_type != evdev.ecodes.EV_KEY:
            return
        duration_us = max(1000, int(spin.get_value() * 1000))
        ev.release_t_us = ev.press_t_us + duration_us
        self._refresh_after_key_timing_change(ev)

    def _on_delete_event(self, btn) -> None:
        self._delete_event(self._timeline._selected)

    def _delete_event(self, ev) -> None:
        deleted = False
        if isinstance(ev, EditableEvent) and ev in self._events:
            self._events.remove(ev)
            deleted = True
        elif isinstance(ev, dict) and ev in self._passthrough_events:
            self._passthrough_events.remove(ev)
            deleted = True
        elif isinstance(ev, EditableMove) and ev in self._synthetic_moves:
            self._synthetic_moves.remove(ev)
            deleted = True
        elif isinstance(ev, EditableControl) and ev in self._control_events:
            self._control_events.remove(ev)
            deleted = True

        if deleted:
            if self._timeline._selected is ev:
                self._timeline._selected = None
                self._revealer.set_reveal_child(False)
            self._refresh_after_timing_edit()

    def _delete_events_bulk(self, evs: list[object]) -> None:
        pending_ids = {id(ev) for ev in evs}
        if not pending_ids:
            return
        kept_events = [e for e in self._events if id(e) not in pending_ids]
        kept_rel = [e for e in self._rel_events if id(e) not in pending_ids]
        kept_passthrough = [e for e in self._passthrough_events if id(e) not in pending_ids]
        kept_moves = [m for m in self._synthetic_moves if id(m) not in pending_ids]
        kept_controls = [c for c in self._control_events if id(c) not in pending_ids]
        deleted = (
            len(kept_events) != len(self._events)
            or len(kept_rel) != len(self._rel_events)
            or len(kept_passthrough) != len(self._passthrough_events)
            or len(kept_moves) != len(self._synthetic_moves)
            or len(kept_controls) != len(self._control_events)
        )
        if not deleted:
            return
        self._events = kept_events
        self._rel_events = kept_rel
        self._passthrough_events = kept_passthrough
        self._synthetic_moves = kept_moves
        self._control_events = kept_controls
        self._clear_selection_if_removed()
        self._refresh_after_timing_edit()

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

    def _on_change_key_clicked(self, btn) -> None:
        ev = self._timeline._selected
        if isinstance(ev, EditableControl) and ev.mode == "compositor_dispatch":
            self._present_compositor_action_dialog(control=ev)
            return
        if isinstance(ev, EditableMove):
            self._present_mouse_move_dialog(move=ev)
            return
        if ev is None or isinstance(ev, (EditableControl, dict)):
            return
        assert isinstance(ev, EditableEvent)

        from keymasq.common.models import ActionType, MappingAction
        from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

        # Build a MappingAction that pre-selects the current key/axis in the dialog.
        if _is_gamepad_axis_event(ev):
            target = _event_target_name(ev)
            current_action = MappingAction(
                action_type=ActionType.GAMEPAD_AXIS,
                target=target,
                axis_value=ev.value,
                output_id=ev.output_id,
            )
            dialog_label = _get_event_name(ev.ev_type, ev.code)
        else:
            key_name_lower = _get_key_name(ev.code).lower()
            dialog_label = _get_key_name(ev.code)
            if ev.device_type == "keyboard":
                current_action = MappingAction(
                    action_type=ActionType.KEYBOARD,
                    target=key_name_lower,
                )
            elif ev.device_type == "mouse":
                current_action = MappingAction(
                    action_type=ActionType.MOUSE,
                    target=key_name_lower,
                )
            else:
                current_action = MappingAction(
                    action_type=ActionType.GAMEPAD,
                    target=key_name_lower,
                    output_id=ev.output_id,
                )

        dialog = KeySelectorDialog(
            self._parent,
            dialog_label,
            current_action,
            allow_passthrough=False,
            allow_clear_mapping=False,
            allow_suppress=False,
            allow_superkey=False,
            allow_repeat=False,
            allow_rapidfire=False,
            allow_tap=False,
            allowed_tabs={
                "gamepad"
                if ev.device_type == "gamepad"
                else "mouse"
                if ev.device_type == "mouse"
                else "keyboard",
                *(() if ev.device_type != "keyboard" else ("navigation", "media")),
            },
            initial_tab=(
                "gamepad"
                if ev.device_type == "gamepad"
                else "mouse"
                if ev.device_type == "mouse"
                else "keyboard"
            ),
            include_mpris_controls=False,
            include_mouse_move_controls=False,
            include_mouse_scroll_controls=False if ev.device_type == "mouse" else True,
        )
        dialog.connect("key-selected", self._on_key_selected_for_edit)
        dialog.present(self._parent)

    def _on_key_selected_for_edit(self, dialog, action) -> None:
        from keymasq.common.models import ActionType

        ev = self._timeline._selected
        if ev is None or action is None or isinstance(ev, (EditableMove, EditableControl, dict)):
            return
        assert isinstance(ev, EditableEvent)

        target = getattr(action, "target", None)
        if action.action_type == ActionType.KEYBOARD and target:
            code = getattr(evdev.ecodes, target.upper(), None)
            if code is not None:
                ev.code = code
                ev.device_type = "keyboard"
                ev.ev_type = evdev.ecodes.EV_KEY
                ev.value = 0
                ev.output_id = None
        elif action.action_type == ActionType.MOUSE and target:
            code = getattr(evdev.ecodes, target.upper(), None)
            if code is not None:
                ev.code = code
                ev.device_type = "mouse"
                ev.ev_type = evdev.ecodes.EV_KEY
                ev.value = 0
                ev.output_id = None
        elif action.action_type == ActionType.GAMEPAD and target:
            code = getattr(evdev.ecodes, target.upper(), None)
            if code is not None:
                ev.code = code
                ev.device_type = "gamepad"
                ev.ev_type = evdev.ecodes.EV_KEY
                ev.value = 0
                ev.output_id = getattr(action, "output_id", None)
        elif action.action_type == ActionType.GAMEPAD_AXIS and target:
            code = getattr(evdev.ecodes, target.upper(), None)
            if code is not None:
                ev.code = code
                ev.device_type = "gamepad"
                ev.ev_type = evdev.ecodes.EV_ABS
                ev.value = int(getattr(action, "axis_value", 0) or 0)
                ev.release_t_us = ev.press_t_us + 1
                ev.output_id = getattr(action, "output_id", None)
        else:
            return

        if ev.ev_type == evdev.ecodes.EV_KEY and ev.release_t_us <= ev.press_t_us + 1:
            ev.release_t_us = ev.press_t_us + 50000
        self._on_selection_changed(ev)
        self._timeline.queue_draw()
        self._sync_close_guard()

    def _on_move_x_changed(self, spin) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableMove):
            if not isinstance(selected_obj, EditableEvent) or not _is_gamepad_axis_event(
                selected_obj
            ):
                return
            selected_obj.value = clamp_gamepad_axis_value(
                _event_target_name(selected_obj), int(spin.get_value())
            )
            self._on_selection_changed(selected_obj)
            self._timeline.queue_draw()
            self._sync_close_guard()
            return
        selected_obj.x = int(spin.get_value())
        self._on_selection_changed(selected_obj)
        self._timeline.queue_draw()
        self._sync_close_guard()

    def _on_move_y_changed(self, spin) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableMove):
            return
        selected_obj.y = int(spin.get_value())
        self._on_selection_changed(selected_obj)
        self._timeline.queue_draw()
        self._sync_close_guard()

    def _refresh_after_control_change(self, control: EditableControl) -> None:
        self._control_events.sort(key=lambda c: c.t_us)
        self._recompute_duration()
        self._update_stats()
        self._update_canvas_width()
        self._timeline.queue_draw()
        self._on_selection_changed(control)
        self._sync_close_guard()

    def _update_timeout_clamp_hint(self, timeout_ms: int) -> None:
        max_timeout = max(1, int(self._macro_exec_timeout_max_ms))
        if timeout_ms > max_timeout:
            self._control_timeout_hint_label.set_label(
                f"Runtime clamp: {timeout_ms}ms -> {max_timeout}ms"
            )
        else:
            self._control_timeout_hint_label.set_label(f"Policy max timeout: {max_timeout}ms")

    def _on_control_a_changed(self, spin: Gtk.SpinButton) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableControl):
            return
        if selected_obj.mode == "wait":
            selected_obj.duration_us = max(0, int(spin.get_value() * 1000))
        elif selected_obj.mode == "wait_random":
            selected_obj.min_us = max(0, int(spin.get_value() * 1000))
            if selected_obj.max_us < selected_obj.min_us:
                selected_obj.max_us = selected_obj.min_us
        self._refresh_after_control_change(selected_obj)

    def _on_control_b_changed(self, spin: Gtk.SpinButton) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableControl):
            return
        if selected_obj.mode == "wait_random":
            selected_obj.max_us = max(0, int(spin.get_value() * 1000))
            if selected_obj.max_us < selected_obj.min_us:
                selected_obj.max_us = selected_obj.min_us
            self._refresh_after_control_change(selected_obj)

    def _on_control_command_changed(self, entry: Gtk.Entry) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableControl):
            return
        if selected_obj.mode in {"exec_sync", "exec_async"}:
            # Do not rebuild the property panel while the command entry is focused.
            # The full control refresh path toggles row visibility, which drops focus
            # from the Gtk.Entry and makes typing impossible.
            selected_obj.command = entry.get_text()
            self._sync_close_guard()

    def _on_control_timeout_changed(self, spin: Gtk.SpinButton) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableControl):
            return
        if selected_obj.mode == "exec_sync":
            selected_obj.timeout_ms = max(1, int(spin.get_value()))
            self._update_timeout_clamp_hint(selected_obj.timeout_ms)
            self._refresh_after_control_change(selected_obj)

    def _on_control_inhibit_toggled(self, check: Gtk.CheckButton) -> None:
        if self._updating_props:
            return
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableControl):
            return
        if selected_obj.mode == "exec_sync":
            selected_obj.inhibit_mouse = bool(check.get_active())
            self._refresh_after_control_change(selected_obj)

    def _sync_macro_settings_controls(self) -> None:
        loop_mode = self._macro_loop_mode
        loop_count = self._macro_loop_count
        loop_stop_behavior = self._macro_loop_stop_behavior
        move_to_start = self._macro_move_to_start
        start_x = self._macro_start_x
        start_y = self._macro_start_y
        block_mouse_movement = self._macro_block_mouse_movement
        name = str(self._macro_data.get("name", self._macro_name) or self._macro_name)
        self._name_entry.set_text(name)
        _set_dropdown_selected_id(
            self._macro_loop_mode_combo,
            _LOOP_MODE_OPTIONS,
            loop_mode,
        )
        self._macro_loop_count_spin.set_value(loop_count)
        self._macro_loop_finish_check.set_active(loop_stop_behavior == "finish_run")
        self._macro_move_to_start_check.set_active(move_to_start)
        self._macro_start_x_spin.set_value(start_x)
        self._macro_start_y_spin.set_value(start_y)
        self._macro_block_mouse_check.set_active(block_mouse_movement)
        self._update_loop_controls()
        self._update_macro_move_start_controls()

    def _on_macro_loop_mode_changed(self, combo: Gtk.DropDown, _pspec=None) -> None:
        self._macro_loop_mode = _get_dropdown_selected_id(combo, _LOOP_MODE_OPTIONS, "none")
        self._update_loop_controls()
        self._sync_close_guard()

    def _on_macro_loop_count_changed(self, spin: Gtk.SpinButton) -> None:
        self._macro_loop_count = max(1, int(spin.get_value()))
        self._sync_close_guard()

    def _on_macro_loop_stop_toggled(self, check: Gtk.CheckButton) -> None:
        self._macro_loop_stop_behavior = "finish_run" if check.get_active() else "cancel_run"
        self._sync_close_guard()

    def _update_loop_controls(self) -> None:
        is_count = self._macro_loop_mode == "count"
        is_stoppable_loop = self._macro_loop_mode in {"hold", "toggle"}
        self._macro_loop_count_label.set_visible(is_count)
        self._macro_loop_count_spin.set_visible(is_count)
        self._macro_loop_finish_check.set_visible(is_stoppable_loop)

    def _on_macro_move_to_start_toggled(self, check: Gtk.CheckButton) -> None:
        self._macro_move_to_start = check.get_active()
        self._update_macro_move_start_controls()
        self._sync_close_guard()

    def _on_macro_start_pos_changed(self, spin: Gtk.SpinButton) -> None:
        self._macro_start_x = int(self._macro_start_x_spin.get_value())
        self._macro_start_y = int(self._macro_start_y_spin.get_value())
        self._sync_close_guard()

    def _on_macro_block_mouse_toggled(self, check: Gtk.CheckButton) -> None:
        self._macro_block_mouse_movement = check.get_active()
        self._sync_close_guard()

    def _update_macro_move_start_controls(self) -> None:
        enabled = self._macro_move_to_start
        self._macro_start_x_spin.set_sensitive(enabled)
        self._macro_start_y_spin.set_sensitive(enabled)
        if self._slurp_available:
            self._macro_capture_delay_spin.set_sensitive(False)
        else:
            self._macro_capture_delay_spin.set_sensitive(enabled and not self._capture_pending)
        self._macro_capture_btn.set_sensitive(enabled and not self._capture_pending)

    def _update_selected_move_capture_controls(
        self,
        selected_move: EditableMove | None = None,
    ) -> None:
        if not hasattr(self, "_move_capture_widgets"):
            return
        move = selected_move
        if move is None:
            selected_obj = self._timeline._selected if hasattr(self, "_timeline") else None
            move = selected_obj if isinstance(selected_obj, EditableMove) else None
        enabled = bool(move is not None and move.mode in {"abs", "natural"})
        for widget in self._move_capture_widgets:
            widget.set_visible(enabled)
        show_delay = enabled and not self._slurp_available
        self._move_capture_delay_label.set_visible(show_delay)
        self._move_capture_delay_spin.set_visible(show_delay)
        self._move_capture_delay_unit_label.set_visible(show_delay)
        if self._slurp_available:
            self._move_capture_delay_spin.set_sensitive(False)
        else:
            self._move_capture_delay_spin.set_sensitive(enabled and not self._move_capture_pending)
        self._move_capture_btn.set_sensitive(enabled and not self._move_capture_pending)

    def _sync_start_position_capture_legacy_state(self) -> None:
        self._capture_timeout_id = self._start_position_capture.timeout_id
        self._capture_pending = self._start_position_capture.pending
        self._capture_request_id = self._start_position_capture.request_id
        self._capture_delay_seconds = self._start_position_capture.delay_seconds
        if hasattr(self, "_macro_capture_btn"):
            self._update_macro_move_start_controls()

    def _sync_selected_move_capture_legacy_state(self) -> None:
        self._move_capture_timeout_id = self._selected_move_capture.timeout_id
        self._move_capture_pending = self._selected_move_capture.pending
        self._move_capture_request_id = self._selected_move_capture.request_id
        self._capture_delay_seconds = self._selected_move_capture.delay_seconds
        if hasattr(self, "_move_capture_btn"):
            self._update_selected_move_capture_controls()

    def _on_capture_start_position_clicked(self, btn: Gtk.Button) -> None:
        self._start_position_capture.begin(
            button=self._macro_capture_btn,
            status_label=self._macro_capture_status,
            delay_seconds=float(self._macro_capture_delay_spin.get_value()),
            apply_position=self._apply_start_capture_position,
        )
        self._sync_start_position_capture_legacy_state()

    def _on_capture_selected_move_clicked(self, btn: Gtk.Button) -> None:
        selected_obj = self._timeline._selected
        if not isinstance(selected_obj, EditableMove) or selected_obj.mode not in {
            "abs",
            "natural",
        }:
            return
        self._selected_move_capture.begin(
            button=self._move_capture_btn,
            status_label=self._move_capture_status,
            delay_seconds=float(self._move_capture_delay_spin.get_value()),
            apply_position=lambda x, y, move=selected_obj: (
                self._apply_selected_move_capture_position(move, x, y)
            ),
        )
        self._sync_selected_move_capture_legacy_state()

    def _apply_start_capture_position(self, x: int, y: int) -> None:
        self._macro_start_x_spin.set_value(x)
        self._macro_start_y_spin.set_value(y)
        self._macro_move_to_start_check.set_active(True)
        self._sync_close_guard()

    def _apply_selected_move_capture_position(
        self,
        move: EditableMove,
        x: int,
        y: int,
    ) -> bool:
        if move.mode not in {"abs", "natural"} or move not in self._synthetic_moves:
            self._move_capture_status.set_text("Capture target no longer available")
            return False

        move.x = int(x)
        move.y = int(y)
        if self._timeline._selected is move:
            self._updating_props = True
            try:
                self._move_x_spin.set_value(move.x)
                self._move_y_spin.set_value(move.y)
            finally:
                self._updating_props = False
            self._on_selection_changed(move)
        self._timeline.queue_draw()
        self._sync_close_guard()
        return True

    def _on_slurp_capture_result(self, request_id: int, result) -> None:
        self._start_position_capture.on_slurp_result(request_id, result)
        self._sync_start_position_capture_legacy_state()

    def _on_move_slurp_capture_result(self, request_id: int, move: EditableMove, result) -> None:
        if self._selected_move_capture.apply is None:
            self._selected_move_capture.apply = lambda x, y: (
                self._apply_selected_move_capture_position(move, x, y)
            )
        self._selected_move_capture.on_slurp_result(request_id, result)
        self._sync_selected_move_capture_legacy_state()

    def _capture_start_position_after_delay(self, request_id: int) -> bool:
        result = self._start_position_capture.capture_after_delay(request_id)
        self._sync_start_position_capture_legacy_state()
        return result

    def _capture_selected_move_after_delay(self, request_id: int, move: EditableMove) -> bool:
        if self._selected_move_capture.apply is None:
            self._selected_move_capture.apply = lambda x, y: (
                self._apply_selected_move_capture_position(move, x, y)
            )
        result = self._selected_move_capture.capture_after_delay(request_id)
        self._sync_selected_move_capture_legacy_state()
        return result

    def _on_capture_start_position_response(
        self,
        request_id: int,
        response: dict | None,
    ) -> bool:
        result = self._start_position_capture.on_response(request_id, response)
        self._sync_start_position_capture_legacy_state()
        return result

    def _on_capture_selected_move_response(
        self,
        request_id: int,
        move: EditableMove,
        response: dict | None,
    ) -> bool:
        if self._selected_move_capture.apply is None:
            self._selected_move_capture.apply = lambda x, y: (
                self._apply_selected_move_capture_position(move, x, y)
            )
        result = self._selected_move_capture.on_response(request_id, response)
        self._sync_selected_move_capture_legacy_state()
        return result

    def _cancel_capture_start_position(self, status_text: str) -> None:
        self._start_position_capture.cancel(status_text)
        self._sync_start_position_capture_legacy_state()

    def _cancel_capture_selected_move(self, status_text: str) -> None:
        self._selected_move_capture.cancel(status_text)
        self._sync_selected_move_capture_legacy_state()

    # ------------------------------------------------------------------
    # Zoom
    # ------------------------------------------------------------------

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
        self._macro_capture_delay_spin.set_value(self._capture_delay_seconds)

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
