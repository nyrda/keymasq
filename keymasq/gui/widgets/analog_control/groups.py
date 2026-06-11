from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import (  # pyright: ignore[reportAttributeAccessIssue]
    Adw,  # pyright: ignore[reportAttributeAccessIssue]
    Gtk,  # pyright: ignore[reportAttributeAccessIssue]
)

from keymasq.gui.widgets.analog_control.options import _MOUSE_DEADZONE_DEFAULT
from keymasq.gui.widgets.analog_curve_graph import (
    ANALOG_CURVE_DEADZONE,
    ANALOG_CURVE_RESPONSE_CURVE,
    ANALOG_CURVE_SENSITIVITY,
    AnalogCurveGraph,
)


class AnalogControlGroupHost(Protocol):
    _slurp_available: bool
    _capture_delay_seconds: float

    def _spin_row(
        self,
        title: str,
        value: float,
        lower: float,
        upper: float,
        step: float,
        digits: int,
        page_step: float | None = None,
    ) -> Adw.SpinRow: ...

    def _compact_int_entry(self, value: int) -> Gtk.Entry: ...

    def _add_spin_secondary_step_controller(
        self,
        row: Adw.SpinRow,
        page_step: float | None = None,
        reset_value: float | None = None,
        split_speed_axis: str | None = None,
    ) -> None: ...

    def _add_split_mouse_speed_desync_controller(
        self,
        row: Adw.SpinRow,
        axis: str,
    ) -> None: ...

    def _on_split_mouse_speed_changed(
        self,
        row: Adw.SpinRow,
        param: object,
        axis: str,
    ) -> None: ...

    def _on_area_radius_changed(
        self,
        row: Adw.SpinRow,
        param: object,
        axis: str,
    ) -> None: ...

    def _on_modified(self, *args) -> None: ...

    def _on_mouse_curve_changed(self, *args) -> None: ...

    def _on_gamepad_output_curve_changed(self, *args) -> None: ...

    def _on_invert_axis_toggled(self, button: Gtk.ToggleButton, axis: str) -> None: ...

    def _on_area_start_enabled_changed(self, *args) -> None: ...

    def _on_area_capture_position_clicked(self, button: Gtk.Button) -> None: ...

    def _on_gamepad_output_selected(self, dropdown: Gtk.DropDown, param: object) -> None: ...

    def _on_gamepad_output_target_toggled(
        self,
        button: Gtk.ToggleButton,
        target: str,
        analog_id: str | None,
    ) -> None: ...

    def _on_add_range_clicked(self, *args) -> None: ...

    def _on_template_wasd(self, *args) -> None: ...

    def _on_template_arrows(self, *args) -> None: ...

    def _on_template_mouse_wheel(self, *args) -> None: ...


@dataclass(slots=True)
class MouseGroupHandle:
    group: Adw.PreferencesGroup
    speed_row: Adw.SpinRow
    speed_x_row: Adw.SpinRow
    speed_y_row: Adw.SpinRow
    area_radius_x_row: Adw.SpinRow
    area_radius_y_row: Adw.SpinRow
    deadzone_row: Adw.SpinRow
    mouse_sensitivity_row: Adw.SpinRow
    mouse_response_curve_row: Adw.SpinRow
    mouse_curve_row: Adw.ActionRow
    mouse_curve_graph: AnalogCurveGraph
    mouse_direction_row: Adw.ActionRow
    mouse_direction_buttons: dict[str, Gtk.ToggleButton]
    invert_axes_row: Adw.ActionRow
    invert_x_btn: Gtk.ToggleButton
    invert_y_btn: Gtk.ToggleButton
    area_start_enabled_row: Adw.SwitchRow
    area_start_position_row: Adw.ActionRow
    area_start_x_entry: Gtk.Entry
    area_start_y_entry: Gtk.Entry
    area_start_capture_row: Adw.ActionRow
    area_start_capture_delay_spin: Gtk.SpinButton
    area_start_capture_status: Gtk.Label
    area_start_capture_btn: Gtk.Button


