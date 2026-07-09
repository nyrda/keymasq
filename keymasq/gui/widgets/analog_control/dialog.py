import logging
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import (  # pyright: ignore[reportAttributeAccessIssue]
    Adw,  # pyright: ignore[reportAttributeAccessIssue]
    Gdk,  # pyright: ignore[reportAttributeAccessIssue]
    GLib,  # pyright: ignore[reportAttributeAccessIssue]
    GObject,  # pyright: ignore[reportAttributeAccessIssue]
    Gtk,  # pyright: ignore[reportAttributeAccessIssue]
)

from keymasq.common.models import (
    SAME_DEVICE_OUTPUT_ID,
    AnalogActionThreshold,
    AnalogControlConfig,
    AnalogGamepadOutputConfig,
    AnalogMouseMotionConfig,
)
from keymasq.common.virtual_devices import is_virtual_gamepad_output_id
from keymasq.gui.widgets.analog_control.compat import (
    analog_controls_docs_url as _analog_controls_docs_url,
)
from keymasq.gui.widgets.analog_control.compat import (
    get_slurp_capture,
    hardware_manager,
    session_compositor_id,
    virtual_gamepad_count,
)
from keymasq.gui.widgets.analog_control.groups import (
    build_digital_group,
    build_gamepad_output_group,
    build_mouse_group,
    build_template_group,
)
from keymasq.gui.widgets.analog_control.options import (
    _INPUT_TYPE_OPTIONS,
    _analog_control_search_text,
    _gamepad_output_target_label_for_input_type,
    _gamepad_output_target_options_for_input_type,
    _group_analog_control_names,
    _input_type_index,
    _mode_index_for_input_type,
    _mode_items_for_input_type,
    _mode_labels_for_input_type,
    _option_labels,
)
from keymasq.gui.widgets.analog_control.threshold_editor import ThresholdEditor
from keymasq.gui.widgets.fuzzy_search import install_listbox_fuzzy_filter
from keymasq.gui.widgets.gamepad_output_choices import (
    gamepad_output_choice_matches,
    load_gamepad_output_choices,
    selected_gamepad_output_id,
    update_gamepad_output_warning_label,
)
from keymasq.gui.widgets.position_capture import (
    PositionCallback,
    PositionCaptureController,
    PositionCaptureMessages,
)
from keymasq.gui.widgets.spin_inputs import (
    SPLIT_DESYNC_KEYS,
    SPLIT_DESYNC_MODIFIERS,
    CompactIntEntryController,
    SplitAxisDesyncController,
    add_spin_secondary_step_controller,
    apply_spin_secondary_step,
    compact_int_entry,
    entry_int_value,
    int_entry_key_pressed,
    sanitize_int_entry_text,
    set_entry_int,
    spin_row,
    spin_secondary_step_direction,
)
from keymasq.gui.widgets.superkey_dialog import ActionListDialog
from keymasq.session.analog_controls import (
    AnalogControlManager,
)
from keymasq.session.profiles import ProfileManager

log = logging.getLogger("keymasq.gui.widgets.analog_control_dialog")


