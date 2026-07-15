"""Mouse settings panel construction."""

from collections.abc import Callable
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.widgets.analog_control.options import MOUSE_DEADZONE_DEFAULT
from keymasq.gui.widgets.analog_curve_graph import (
    ANALOG_CURVE_DEADZONE,
    ANALOG_CURVE_RESPONSE_CURVE,
    ANALOG_CURVE_SENSITIVITY,
    AnalogCurveGraph,
)
from keymasq.gui.widgets.position_capture import PositionCaptureController
from keymasq.gui.widgets.spin_inputs import (
    CompactIntEntryController,
    SplitAxisDesyncController,
    add_spin_secondary_step_controller,
    compact_int_entry,
    spin_row,
)


@dataclass(frozen=True, slots=True)
class MousePanelConfig:
    capture: PositionCaptureController
    int_entries: CompactIntEntryController
    split_desync: SplitAxisDesyncController
    modified: Callable[[], None]
    split_speed_changed: Callable[[Adw.SpinRow, str], None]
    area_radius_changed: Callable[[Adw.SpinRow, str], None]
    curve_changed: Callable[[], None]
    invert_axis_toggled: Callable[[Gtk.ToggleButton, str], None]
    area_start_enabled_changed: Callable[[], None]
    begin_capture: Callable[[], None]


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