@dataclass(slots=True)
class GamepadOutputGroupHandle:
    group: Adw.PreferencesGroup
    gamepad_output_target_row: Adw.ActionRow
    gamepad_output_dropdown: Gtk.DropDown
    gamepad_output_target_side_row: Adw.ActionRow
    gamepad_output_target_box: Gtk.Box
    gamepad_output_deadzone_row: Adw.SpinRow
    gamepad_output_rest_row: Adw.SpinRow
    gamepad_output_direction_row: Adw.ActionRow
    gamepad_output_direction_min_btn: Gtk.ToggleButton
    gamepad_output_direction_max_btn: Gtk.ToggleButton
    gamepad_output_direction_both_btn: Gtk.ToggleButton
    gamepad_output_sensitivity_row: Adw.SpinRow
    gamepad_output_response_curve_row: Adw.SpinRow
    gamepad_output_curve_row: Adw.ActionRow
    gamepad_output_curve_graph: AnalogCurveGraph
    gamepad_output_warning_row: Adw.ActionRow
    gamepad_output_warning_label: Gtk.Label


@dataclass(slots=True)
class DigitalGroupHandle:
    group: Adw.PreferencesGroup
    add_range_row: Adw.ActionRow


@dataclass(slots=True)
class TemplateGroupHandle:
    group: Adw.PreferencesGroup


