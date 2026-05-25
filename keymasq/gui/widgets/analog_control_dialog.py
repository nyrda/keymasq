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

from keymasq import __version__
from keymasq.common.models import (
    SAME_DEVICE_OUTPUT_ID,
    AnalogActionThreshold,
    AnalogControlConfig,
    AnalogGamepadOutputConfig,
    AnalogMouseMotionConfig,
    MappingAction,
    analog_control_primary_mode,
)
from keymasq.common.slurp import get_slurp_capture
from keymasq.common.virtual_devices import is_virtual_gamepad_output_id
from keymasq.gui.session_client import session_request_async
from keymasq.gui.widgets.action_labels import describe_mapping_action_verbose
from keymasq.gui.widgets.analog_curve_graph import AnalogCurveGraph
from keymasq.gui.widgets.fuzzy_search import install_listbox_fuzzy_filter
from keymasq.gui.widgets.key_selector_dialog import (  # pyright: ignore[reportPrivateUsage]
    _gamepad_output_choice_matches,
    _gamepad_output_choices_for,
    _gamepad_output_unavailable_message,
    _virtual_gamepad_count,
)
from keymasq.gui.widgets.superkey_dialog import ActionListDialog
from keymasq.gui.widgets.threshold_range_bar import ThresholdRangeBar
from keymasq.session.analog_controls import (
    AnalogControlManager,
    analog_control_arrow_template,
    analog_control_mouse_wheel_template,
    analog_control_wasd_template,
)
from keymasq.session.compositor import detect_compositor_sync
from keymasq.session.hardware import HardwareManager
from keymasq.session.profiles import ProfileManager

log = logging.getLogger("keymasq.gui.widgets.analog_control_dialog")

_STICK_MODE_ITEMS = ("mouse", "mouse_area", "digital", "gamepad")
_STICK_MODE_LABELS = (
    "Mouse Movement",
    "Mouse Area",
    "Digital Actions",
    "Analog Output",
)


def _analog_control_search_text(
    config: AnalogControlConfig | None,
    name: str,
    group_title: str,
) -> str:
    if config is None:
        return f"{name} {group_title}"
    output_parts: list[str] = []
    if config.mouse_motion.enabled:
        output_parts.append("mouse")
    if config.gamepad_output.enabled:
        output_parts.append("gamepad output")
    if config.thresholds:
        output_parts.append(f"{len(config.thresholds)} ranges thresholds")
    return " ".join(
        [
            str(config.name or ""),
            str(config.description or ""),
            group_title,
            config.input_type,
            analog_control_primary_mode(config),
            " ".join(output_parts),
        ]
    )
_AXIS_MODE_ITEMS = ("digital", "gamepad", "mouse")
_AXIS_MODE_LABELS = ("Digital Actions", "Analog Output", "Mouse Movement")
_INPUT_TYPE_ITEMS = ("stick", "axis")
_INPUT_TYPE_LABELS = ("Stick", "1D Axis / Trigger")
_GAMEPAD_OUTPUT_TARGET_ITEMS = ("same", "left", "right")
_STICK_OUTPUT_TARGET_LABELS = ("Same Stick", "Left Stick", "Right Stick")
_AXIS_OUTPUT_TARGET_LABELS = ("Same Axis", "Left Trigger", "Right Trigger")
_CONTROL_GROUPS = (("axis", "1D Axes / Triggers"), ("stick", "Sticks"))
_AXIS_ITEMS = ("x", "y")
_SPLIT_SPEED_DESYNC_MODIFIERS = Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.CONTROL_MASK
_SPLIT_SPEED_DESYNC_KEYS = {
    Gdk.KEY_Shift_L,
    Gdk.KEY_Shift_R,
    Gdk.KEY_Control_L,
    Gdk.KEY_Control_R,
}


def _docs_version() -> str:
    version = __version__.strip()
    if not version:
        return "master"
    if "dev" in version:
        return "master"
    return f"v{version.removeprefix('v')}"


def _analog_controls_docs_url() -> str:
    return f"https://keymasq.tools/docs/{_docs_version()}/ANALOG_CONTROLS/"


def _compute_hysteresis(threshold: AnalogActionThreshold) -> float:
    margin_low = threshold.trigger_min - threshold.release_min
    margin_high = threshold.release_max - threshold.trigger_max
    if margin_low < 0.001 and margin_high >= 0.001:
        return round(margin_high, 2)
    if margin_high < 0.001 and margin_low >= 0.001:
        return round(margin_low, 2)
    return round(min(margin_low, margin_high), 2)


def _clamp_threshold_value(value: float) -> float:
    return max(-1.0, min(1.0, float(value)))


def _to_percent(value: float) -> float:
    return round(_clamp_threshold_value(value) * 100.0, 0)


def _from_percent(value: float) -> float:
    return round(max(-100.0, min(100.0, float(value))) / 100.0, 2)


def _group_analog_control_names(
    names: list[str],
    configs: dict[str, AnalogControlConfig],
) -> list[tuple[str, list[str]]]:
    grouped: list[tuple[str, list[str]]] = []
    used: set[str] = set()
    for input_type, title in _CONTROL_GROUPS:
        group_names = [
            name
            for name in names
            if (config := configs.get(name)) is not None and config.input_type == input_type
        ]
        if group_names:
            grouped.append((title, group_names))
            used.update(group_names)

    other_names = [name for name in names if name not in used]
    if other_names:
        grouped.append(("Other", other_names))
    return grouped


def _mode_items_for_input_type(input_type: str) -> tuple[str, ...]:
    return _AXIS_MODE_ITEMS if input_type == "axis" else _STICK_MODE_ITEMS


def _mode_labels_for_input_type(input_type: str) -> tuple[str, ...]:
    return _AXIS_MODE_LABELS if input_type == "axis" else _STICK_MODE_LABELS


