import logging
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GObject, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.models import (
    AnalogActionThreshold,
    AnalogControlConfig,
    AnalogGamepadOutputConfig,
    AnalogMouseMotionConfig,
    MappingAction,
)
from keymasq.gui.widgets.action_labels import describe_mapping_action_verbose
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
from keymasq.session.hardware import HardwareManager
from keymasq.session.profiles import ProfileManager

log = logging.getLogger("keymasq.gui.widgets.analog_control_dialog")

_STICK_MODE_ITEMS = ("mouse", "digital", "gamepad", "both")
_STICK_MODE_LABELS = ("Mouse Movement", "Digital Actions", "Gamepad Output", "Mouse + Digital")
_TRIGGER_MODE_ITEMS = ("digital", "gamepad")
_TRIGGER_MODE_LABELS = ("Digital Actions", "Gamepad Output")
_INPUT_TYPE_ITEMS = ("stick", "trigger")
_INPUT_TYPE_LABELS = ("Stick", "Trigger")
_CONTROL_GROUPS = (("trigger", "Triggers"), ("stick", "Sticks"))
_AXIS_ITEMS = ("x", "y")


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
    return _TRIGGER_MODE_ITEMS if input_type == "trigger" else _STICK_MODE_ITEMS


def _mode_labels_for_input_type(input_type: str) -> tuple[str, ...]:
    return _TRIGGER_MODE_LABELS if input_type == "trigger" else _STICK_MODE_LABELS


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
        self._editing_new_control = False
        self._syncing_threshold = False
        self._selected_gamepad_output_id: str | None = None
        self._gamepad_output_ids: list[str | None] = []
        self._gamepad_output_dropdown: Gtk.DropDown | None = None
        self._gamepad_output_warning_label: Gtk.Label | None = None
        self._refreshing_gamepad_output_choices = False
        self._mode_items: tuple[str, ...] = _STICK_MODE_ITEMS
        self.new_control_row: Gtk.ListBoxRow | None = None

        self._build_ui()
        self._load_controls()

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

        label = Gtk.Label(label="Analog Controls")
        label.add_css_class("title-4")
        label.set_halign(Gtk.Align.START)
        box.append(label)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.list_box = Gtk.ListBox()
        self.list_box.set_vexpand(True)
        self.list_box.connect("row-selected", self._on_control_selected)
        scrolled.set_child(self.list_box)
        box.append(scrolled)
        return box

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

        self.mode_dropdown = Gtk.DropDown.new_from_strings(list(_STICK_MODE_LABELS))
        self.mode_dropdown.connect("notify::selected", self._on_mode_changed)
        self._attach_labeled(fields_grid, "Mode:", 2, self.mode_dropdown)

        self.input_type_dropdown = Gtk.DropDown.new_from_strings(list(_INPUT_TYPE_LABELS))
        self.input_type_dropdown.connect("notify::selected", self._on_input_type_changed)
        self._attach_labeled(fields_grid, "Input Type:", 3, self.input_type_dropdown)

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

        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", self._on_close_clicked)
        footer.append(close_btn)

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

        self.speed_row = self._spin_row("Speed", 900, 0, 5000, 25, 0)
        group.add(self.speed_row)
        self.deadzone_row = self._spin_row("Deadzone", 0.15, 0, 0.95, 0.01, 2)
        group.add(self.deadzone_row)

        self.curve_row = Adw.ComboRow(title="Curve")
        self.curve_model = Gtk.StringList.new(["Soft", "Linear", "Fast"])
        self.curve_row.set_model(self.curve_model)
        self.curve_row.connect("notify::selected", self._on_modified)
        group.add(self.curve_row)

        self.invert_x_row = Adw.SwitchRow(title="Invert X")
        self.invert_x_row.connect("notify::active", self._on_modified)
        self.invert_y_row = Adw.SwitchRow(title="Invert Y")
        self.invert_y_row.connect("notify::active", self._on_modified)
        group.add(self.invert_x_row)
        group.add(self.invert_y_row)
        return group

    def _build_gamepad_output_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Gamepad Output Settings",
            description="Route the stick or trigger to a gamepad output device.",
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

        self.gamepad_output_deadzone_row = self._spin_row(
            "Output Deadzone",
            15,
            0,
            95,
            1,
            0,
        )
        self.gamepad_output_deadzone_row.set_subtitle(
            "Percent below which output is sent as centered or released"
        )
        group.add(self.gamepad_output_deadzone_row)

        warning_row = Adw.ActionRow()
        warning = Gtk.Label(xalign=0, wrap=True)
        warning.add_css_class("warning")
        warning.add_css_class("caption")
        warning_row.set_child(warning)
        warning_row.set_visible(False)
        self._gamepad_output_warning_label = warning
        self._gamepad_output_warning_row = warning_row
        group.add(warning_row)

        self._refresh_gamepad_output_choices()
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
    ) -> Adw.SpinRow:
        row = Adw.SpinRow(
            title=title,
            adjustment=Gtk.Adjustment(
                value=value,
                lower=lower,
                upper=upper,
                step_increment=step,
            ),
            digits=digits,
        )
        row.connect("notify::value", self._on_modified)
        return row

    def _build_new_control_row(self) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row._is_new_analog_control = True
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
        label = Gtk.Label(label=title, xalign=0)
        label.add_css_class("caption")
        label.add_css_class("dim-label")
        label.set_margin_start(6)
        label.set_margin_end(6)
        label.set_margin_top(10)
        label.set_margin_bottom(2)
        row.set_child(label)
        return row

    def _build_saved_control_row(self, name: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row._analog_control_name = name
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

    def _current_input_type(self) -> str:
        selected = int(self.input_type_dropdown.get_selected())
        if selected < 0 or selected >= len(_INPUT_TYPE_ITEMS):
            return "stick"
        return _INPUT_TYPE_ITEMS[selected]

    def _is_trigger_control(self) -> bool:
        return self._current_input_type() == "trigger"

    def _update_mode_visibility(self) -> None:
        if not hasattr(self, "mouse_group"):
            return
        input_type = self._current_input_type()
        mode = self._current_mode()
        is_trigger = input_type == "trigger"
        digital_visible = mode in {"digital", "both"}
        self.mouse_group.set_visible(not is_trigger and mode in {"mouse", "both"})
        self.gamepad_output_group.set_visible(mode == "gamepad")
        self.digital_group.set_visible(digital_visible)
        self.template_group.set_visible(digital_visible and not is_trigger)
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
        self._update_gamepad_output_warning()

    def _gamepad_output_choices(self) -> list[tuple[str | None, str]]:
        count = _virtual_gamepad_count()
        try:
            hardware_configs = list(HardwareManager().list_hardware())
        except Exception:
            hardware_configs = []
        return _gamepad_output_choices_for(
            self._selected_gamepad_output_id,
            count,
            hardware_configs,
        )

    def _on_gamepad_output_selected(self, dropdown: Gtk.DropDown, _param) -> None:
        if self._refreshing_gamepad_output_choices:
            return
        selected = int(dropdown.get_selected())
        if 0 <= selected < len(self._gamepad_output_ids):
            self._selected_gamepad_output_id = self._gamepad_output_ids[selected]
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

    def _load_controls(self) -> None:
        while row := self.list_box.get_row_at_index(0):
            self.list_box.remove(row)

        names = self.manager.list_analog_controls()
        configs = self.manager.get_all_analog_controls()
        first_saved_row: Gtk.ListBoxRow | None = None
        for title, group_names in _group_analog_control_names(names, configs):
            self.list_box.append(self._build_control_group_row(title))
            for name in group_names:
                row = self._build_saved_control_row(name)
                self.list_box.append(row)
                if first_saved_row is None:
                    first_saved_row = row

        self.new_control_row = self._build_new_control_row()
        self.list_box.append(self.new_control_row)

        if first_saved_row is not None:
            self.list_box.select_row(first_saved_row)
        else:
            self.list_box.select_row(self.new_control_row)

    def _on_control_selected(self, _list_box, row) -> None:
        if row is None:
            self._current_config = None
            self._current_name = None
            self._editing_new_control = False
            self.editor_box.set_sensitive(False)
            self.delete_btn.set_sensitive(False)
            self._modified = False
            self._update_buttons()
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
        self._modified = False
        self._update_buttons()

    def _load_config(self, config: AnalogControlConfig) -> None:
        self._current_config = config
        self._current_name = config.name
        self._thresholds = [self._copy_threshold(threshold) for threshold in config.thresholds]
        self.editor_box.set_sensitive(True)
        self.name_entry.set_text(config.name)
        self.description_entry.set_text(config.description or "")
        self.input_type_dropdown.set_selected(self._input_type_index(config))
        self._set_mode_options(config.input_type, self._mode_value(config))
        self.speed_row.set_value(config.mouse_motion.speed)
        self.deadzone_row.set_value(config.mouse_motion.deadzone)
        self.curve_row.set_selected(
            {"soft": 0, "linear": 1, "fast": 2}.get(config.mouse_motion.curve, 0)
        )
        self.invert_x_row.set_active(config.mouse_motion.invert_x)
        self.invert_y_row.set_active(config.mouse_motion.invert_y)
        self._selected_gamepad_output_id = config.gamepad_output.output_id
        self._refresh_gamepad_output_choices()
        self.gamepad_output_deadzone_row.set_value(
            round(config.gamepad_output.deadzone * 100.0)
        )
        self._update_gamepad_output_visibility()
        self._refresh_thresholds()
        self._update_mode_visibility()

    def _mode_value(self, config: AnalogControlConfig) -> str:
        if config.gamepad_output.enabled:
            return "gamepad"
        if config.input_type == "trigger":
            return "digital"
        has_mouse = bool(config.mouse_motion.enabled)
        has_digital = bool(config.thresholds)
        if has_mouse and has_digital:
            return "both"
        if has_digital:
            return "digital"
        return "mouse"

    def _input_type_index(self, config: AnalogControlConfig) -> int:
        if config.input_type == "trigger":
            return _INPUT_TYPE_ITEMS.index("trigger")
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
        is_trigger = self._is_trigger_control()
        row = Adw.ExpanderRow()
        title = f"Range {index + 1}"
        if not is_trigger:
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
        if is_trigger:
            range_bar.set_domain(0.0, 1.0)
        range_bar.set_ranges(
            threshold.trigger_min,
            threshold.trigger_max,
            threshold.release_min,
            threshold.release_max,
        )
        bar_row.set_child(range_bar)
        row._range_bar = range_bar
        row.add_row(bar_row)

        if not is_trigger:
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

        value_lower = 0.0 if is_trigger else -100.0
        trigger_min_spin = self._percent_spin_row(
            "Activation Min",
            threshold.trigger_min,
            self._on_primary_threshold_changed,
            index,
            row,
            lower=value_lower,
        )
        trigger_max_spin = self._percent_spin_row(
            "Activation Max",
            threshold.trigger_max,
            self._on_primary_threshold_changed,
            index,
            row,
            lower=value_lower,
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
            lower=value_lower,
        )
        release_max_spin = self._percent_spin_row(
            "Release Max",
            threshold.release_max,
            self._on_advanced_threshold_changed,
            index,
            row,
            lower=value_lower,
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
        return (0.0, 1.0) if self._is_trigger_control() else (-1.0, 1.0)

    def _sync_thresholds_for_input_type(self) -> None:
        minimum, maximum = self._threshold_domain()
        if self._is_trigger_control():
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
        self._load_config(AnalogControlConfig(name="New Analog Control"))
        self._current_name = None
        self._editing_new_control = True
        self.delete_btn.set_sensitive(False)
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
        trigger_min = 0.50 if self._is_trigger_control() else 0.65
        release_min = 0.45 if self._is_trigger_control() else 0.55
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
        if not self._is_trigger_control() and self._current_mode() == "mouse":
            self.mode_dropdown.set_selected(_STICK_MODE_ITEMS.index("both"))
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
        if self._is_trigger_control():
            return
        self._thresholds.extend(self._copy_threshold(threshold) for threshold in thresholds)
        if self._current_mode() == "mouse":
            self.mode_dropdown.set_selected(_STICK_MODE_ITEMS.index("both"))
        self._refresh_thresholds()
        self._on_modified()

    def _on_input_type_changed(self, _dropdown, _param) -> None:
        if not hasattr(self, "digital_group"):
            return
        mode = self._current_mode()
        input_type = self._current_input_type()
        if input_type == "trigger" and mode not in _TRIGGER_MODE_ITEMS:
            mode = "digital"
        self._set_mode_options(input_type, mode)
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
        self.close()

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
                enabled=input_type == "stick" and mode in {"mouse", "both"},
                speed=self.speed_row.get_value(),
                deadzone=self.deadzone_row.get_value(),
                curve=["soft", "linear", "fast"][int(self.curve_row.get_selected())],
                invert_x=self.invert_x_row.get_active(),
                invert_y=self.invert_y_row.get_active(),
            ),
            gamepad_output=AnalogGamepadOutputConfig(
                enabled=mode == "gamepad",
                output_id=self._selected_gamepad_output_id,
                deadzone=self.gamepad_output_deadzone_row.get_value() / 100.0,
            ),
            thresholds=(
                list(self._thresholds)
                if mode in {"digital", "both"}
                else []
            ),
        )
        try:
            self.manager.save_analog_control(config, replacing_name=old_name)
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
        if self.profile_manager is not None:
            self.profile_manager.replace_analog_control_with_suppress(name)
        if self.manager.delete_analog_control(name):
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