def build_mouse_group(host: AnalogControlGroupHost) -> MouseGroupHandle:
    group = Adw.PreferencesGroup(title="Mouse Movement")

    speed_row = host._spin_row("Speed", 900, 0, 5000, 25, 0, page_step=500)
    host._add_spin_secondary_step_controller(speed_row, page_step=500)
    group.add(speed_row)
    speed_x_row = host._spin_row(
        "Horizontal Speed",
        900,
        0,
        5000,
        25,
        0,
        page_step=500,
    )
    speed_x_row.connect("notify::value", host._on_split_mouse_speed_changed, "x")
    host._add_split_mouse_speed_desync_controller(speed_x_row, "x")
    host._add_spin_secondary_step_controller(
        speed_x_row,
        page_step=500,
        split_speed_axis="x",
    )
    group.add(speed_x_row)
    speed_y_row = host._spin_row(
        "Vertical Speed",
        900,
        0,
        5000,
        25,
        0,
        page_step=500,
    )
    speed_y_row.connect("notify::value", host._on_split_mouse_speed_changed, "y")
    host._add_split_mouse_speed_desync_controller(speed_y_row, "y")
    host._add_spin_secondary_step_controller(
        speed_y_row,
        page_step=500,
        split_speed_axis="y",
    )
    group.add(speed_y_row)
    area_radius_x_row = host._spin_row(
        "Horizontal Radius",
        400,
        0,
        10000,
        10,
        0,
        page_step=100,
    )
    area_radius_x_row.set_subtitle("Horizontal radius from the start point")
    area_radius_x_row.connect("notify::value", host._on_area_radius_changed, "x")
    host._add_split_mouse_speed_desync_controller(area_radius_x_row, "x")
    host._add_spin_secondary_step_controller(
        area_radius_x_row,
        page_step=100,
        split_speed_axis="x",
    )
    group.add(area_radius_x_row)
    area_radius_y_row = host._spin_row(
        "Vertical Radius",
        400,
        0,
        10000,
        10,
        0,
        page_step=100,
    )
    area_radius_y_row.set_subtitle("Vertical radius from the start point")
    area_radius_y_row.connect("notify::value", host._on_area_radius_changed, "y")
    host._add_split_mouse_speed_desync_controller(area_radius_y_row, "y")
    host._add_spin_secondary_step_controller(
        area_radius_y_row,
        page_step=100,
        split_speed_axis="y",
    )
    group.add(area_radius_y_row)
    deadzone_row = host._spin_row(
        "Deadzone",
        _MOUSE_DEADZONE_DEFAULT,
        ANALOG_CURVE_DEADZONE.lower,
        ANALOG_CURVE_DEADZONE.upper,
        ANALOG_CURVE_DEADZONE.step,
        ANALOG_CURVE_DEADZONE.digits,
        page_step=ANALOG_CURVE_DEADZONE.page_step,
    )
    host._add_spin_secondary_step_controller(
        deadzone_row,
        page_step=ANALOG_CURVE_DEADZONE.page_step,
    )
    deadzone_row.connect("notify::value", host._on_mouse_curve_changed)
    group.add(deadzone_row)

    mouse_sensitivity_row = host._spin_row(
        "Sensitivity",
        ANALOG_CURVE_SENSITIVITY.default,
        ANALOG_CURVE_SENSITIVITY.lower,
        ANALOG_CURVE_SENSITIVITY.upper,
        ANALOG_CURVE_SENSITIVITY.step,
        ANALOG_CURVE_SENSITIVITY.digits,
        page_step=ANALOG_CURVE_SENSITIVITY.page_step,
    )
    host._add_spin_secondary_step_controller(
        mouse_sensitivity_row,
        page_step=ANALOG_CURVE_SENSITIVITY.page_step,
    )
    mouse_sensitivity_row.set_subtitle("How quickly movement reaches full speed")
    mouse_sensitivity_row.connect("notify::value", host._on_mouse_curve_changed)
    group.add(mouse_sensitivity_row)

    mouse_response_curve_row = host._spin_row(
        "Response Curve",
        ANALOG_CURVE_RESPONSE_CURVE.default,
        ANALOG_CURVE_RESPONSE_CURVE.lower,
        ANALOG_CURVE_RESPONSE_CURVE.upper,
        ANALOG_CURVE_RESPONSE_CURVE.step,
        ANALOG_CURVE_RESPONSE_CURVE.digits,
        page_step=ANALOG_CURVE_RESPONSE_CURVE.page_step,
    )
    host._add_spin_secondary_step_controller(
        mouse_response_curve_row,
        page_step=ANALOG_CURVE_RESPONSE_CURVE.page_step,
    )
    mouse_response_curve_row.set_subtitle(
        "Below 1 is faster near center, above 1 is slower near center"
    )
    mouse_response_curve_row.connect("notify::value", host._on_mouse_curve_changed)
    group.add(mouse_response_curve_row)

    mouse_curve_row = Adw.ActionRow(title="Response Preview")
    mouse_curve_graph = AnalogCurveGraph()
    mouse_curve_row.set_child(mouse_curve_graph)
    group.add(mouse_curve_row)

    mouse_direction_row = Adw.ActionRow(title="Direction")
    direction_buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    direction_buttons.set_valign(Gtk.Align.CENTER)
    mouse_direction_buttons: dict[str, Gtk.ToggleButton] = {}
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
            button.connect("toggled", host._on_modified)
            mouse_direction_buttons[direction] = button
            row_box.append(button)
        direction_buttons.append(row_box)
    mouse_direction_row.add_suffix(direction_buttons)
    group.add(mouse_direction_row)

    invert_axes_row = Adw.ActionRow(title="Invert Axes")
    invert_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    invert_buttons.add_css_class("linked")
    invert_buttons.set_valign(Gtk.Align.CENTER)
    invert_x_btn = Gtk.ToggleButton(label="X")
    invert_x_btn.connect("toggled", host._on_invert_axis_toggled, "x")
    invert_buttons.append(invert_x_btn)
    invert_y_btn = Gtk.ToggleButton(label="Y")
    invert_y_btn.connect("toggled", host._on_invert_axis_toggled, "y")
    invert_buttons.append(invert_y_btn)
    invert_axes_row.add_suffix(invert_buttons)
    group.add(invert_axes_row)

    area_start_enabled_row = Adw.SwitchRow(title="Anchor to a Start Position")
    area_start_enabled_row.connect(
        "notify::active",
        host._on_area_start_enabled_changed,
    )
    group.add(area_start_enabled_row)
    area_start_position_row = Adw.ActionRow(title="Start Position")
    start_position_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    start_position_box.set_valign(Gtk.Align.CENTER)
    start_position_box.append(Gtk.Label(label="X"))
    area_start_x_entry = host._compact_int_entry(0)
    start_position_box.append(area_start_x_entry)
    start_position_box.append(Gtk.Label(label="Y"))
    area_start_y_entry = host._compact_int_entry(0)
    start_position_box.append(area_start_y_entry)
    area_start_position_row.add_suffix(start_position_box)
    group.add(area_start_position_row)
    area_start_capture_row = Adw.ActionRow()
    capture_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    capture_box.set_halign(Gtk.Align.END)
    capture_box.set_valign(Gtk.Align.CENTER)
    if not host._slurp_available:
        capture_box.append(Gtk.Label(label="in"))
    area_start_capture_delay_spin = Gtk.SpinButton()
    area_start_capture_delay_spin.set_adjustment(
        Gtk.Adjustment(
            value=host._capture_delay_seconds,
            lower=0.2,
            upper=15.0,
            step_increment=0.2,
        )
    )
    area_start_capture_delay_spin.set_digits(1)
    area_start_capture_delay_spin.set_width_chars(4)
    area_start_capture_delay_spin.set_visible(not host._slurp_available)
    if not host._slurp_available:
        capture_box.append(area_start_capture_delay_spin)
    if not host._slurp_available:
        capture_box.append(Gtk.Label(label="s"))
    area_start_capture_status = Gtk.Label(label="")
    area_start_capture_status.set_xalign(1.0)
    area_start_capture_status.set_halign(Gtk.Align.END)
    area_start_capture_status.add_css_class("dim-label")
    area_start_capture_status.add_css_class("capture-status-label")
    capture_box.append(area_start_capture_status)
    area_start_capture_btn = Gtk.Button(
        label="Capture" if host._slurp_available else "Capture Position"
    )
    area_start_capture_btn.connect("clicked", host._on_area_capture_position_clicked)
    capture_box.append(area_start_capture_btn)
    area_start_capture_row.add_suffix(capture_box)
    group.add(area_start_capture_row)
    return MouseGroupHandle(
        group=group,
        speed_row=speed_row,
        speed_x_row=speed_x_row,
        speed_y_row=speed_y_row,
        area_radius_x_row=area_radius_x_row,
        area_radius_y_row=area_radius_y_row,
        deadzone_row=deadzone_row,
        mouse_sensitivity_row=mouse_sensitivity_row,
        mouse_response_curve_row=mouse_response_curve_row,
        mouse_curve_row=mouse_curve_row,
        mouse_curve_graph=mouse_curve_graph,
        mouse_direction_row=mouse_direction_row,
        mouse_direction_buttons=mouse_direction_buttons,
        invert_axes_row=invert_axes_row,
        invert_x_btn=invert_x_btn,
        invert_y_btn=invert_y_btn,
        area_start_enabled_row=area_start_enabled_row,
        area_start_position_row=area_start_position_row,
        area_start_x_entry=area_start_x_entry,
        area_start_y_entry=area_start_y_entry,
        area_start_capture_row=area_start_capture_row,
        area_start_capture_delay_spin=area_start_capture_delay_spin,
        area_start_capture_status=area_start_capture_status,
        area_start_capture_btn=area_start_capture_btn,
    )