def build_mouse_group(config: MousePanelConfig) -> MouseGroupHandle:
    def modified(*_args: object) -> None:
        config.modified()

    def split_speed_changed(row: Adw.SpinRow, _param: object, axis: str) -> None:
        config.split_speed_changed(row, axis)

    def area_radius_changed(row: Adw.SpinRow, _param: object, axis: str) -> None:
        config.area_radius_changed(row, axis)

    def curve_changed(*_args: object) -> None:
        config.curve_changed()

    def invert_axis_toggled(button: Gtk.ToggleButton, axis: str) -> None:
        config.invert_axis_toggled(button, axis)

    def area_start_enabled_changed(*_args: object) -> None:
        config.area_start_enabled_changed()

    def begin_capture(*_args: object) -> None:
        config.begin_capture()

    group = Adw.PreferencesGroup(title="Mouse Movement")

    speed_row = spin_row(
        "Speed",
        900,
        0,
        5000,
        25,
        0,
        page_step=500,
        on_changed=modified,
    )
    add_spin_secondary_step_controller(speed_row, page_step=500)
    group.add(speed_row)
    speed_x_row = spin_row(
        "Horizontal Speed",
        900,
        0,
        5000,
        25,
        0,
        page_step=500,
        on_changed=modified,
    )
    speed_x_row.connect("notify::value", split_speed_changed, "x")
    config.split_desync.add_click_controller(speed_x_row, "x")
    add_spin_secondary_step_controller(
        speed_x_row,
        page_step=500,
        split_desync_axis="x",
        request_split_desync=config.split_desync.request,
    )
    group.add(speed_x_row)
    speed_y_row = spin_row(
        "Vertical Speed",
        900,
        0,
        5000,
        25,
        0,
        page_step=500,
        on_changed=modified,
    )
    speed_y_row.connect("notify::value", split_speed_changed, "y")
    config.split_desync.add_click_controller(speed_y_row, "y")
    add_spin_secondary_step_controller(
        speed_y_row,
        page_step=500,
        split_desync_axis="y",
        request_split_desync=config.split_desync.request,
    )
    group.add(speed_y_row)
    area_radius_x_row = spin_row(
        "Horizontal Radius",
        400,
        0,
        10000,
        10,
        0,
        page_step=100,
        on_changed=modified,
    )
    area_radius_x_row.set_subtitle("Horizontal radius from the start point")
    area_radius_x_row.connect("notify::value", area_radius_changed, "x")
    config.split_desync.add_click_controller(area_radius_x_row, "x")
    add_spin_secondary_step_controller(
        area_radius_x_row,
        page_step=100,
        split_desync_axis="x",
        request_split_desync=config.split_desync.request,
    )
    group.add(area_radius_x_row)
    area_radius_y_row = spin_row(
        "Vertical Radius",
        400,
        0,
        10000,
        10,
        0,
        page_step=100,
        on_changed=modified,
    )
    area_radius_y_row.set_subtitle("Vertical radius from the start point")
    area_radius_y_row.connect("notify::value", area_radius_changed, "y")
    config.split_desync.add_click_controller(area_radius_y_row, "y")
    add_spin_secondary_step_controller(
        area_radius_y_row,
        page_step=100,
        split_desync_axis="y",
        request_split_desync=config.split_desync.request,
    )
    group.add(area_radius_y_row)
    deadzone_row = spin_row(
        "Deadzone",
        MOUSE_DEADZONE_DEFAULT,
        ANALOG_CURVE_DEADZONE.lower,
        ANALOG_CURVE_DEADZONE.upper,
        ANALOG_CURVE_DEADZONE.step,
        ANALOG_CURVE_DEADZONE.digits,
        page_step=ANALOG_CURVE_DEADZONE.page_step,
        on_changed=modified,
    )
    add_spin_secondary_step_controller(
        deadzone_row,
        page_step=ANALOG_CURVE_DEADZONE.page_step,
    )
    deadzone_row.connect("notify::value", curve_changed)
    group.add(deadzone_row)

    mouse_sensitivity_row = spin_row(
        "Sensitivity",
        ANALOG_CURVE_SENSITIVITY.default,
        ANALOG_CURVE_SENSITIVITY.lower,
        ANALOG_CURVE_SENSITIVITY.upper,
        ANALOG_CURVE_SENSITIVITY.step,
        ANALOG_CURVE_SENSITIVITY.digits,
        page_step=ANALOG_CURVE_SENSITIVITY.page_step,
        on_changed=modified,
    )
    add_spin_secondary_step_controller(
        mouse_sensitivity_row,
        page_step=ANALOG_CURVE_SENSITIVITY.page_step,
    )
    mouse_sensitivity_row.set_subtitle("How quickly movement reaches full speed")
    mouse_sensitivity_row.connect("notify::value", curve_changed)
    group.add(mouse_sensitivity_row)

    mouse_response_curve_row = spin_row(
        "Response Curve",
        ANALOG_CURVE_RESPONSE_CURVE.default,
        ANALOG_CURVE_RESPONSE_CURVE.lower,
        ANALOG_CURVE_RESPONSE_CURVE.upper,
        ANALOG_CURVE_RESPONSE_CURVE.step,
        ANALOG_CURVE_RESPONSE_CURVE.digits,
        page_step=ANALOG_CURVE_RESPONSE_CURVE.page_step,
        on_changed=modified,
    )
    add_spin_secondary_step_controller(
        mouse_response_curve_row,
        page_step=ANALOG_CURVE_RESPONSE_CURVE.page_step,
    )
    mouse_response_curve_row.set_subtitle(
        "Below 1 is faster near center, above 1 is slower near center"
    )
    mouse_response_curve_row.connect("notify::value", curve_changed)
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
            button.connect("toggled", modified)
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
    invert_x_btn.connect("toggled", invert_axis_toggled, "x")
    invert_buttons.append(invert_x_btn)
    invert_y_btn = Gtk.ToggleButton(label="Y")
    invert_y_btn.connect("toggled", invert_axis_toggled, "y")
    invert_buttons.append(invert_y_btn)
    invert_axes_row.add_suffix(invert_buttons)
    group.add(invert_axes_row)

    area_start_enabled_row = Adw.SwitchRow(title="Anchor to a Start Position")
    area_start_enabled_row.connect(
        "notify::active",
        area_start_enabled_changed,
    )
    group.add(area_start_enabled_row)
    area_start_position_row = Adw.ActionRow(title="Start Position")
    start_position_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    start_position_box.set_valign(Gtk.Align.CENTER)
    start_position_box.append(Gtk.Label(label="X"))
    area_start_x_entry = compact_int_entry(0, config.int_entries)
    start_position_box.append(area_start_x_entry)
    start_position_box.append(Gtk.Label(label="Y"))
    area_start_y_entry = compact_int_entry(0, config.int_entries)
    start_position_box.append(area_start_y_entry)
    area_start_position_row.add_suffix(start_position_box)
    group.add(area_start_position_row)
    area_start_capture_row = Adw.ActionRow()
    capture_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    capture_box.set_halign(Gtk.Align.END)
    capture_box.set_valign(Gtk.Align.CENTER)
    if not config.capture.slurp_available:
        capture_box.append(Gtk.Label(label="in"))
    area_start_capture_delay_spin = Gtk.SpinButton()
    area_start_capture_delay_spin.set_adjustment(
        Gtk.Adjustment(
            value=config.capture.delay_seconds,
            lower=0.2,
            upper=15.0,
            step_increment=0.2,
        )
    )
    area_start_capture_delay_spin.set_digits(1)
    area_start_capture_delay_spin.set_width_chars(4)
    area_start_capture_delay_spin.set_visible(not config.capture.slurp_available)
    if not config.capture.slurp_available:
        capture_box.append(area_start_capture_delay_spin)
    if not config.capture.slurp_available:
        capture_box.append(Gtk.Label(label="s"))
    area_start_capture_status = Gtk.Label(label="")
    area_start_capture_status.set_xalign(1.0)
    area_start_capture_status.set_halign(Gtk.Align.END)
    area_start_capture_status.add_css_class("dim-label")
    area_start_capture_status.add_css_class("capture-status-label")
    capture_box.append(area_start_capture_status)
    area_start_capture_btn = Gtk.Button(
        label=("Capture" if config.capture.slurp_available else "Capture Position")
    )
    area_start_capture_btn.connect("clicked", begin_capture)
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