def _gamepad_output_target_labels_for_input_type(input_type: str) -> tuple[str, ...]:
    return (
        _AXIS_OUTPUT_TARGET_LABELS
        if input_type == "axis"
        else _STICK_OUTPUT_TARGET_LABELS
    )


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
        self._suppress_selection_guard = False
        self._editing_new_control = False
        self._syncing_threshold = False
        self._syncing_mouse_speed = False
        self._syncing_area_radius = False
        self._syncing_start_entry = False
        self._syncing_invert_axes = False
        self._last_speed_x = 900.0
        self._last_speed_y = 900.0
        self._last_area_radius_x = 400.0
        self._last_area_radius_y = 400.0
        self._split_mouse_speed_desync_axis: str | None = None
        self._split_mouse_speed_desync_clear_id = 0
        self._split_mouse_speed_modifier_active = False
        self._selected_gamepad_output_id: str | None = SAME_DEVICE_OUTPUT_ID
        self._gamepad_output_ids: list[str | None] = []
        self._gamepad_output_dropdown: Gtk.DropDown | None = None
        self._gamepad_output_warning_label: Gtk.Label | None = None
        self._refreshing_gamepad_output_choices = False
        self._mode_items: tuple[str, ...] = _STICK_MODE_ITEMS
        self._gamepad_output_target_items: list[tuple[str, str | None]] = []
        self._gamepad_output_target_buttons: dict[str, Gtk.ToggleButton] = {}
        self._gamepad_output_target_box: Gtk.Box | None = None
        self._hardware_output_configs: dict[str, object] = {}
        self._capture_delay_seconds: float = 2.0
        self._capture_timeout_id: int = 0
        self._capture_pending: bool = False
        self._capture_request_id: int = 0
        self._capture_apply: Callable[[int, int], None] | None = None
        self._capture_status_label: Gtk.Label | None = None
        self._capture_button: Gtk.Button | None = None
        self._slurp_capture = get_slurp_capture()
        self._slurp_capture.set_compositor(detect_compositor_sync())
        self._slurp_available = self._slurp_capture.available
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
        if keyval in _SPLIT_SPEED_DESYNC_KEYS:
            self._split_mouse_speed_modifier_active = True
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
        if keyval in _SPLIT_SPEED_DESYNC_KEYS:
            self._split_mouse_speed_modifier_active = False

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

        self.input_type_dropdown = Gtk.DropDown.new_from_strings(list(_INPUT_TYPE_LABELS))
        self.mode_dropdown = Gtk.DropDown.new_from_strings(list(_STICK_MODE_LABELS))
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
        group = Adw.PreferencesGroup(title="Mouse Movement")

        self.speed_row = self._spin_row("Speed", 900, 0, 5000, 25, 0, page_step=500)
        self._add_spin_secondary_step_controller(self.speed_row, page_step=500)
        group.add(self.speed_row)
        self.speed_x_row = self._spin_row(
            "Horizontal Speed",
            900,
            0,
            5000,
            25,
            0,
            page_step=500,
        )
        self.speed_x_row.connect("notify::value", self._on_split_mouse_speed_changed, "x")
        self._add_split_mouse_speed_desync_controller(self.speed_x_row, "x")
        self._add_spin_secondary_step_controller(
            self.speed_x_row,
            page_step=500,
            split_speed_axis="x",
        )
        group.add(self.speed_x_row)
        self.speed_y_row = self._spin_row(
            "Vertical Speed",
            900,
            0,
            5000,
            25,
            0,
            page_step=500,
        )
        self.speed_y_row.connect("notify::value", self._on_split_mouse_speed_changed, "y")
        self._add_split_mouse_speed_desync_controller(self.speed_y_row, "y")
        self._add_spin_secondary_step_controller(
            self.speed_y_row,
            page_step=500,
            split_speed_axis="y",
        )
        group.add(self.speed_y_row)
        self.area_radius_x_row = self._spin_row(
            "Horizontal Radius",
            400,
            0,
            10000,
            10,
            0,
            page_step=100,
        )
        self.area_radius_x_row.set_subtitle("Horizontal radius from the start point")
        self.area_radius_x_row.connect("notify::value", self._on_area_radius_changed, "x")
        self._add_split_mouse_speed_desync_controller(self.area_radius_x_row, "x")
        self._add_spin_secondary_step_controller(
            self.area_radius_x_row,
            page_step=100,
            split_speed_axis="x",
        )
        group.add(self.area_radius_x_row)
        self.area_radius_y_row = self._spin_row(
            "Vertical Radius",
            400,
            0,
            10000,
            10,
            0,
            page_step=100,
        )
        self.area_radius_y_row.set_subtitle("Vertical radius from the start point")
        self.area_radius_y_row.connect("notify::value", self._on_area_radius_changed, "y")
        self._add_split_mouse_speed_desync_controller(self.area_radius_y_row, "y")
        self._add_spin_secondary_step_controller(
            self.area_radius_y_row,
            page_step=100,
            split_speed_axis="y",
        )
        group.add(self.area_radius_y_row)
        self.deadzone_row = self._spin_row(
            "Deadzone",
            0.15,
            0,
            0.95,
            0.01,
            2,
            page_step=0.05,
        )
        self._add_spin_secondary_step_controller(self.deadzone_row, page_step=0.05)
        self.deadzone_row.connect("notify::value", self._on_mouse_curve_changed)
        group.add(self.deadzone_row)

        self.mouse_sensitivity_row = self._spin_row(
            "Sensitivity",
            1.0,
            0.1,
            2.0,
            0.05,
            2,
            page_step=0.25,
        )
        self._add_spin_secondary_step_controller(
            self.mouse_sensitivity_row,
            page_step=0.25,
        )
        self.mouse_sensitivity_row.set_subtitle("How quickly movement reaches full speed")
        self.mouse_sensitivity_row.connect("notify::value", self._on_mouse_curve_changed)
        group.add(self.mouse_sensitivity_row)

        self.mouse_response_curve_row = self._spin_row(
            "Response Curve",
            1.0,
            0.25,
            4.0,
            0.05,
            2,
            page_step=0.25,
        )
        self._add_spin_secondary_step_controller(
            self.mouse_response_curve_row,
            page_step=0.25,
        )
        self.mouse_response_curve_row.set_subtitle(
            "Below 1 is faster near center, above 1 is slower near center"
        )
        self.mouse_response_curve_row.connect("notify::value", self._on_mouse_curve_changed)
        group.add(self.mouse_response_curve_row)

        self.mouse_curve_row = Adw.ActionRow(title="Response Preview")
        self.mouse_curve_graph = AnalogCurveGraph()
        self.mouse_curve_row.set_child(self.mouse_curve_graph)
        group.add(self.mouse_curve_row)

        self.mouse_direction_row = Adw.ActionRow(title="Direction")
        direction_buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        direction_buttons.set_valign(Gtk.Align.CENTER)
        self._mouse_direction_buttons: dict[str, Gtk.ToggleButton] = {}
        direction_group: Gtk.ToggleButton | None = None
        for row_items in (
            (("left", "Left"), ("right", "Right"), ("horizontal", "Left/Right")),
            (("up", "Up"), ("down", "Down"), ("vertical", "Up/Down")),
        ):
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            row_box.add_css_class("linked")
            row_box.set_homogeneous(True)
            for direction, label in row_items:
                button = Gtk.ToggleButton(label=label)
                if direction_group is None:
                    direction_group = button
                else:
                    button.set_group(direction_group)
                if direction == "right":
                    button.set_active(True)
                button.connect("toggled", self._on_modified)
                self._mouse_direction_buttons[direction] = button
                row_box.append(button)
            direction_buttons.append(row_box)
        self.mouse_direction_row.add_suffix(direction_buttons)
        group.add(self.mouse_direction_row)

        self.invert_axes_row = Adw.ActionRow(title="Invert Axes")
        invert_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        invert_buttons.add_css_class("linked")
        invert_buttons.set_valign(Gtk.Align.CENTER)
        self.invert_x_btn = Gtk.ToggleButton(label="X")
        self.invert_x_btn.connect("toggled", self._on_invert_axis_toggled, "x")
        invert_buttons.append(self.invert_x_btn)
        self.invert_y_btn = Gtk.ToggleButton(label="Y")
        self.invert_y_btn.connect("toggled", self._on_invert_axis_toggled, "y")
        invert_buttons.append(self.invert_y_btn)
        self.invert_axes_row.add_suffix(invert_buttons)
        group.add(self.invert_axes_row)

        self.area_start_enabled_row = Adw.SwitchRow(title="Anchor to a Start Position")
        self.area_start_enabled_row.connect(
            "notify::active",
            self._on_area_start_enabled_changed,
        )
        group.add(self.area_start_enabled_row)
        self.area_start_position_row = Adw.ActionRow(title="Start Position")
        start_position_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        start_position_box.set_valign(Gtk.Align.CENTER)
        start_position_box.append(Gtk.Label(label="X"))
        self.area_start_x_entry = self._compact_int_entry(0)
        start_position_box.append(self.area_start_x_entry)
        start_position_box.append(Gtk.Label(label="Y"))
        self.area_start_y_entry = self._compact_int_entry(0)
        start_position_box.append(self.area_start_y_entry)
        self.area_start_position_row.add_suffix(start_position_box)
        group.add(self.area_start_position_row)
        self.area_start_capture_row = Adw.ActionRow()
        capture_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        capture_box.set_halign(Gtk.Align.END)
        capture_box.set_valign(Gtk.Align.CENTER)
        if not self._slurp_available:
            capture_box.append(Gtk.Label(label="in"))
        self.area_start_capture_delay_spin = Gtk.SpinButton()
        self.area_start_capture_delay_spin.set_adjustment(
            Gtk.Adjustment(
                value=self._capture_delay_seconds,
                lower=0.2,
                upper=15.0,
                step_increment=0.2,
            )
        )
        self.area_start_capture_delay_spin.set_digits(1)
        self.area_start_capture_delay_spin.set_width_chars(4)
        self.area_start_capture_delay_spin.set_visible(not self._slurp_available)
        if not self._slurp_available:
            capture_box.append(self.area_start_capture_delay_spin)
        if not self._slurp_available:
            capture_box.append(Gtk.Label(label="s"))
        self.area_start_capture_status = Gtk.Label(label="")
        self.area_start_capture_status.set_xalign(1.0)
        self.area_start_capture_status.set_halign(Gtk.Align.END)
        self.area_start_capture_status.add_css_class("dim-label")
        self.area_start_capture_status.add_css_class("capture-status-label")
        capture_box.append(self.area_start_capture_status)
        self.area_start_capture_btn = Gtk.Button(
            label="Capture" if self._slurp_available else "Capture Position"
        )
        self.area_start_capture_btn.connect("clicked", self._on_area_capture_position_clicked)
        capture_box.append(self.area_start_capture_btn)
        self.area_start_capture_row.add_suffix(capture_box)
        group.add(self.area_start_capture_row)
        self._update_mouse_curve_graph()
        return group

    def _build_gamepad_output_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Analog Output Settings",
            description="Route the stick or axis to a gamepad output device.",
        )

        self.gamepad_output_target_row = Adw.ActionRow(title="Output")
        dropdown = Gtk.DropDown()
        if dropdown is None:
            raise RuntimeError("failed to create gamepad output dropdown")
        dropdown.set_valign(Gtk.Align.CENTER)
        dropdown.connect(
            "notify::selected",
            self._on_gamepad_output_selected,
        )
        self._gamepad_output_dropdown = dropdown
        self.gamepad_output_target_row.add_suffix(dropdown)
        self.gamepad_output_target_row.set_activatable_widget(dropdown)
        group.add(self.gamepad_output_target_row)

        self.gamepad_output_target_side_row = Adw.ActionRow(title="Output Control")
        target_buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        target_buttons.set_halign(Gtk.Align.END)
        target_buttons.set_valign(Gtk.Align.CENTER)
        self._gamepad_output_target_box = target_buttons
        self.gamepad_output_target_side_row.add_suffix(target_buttons)
        group.add(self.gamepad_output_target_side_row)

        self.gamepad_output_deadzone_row = self._spin_row(
            "Output Deadzone",
            0,
            0,
            95,
            1,
            0,
            page_step=5,
        )
        self._add_spin_secondary_step_controller(
            self.gamepad_output_deadzone_row,
            page_step=5,
        )
        self.gamepad_output_deadzone_row.set_subtitle(
            "Percent below which output is sent as centered or released"
        )
        self.gamepad_output_deadzone_row.connect(
            "notify::value",
            self._on_gamepad_output_curve_changed,
        )
        group.add(self.gamepad_output_deadzone_row)

        self.gamepad_output_rest_row = self._spin_row(
            "Output Rest",
            0,
            -2147483648,
            2147483647,
            1,
            0,
        )
        self._add_spin_secondary_step_controller(
            self.gamepad_output_rest_row,
            reset_value=0,
        )
        self.gamepad_output_rest_row.set_subtitle(
            "Raw value written when the output axis is released"
        )
        group.add(self.gamepad_output_rest_row)

        self.gamepad_output_direction_row = Adw.ActionRow(title="Output Direction")
        direction_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        direction_buttons.add_css_class("linked")
        direction_buttons.set_valign(Gtk.Align.CENTER)
        self.gamepad_output_direction_min_btn = Gtk.ToggleButton(label="Min")
        self.gamepad_output_direction_max_btn = Gtk.ToggleButton(label="Max")
        self.gamepad_output_direction_both_btn = Gtk.ToggleButton(label="Both")
        self.gamepad_output_direction_max_btn.set_group(
            self.gamepad_output_direction_min_btn
        )
        self.gamepad_output_direction_both_btn.set_group(
            self.gamepad_output_direction_min_btn
        )
        self.gamepad_output_direction_max_btn.set_active(True)
        self.gamepad_output_direction_min_btn.connect("toggled", self._on_modified)
        self.gamepad_output_direction_max_btn.connect("toggled", self._on_modified)
        self.gamepad_output_direction_both_btn.connect("toggled", self._on_modified)
        direction_buttons.append(self.gamepad_output_direction_min_btn)
        direction_buttons.append(self.gamepad_output_direction_max_btn)
        direction_buttons.append(self.gamepad_output_direction_both_btn)
        self.gamepad_output_direction_row.add_suffix(direction_buttons)
        group.add(self.gamepad_output_direction_row)

        self.gamepad_output_sensitivity_row = self._spin_row(
            "Sensitivity",
            1.0,
            0.1,
            2.0,
            0.05,
            2,
            page_step=0.25,
        )
        self._add_spin_secondary_step_controller(
            self.gamepad_output_sensitivity_row,
            page_step=0.25,
        )
        self.gamepad_output_sensitivity_row.set_subtitle(
            "How quickly stick output reaches full range"
        )
        self.gamepad_output_sensitivity_row.connect(
            "notify::value",
            self._on_gamepad_output_curve_changed,
        )
        group.add(self.gamepad_output_sensitivity_row)

        self.gamepad_output_response_curve_row = self._spin_row(
            "Response Curve",
            1.0,
            0.25,
            4.0,
            0.05,
            2,
            page_step=0.25,
        )
        self._add_spin_secondary_step_controller(
            self.gamepad_output_response_curve_row,
            page_step=0.25,
        )
        self.gamepad_output_response_curve_row.set_subtitle(
            "Below 1 is faster near center, above 1 is slower near center"
        )
        self.gamepad_output_response_curve_row.connect(
            "notify::value",
            self._on_gamepad_output_curve_changed,
        )
        group.add(self.gamepad_output_response_curve_row)

        self.gamepad_output_curve_row = Adw.ActionRow(title="Response Preview")
        self.gamepad_output_curve_graph = AnalogCurveGraph()
        self.gamepad_output_curve_row.set_child(self.gamepad_output_curve_graph)
        group.add(self.gamepad_output_curve_row)

        warning_row = Adw.ActionRow()
        warning = Gtk.Label(xalign=0, wrap=True)
        warning.add_css_class("warning")
        warning.add_css_class("caption")
        warning_row.set_child(warning)
        warning_row.set_visible(False)
        self._gamepad_output_warning_label = warning
        self._gamepad_output_warning_row = warning_row
        group.add(warning_row)

        self._update_gamepad_output_curve_graph()
        self._refresh_gamepad_output_choices()
        self._set_gamepad_output_target_options("stick", "same")
        self._update_gamepad_output_visibility()
        return group

    def _build_digital_group(self) -> Adw.PreferencesGroup:
        self.digital_group = Adw.PreferencesGroup(title="Digital Action Ranges")
        self.add_range_row = Adw.ActionRow(
            title="+ Add Range",
            subtitle="Create a new editable activation and release range",
        )
        self.add_range_row.set_activatable(True)
        self.add_range_row.connect("activated", self._on_add_range_clicked)
        self.digital_group.add(self.add_range_row)
        return self.digital_group

    def _build_template_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Range Templates",
            description=(
                "Templates append editable digital ranges. "
                "They do not create special runtime modes."
            ),
        )
        for title, subtitle, callback in (
            (
                "Apply WASD Template",
                "Adds four keyboard ranges for left-stick movement",
                self._on_template_wasd,
            ),
            ("Apply Arrow Keys Template", "Adds four arrow-key ranges", self._on_template_arrows),
            (
                "Apply Mouse Wheel Template",
                "Adds two rapidfire wheel ranges",
                self._on_template_mouse_wheel,
            ),
        ):
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            row.set_activatable(True)
            row.connect("activated", callback)
            group.add(row)
        return group

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
        row = Adw.SpinRow(
            title=title,
            adjustment=Gtk.Adjustment(
                value=value,
                lower=lower,
                upper=upper,
                step_increment=step,
                page_increment=page_step if page_step is not None else step,
            ),
            digits=digits,
        )
        row.connect("notify::value", self._on_modified)
        return row

    def _compact_int_entry(self, value: int) -> Gtk.Entry:
        entry = Gtk.Entry()
        entry.set_text(str(int(value)))
        entry.set_width_chars(6)
        entry.set_max_width_chars(6)
        entry.set_alignment(0.5)
        entry.set_input_purpose(Gtk.InputPurpose.NUMBER)
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_int_entry_key_pressed, entry)
        entry.add_controller(key_controller)
        entry.connect("changed", self._on_int_entry_changed)
        return entry

    def _on_int_entry_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
        entry: Gtk.Entry,
    ) -> bool:
        if state & (
            Gdk.ModifierType.CONTROL_MASK
            | Gdk.ModifierType.ALT_MASK
            | Gdk.ModifierType.META_MASK
        ):
            return False
        codepoint = Gdk.keyval_to_unicode(keyval)
        if codepoint == 0:
            return False
        char = chr(codepoint)
        if char.isdigit():
            return False
        if char == "-":
            return entry.get_position() != 0 or entry.get_text().startswith("-")
        return char.isprintable()

    def _on_int_entry_changed(self, entry: Gtk.Entry) -> None:
        if self._syncing_start_entry:
            return
        text = entry.get_text()
        sanitized = self._sanitize_int_entry_text(text)
        if sanitized != text:
            GLib.idle_add(self._apply_sanitized_int_entry_text, entry)
        self._on_modified()

    def _apply_sanitized_int_entry_text(self, entry: Gtk.Entry) -> bool:
        sanitized = self._sanitize_int_entry_text(entry.get_text())
        if sanitized != entry.get_text():
            self._syncing_start_entry = True
            try:
                entry.set_text(sanitized)
                entry.set_position(len(sanitized))
            finally:
                self._syncing_start_entry = False
        return False

    def _sanitize_int_entry_text(self, text: str) -> str:
        stripped = str(text)
        prefix = "-" if stripped.startswith("-") else ""
        digits = "".join(ch for ch in stripped[len(prefix) :] if ch.isdigit())
        return f"{prefix}{digits}"

    def _entry_int_value(self, entry: Gtk.Entry) -> int:
        try:
            return int(entry.get_text().strip())
        except ValueError:
            return 0

    def _set_entry_int(self, entry: Gtk.Entry, value: int) -> None:
        entry.set_text(str(int(value)))

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
        selected = items.index(mode) if mode in items else 0
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
            choices = [("same", None, "Same Axis" if input_type == "axis" else "Same Stick")]
        selected_key = self._gamepad_output_target_key(target, selected_analog_id)
        if selected_key not in {
            self._gamepad_output_target_key(item_target, analog_id)
            for item_target, analog_id, _label in choices
        }:
            selected_key = self._gamepad_output_target_key(choices[0][0], choices[0][1])

        target_box = self._gamepad_output_target_box
        if target_box is None:
            return
        while child := target_box.get_first_child():
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
            same_label = "Same Axis" if input_type == "axis" else "Same Stick"
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

        labels = _gamepad_output_target_labels_for_input_type(input_type)
        return [
            (item, None, label)
            for item, label in zip(_GAMEPAD_OUTPUT_TARGET_ITEMS, labels, strict=True)
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
        click = Gtk.GestureClick()
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self._on_split_mouse_speed_click, axis)
        click.connect("released", self._on_split_mouse_speed_click, axis)
        row.add_controller(click)

    def _on_split_mouse_speed_click(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        _x: float,
        _y: float,
        axis: str,
    ) -> None:
        try:
            state = gesture.get_current_event_state()
        except Exception:
            state = Gdk.ModifierType(0)
        if state & _SPLIT_SPEED_DESYNC_MODIFIERS:
            self._request_split_mouse_speed_desync(axis)

    def _request_split_mouse_speed_desync(self, axis: str) -> None:
        self._split_mouse_speed_desync_axis = axis
        if self._split_mouse_speed_desync_clear_id:
            GLib.source_remove(self._split_mouse_speed_desync_clear_id)
        self._split_mouse_speed_desync_clear_id = GLib.idle_add(
            self._clear_split_mouse_speed_desync,
            axis,
        )

    def _clear_split_mouse_speed_desync(self, axis: str | None = None) -> bool:
        if axis is None or self._split_mouse_speed_desync_axis == axis:
            self._split_mouse_speed_desync_axis = None
        self._split_mouse_speed_desync_clear_id = 0
        return False

    def _split_mouse_speed_desync_requested(self, axis: str) -> bool:
        return (
            self._split_mouse_speed_modifier_active
            or self._split_mouse_speed_desync_axis == axis
        )

    def _add_spin_secondary_step_controller(
        self,
        row: Adw.SpinRow,
        page_step: float | None = None,
        reset_value: float | None = None,
        split_speed_axis: str | None = None,
    ) -> None:
        click = Gtk.GestureClick()
        click.set_button(3)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect(
            "pressed",
            self._on_spin_secondary_step_pressed,
            row,
            page_step,
            reset_value,
            split_speed_axis,
        )
        row.add_controller(click)

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
        except Exception:
            state = Gdk.ModifierType(0)
        if split_speed_axis and state & _SPLIT_SPEED_DESYNC_MODIFIERS:
            self._request_split_mouse_speed_desync(split_speed_axis)
        self._apply_spin_secondary_step(row, direction, page_step, reset_value)

    def _spin_secondary_step_direction(self, row: Adw.SpinRow, x: float) -> int | None:
        width = row.get_width()
        if width <= 0:
            return None
        button_area_width = min(96.0, max(64.0, width * 0.35))
        button_area_start = width - button_area_width
        if x < button_area_start:
            return None
        return 1 if x >= width - (button_area_width / 2.0) else -1

    def _apply_spin_secondary_step(
        self,
        row: Adw.SpinRow,
        direction: int,
        page_step: float | None,
        reset_value: float | None = None,
    ) -> None:
        if reset_value is not None:
            row.set_value(reset_value)
            return
        if page_step is None:
            return
        adjustment = row.get_adjustment()
        next_value = row.get_value() + (page_step if direction > 0 else -page_step)
        row.set_value(
            min(
                adjustment.get_upper(),
                max(adjustment.get_lower(), next_value),
            )
        )

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
            if self._split_mouse_speed_desync_clear_id:
                GLib.source_remove(self._split_mouse_speed_desync_clear_id)
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
            if self._split_mouse_speed_desync_clear_id:
                GLib.source_remove(self._split_mouse_speed_desync_clear_id)
            self._clear_split_mouse_speed_desync(axis)
        self._remember_area_radii()

    def _on_invert_axis_toggled(self, button: Gtk.ToggleButton, axis: str) -> None:
        if self._syncing_invert_axes:
            return
        other = self.invert_y_btn if axis == "x" else self.invert_x_btn
        if not self._split_mouse_speed_desync_requested(axis):
            self._syncing_invert_axes = True
            try:
                other.set_active(button.get_active())
            finally:
                self._syncing_invert_axes = False
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
        if selected < 0 or selected >= len(_INPUT_TYPE_ITEMS):
            return "stick"
        return _INPUT_TYPE_ITEMS[selected]

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
        self.invert_axes_row.set_visible(mouse_visible and not is_axis)
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
            if _gamepad_output_choice_matches(output_id, selected_id):
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
        count = _virtual_gamepad_count()
        try:
            hardware_configs = list(HardwareManager().list_hardware())
        except Exception:
            hardware_configs = []
        self._hardware_output_configs = {
            str(getattr(config, "hardware_id", "") or ""): config
            for config in hardware_configs
        }
        helper_selected_id = (
            None
            if self._selected_gamepad_output_id == SAME_DEVICE_OUTPUT_ID
            else self._selected_gamepad_output_id
        )
        return [(SAME_DEVICE_OUTPUT_ID, "Default (same device)")] + _gamepad_output_choices_for(
            helper_selected_id,
            count,
            hardware_configs,
        )

    def _on_gamepad_output_selected(self, dropdown: Gtk.DropDown, _param) -> None:
        if self._refreshing_gamepad_output_choices:
            return
        current_target = self._current_gamepad_output_target()
        current_analog_id = self._current_gamepad_output_target_analog_id()
        selected = int(dropdown.get_selected())
        if 0 <= selected < len(self._gamepad_output_ids):
            self._selected_gamepad_output_id = self._gamepad_output_ids[selected]
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
        message = _gamepad_output_unavailable_message(
            self._selected_gamepad_output_id,
            _virtual_gamepad_count(),
        )
        label.set_label(message or "")
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
            return _INPUT_TYPE_ITEMS.index("axis")
        return _INPUT_TYPE_ITEMS.index("stick")

    def _refresh_thresholds(self, expanded_indices: set[int] | None = None) -> None:
        for row in getattr(self, "_threshold_rows", []):
            self.digital_group.remove(row)
        self._threshold_rows = []

        for index, threshold in enumerate(self._thresholds):
            row = self._build_threshold_row(index, threshold)
            if expanded_indices and index in expanded_indices:
                row.set_expanded(True)
            self.digital_group.add(row)
            self._threshold_rows.append(row)

    def _expanded_threshold_indices(self) -> set[int]:
        expanded: set[int] = set()
        for index, row in enumerate(getattr(self, "_threshold_rows", [])):
            if row.get_expanded():
                expanded.add(index)
        return expanded

    def _build_threshold_row(
        self,
        index: int,
        threshold: AnalogActionThreshold,
    ) -> Adw.ExpanderRow:
        is_axis = self._is_axis_control()
        row = Adw.ExpanderRow()
        title = f"Range {index + 1}"
        if not is_axis:
            title = f"{title}: {threshold.axis.upper()}"
        row.set_title(title)
        row.set_subtitle(self._threshold_subtitle(threshold))
        row.set_enable_expansion(True)

        edit_btn = Gtk.Button(label="Edit Actions")
        edit_btn.add_css_class("flat")
        edit_btn.connect("clicked", self._on_edit_threshold_actions_clicked, index)
        row.add_suffix(edit_btn)

        remove_btn = Gtk.Button(icon_name="edit-delete-symbolic")
        remove_btn.add_css_class("flat")
        remove_btn.add_css_class("destructive-action")
        remove_btn.connect("clicked", self._on_remove_threshold_clicked, index)
        row.add_suffix(remove_btn)

        bar_row = Adw.ActionRow()
        range_bar = ThresholdRangeBar()
        range_bar.set_ranges(
            threshold.trigger_min,
            threshold.trigger_max,
            threshold.release_min,
            threshold.release_max,
        )
        bar_row.set_child(range_bar)
        row._range_bar = range_bar
        row.add_row(bar_row)

        if not is_axis:
            axis_row = Adw.ActionRow(title="Axis")
            axis_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            axis_buttons.add_css_class("linked")
            axis_group: Gtk.ToggleButton | None = None
            for axis in _AXIS_ITEMS:
                button = Gtk.ToggleButton(label=axis.upper())
                if axis_group is None:
                    axis_group = button
                else:
                    button.set_group(axis_group)
                button.set_active(threshold.axis == axis)
                button.connect("toggled", self._on_threshold_axis_toggled, index, axis, row)
                axis_buttons.append(button)
            axis_row.add_suffix(axis_buttons)
            row.add_row(axis_row)

        trigger_min_spin = self._percent_spin_row(
            "Activation Min",
            threshold.trigger_min,
            self._on_primary_threshold_changed,
            index,
            row,
        )
        trigger_max_spin = self._percent_spin_row(
            "Activation Max",
            threshold.trigger_max,
            self._on_primary_threshold_changed,
            index,
            row,
        )
        hysteresis_spin = self._percent_spin_row(
            "Hysteresis",
            _compute_hysteresis(threshold),
            self._on_primary_threshold_changed,
            index,
            row,
            lower=0.0,
            upper=100.0,
            step=1.0,
        )
        row._spin_trigger_min = trigger_min_spin
        row._spin_trigger_max = trigger_max_spin
        row._spin_hysteresis = hysteresis_spin
        row.add_row(trigger_min_spin)
        row.add_row(trigger_max_spin)
        row.add_row(hysteresis_spin)

        advanced_row = Adw.ExpanderRow(title="Advanced")
        advanced_row.set_subtitle("Release range")
        release_min_spin = self._percent_spin_row(
            "Release Min",
            threshold.release_min,
            self._on_advanced_threshold_changed,
            index,
            row,
        )
        release_max_spin = self._percent_spin_row(
            "Release Max",
            threshold.release_max,
            self._on_advanced_threshold_changed,
            index,
            row,
        )
        row._spin_release_min = release_min_spin
        row._spin_release_max = release_max_spin
        advanced_row.add_row(release_min_spin)
        advanced_row.add_row(release_max_spin)
        row.add_row(advanced_row)

        if threshold.actions:
            for action_index, action in enumerate(threshold.actions, start=1):
                child = Adw.ActionRow()
                child.set_use_markup(False)
                child.set_title(f"{action_index}. {describe_mapping_action_verbose(action)}")
                row.add_row(child)
        return row

    def _percent_spin_row(
        self,
        title: str,
        value: float,
        callback: Callable[[Adw.SpinRow, object, int, Adw.ExpanderRow], None],
        index: int,
        row: Adw.ExpanderRow,
        *,
        lower: float = -100.0,
        upper: float = 100.0,
        step: float = 5.0,
    ) -> Adw.SpinRow:
        spin = Adw.SpinRow(
            title=f"{title} (%)",
            adjustment=Gtk.Adjustment(
                value=_to_percent(value),
                lower=lower,
                upper=upper,
                step_increment=step,
            ),
            digits=0,
        )
        spin.connect("notify::value", callback, index, row)
        return spin

    def _threshold_subtitle(self, threshold: AnalogActionThreshold) -> str:
        count = len(threshold.actions)
        noun = "action" if count == 1 else "actions"
        hysteresis = _compute_hysteresis(threshold)
        return (
            f"activate {_to_percent(threshold.trigger_min):.0f}%.."
            f"{_to_percent(threshold.trigger_max):.0f}% · "
            f"hysteresis {_to_percent(hysteresis):.0f}% · "
            f"{count} {noun}"
        )

    def _threshold_domain(self) -> tuple[float, float]:
        return (-1.0, 1.0)

    def _sync_thresholds_for_input_type(self) -> None:
        minimum, maximum = self._threshold_domain()
        if self._is_axis_control():
            for threshold in self._thresholds:
                threshold.axis = "x"
                threshold.trigger_min = max(minimum, min(maximum, threshold.trigger_min))
                threshold.trigger_max = max(minimum, min(maximum, threshold.trigger_max))
                threshold.release_min = max(minimum, min(maximum, threshold.release_min))
                threshold.release_max = max(minimum, min(maximum, threshold.release_max))
                if threshold.trigger_min > threshold.trigger_max:
                    threshold.trigger_min, threshold.trigger_max = (
                        threshold.trigger_max,
                        threshold.trigger_min,
                    )
                threshold.release_min = min(threshold.release_min, threshold.trigger_min)
                threshold.release_max = max(threshold.release_max, threshold.trigger_max)

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
        if (
            self.new_control_row is not None
            and self.list_box.get_selected_row() is not self.new_control_row
        ):
            self.list_box.select_row(self.new_control_row)
        else:
            self._begin_new_control()

    def _on_add_range_clicked(self, *_args) -> None:
        trigger_min = 0.50 if self._is_axis_control() else 0.65
        release_min = 0.45 if self._is_axis_control() else 0.55
        self._thresholds.append(
            AnalogActionThreshold(
                axis="x",
                trigger_min=trigger_min,
                trigger_max=1.0,
                release_min=release_min,
                release_max=1.0,
                actions=[],
            )
        )
        if self._current_mode() == "mouse":
            self.mode_dropdown.set_selected(self._mode_items.index("digital"))
        self._refresh_thresholds()
        self._on_modified()

    def _on_remove_threshold_clicked(self, _button, index: int) -> None:
        if 0 <= index < len(self._thresholds):
            self._thresholds.pop(index)
        self._refresh_thresholds()
        self._on_modified()

    def _on_edit_threshold_actions_clicked(self, _button, index: int) -> None:
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

    def _on_threshold_actions_selected(
        self,
        _dialog,
        actions: list[object],
        index: int,
    ) -> None:
        if not 0 <= index < len(self._thresholds):
            return
        expanded = self._expanded_threshold_indices()
        expanded.add(index)
        self._thresholds[index].actions = [
            action for action in actions if isinstance(action, MappingAction)
        ]
        self._refresh_thresholds(expanded)
        self._on_modified()

    def _on_threshold_axis_toggled(
        self,
        button: Gtk.ToggleButton,
        index: int,
        axis: str,
        row: Adw.ExpanderRow,
    ) -> None:
        if not button.get_active():
            return
        if not 0 <= index < len(self._thresholds):
            return
        self._thresholds[index].axis = axis
        row.set_title(f"Range {index + 1}: {axis.upper()}")
        row.set_subtitle(self._threshold_subtitle(self._thresholds[index]))
        self._on_modified()

    def _on_primary_threshold_changed(
        self,
        _spin: Adw.SpinRow,
        _param: object,
        index: int,
        row: Adw.ExpanderRow,
    ) -> None:
        if self._syncing_threshold:
            return
        if not 0 <= index < len(self._thresholds):
            return
        threshold = self._thresholds[index]
        trigger_min = _from_percent(row._spin_trigger_min.get_value())
        trigger_max = _from_percent(row._spin_trigger_max.get_value())
        if trigger_min > trigger_max:
            trigger_min, trigger_max = trigger_max, trigger_min
            self._set_spin_value(row._spin_trigger_min, trigger_min)
            self._set_spin_value(row._spin_trigger_max, trigger_max)

        hysteresis = max(0.0, min(1.0, _from_percent(row._spin_hysteresis.get_value())))
        minimum, maximum = self._threshold_domain()
        release_min = max(minimum, trigger_min - hysteresis)
        release_max = min(maximum, trigger_max + hysteresis)
        threshold.trigger_min = trigger_min
        threshold.trigger_max = trigger_max
        threshold.release_min = release_min
        threshold.release_max = release_max

        self._syncing_threshold = True
        try:
            row._spin_release_min.set_value(_to_percent(release_min))
            row._spin_release_max.set_value(_to_percent(release_max))
        finally:
            self._syncing_threshold = False
        self._update_threshold_row_visuals(row, index)
        self._on_modified()

    def _on_advanced_threshold_changed(
        self,
        _spin: Adw.SpinRow,
        _param: object,
        index: int,
        row: Adw.ExpanderRow,
    ) -> None:
        if self._syncing_threshold:
            return
        if not 0 <= index < len(self._thresholds):
            return
        threshold = self._thresholds[index]
        release_min = min(
            threshold.trigger_min,
            _from_percent(row._spin_release_min.get_value()),
        )
        release_max = max(
            threshold.trigger_max,
            _from_percent(row._spin_release_max.get_value()),
        )
        threshold.release_min = release_min
        threshold.release_max = release_max
        hysteresis = _compute_hysteresis(threshold)

        self._syncing_threshold = True
        try:
            row._spin_release_min.set_value(_to_percent(release_min))
            row._spin_release_max.set_value(_to_percent(release_max))
            row._spin_hysteresis.set_value(_to_percent(hysteresis))
        finally:
            self._syncing_threshold = False
        self._update_threshold_row_visuals(row, index)
        self._on_modified()

    def _set_spin_value(self, spin: Adw.SpinRow, value: float) -> None:
        self._syncing_threshold = True
        try:
            spin.set_value(_to_percent(value))
        finally:
            self._syncing_threshold = False

    def _update_threshold_row_visuals(self, row: Adw.ExpanderRow, index: int) -> None:
        if not 0 <= index < len(self._thresholds):
            return
        threshold = self._thresholds[index]
        minimum, maximum = self._threshold_domain()
        row._range_bar.set_domain(minimum, maximum)
        row._range_bar.set_ranges(
            threshold.trigger_min,
            threshold.trigger_max,
            threshold.release_min,
            threshold.release_max,
        )
        row.set_subtitle(self._threshold_subtitle(threshold))

    def _on_template_wasd(self, *_args) -> None:
        self._apply_template(analog_control_wasd_template())

    def _on_template_arrows(self, *_args) -> None:
        self._apply_template(analog_control_arrow_template())

    def _on_template_mouse_wheel(self, *_args) -> None:
        self._apply_template(analog_control_mouse_wheel_template())

    def _apply_template(self, thresholds: list[AnalogActionThreshold]) -> None:
        if self._is_axis_control():
            return
        self._thresholds.extend(self._copy_threshold(threshold) for threshold in thresholds)
        if self._current_mode() == "mouse":
            self.mode_dropdown.set_selected(self._mode_items.index("digital"))
        self._refresh_thresholds()
        self._on_modified()

    def _on_input_type_changed(self, _dropdown, _param) -> None:
        if not hasattr(self, "digital_group"):
            return
        mode = self._current_mode()
        input_type = self._current_input_type()
        if input_type == "axis" and mode not in _AXIS_MODE_ITEMS:
            mode = "digital"
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
        dialog.set_body("Save your changes before switching, or discard them?")
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
        self._selection_warning_dialog = None
        self._pending_selection_key = None
        if response == "discard":
            self._modified = False
            self._update_buttons()
            self._select_selection_key(pending_key)
            return
        if response == "save" and self._save_current_control():
            self._select_selection_key(pending_key)

    def _on_analog_controls_docs_clicked(self, _button: Gtk.Button) -> None:
        url = _analog_controls_docs_url()
        try:
            launcher = Gtk.UriLauncher.new(url)
            launcher.launch(None, None, None)
        except Exception as exc:
            log.warning("Could not open Analog Controls documentation %s: %s", url, exc)

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

    def _begin_position_capture(
        self,
        button: Gtk.Button | None,
        status_label: Gtk.Label | None,
        apply_position: Callable[[int, int], None],
    ) -> None:
        self._cancel_capture_position("")
        self._capture_request_id += 1
        request_id = self._capture_request_id
        self._capture_button = button
        self._capture_status_label = status_label
        self._capture_apply = apply_position

        if self._slurp_available:
            self._capture_pending = True
            if button is not None:
                button.set_sensitive(False)
            self._set_capture_status(status_label, "Click to capture position...")
            self._slurp_capture.capture_point(
                lambda result, expected_id=request_id: self._on_slurp_capture_result(
                    expected_id,
                    result,
                )
            )
            return

        self._capture_delay_seconds = float(self.area_start_capture_delay_spin.get_value())
        self._capture_pending = True
        if button is not None:
            button.set_sensitive(False)
        self._set_capture_status(
            status_label,
            f"Move cursor now... capturing in {self._capture_delay_seconds:.1f}s",
        )
        self._capture_timeout_id = GLib.timeout_add(
            int(self._capture_delay_seconds * 1000),
            lambda expected_id=request_id: self._capture_position_after_delay(expected_id),
        )

    def _on_slurp_capture_result(self, request_id: int, result) -> None:
        if request_id != self._capture_request_id:
            return
        self._capture_pending = False
        if self._capture_button is not None:
            self._capture_button.set_sensitive(True)

        if result is None:
            self._set_capture_status(
                self._capture_status_label,
                "Capture cancelled or failed",
                error=True,
            )
            return

        if self._capture_apply is not None:
            self._capture_apply(int(result.x), int(result.y))
        self._set_capture_status(self._capture_status_label, "")

    def _capture_position_after_delay(self, request_id: int) -> bool:
        self._capture_timeout_id = 0
        if request_id != self._capture_request_id or not self._capture_pending:
            return False
        self._set_capture_status(self._capture_status_label, "Reading cursor position...")
        session_request_async(
            {"command": "get_cursor_position"},
            lambda response, expected_id=request_id: self._on_capture_position_response(
                expected_id,
                response,
            ),
            timeout=5.0,
        )
        return False

    def _on_capture_position_response(self, request_id: int, response: dict | None) -> bool:
        if request_id != self._capture_request_id:
            return False
        self._capture_pending = False
        if self._capture_button is not None:
            self._capture_button.set_sensitive(True)

        if not response or response.get("status") != "ok":
            message = (
                (response or {}).get("message") or (response or {}).get("error") or "Capture failed"
            )
            if "Unknown command: get_cursor_position" in message:
                message = "Please restart Keymasq Session, then try again"
            self._set_capture_status(self._capture_status_label, str(message), error=True)
            return False

        x = int(response.get("x", 0))
        y = int(response.get("y", 0))
        if self._capture_apply is not None:
            self._capture_apply(x, y)
        self._set_capture_status(self._capture_status_label, "")
        return False

    def _cancel_capture_position(self, status_text: str) -> None:
        self._capture_request_id += 1
        if self._capture_timeout_id:
            GLib.source_remove(self._capture_timeout_id)
            self._capture_timeout_id = 0
        self._capture_pending = False
        if self._capture_button is not None:
            self._capture_button.set_sensitive(True)
        self._set_capture_status(self._capture_status_label, status_text)
        self._capture_apply = None
        self._capture_button = None
        self._capture_status_label = None

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
                invert_y=self.invert_y_btn.get_active(),
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
                    and self.gamepad_output_direction_min_btn.get_active()
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