def build_gamepad_output_group(host: AnalogControlGroupHost) -> GamepadOutputGroupHandle:
    group = Adw.PreferencesGroup(
        title="Analog Output Settings",
        description="Route the stick or axis to a gamepad output device.",
    )

    gamepad_output_target_row = Adw.ActionRow(title="Output")
    dropdown = Gtk.DropDown()
    if dropdown is None:
        raise RuntimeError("failed to create gamepad output dropdown")
    dropdown.set_valign(Gtk.Align.CENTER)
    dropdown.connect(
        "notify::selected",
        host._on_gamepad_output_selected,
    )
    gamepad_output_target_row.add_suffix(dropdown)
    gamepad_output_target_row.set_activatable_widget(dropdown)
    group.add(gamepad_output_target_row)

    gamepad_output_target_side_row = Adw.ActionRow(title="Output Control")
    target_buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    target_buttons.set_halign(Gtk.Align.END)
    target_buttons.set_valign(Gtk.Align.CENTER)
    gamepad_output_target_side_row.add_suffix(target_buttons)
    group.add(gamepad_output_target_side_row)

    gamepad_output_deadzone_row = host._spin_row(
        "Output Deadzone",
        ANALOG_CURVE_DEADZONE.default * 100.0,
        ANALOG_CURVE_DEADZONE.lower * 100.0,
        ANALOG_CURVE_DEADZONE.upper * 100.0,
        1,
        0,
        page_step=ANALOG_CURVE_DEADZONE.page_step * 100.0,
    )
    host._add_spin_secondary_step_controller(
        gamepad_output_deadzone_row,
        page_step=ANALOG_CURVE_DEADZONE.page_step * 100.0,
    )
    gamepad_output_deadzone_row.set_subtitle(
        "Percent below which output is sent as centered or released"
    )
    gamepad_output_deadzone_row.connect(
        "notify::value",
        host._on_gamepad_output_curve_changed,
    )
    group.add(gamepad_output_deadzone_row)

    gamepad_output_rest_row = host._spin_row(
        "Output Rest",
        0,
        -2147483648,
        2147483647,
        1,
        0,
    )
    host._add_spin_secondary_step_controller(
        gamepad_output_rest_row,
        reset_value=0,
    )
    gamepad_output_rest_row.set_subtitle(
        "Raw value written when the output axis is released"
    )
    group.add(gamepad_output_rest_row)

    gamepad_output_direction_row = Adw.ActionRow(title="Output Direction")
    direction_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    direction_buttons.add_css_class("linked")
    direction_buttons.set_valign(Gtk.Align.CENTER)
    gamepad_output_direction_min_btn = Gtk.ToggleButton(label="Min")
    gamepad_output_direction_max_btn = Gtk.ToggleButton(label="Max")
    gamepad_output_direction_both_btn = Gtk.ToggleButton(label="Both")
    gamepad_output_direction_max_btn.set_group(gamepad_output_direction_min_btn)
    gamepad_output_direction_both_btn.set_group(gamepad_output_direction_min_btn)
    gamepad_output_direction_max_btn.set_active(True)
    gamepad_output_direction_min_btn.connect("toggled", host._on_modified)
    gamepad_output_direction_max_btn.connect("toggled", host._on_modified)
    gamepad_output_direction_both_btn.connect("toggled", host._on_modified)
    direction_buttons.append(gamepad_output_direction_min_btn)
    direction_buttons.append(gamepad_output_direction_max_btn)
    direction_buttons.append(gamepad_output_direction_both_btn)
    gamepad_output_direction_row.add_suffix(direction_buttons)
    group.add(gamepad_output_direction_row)

    gamepad_output_sensitivity_row = host._spin_row(
        "Sensitivity",
        ANALOG_CURVE_SENSITIVITY.default,
        ANALOG_CURVE_SENSITIVITY.lower,
        ANALOG_CURVE_SENSITIVITY.upper,
        ANALOG_CURVE_SENSITIVITY.step,
        ANALOG_CURVE_SENSITIVITY.digits,
        page_step=ANALOG_CURVE_SENSITIVITY.page_step,
    )
    host._add_spin_secondary_step_controller(
        gamepad_output_sensitivity_row,
        page_step=ANALOG_CURVE_SENSITIVITY.page_step,
    )
    gamepad_output_sensitivity_row.set_subtitle(
        "How quickly stick output reaches full range"
    )
    gamepad_output_sensitivity_row.connect(
        "notify::value",
        host._on_gamepad_output_curve_changed,
    )
    group.add(gamepad_output_sensitivity_row)

    gamepad_output_response_curve_row = host._spin_row(
        "Response Curve",
        ANALOG_CURVE_RESPONSE_CURVE.default,
        ANALOG_CURVE_RESPONSE_CURVE.lower,
        ANALOG_CURVE_RESPONSE_CURVE.upper,
        ANALOG_CURVE_RESPONSE_CURVE.step,
        ANALOG_CURVE_RESPONSE_CURVE.digits,
        page_step=ANALOG_CURVE_RESPONSE_CURVE.page_step,
    )
    host._add_spin_secondary_step_controller(
        gamepad_output_response_curve_row,
        page_step=ANALOG_CURVE_RESPONSE_CURVE.page_step,
    )
    gamepad_output_response_curve_row.set_subtitle(
        "Below 1 is faster near center, above 1 is slower near center"
    )
    gamepad_output_response_curve_row.connect(
        "notify::value",
        host._on_gamepad_output_curve_changed,
    )
    group.add(gamepad_output_response_curve_row)

    gamepad_output_curve_row = Adw.ActionRow(title="Response Preview")
    gamepad_output_curve_graph = AnalogCurveGraph()
    gamepad_output_curve_row.set_child(gamepad_output_curve_graph)
    group.add(gamepad_output_curve_row)

    warning_row = Adw.ActionRow()
    warning = Gtk.Label(xalign=0, wrap=True)
    warning.add_css_class("warning")
    warning.add_css_class("caption")
    warning_row.set_child(warning)
    warning_row.set_visible(False)
    group.add(warning_row)

    return GamepadOutputGroupHandle(
        group=group,
        gamepad_output_target_row=gamepad_output_target_row,
        gamepad_output_dropdown=dropdown,
        gamepad_output_target_side_row=gamepad_output_target_side_row,
        gamepad_output_target_box=target_buttons,
        gamepad_output_deadzone_row=gamepad_output_deadzone_row,
        gamepad_output_rest_row=gamepad_output_rest_row,
        gamepad_output_direction_row=gamepad_output_direction_row,
        gamepad_output_direction_min_btn=gamepad_output_direction_min_btn,
        gamepad_output_direction_max_btn=gamepad_output_direction_max_btn,
        gamepad_output_direction_both_btn=gamepad_output_direction_both_btn,
        gamepad_output_sensitivity_row=gamepad_output_sensitivity_row,
        gamepad_output_response_curve_row=gamepad_output_response_curve_row,
        gamepad_output_curve_row=gamepad_output_curve_row,
        gamepad_output_curve_graph=gamepad_output_curve_graph,
        gamepad_output_warning_row=warning_row,
        gamepad_output_warning_label=warning,
    )