class AnalogControlDialog(Adw.Dialog):
    __gsignals__ = {
        "analog-control-saved": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "analog-control-deleted": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, parent: Gtk.Window, profile_manager: ProfileManager | None = None):
        super().__init__(title="Manage Analog Controls", content_width=920, content_height=640)
        self._parent = parent
        self.profile_manager = profile_manager
        self.manager = AnalogControlManager()
        self._current_config: AnalogControlConfig | None = None
        self._current_name: str | None = None
        self._thresholds: list[AnalogActionThreshold] = []
        self._modified = False
        self._close_warning_dialog: Adw.AlertDialog | None = None
        self._selection_warning_dialog: Adw.AlertDialog | None = None
        self._active_selection_key: tuple[str, str | None] | None = None
        self._pending_selection_key: tuple[str, str | None] | None = None
        self._pending_new_control_after_warning = False
        self._suppress_selection_guard = False
        self._editing_new_control = False
        self._syncing_mouse_speed = False
        self._syncing_area_radius = False
        self._syncing_start_entry = False
        self._int_entry_controller = CompactIntEntryController(self._on_modified)
        self._syncing_invert_axes = False
        self._last_speed_x = 900.0
        self._last_speed_y = 900.0
        self._last_area_radius_x = 400.0
        self._last_area_radius_y = 400.0
        self._split_desync_controller = SplitAxisDesyncController()
        self._split_mouse_speed_desync_axis: str | None = None
        self._split_mouse_speed_desync_clear_id = 0
        self._split_mouse_speed_modifier_active = False
        self._selected_gamepad_output_id: str | None = SAME_DEVICE_OUTPUT_ID
        self._gamepad_output_ids: list[str | None] = []
        self._gamepad_output_dropdown: Gtk.DropDown | None = None
        self._gamepad_output_warning_label: Gtk.Label | None = None
        self._refreshing_gamepad_output_choices = False
        self._mode_items: tuple[str, ...] = _mode_items_for_input_type("stick")
        self._gamepad_output_target_items: list[tuple[str, str | None]] = []
        self._gamepad_output_target_buttons: dict[str, Gtk.ToggleButton] = {}
        self._gamepad_output_target_box: Gtk.Box | None = None
        self._hardware_output_configs: dict[str, object] = {}
        self._capture_delay_seconds: float = 2.0
        self._capture_timeout_id: int = 0
        self._capture_pending: bool = False
        self._capture_request_id: int = 0
        self._capture_apply: PositionCallback | None = None
        self._capture_status_label: Gtk.Label | None = None
        self._capture_button: Gtk.Button | None = None
        self._slurp_capture = get_slurp_capture()
        self._slurp_capture.set_compositor(session_compositor_id())
        self._slurp_available = self._slurp_capture.available
        self._position_capture = PositionCaptureController(
            slurp_capture=self._slurp_capture,
            slurp_available=self._slurp_available,
            set_status=self._set_position_capture_status,
            on_state_changed=self._sync_position_capture_legacy_state,
            messages=PositionCaptureMessages(slurp_success="", response_success=""),
        )
        self.new_control_row: Gtk.ListBoxRow | None = None

        self._build_ui()
        self._load_controls()
        self._setup_shortcuts()

    def _setup_shortcuts(self) -> None:
        key_controller = Gtk.EventControllerKey()
        key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_controller.connect("key-pressed", self._on_key_pressed)
        key_controller.connect("key-released", self._on_key_released)
        self.add_controller(key_controller)

    def _on_key_pressed(self, _controller, keyval, _keycode, _state) -> bool:
        if keyval in SPLIT_DESYNC_KEYS:
            self._split_desync_controller.set_modifier_key(keyval, True)
            self._sync_split_desync_legacy_state()
        if keyval in (Gdk.KEY_f, Gdk.KEY_F) and _state & Gdk.ModifierType.CONTROL_MASK:
            self._show_search()
            return True
        if keyval == Gdk.KEY_Escape and self.search_entry.get_visible():
            self._hide_search()
            return True
        if keyval == Gdk.KEY_Escape:
            self._request_close()
            return True
        return False

    def _on_key_released(self, _controller, keyval, _keycode, _state) -> None:
        if keyval in SPLIT_DESYNC_KEYS:
            self._split_desync_controller.set_modifier_key(keyval, False)
            self._sync_split_desync_legacy_state()

    def do_close_attempt(self) -> None:
        self._request_close()

    def _build_ui(self) -> None:
        main = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        main.append(self._build_left_panel())
        main.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        main.append(self._build_right_panel())
        self.set_child(main)

    def _build_left_panel(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_size_request(220, -1)

        header = Gtk.CenterBox()

        self.search_button = Gtk.Button()
        self.search_button.set_icon_name("system-search-symbolic")
        self.search_button.set_tooltip_text("Search Analog Controls")
        self.search_button.connect("clicked", self._on_search_clicked)
        header.set_start_widget(self.search_button)

        label = Gtk.Label(label="Analog Controls")
        label.add_css_class("title-4")
        label.set_halign(Gtk.Align.CENTER)
        header.set_center_widget(label)

        header_spacer = Gtk.Box()
        header_spacer.set_size_request(34, -1)
        header.set_end_widget(header_spacer)
        box.append(header)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search Analog Controls")
        self.search_entry.set_tooltip_text(
            "Filter Analog Controls by name, description, input type, or output"
        )
        self.search_entry.set_visible(False)
        self.search_entry.connect("stop-search", self._on_search_stop)
        box.append(self.search_entry)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.list_box = Gtk.ListBox()
        self.list_box.set_vexpand(True)
        self.list_box.connect("row-selected", self._on_control_selected)
        install_listbox_fuzzy_filter(
            self.list_box,
            self.search_entry,
            before_filter_changed=self._before_search_filter_changed,
            after_filter_changed=self._after_search_filter_changed,
        )
        scrolled.set_child(self.list_box)
        box.append(scrolled)

        footer = Gtk.CenterBox()

        self.analog_controls_docs_btn = Gtk.Button(label="?")
        self.analog_controls_docs_btn.add_css_class("flat")
        self.analog_controls_docs_btn.add_css_class("actions-docs-button")
        self.analog_controls_docs_btn.set_tooltip_text(
            "Open Analog Controls documentation"
        )
        self.analog_controls_docs_btn.connect(
            "clicked",
            self._on_analog_controls_docs_clicked,
        )
        footer.set_start_widget(self.analog_controls_docs_btn)

        add_button = Gtk.Button(icon_name="list-add-symbolic")
        add_button.set_tooltip_text("Add a new Analog Control")
        add_button.connect("clicked", self._on_add_clicked)
        footer.set_center_widget(add_button)

        box.append(footer)
        return box

    def _show_search(self) -> None:
        self.search_entry.set_visible(True)
        self.search_entry.grab_focus()
        self.search_entry.select_region(0, -1)

    def _hide_search(self) -> None:
        self.search_entry.set_text("")
        self.search_entry.set_visible(False)

    def _on_search_clicked(self, _button: Gtk.Button) -> None:
        self._show_search()

    def _on_search_stop(self, _entry: Gtk.SearchEntry) -> None:
        self._hide_search()

    def _before_search_filter_changed(self) -> None:
        self._suppress_selection_guard = True

    def _after_search_filter_changed(self) -> None:
        try:
            self._restore_active_selection()
        finally:
            self._suppress_selection_guard = False

    def _build_right_panel(self) -> Gtk.Widget:
        self.right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.right_box.set_margin_top(12)
        self.right_box.set_margin_bottom(12)
        self.right_box.set_margin_start(12)
        self.right_box.set_margin_end(12)
        self.right_box.set_hexpand(True)

        self.editor_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.editor_box.set_sensitive(False)

        fields_grid = Gtk.Grid()
        fields_grid.set_column_spacing(12)
        fields_grid.set_row_spacing(8)

        self._attach_labeled(fields_grid, "Name:", 0, self._build_name_entry())
        self._attach_labeled(fields_grid, "Description:", 1, self._build_description_entry())

        self.input_type_dropdown = Gtk.DropDown.new_from_strings(
            list(_option_labels(_INPUT_TYPE_OPTIONS))
        )
        self.mode_dropdown = Gtk.DropDown.new_from_strings(
            list(_mode_labels_for_input_type("stick"))
        )
        self.input_type_dropdown.connect("notify::selected", self._on_input_type_changed)
        self.mode_dropdown.connect("notify::selected", self._on_mode_changed)
        self._attach_labeled(fields_grid, "Input Type:", 2, self.input_type_dropdown)
        self._attach_labeled(fields_grid, "Mode:", 3, self.mode_dropdown)

        self.editor_box.append(fields_grid)
        self.editor_box.append(Gtk.Separator())

        self.mouse_group = self._build_mouse_group()
        self.gamepad_output_group = self._build_gamepad_output_group()
        self.digital_group = self._build_digital_group()
        self.template_group = self._build_template_group()
        self.threshold_editor = ThresholdEditor(
            self.digital_group,
            get_thresholds=lambda: self._thresholds,
            set_thresholds=self._set_thresholds,
            get_domain=self._threshold_domain,
            is_axis_control=self._is_axis_control,
            get_current_mode=self._current_mode,
            ensure_digital_mode=self._ensure_digital_mode,
            on_modified=self._on_modified,
            open_actions_dialog=self._open_threshold_actions_dialog,
        )
        self._threshold_rows = self.threshold_editor.threshold_rows
        self.editor_box.append(self.mouse_group)
        self.editor_box.append(self.gamepad_output_group)
        self.editor_box.append(self.digital_group)
        self.editor_box.append(self.template_group)

        editor_scrolled = Gtk.ScrolledWindow()
        editor_scrolled.set_vexpand(True)
        editor_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        editor_scrolled.set_child(self.editor_box)
        self.right_box.append(editor_scrolled)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        footer.set_hexpand(True)
        footer.set_margin_top(12)

        self.delete_btn = Gtk.Button(label="Delete")
        self.delete_btn.set_sensitive(False)
        self.delete_btn.add_css_class("destructive-action")
        self.delete_btn.connect("clicked", self._on_delete_clicked)
        footer.append(self.delete_btn)

        footer_spacer = Gtk.Box()
        footer_spacer.set_hexpand(True)
        footer.append(footer_spacer)

        self.save_btn = Gtk.Button(label="Save")
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.set_sensitive(False)
        self.save_btn.connect("clicked", self._on_save_clicked)
        footer.append(self.save_btn)

        self.revert_btn = Gtk.Button(label="Revert")
        self.revert_btn.set_sensitive(False)
        self.revert_btn.connect("clicked", self._on_revert_clicked)
        footer.append(self.revert_btn)

        self.close_btn = Gtk.Button(label="Close")
        self.close_btn.connect("clicked", self._on_close_clicked)
        footer.append(self.close_btn)

        self.right_box.append(footer)
        self._update_mode_visibility()
        return self.right_box

    def _build_name_entry(self) -> Gtk.Entry:
        self.name_entry = Gtk.Entry()
        self.name_entry.set_hexpand(True)
        self.name_entry.connect("changed", self._on_modified)
        return self.name_entry

    def _build_description_entry(self) -> Gtk.Entry:
        self.description_entry = Gtk.Entry()
        self.description_entry.set_hexpand(True)
        self.description_entry.connect("changed", self._on_modified)
        return self.description_entry

    def _attach_labeled(
        self,
        grid: Gtk.Grid,
        label_text: str,
        row: int,
        widget: Gtk.Widget,
    ) -> None:
        label = Gtk.Label(label=label_text)
        label.set_xalign(0)
        label.set_size_request(104, -1)
        grid.attach(label, 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)

    def _build_mouse_group(self) -> Adw.PreferencesGroup:
        handle = build_mouse_group(self)
        self._install_mouse_group(handle)
        self._update_mouse_curve_graph()
        return handle.group

    def _install_mouse_group(self, handle) -> None:
        self.speed_row = handle.speed_row
        self.speed_x_row = handle.speed_x_row
        self.speed_y_row = handle.speed_y_row
        self.area_radius_x_row = handle.area_radius_x_row
        self.area_radius_y_row = handle.area_radius_y_row
        self.deadzone_row = handle.deadzone_row
        self.mouse_sensitivity_row = handle.mouse_sensitivity_row
        self.mouse_response_curve_row = handle.mouse_response_curve_row
        self.mouse_curve_row = handle.mouse_curve_row
        self.mouse_curve_graph = handle.mouse_curve_graph
        self.mouse_direction_row = handle.mouse_direction_row
        self._mouse_direction_buttons = handle.mouse_direction_buttons
        self.invert_axes_row = handle.invert_axes_row
        self.invert_x_btn = handle.invert_x_btn
        self.invert_y_btn = handle.invert_y_btn
        self.area_start_enabled_row = handle.area_start_enabled_row
        self.area_start_position_row = handle.area_start_position_row
        self.area_start_x_entry = handle.area_start_x_entry
        self.area_start_y_entry = handle.area_start_y_entry
        self.area_start_capture_row = handle.area_start_capture_row
        self.area_start_capture_delay_spin = handle.area_start_capture_delay_spin
        self.area_start_capture_status = handle.area_start_capture_status
        self.area_start_capture_btn = handle.area_start_capture_btn

    def _build_gamepad_output_group(self) -> Adw.PreferencesGroup:
        handle = build_gamepad_output_group(self)
        self._install_gamepad_output_group(handle)
        self._update_gamepad_output_curve_graph()
        self._refresh_gamepad_output_choices()
        self._set_gamepad_output_target_options("stick", "same")
        self._update_gamepad_output_visibility()
        return handle.group

    def _install_gamepad_output_group(self, handle) -> None:
        self.gamepad_output_target_row = handle.gamepad_output_target_row
        self._gamepad_output_dropdown = handle.gamepad_output_dropdown
        self.gamepad_output_target_side_row = handle.gamepad_output_target_side_row
        self._gamepad_output_target_box = handle.gamepad_output_target_box
        self.gamepad_output_deadzone_row = handle.gamepad_output_deadzone_row
        self.gamepad_output_rest_row = handle.gamepad_output_rest_row
        self.gamepad_output_direction_row = handle.gamepad_output_direction_row
        self.gamepad_output_direction_min_btn = handle.gamepad_output_direction_min_btn
        self.gamepad_output_direction_max_btn = handle.gamepad_output_direction_max_btn
        self.gamepad_output_direction_both_btn = handle.gamepad_output_direction_both_btn
        self.gamepad_output_invert_row = handle.gamepad_output_invert_row
        self.gamepad_output_invert_x_btn = handle.gamepad_output_invert_x_btn
        self.gamepad_output_invert_y_btn = handle.gamepad_output_invert_y_btn
        self.gamepad_output_sensitivity_row = handle.gamepad_output_sensitivity_row
        self.gamepad_output_response_curve_row = handle.gamepad_output_response_curve_row
        self.gamepad_output_curve_row = handle.gamepad_output_curve_row
        self.gamepad_output_curve_graph = handle.gamepad_output_curve_graph
        self._gamepad_output_warning_row = handle.gamepad_output_warning_row
        self._gamepad_output_warning_label = handle.gamepad_output_warning_label

    def _build_digital_group(self) -> Adw.PreferencesGroup:
        handle = build_digital_group(self)
        self.add_range_row = handle.add_range_row
        return handle.group

    def _build_template_group(self) -> Adw.PreferencesGroup:
        handle = build_template_group(self)
        return handle.group

    def _spin_row(
        self,
        title: str,
        value: float,
        lower: float,
        upper: float,
        step: float,
        digits: int,
        page_step: float | None = None,
    ) -> Adw.SpinRow:
        return spin_row(
            title,
            value,
            lower,
            upper,
            step,
            digits,
            page_step=page_step,
            on_changed=self._on_modified,
        )

    def _compact_int_entry(self, value: int) -> Gtk.Entry:
        return compact_int_entry(value, self._int_entry_controller)

    def _on_int_entry_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
        entry: Gtk.Entry,
    ) -> bool:
        return int_entry_key_pressed(_controller, keyval, _keycode, state, entry)

    def _on_int_entry_changed(self, entry: Gtk.Entry) -> None:
        self._int_entry_controller.on_changed(entry)

    def _apply_sanitized_int_entry_text(self, entry: Gtk.Entry) -> bool:
        return self._int_entry_controller.apply_sanitized_text(entry)

    def _sanitize_int_entry_text(self, text: str) -> str:
        return sanitize_int_entry_text(text)

    def _entry_int_value(self, entry: Gtk.Entry) -> int:
        return entry_int_value(entry)

    def _set_entry_int(self, entry: Gtk.Entry, value: int) -> None:
        set_entry_int(entry, value)

    def _build_new_control_row(self) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row._is_new_analog_control = True
        row._search_text = "add new analog control"
        row.add_css_class("superkey-add-row")
        row.set_tooltip_text("Add a new Analog Control")
        label = Gtk.Label(label="+ Add", xalign=0)
        label.add_css_class("dim-label")
        row.set_child(label)
        return row

    def _build_control_group_row(self, title: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)
        row._search_text = title
        label = Gtk.Label(label=title, xalign=0)
        label.add_css_class("caption")
        label.add_css_class("dim-label")
        label.set_margin_start(6)
        label.set_margin_end(6)
        label.set_margin_top(10)
        label.set_margin_bottom(2)
        row.set_child(label)
        return row

    def _build_saved_control_row(
        self,
        name: str,
        search_text: str | None = None,
    ) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row._analog_control_name = name
        row._search_text = search_text or name
        label = Gtk.Label(label=name, xalign=0)
        label.set_margin_start(6)
        label.set_margin_end(6)
        label.set_margin_top(6)
        label.set_margin_bottom(6)
        row.set_child(label)
        return row

    def _current_mode(self) -> str:
        items = self._mode_items
        selected = int(self.mode_dropdown.get_selected())
        if selected < 0 or selected >= len(items):
            return items[0]
        return items[selected]

    def _set_mode_options(self, input_type: str, selected_mode: str | None = None) -> None:
        items = _mode_items_for_input_type(input_type)
        labels = _mode_labels_for_input_type(input_type)
        mode = selected_mode or items[0]
        selected = _mode_index_for_input_type(input_type, mode) if mode in items else 0
        self._mode_items = items
        self.mode_dropdown.set_model(Gtk.StringList.new(list(labels)))
        self.mode_dropdown.set_selected(selected)

    def _set_gamepad_output_target_options(
        self,
        input_type: str,
        selected_target: str | None = None,
        selected_analog_id: str | None = None,
    ) -> None:
        target = selected_target or "same"
        choices = self._gamepad_output_target_choices(input_type)
        if not choices:
            choices = [
                (
                    "same",
                    None,
                    _gamepad_output_target_label_for_input_type(input_type, "same"),
                )
            ]
        selected_key = self._gamepad_output_target_key(target, selected_analog_id)
        if selected_key not in {
            self._gamepad_output_target_key(item_target, analog_id)
            for item_target, analog_id, _label in choices
        }:
            selected_key = self._gamepad_output_target_key(choices[0][0], choices[0][1])

        target_box = self._gamepad_output_target_box
        if target_box is None:
            return
        while True:
            child = target_box.get_first_child()
            if child is None:
                break
            target_box.remove(child)
        self._gamepad_output_target_buttons.clear()
        self._gamepad_output_target_items = [
            (item_target, analog_id) for item_target, analog_id, _label in choices
        ]
        target_group: Gtk.ToggleButton | None = None
        row_box: Gtk.Box | None = None
        for item_target, analog_id, label in choices:
            if row_box is None or len(self._gamepad_output_target_buttons) % 3 == 0:
                row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
                if row_box is None:
                    raise RuntimeError("failed to create gamepad output target row")
                row_box.add_css_class("linked")
                row_box.set_halign(Gtk.Align.END)
                row_box.set_valign(Gtk.Align.CENTER)
                row_box.set_homogeneous(True)
                target_box.append(row_box)
            key = self._gamepad_output_target_key(item_target, analog_id)
            button = Gtk.ToggleButton(label=label)
            button.add_css_class("analog-output-target-button")
            button.set_valign(Gtk.Align.CENTER)
            button.set_size_request(-1, 34)
            if target_group is None:
                target_group = button
            else:
                button.set_group(target_group)
            button.connect(
                "toggled",
                self._on_gamepad_output_target_toggled,
                item_target,
                analog_id,
            )
            self._gamepad_output_target_buttons[key] = button
            assert row_box is not None
            row_box.append(button)
            if key == selected_key:
                button.set_active(True)

    def _gamepad_output_target_choices(
        self,
        input_type: str,
    ) -> list[tuple[str, str | None, str]]:
        selected_output_id = self._selected_gamepad_output_id
        hardware_config = (
            self._hardware_output_configs.get(selected_output_id or "")
            if selected_output_id and not is_virtual_gamepad_output_id(selected_output_id)
            else None
        )
        if hardware_config is not None:
            same_label = _gamepad_output_target_label_for_input_type(input_type, "same")
            choices: list[tuple[str, str | None, str]] = [("same", None, same_label)]
            for analog in getattr(hardware_config, "analog_inputs", []) or []:
                if getattr(analog, "type", None) != input_type:
                    continue
                analog_id = str(getattr(analog, "id", "") or "")
                if not analog_id:
                    continue
                label = str(getattr(analog, "label", "") or analog_id)
                choices.append(("analog", analog_id, label))
            return choices

        return [
            (option.item_id, None, option.label)
            for option in _gamepad_output_target_options_for_input_type(input_type)
        ]

    def _gamepad_output_target_key(self, target: str, analog_id: str | None) -> str:
        if target == "analog" and analog_id:
            return f"analog:{analog_id}"
        return target

    def _current_gamepad_output_target(self) -> str:
        for target, analog_id in self._gamepad_output_target_items:
            button = self._gamepad_output_target_buttons.get(
                self._gamepad_output_target_key(target, analog_id)
            )
            if button is not None and button.get_active():
                return target
        return "same"

    def _current_gamepad_output_target_analog_id(self) -> str | None:
        for target, analog_id in self._gamepad_output_target_items:
            button = self._gamepad_output_target_buttons.get(
                self._gamepad_output_target_key(target, analog_id)
            )
            if button is not None and button.get_active() and target == "analog":
                return analog_id
        return None

    def _current_gamepad_output_direction(self) -> str:
        if self.gamepad_output_direction_both_btn.get_active():
            return "both"
        if self.gamepad_output_direction_min_btn.get_active():
            return "min"
        return "max"

    def _current_mouse_direction(self) -> str:
        for direction, button in self._mouse_direction_buttons.items():
            if button.get_active():
                return direction
        return "right"

    def _remember_split_mouse_speeds(self) -> None:
        self._last_speed_x = self.speed_x_row.get_value()
        self._last_speed_y = self.speed_y_row.get_value()

    def _remember_area_radii(self) -> None:
        self._last_area_radius_x = self.area_radius_x_row.get_value()
        self._last_area_radius_y = self.area_radius_y_row.get_value()

    def _add_split_mouse_speed_desync_controller(
        self,
        row: Adw.SpinRow,
        axis: str,
    ) -> None:
        self._split_desync_controller.add_click_controller(row, axis)

    def _on_split_mouse_speed_click(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        _x: float,
        _y: float,
        axis: str,
    ) -> None:
        self._split_desync_controller._on_click(gesture, _n_press, _x, _y, axis)
        self._sync_split_desync_legacy_state()

    def _request_split_mouse_speed_desync(self, axis: str) -> None:
        self._split_desync_controller.request(axis)
        self._sync_split_desync_legacy_state()

    def _clear_split_mouse_speed_desync(self, axis: str | None = None) -> bool:
        result = self._split_desync_controller.clear(axis)
        self._sync_split_desync_legacy_state()
        return result

    def _split_mouse_speed_desync_requested(self, axis: str) -> bool:
        return self._split_desync_controller.requested(axis)

    def _sync_split_desync_legacy_state(self) -> None:
        self._split_mouse_speed_desync_axis = self._split_desync_controller.axis
        self._split_mouse_speed_desync_clear_id = self._split_desync_controller.clear_id
        self._split_mouse_speed_modifier_active = self._split_desync_controller.modifier_active

    def _add_spin_secondary_step_controller(
        self,
        row: Adw.SpinRow,
        page_step: float | None = None,
        reset_value: float | None = None,
        split_speed_axis: str | None = None,
    ) -> None:
        add_spin_secondary_step_controller(
            row,
            page_step=page_step,
            reset_value=reset_value,
            split_desync_axis=split_speed_axis,
            request_split_desync=self._request_split_mouse_speed_desync,
        )

    def _on_spin_secondary_step_pressed(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        _y: float,
        row: Adw.SpinRow,
        page_step: float | None,
        reset_value: float | None,
        split_speed_axis: str | None,
    ) -> None:
        direction = self._spin_secondary_step_direction(row, x)
        if direction is None:
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        try:
            state = gesture.get_current_event_state()
        except (RuntimeError, TypeError, ValueError):
            state = Gdk.ModifierType(0)
        if split_speed_axis and state & SPLIT_DESYNC_MODIFIERS:
            self._request_split_mouse_speed_desync(split_speed_axis)
        self._apply_spin_secondary_step(row, direction, page_step, reset_value)

    def _spin_secondary_step_direction(self, row: Adw.SpinRow, x: float) -> int | None:
        return spin_secondary_step_direction(row, x)

    def _apply_spin_secondary_step(
        self,
        row: Adw.SpinRow,
        direction: int,
        page_step: float | None,
        reset_value: float | None = None,
    ) -> None:
        apply_spin_secondary_step(row, direction, page_step, reset_value)

    def _on_split_mouse_speed_changed(
        self,
        _row: Adw.SpinRow,
        _param: object,
        axis: str,
    ) -> None:
        if self._syncing_mouse_speed:
            return

        was_synced = abs(self._last_speed_x - self._last_speed_y) < 0.000001
        if was_synced and not self._split_mouse_speed_desync_requested(axis):
            self._syncing_mouse_speed = True
            try:
                if axis == "x":
                    self.speed_y_row.set_value(self.speed_x_row.get_value())
                else:
                    self.speed_x_row.set_value(self.speed_y_row.get_value())
            finally:
                self._syncing_mouse_speed = False
        if self._split_mouse_speed_desync_axis == axis:
            clear_id = self._split_mouse_speed_desync_clear_id
            self._split_mouse_speed_desync_clear_id = 0
            if clear_id:
                GLib.source_remove(clear_id)
            self._clear_split_mouse_speed_desync(axis)
        self._remember_split_mouse_speeds()

    def _on_area_radius_changed(
        self,
        _row: Adw.SpinRow,
        _param: object,
        axis: str,
    ) -> None:
        if self._syncing_area_radius:
            return

        was_synced = abs(self._last_area_radius_x - self._last_area_radius_y) < 0.000001
        if was_synced and not self._split_mouse_speed_desync_requested(axis):
            self._syncing_area_radius = True
            try:
                if axis == "x":
                    self.area_radius_y_row.set_value(self.area_radius_x_row.get_value())
                else:
                    self.area_radius_x_row.set_value(self.area_radius_y_row.get_value())
            finally:
                self._syncing_area_radius = False
        if self._split_mouse_speed_desync_axis == axis:
            clear_id = self._split_mouse_speed_desync_clear_id
            self._split_mouse_speed_desync_clear_id = 0
            if clear_id:
                GLib.source_remove(clear_id)
            self._clear_split_mouse_speed_desync(axis)
        self._remember_area_radii()

    def _on_invert_axis_toggled(
        self,
        _button: Gtk.ToggleButton,
        _axis: str,
    ) -> None:
        if self._syncing_invert_axes:
            return
        self._on_modified()

    def _on_gamepad_output_target_toggled(
        self,
        button: Gtk.ToggleButton,
        target: str,
        analog_id: str | None,
    ) -> None:
        _ = target, analog_id
        if not button.get_active():
            return
        self._on_modified()

    def _on_gamepad_output_direction_toggled(self, button: Gtk.ToggleButton) -> None:
        if not button.get_active():
            return
        self._update_mode_visibility()
        self._on_modified()

    def _on_gamepad_output_invert_toggled(
        self,
        _button: Gtk.ToggleButton,
        _axis: str,
    ) -> None:
        self._on_modified()

    def _on_area_start_enabled_changed(self, *_args) -> None:
        self._update_mode_visibility()
        self._on_modified()

    def _on_area_capture_position_clicked(self, _button: Gtk.Button) -> None:
        self._begin_position_capture(
            self.area_start_capture_btn,
            self.area_start_capture_status,
            self._apply_area_start_capture_position,
        )

    def _apply_area_start_capture_position(self, x: int, y: int) -> None:
        self._set_entry_int(self.area_start_x_entry, int(x))
        self._set_entry_int(self.area_start_y_entry, int(y))
        self._on_modified()

    def _current_input_type(self) -> str:
        selected = int(self.input_type_dropdown.get_selected())
        if selected < 0 or selected >= len(_INPUT_TYPE_OPTIONS):
            return "stick"
        return _INPUT_TYPE_OPTIONS[selected].item_id

    def _is_axis_control(self) -> bool:
        return self._current_input_type() == "axis"

    def _update_mode_visibility(self) -> None:
        if not hasattr(self, "mouse_group"):
            return
        input_type = self._current_input_type()
        mode = self._current_mode()
        is_axis = input_type == "axis"
        digital_visible = mode == "digital"
        mouse_visible = mode in {"mouse", "mouse_area"}
        mouse_area_visible = mode == "mouse_area" and not is_axis
        mouse_velocity_visible = mouse_visible and not mouse_area_visible
        self.mouse_group.set_visible(mouse_visible)
        self.speed_row.set_visible(mouse_velocity_visible and is_axis)
        self.speed_x_row.set_visible(mouse_velocity_visible and not is_axis)
        self.speed_y_row.set_visible(mouse_velocity_visible and not is_axis)
        self.area_radius_x_row.set_visible(mouse_area_visible)
        self.area_radius_y_row.set_visible(mouse_area_visible)
        self.mouse_direction_row.set_visible(mouse_velocity_visible and is_axis)
        self.invert_axes_row.set_title("Invert Axis" if is_axis else "Invert Axes")
        self.invert_axes_row.set_visible(mouse_visible)
        self.invert_y_btn.set_visible(not is_axis)
        self.area_start_enabled_row.set_visible(mouse_area_visible)
        show_area_start = mouse_area_visible and self.area_start_enabled_row.get_active()
        self.area_start_position_row.set_visible(show_area_start)
        self.area_start_capture_row.set_visible(show_area_start)
        if not show_area_start:
            self._cancel_capture_position("")
        self.gamepad_output_group.set_visible(mode == "gamepad")
        show_axis_output_tuning = mode == "gamepad" and is_axis
        show_output_tuning = mode == "gamepad"
        self.gamepad_output_rest_row.set_visible(show_axis_output_tuning)
        self.gamepad_output_direction_row.set_visible(show_axis_output_tuning)
        show_output_invert = mode == "gamepad" and (
            not is_axis or self._current_gamepad_output_direction() == "both"
        )
        self.gamepad_output_invert_row.set_title(
            "Invert Output Axis" if is_axis else "Invert Output Axes"
        )
        self.gamepad_output_invert_row.set_visible(show_output_invert)
        self.gamepad_output_invert_y_btn.set_visible(not is_axis)
        self.gamepad_output_sensitivity_row.set_visible(show_output_tuning)
        self.gamepad_output_response_curve_row.set_visible(show_output_tuning)
        self.gamepad_output_curve_row.set_visible(show_output_tuning)
        self.digital_group.set_visible(digital_visible)
        self.template_group.set_visible(digital_visible and not is_axis)
        self._update_gamepad_output_visibility()

    def _update_gamepad_output_visibility(self) -> None:
        if not hasattr(self, "gamepad_output_group"):
            return
        self._update_gamepad_output_warning()

    def _refresh_gamepad_output_choices(self) -> None:
        dropdown = self._gamepad_output_dropdown
        if dropdown is None:
            return
        selected_id = self._selected_gamepad_output_id
        choices = self._gamepad_output_choices()
        self._gamepad_output_ids = [output_id for output_id, _label in choices]
        selected = 0
        for index, output_id in enumerate(self._gamepad_output_ids):
            if gamepad_output_choice_matches(output_id, selected_id):
                selected = index
                break
        self._refreshing_gamepad_output_choices = True
        try:
            dropdown.set_model(Gtk.StringList.new([label for _output_id, label in choices]))
            dropdown.set_selected(selected)
        finally:
            self._refreshing_gamepad_output_choices = False
        self._selected_gamepad_output_id = self._gamepad_output_ids[selected]
        self._set_gamepad_output_target_options(
            self._current_input_type(),
            self._current_gamepad_output_target(),
            self._current_gamepad_output_target_analog_id(),
        )
        self._update_gamepad_output_warning()

    def _gamepad_output_choices(self) -> list[tuple[str | None, str]]:
        helper_selected_id = (
            None
            if self._selected_gamepad_output_id == SAME_DEVICE_OUTPUT_ID
            else self._selected_gamepad_output_id
        )
        choice_set = load_gamepad_output_choices(
            helper_selected_id,
            count_loader=virtual_gamepad_count,
            hardware_manager_factory=hardware_manager,
        )
        self._hardware_output_configs = {
            str(getattr(config, "hardware_id", "") or ""): config
            for config in choice_set.hardware_configs
        }
        return [(SAME_DEVICE_OUTPUT_ID, "Default (same device)")] + choice_set.choices

    def _on_gamepad_output_selected(self, dropdown: Gtk.DropDown, _param) -> None:
        if self._refreshing_gamepad_output_choices:
            return
        current_target = self._current_gamepad_output_target()
        current_analog_id = self._current_gamepad_output_target_analog_id()
        self._selected_gamepad_output_id = selected_gamepad_output_id(
            int(dropdown.get_selected()),
            self._gamepad_output_ids,
            self._selected_gamepad_output_id,
        )
        self._set_gamepad_output_target_options(
            self._current_input_type(),
            current_target,
            current_analog_id,
        )
        self._update_gamepad_output_warning()
        self._on_modified()

    def _update_gamepad_output_warning(self) -> None:
        label = self._gamepad_output_warning_label
        row = getattr(self, "_gamepad_output_warning_row", None)
        if label is None or row is None:
            return
        if self._current_mode() != "gamepad":
            label.set_label("")
            row.set_visible(False)
            return
        message = update_gamepad_output_warning_label(
            label,
            self._selected_gamepad_output_id,
            count_loader=virtual_gamepad_count,
        )
        row.set_visible(bool(message))

    def _on_gamepad_output_curve_changed(self, *_args) -> None:
        self._update_gamepad_output_curve_graph()

    def _on_mouse_curve_changed(self, *_args) -> None:
        self._update_mouse_curve_graph()

    def _update_mouse_curve_graph(self) -> None:
        if not hasattr(self, "mouse_curve_graph"):
            return
        self.mouse_curve_graph.set_curve(
            deadzone=self.deadzone_row.get_value(),
            sensitivity=self.mouse_sensitivity_row.get_value(),
            response_curve=self.mouse_response_curve_row.get_value(),
        )

    def _update_gamepad_output_curve_graph(self) -> None:
        if not hasattr(self, "gamepad_output_curve_graph"):
            return
        self.gamepad_output_curve_graph.set_curve(
            deadzone=self.gamepad_output_deadzone_row.get_value() / 100.0,
            sensitivity=self.gamepad_output_sensitivity_row.get_value(),
            response_curve=self.gamepad_output_response_curve_row.get_value(),
        )

    def _load_controls(self) -> None:
        while row := self.list_box.get_row_at_index(0):
            self.list_box.remove(row)

        names = self.manager.list_analog_controls()
        configs = self.manager.get_all_analog_controls()
        first_saved_row: Gtk.ListBoxRow | None = None
        for title, group_names in _group_analog_control_names(names, configs):
            self.list_box.append(self._build_control_group_row(title))
            for name in group_names:
                row = self._build_saved_control_row(
                    name,
                    _analog_control_search_text(configs.get(name), name, title),
                )
                self.list_box.append(row)
                if first_saved_row is None:
                    first_saved_row = row

        self.new_control_row = self._build_new_control_row()
        self.list_box.append(self.new_control_row)
        self.list_box.invalidate_filter()

        if first_saved_row is not None:
            self.list_box.select_row(first_saved_row)
        else:
            self.list_box.select_row(self.new_control_row)

    def _on_control_selected(self, _list_box, row) -> None:
        if self._suppress_selection_guard:
            return

        selection_key = self._selection_key_for_row(row)
        if selection_key != self._active_selection_key:
            self._cancel_active_position_capture()

        if row is None:
            self._current_config = None
            self._current_name = None
            self._editing_new_control = False
            self.editor_box.set_sensitive(False)
            self.delete_btn.set_sensitive(False)
            self._modified = False
            self._active_selection_key = None
            self._update_buttons()
            return

        if (
            self._modified
            and selection_key is not None
            and self._active_selection_key is not None
            and selection_key != self._active_selection_key
        ):
            self._pending_selection_key = selection_key
            self._pending_new_control_after_warning = selection_key == ("new", None)
            self._restore_active_selection()
            self._show_unsaved_selection_warning()
            return

        if getattr(row, "_is_new_analog_control", False):
            if self._editing_new_control and self.editor_box.get_sensitive():
                return
            self._begin_new_control()
            return

        name = getattr(row, "_analog_control_name", None)
        if not isinstance(name, str):
            return
        if name == self._current_name and self.editor_box.get_sensitive():
            return
        config = self.manager.get_analog_control(name)
        if config is None:
            return
        self._editing_new_control = False
        self._load_config(config)
        self.delete_btn.set_sensitive(True)
        self._active_selection_key = ("name", name)
        self._modified = False
        self._update_buttons()

    def _selection_key_for_row(
        self,
        row: Gtk.ListBoxRow | None,
    ) -> tuple[str, str | None] | None:
        if row is None:
            return None
        if getattr(row, "_is_new_analog_control", False):
            return ("new", None)
        name = getattr(row, "_analog_control_name", None)
        if isinstance(name, str):
            return ("name", name)
        return None

    def _row_for_selection_key(
        self,
        key: tuple[str, str | None] | None,
    ) -> Gtk.ListBoxRow | None:
        if key is None:
            return None
        kind, name = key
        if kind == "new":
            return self.new_control_row
        idx = 0
        while True:
            row = self.list_box.get_row_at_index(idx)
            if row is None:
                return None
            if getattr(row, "_analog_control_name", None) == name:
                return row
            idx += 1

    def _restore_active_selection(self) -> None:
        row = self._row_for_selection_key(self._active_selection_key)
        if row is None:
            return
        self._suppress_selection_guard = True
        try:
            self.list_box.select_row(row)
        finally:
            self._suppress_selection_guard = False

    def _select_selection_key(self, key: tuple[str, str | None] | None) -> None:
        row = self._row_for_selection_key(key)
        if row is not None:
            self.list_box.select_row(row)

    def _load_config(self, config: AnalogControlConfig) -> None:
        self._current_config = config
        self._current_name = config.name
        self._thresholds = [self._copy_threshold(threshold) for threshold in config.thresholds]
        self.editor_box.set_sensitive(True)
        self.name_entry.set_text(config.name)
        self.description_entry.set_text(config.description or "")
        self.input_type_dropdown.set_selected(self._input_type_index(config))
        self._set_mode_options(config.input_type, self._mode_value(config))
        self._syncing_mouse_speed = True
        try:
            self.speed_row.set_value(config.mouse_motion.speed)
            self.speed_x_row.set_value(
                config.mouse_motion.speed_x
                if config.mouse_motion.speed_x is not None
                else config.mouse_motion.speed
            )
            self.speed_y_row.set_value(
                config.mouse_motion.speed_y
                if config.mouse_motion.speed_y is not None
                else config.mouse_motion.speed
            )
            self._syncing_area_radius = True
            self.area_radius_x_row.set_value(config.mouse_motion.area_radius_x)
            self.area_radius_y_row.set_value(config.mouse_motion.area_radius_y)
            self._syncing_area_radius = False
        finally:
            self._syncing_mouse_speed = False
            self._syncing_area_radius = False
        self._remember_split_mouse_speeds()
        self._remember_area_radii()
        self.deadzone_row.set_value(config.mouse_motion.deadzone)
        self.mouse_sensitivity_row.set_value(config.mouse_motion.sensitivity)
        self.mouse_response_curve_row.set_value(config.mouse_motion.response_curve)
        direction_button = self._mouse_direction_buttons.get(config.mouse_motion.direction)
        if direction_button is not None:
            direction_button.set_active(True)
        self._syncing_invert_axes = True
        try:
            self.invert_x_btn.set_active(config.mouse_motion.invert_x)
            self.invert_y_btn.set_active(config.mouse_motion.invert_y)
        finally:
            self._syncing_invert_axes = False
        self.area_start_enabled_row.set_active(config.mouse_motion.area_start_enabled)
        self._set_entry_int(self.area_start_x_entry, config.mouse_motion.area_start_x)
        self._set_entry_int(self.area_start_y_entry, config.mouse_motion.area_start_y)
        self._selected_gamepad_output_id = config.gamepad_output.output_id
        self._refresh_gamepad_output_choices()
        self._set_gamepad_output_target_options(
            config.input_type,
            config.gamepad_output.target,
            config.gamepad_output.target_analog_id,
        )
        self.gamepad_output_deadzone_row.set_value(
            round(config.gamepad_output.deadzone * 100.0)
        )
        self.gamepad_output_rest_row.set_value(config.gamepad_output.output_rest or 0)
        if config.gamepad_output.output_direction == "both":
            self.gamepad_output_direction_both_btn.set_active(True)
        elif config.gamepad_output.output_direction == "min":
            self.gamepad_output_direction_min_btn.set_active(True)
        else:
            self.gamepad_output_direction_max_btn.set_active(True)
        self.gamepad_output_invert_x_btn.set_active(
            config.gamepad_output.output_invert_x
            if config.input_type == "stick"
            else (
                config.gamepad_output.output_direction == "both"
                and config.gamepad_output.output_invert
            )
        )
        self.gamepad_output_invert_y_btn.set_active(config.gamepad_output.output_invert_y)
        self.gamepad_output_sensitivity_row.set_value(config.gamepad_output.sensitivity)
        self.gamepad_output_response_curve_row.set_value(config.gamepad_output.response_curve)
        self._update_mouse_curve_graph()
        self._update_gamepad_output_curve_graph()
        self._update_gamepad_output_visibility()
        self._refresh_thresholds()
        self._update_mode_visibility()

    def _mode_value(self, config: AnalogControlConfig) -> str:
        if config.gamepad_output.enabled:
            return "gamepad"
        has_mouse = bool(config.mouse_motion.enabled)
        has_digital = bool(config.thresholds)
        if has_mouse and config.mouse_motion.mode == "area":
            return "mouse_area"
        if has_digital:
            return "digital"
        return "mouse"

    def _input_type_index(self, config: AnalogControlConfig) -> int:
        if config.input_type == "axis":
            return _input_type_index("axis")
        return _input_type_index("stick")

    def _refresh_thresholds(self, expanded_indices: set[int] | None = None) -> None:
        self.threshold_editor.refresh(expanded_indices)

    def _expanded_threshold_indices(self) -> set[int]:
        return self.threshold_editor.expanded_indices()

    def _threshold_domain(self) -> tuple[float, float]:
        return (-1.0, 1.0)

    def _set_thresholds(self, thresholds: list[AnalogActionThreshold]) -> None:
        self._thresholds = thresholds

    def _ensure_digital_mode(self) -> None:
        self.mode_dropdown.set_selected(self._mode_items.index("digital"))

    def _sync_thresholds_for_input_type(self) -> None:
        self.threshold_editor.sync_for_input_type()

    def _begin_new_control(self) -> None:
        self._load_config(
            AnalogControlConfig(
                name="New Analog Control",
                gamepad_output=AnalogGamepadOutputConfig(output_id=SAME_DEVICE_OUTPUT_ID),
            )
        )
        self._current_name = None
        self._editing_new_control = True
        self.delete_btn.set_sensitive(False)
        self._active_selection_key = ("new", None)
        self._modified = True
        self._update_buttons()
        self.name_entry.grab_focus()

    def _on_add_clicked(self, _button=None) -> None:
        self._request_new_control()

    def _request_new_control(self) -> None:
        if self._new_control_reset_needs_warning():
            self._queue_new_control_warning()
            return
        self._select_or_begin_new_control()

    def _select_or_begin_new_control(self) -> None:
        if (
            self.new_control_row is not None
            and self.list_box.get_selected_row() is not self.new_control_row
        ):
            self.list_box.select_row(self.new_control_row)
        else:
            self._begin_new_control()

    def _new_control_reset_needs_warning(self) -> bool:
        if not self._modified:
            return False
        if self._active_selection_key != ("new", None):
            return True
        return not self._is_pristine_new_control_draft()

    def _is_pristine_new_control_draft(self) -> bool:
        default = AnalogControlConfig(
            name="New Analog Control",
            gamepad_output=AnalogGamepadOutputConfig(output_id=SAME_DEVICE_OUTPUT_ID),
        )
        mouse = default.mouse_motion
        gamepad = default.gamepad_output
        return (
            self._editing_new_control
            and self.name_entry.get_text() == default.name
            and self.description_entry.get_text() == ""
            and self._current_input_type() == default.input_type
            and self._current_mode() == self._mode_value(default)
            and not self._thresholds
            and self.speed_row.get_value() == mouse.speed
            and self.speed_x_row.get_value() == mouse.speed_x
            and self.speed_y_row.get_value() == mouse.speed_y
            and self.area_radius_x_row.get_value() == mouse.area_radius_x
            and self.area_radius_y_row.get_value() == mouse.area_radius_y
            and self.area_start_enabled_row.get_active() == mouse.area_start_enabled
            and self._entry_int_value(self.area_start_x_entry) == mouse.area_start_x
            and self._entry_int_value(self.area_start_y_entry) == mouse.area_start_y
            and self.deadzone_row.get_value() == mouse.deadzone
            and self.mouse_sensitivity_row.get_value() == mouse.sensitivity
            and self.mouse_response_curve_row.get_value() == mouse.response_curve
            and self._current_mouse_direction() == mouse.direction
            and self.invert_x_btn.get_active() == mouse.invert_x
            and self.invert_y_btn.get_active() == mouse.invert_y
            and self._selected_gamepad_output_id == gamepad.output_id
            and self.gamepad_output_deadzone_row.get_value() == gamepad.deadzone * 100.0
            and self._current_gamepad_output_target() == gamepad.target
            and self._current_gamepad_output_target_analog_id() == gamepad.target_analog_id
            and int(self.gamepad_output_rest_row.get_value()) == (gamepad.output_rest or 0)
            and self._current_gamepad_output_direction() == gamepad.output_direction
            and self.gamepad_output_invert_x_btn.get_active() == gamepad.output_invert_x
            and self.gamepad_output_invert_y_btn.get_active() == gamepad.output_invert_y
            and self.gamepad_output_sensitivity_row.get_value() == gamepad.sensitivity
            and self.gamepad_output_response_curve_row.get_value() == gamepad.response_curve
        )

    def _queue_new_control_warning(self) -> None:
        if self._selection_warning_dialog is not None:
            return
        self._pending_selection_key = ("new", None)
        self._pending_new_control_after_warning = True
        self._show_unsaved_selection_warning()

    def _open_threshold_actions_dialog(self, index: int) -> None:
        if not 0 <= index < len(self._thresholds):
            return
        dialog = ActionListDialog(
            self._parent,
            f"Edit Range {index + 1} Actions",
            "overload",
            list(self._thresholds[index].actions),
            action_key="analog_threshold",
        )
        dialog.connect("actions-selected", self._on_threshold_actions_selected, index)
        dialog.present(self._parent)

    def _on_add_range_clicked(self, *_args) -> None:
        self.threshold_editor.add_range()

    def _on_remove_threshold_clicked(self, _button, index: int) -> None:
        self.threshold_editor.remove_threshold(index)

    def _on_edit_threshold_actions_clicked(self, _button, index: int) -> None:
        self.threshold_editor.edit_threshold_actions(index)

    def _on_threshold_actions_selected(
        self,
        _dialog,
        actions: list[object],
        index: int,
    ) -> None:
        self.threshold_editor.actions_selected(actions, index)

    def _on_template_wasd(self, *_args) -> None:
        self.threshold_editor.apply_wasd_template()

    def _on_template_arrows(self, *_args) -> None:
        self.threshold_editor.apply_arrow_template()

    def _on_template_mouse_wheel(self, *_args) -> None:
        self.threshold_editor.apply_mouse_wheel_template()

    def _apply_template(self, thresholds: list[AnalogActionThreshold]) -> None:
        self.threshold_editor.apply_template(thresholds)

    def _on_input_type_changed(self, _dropdown, _param) -> None:
        if not hasattr(self, "digital_group"):
            return
        mode = self._current_mode()
        input_type = self._current_input_type()
        mode_items = _mode_items_for_input_type(input_type)
        if mode not in mode_items:
            mode = mode_items[0]
        self._set_mode_options(input_type, mode)
        self._set_gamepad_output_target_options(
            input_type,
            self._current_gamepad_output_target(),
            self._current_gamepad_output_target_analog_id(),
        )
        self._sync_thresholds_for_input_type()
        self._refresh_thresholds(self._expanded_threshold_indices())
        self._update_mode_visibility()
        self._on_modified()

    def _on_mode_changed(self, _dropdown, _param) -> None:
        self._update_mode_visibility()
        self._on_modified()

    def _on_modified(self, *_args) -> None:
        if not hasattr(self, "save_btn"):
            return
        self._modified = True
        self._update_buttons()

    def _update_buttons(self) -> None:
        if not hasattr(self, "save_btn"):
            return
        self.save_btn.set_sensitive(self._modified)
        self.revert_btn.set_sensitive(self._modified)
        self.set_can_close(not self._modified)

    def _on_revert_clicked(self, _button) -> None:
        if self._editing_new_control:
            self._begin_new_control()
        elif self._current_config is not None:
            self._load_config(self._current_config)
        self._modified = False
        self._update_buttons()

    def _on_save_clicked(self, _button) -> None:
        self._save_current_control()

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self._request_close()

    def _request_close(self) -> None:
        if not self._modified:
            self._cancel_capture_position("")
            self.force_close()
            return
        self._show_unsaved_close_warning()

    def _show_unsaved_close_warning(self) -> None:
        if self._close_warning_dialog is not None:
            return

        dialog = Adw.AlertDialog()
        dialog.set_heading("Unsaved Analog Control Changes")
        dialog.set_body("Save your changes before closing, or discard them?")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("discard", "Discard")
        dialog.add_response("save", "Save")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._on_unsaved_close_response)
        self._close_warning_dialog = dialog
        dialog.present(self)

    def _on_unsaved_close_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        self._close_warning_dialog = None
        if response == "discard":
            self._modified = False
            self._update_buttons()
            self._cancel_capture_position("")
            self.force_close()
            return
        if response == "save" and self._save_current_control():
            self._cancel_capture_position("")
            self.force_close()

    def _show_unsaved_selection_warning(self) -> None:
        if self._selection_warning_dialog is not None:
            return

        dialog = Adw.AlertDialog()
        dialog.set_heading("Unsaved Analog Control Changes")
        dialog.set_body(
            "Save your changes before starting a new Analog Control, or discard them?"
            if self._pending_new_control_after_warning
            else "Save your changes before switching, or discard them?"
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("discard", "Discard")
        dialog.add_response("save", "Save")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._on_unsaved_selection_response)
        self._selection_warning_dialog = dialog
        dialog.present(self)

    def _on_unsaved_selection_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        pending_key = self._pending_selection_key
        pending_new_control = self._pending_new_control_after_warning
        self._selection_warning_dialog = None
        self._pending_selection_key = None
        self._pending_new_control_after_warning = False
        if response == "discard":
            self._modified = False
            self._update_buttons()
            if pending_new_control:
                self._select_or_begin_new_control()
            else:
                self._select_selection_key(pending_key)
            return
        if response == "save" and self._save_current_control():
            if pending_new_control:
                self._select_or_begin_new_control()
            else:
                self._select_selection_key(pending_key)

    def _on_analog_controls_docs_clicked(self, _button: Gtk.Button) -> None:
        url = _analog_controls_docs_url()
        try:
            launcher = Gtk.UriLauncher.new(url)
            launcher.launch(None, None, None)
        except Exception:
            log.exception("Could not open Analog Controls documentation %s", url)

    def _set_position_capture_status(
        self,
        status_label: Gtk.Label | None,
        text: str,
        error: bool,
    ) -> None:
        self._set_capture_status(status_label, text, error=error)

    def _set_capture_status(
        self,
        status_label: Gtk.Label | None,
        text: str,
        *,
        error: bool = False,
    ) -> None:
        if status_label is None:
            return
        status_label.set_text(text)
        if error:
            status_label.add_css_class("capture-error-label")
        else:
            status_label.remove_css_class("capture-error-label")

    def _sync_position_capture_legacy_state(self) -> None:
        self._capture_timeout_id = self._position_capture.timeout_id
        self._capture_pending = self._position_capture.pending
        self._capture_request_id = self._position_capture.request_id
        self._capture_apply = self._position_capture.apply
        self._capture_status_label = self._position_capture.status_label
        self._capture_button = self._position_capture.button
        self._capture_delay_seconds = self._position_capture.delay_seconds

    def _begin_position_capture(
        self,
        button: Gtk.Button | None,
        status_label: Gtk.Label | None,
        apply_position: Callable[[int, int], None],
    ) -> None:
        delay_seconds = (
            float(self.area_start_capture_delay_spin.get_value())
            if hasattr(self, "area_start_capture_delay_spin")
            else self._capture_delay_seconds
        )
        self._position_capture.begin(
            button=button,
            status_label=status_label,
            delay_seconds=delay_seconds,
            apply_position=apply_position,
        )
        self._sync_position_capture_legacy_state()

    def _on_slurp_capture_result(self, request_id: int, result) -> None:
        self._position_capture.on_slurp_result(request_id, result)
        self._sync_position_capture_legacy_state()

    def _capture_position_after_delay(self, request_id: int) -> bool:
        result = self._position_capture.capture_after_delay(request_id)
        self._sync_position_capture_legacy_state()
        return result

    def _on_capture_position_response(self, request_id: int, response: dict | None) -> bool:
        result = self._position_capture.on_response(request_id, response)
        self._sync_position_capture_legacy_state()
        return result

    def _cancel_capture_position(self, status_text: str) -> None:
        self._position_capture.cancel(status_text)
        self._sync_position_capture_legacy_state()

    def _cancel_active_position_capture(self) -> None:
        if self._capture_pending or self._capture_timeout_id or self._capture_apply is not None:
            self._cancel_capture_position("")

    def _save_current_control(self) -> bool:
        name = self.name_entry.get_text().strip()
        if not name:
            return False

        mode = self._current_mode()
        input_type = self._current_input_type()
        self._sync_thresholds_for_input_type()
        old_name = self._current_name
        config = AnalogControlConfig(
            name=name,
            description=self.description_entry.get_text().strip() or None,
            input_type=input_type,
            mouse_motion=AnalogMouseMotionConfig(
                enabled=mode in {"mouse", "mouse_area"},
                mode="area" if mode == "mouse_area" else "velocity",
                speed=self.speed_row.get_value(),
                speed_x=self.speed_x_row.get_value(),
                speed_y=self.speed_y_row.get_value(),
                area_radius_x=self.area_radius_x_row.get_value(),
                area_radius_y=self.area_radius_y_row.get_value(),
                area_start_enabled=self.area_start_enabled_row.get_active(),
                area_start_x=self._entry_int_value(self.area_start_x_entry),
                area_start_y=self._entry_int_value(self.area_start_y_entry),
                deadzone=self.deadzone_row.get_value(),
                sensitivity=self.mouse_sensitivity_row.get_value(),
                response_curve=self.mouse_response_curve_row.get_value(),
                direction=self._current_mouse_direction(),
                invert_x=self.invert_x_btn.get_active(),
                invert_y=input_type != "axis" and self.invert_y_btn.get_active(),
                tick_ms=(
                    self._current_config.mouse_motion.tick_ms
                    if self._current_config is not None
                    else 8
                ),
            ),
            gamepad_output=AnalogGamepadOutputConfig(
                enabled=mode == "gamepad",
                output_id=self._selected_gamepad_output_id,
                deadzone=self.gamepad_output_deadzone_row.get_value() / 100.0,
                target=self._current_gamepad_output_target(),
                target_analog_id=self._current_gamepad_output_target_analog_id(),
                output_rest=(
                    int(self.gamepad_output_rest_row.get_value())
                    if input_type == "axis"
                    else None
                ),
                output_direction=(
                    self._current_gamepad_output_direction()
                    if input_type == "axis"
                    else "max"
                ),
                output_invert=(
                    input_type == "axis"
                    and (
                        self.gamepad_output_direction_min_btn.get_active()
                        or (
                            self.gamepad_output_direction_both_btn.get_active()
                            and self.gamepad_output_invert_x_btn.get_active()
                        )
                    )
                ),
                output_invert_x=(
                    input_type == "stick"
                    and self.gamepad_output_invert_x_btn.get_active()
                ),
                output_invert_y=(
                    input_type == "stick"
                    and self.gamepad_output_invert_y_btn.get_active()
                ),
                sensitivity=self.gamepad_output_sensitivity_row.get_value(),
                response_curve=self.gamepad_output_response_curve_row.get_value(),
            ),
            thresholds=(
                list(self._thresholds)
                if mode == "digital"
                else []
            ),
        )
        try:
            self.manager.save_analog_control(config, replacing_name=old_name)
            if (
                old_name
                and old_name != name
                and self.profile_manager is not None
            ):
                self.profile_manager.rename_analog_control_references(old_name, name)
        except ValueError as exc:
            self._show_save_error(str(exc))
            return False

        self._current_name = name
        self._current_config = config
        self._editing_new_control = False
        self._modified = False
        self._update_buttons()
        self._load_controls()
        self._select_control(name)
        self.emit("analog-control-saved", name)
        return True

    def _show_save_error(self, message: str) -> None:
        dialog = Adw.AlertDialog()
        dialog.set_heading("Unable To Save Analog Control")
        dialog.set_body(message)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self)

    def select_control_by_name(self, name: str) -> None:
        """Select a saved analog control by name, e.g. when opened to edit one."""
        self._select_control(name)

    def _select_control(self, name: str) -> None:
        idx = 0
        while True:
            row = self.list_box.get_row_at_index(idx)
            if row is None:
                return
            if getattr(row, "_analog_control_name", None) == name:
                self.list_box.select_row(row)
                return
            idx += 1

    def _on_delete_clicked(self, _button) -> None:
        if not self._current_name:
            return
        name = self._current_name
        if not self.manager.delete_analog_control(name):
            return
        if self.profile_manager is not None:
            self.profile_manager.replace_analog_control_with_suppress(name)
        self.emit("analog-control-deleted", name)
        self._current_name = None
        self._current_config = None
        self._editing_new_control = False
        self._thresholds = []
        self.editor_box.set_sensitive(False)
        self.delete_btn.set_sensitive(False)
        self._modified = False
        self._update_buttons()
        self._load_controls()

    def _copy_threshold(self, threshold: AnalogActionThreshold) -> AnalogActionThreshold:
        return AnalogActionThreshold(
            axis=threshold.axis,
            trigger_min=threshold.trigger_min,
            trigger_max=threshold.trigger_max,
            release_min=threshold.release_min,
            release_max=threshold.release_max,
            actions=list(threshold.actions),
        )