def build_digital_group(host: AnalogControlGroupHost) -> DigitalGroupHandle:
    group = Adw.PreferencesGroup(title="Digital Action Ranges")
    add_range_row = Adw.ActionRow(
        title="+ Add Range",
        subtitle="Create a new editable activation and release range",
    )
    add_range_row.set_activatable(True)
    add_range_row.connect("activated", host._on_add_range_clicked)
    group.add(add_range_row)
    return DigitalGroupHandle(group=group, add_range_row=add_range_row)


def build_template_group(host: AnalogControlGroupHost) -> TemplateGroupHandle:
    group = Adw.PreferencesGroup(
        title="Range Templates",
        description=(
            "Templates append editable digital ranges. "
            "They do not create special runtime modes."
        ),
    )
    template_rows: tuple[tuple[str, str, Callable[..., None]], ...] = (
        (
            "Apply WASD Template",
            "Adds four keyboard ranges for left-stick movement",
            host._on_template_wasd,
        ),
        ("Apply Arrow Keys Template", "Adds four arrow-key ranges", host._on_template_arrows),
        (
            "Apply Mouse Wheel Template",
            "Adds two rapidfire wheel ranges",
            host._on_template_mouse_wheel,
        ),
    )
    for title, subtitle, callback in template_rows:
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        row.set_activatable(True)
        row.connect("activated", callback)
        group.add(row)
    return TemplateGroupHandle(group=group)
