"""Gamepad-output settings panel construction."""

from collections.abc import Callable
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.widgets.analog_curve_graph import (
    ANALOG_CURVE_DEADZONE,
    ANALOG_CURVE_RESPONSE_CURVE,
    ANALOG_CURVE_SENSITIVITY,
    AnalogCurveGraph,
)
from keymasq.gui.widgets.spin_inputs import add_spin_secondary_step_controller, spin_row


@dataclass(frozen=True, slots=True)
class GamepadPanelCallbacks:
    modified: Callable[[], None]
    output_selected: Callable[[Gtk.DropDown], None]
    direction_toggled: Callable[[Gtk.ToggleButton], None]
    curve_changed: Callable[[], None]


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
    gamepad_output_invert_row: Adw.ActionRow
    gamepad_output_invert_x_btn: Gtk.ToggleButton
    gamepad_output_invert_y_btn: Gtk.ToggleButton
    gamepad_output_sensitivity_row: Adw.SpinRow
    gamepad_output_response_curve_row: Adw.SpinRow
    gamepad_output_curve_row: Adw.ActionRow
    gamepad_output_curve_graph: AnalogCurveGraph
    gamepad_output_warning_row: Adw.ActionRow
    gamepad_output_warning_label: Gtk.Label


def build_gamepad_output_group(
    callbacks: GamepadPanelCallbacks,
) -> GamepadOutputGroupHandle:
    def modified(*_args: object) -> None:
        callbacks.modified()

    def output_selected(dropdown: Gtk.DropDown, _param: object) -> None:
        callbacks.output_selected(dropdown)

    def direction_toggled(button: Gtk.ToggleButton) -> None:
        callbacks.direction_toggled(button)

    def curve_changed(*_args: object) -> None:
        callbacks.curve_changed()

    group = Adw.PreferencesGroup(
        title="Analog Output Settings",
        description="Route the stick or axis to a gamepad output device.",
    )

    gamepad_output_target_row = Adw.ActionRow(title="Output")
    dropdown = Gtk.DropDown()
    dropdown.set_valign(Gtk.Align.CENTER)
    dropdown.connect(
        "notify::selected",
        output_selected,
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

    gamepad_output_deadzone_row = spin_row(
        "Output Deadzone",
        ANALOG_CURVE_DEADZONE.default * 100.0,
        ANALOG_CURVE_DEADZONE.lower * 100.0,
        ANALOG_CURVE_DEADZONE.upper * 100.0,
        1,
        0,
        page_step=ANALOG_CURVE_DEADZONE.page_step * 100.0,
        on_changed=modified,
    )
    add_spin_secondary_step_controller(
        gamepad_output_deadzone_row,
        page_step=ANALOG_CURVE_DEADZONE.page_step * 100.0,
    )
    gamepad_output_deadzone_row.set_subtitle(
        "Percent below which output is sent as centered or released"
    )
    gamepad_output_deadzone_row.connect(
        "notify::value",
        curve_changed,
    )
    group.add(gamepad_output_deadzone_row)

    gamepad_output_rest_row = spin_row(
        "Output Rest",
        0,
        -2147483648,
        2147483647,
        1,
        0,
        on_changed=modified,
    )
    add_spin_secondary_step_controller(
        gamepad_output_rest_row,
        reset_value=0,
    )
    gamepad_output_rest_row.set_subtitle("Raw value written when the output axis is released")
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
    gamepad_output_direction_min_btn.connect("toggled", direction_toggled)
    gamepad_output_direction_max_btn.connect("toggled", direction_toggled)
    gamepad_output_direction_both_btn.connect("toggled", direction_toggled)
    direction_buttons.append(gamepad_output_direction_min_btn)
    direction_buttons.append(gamepad_output_direction_max_btn)
    direction_buttons.append(gamepad_output_direction_both_btn)
    gamepad_output_direction_row.add_suffix(direction_buttons)
    group.add(gamepad_output_direction_row)

    gamepad_output_invert_row = Adw.ActionRow(title="Invert Output Axes")
    gamepad_output_invert_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    gamepad_output_invert_buttons.add_css_class("linked")
    gamepad_output_invert_buttons.set_valign(Gtk.Align.CENTER)
    gamepad_output_invert_x_btn = Gtk.ToggleButton(label="X")
    gamepad_output_invert_x_btn.connect(
        "toggled",
        modified,
    )
    gamepad_output_invert_buttons.append(gamepad_output_invert_x_btn)
    gamepad_output_invert_y_btn = Gtk.ToggleButton(label="Y")
    gamepad_output_invert_y_btn.connect(
        "toggled",
        modified,
    )
    gamepad_output_invert_buttons.append(gamepad_output_invert_y_btn)
    gamepad_output_invert_row.add_suffix(gamepad_output_invert_buttons)
    group.add(gamepad_output_invert_row)

    gamepad_output_sensitivity_row = spin_row(
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
        gamepad_output_sensitivity_row,
        page_step=ANALOG_CURVE_SENSITIVITY.page_step,
    )
    gamepad_output_sensitivity_row.set_subtitle("How quickly stick output reaches full range")
    gamepad_output_sensitivity_row.connect(
        "notify::value",
        curve_changed,
    )
    group.add(gamepad_output_sensitivity_row)

    gamepad_output_response_curve_row = spin_row(
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
        gamepad_output_response_curve_row,
        page_step=ANALOG_CURVE_RESPONSE_CURVE.page_step,
    )
    gamepad_output_response_curve_row.set_subtitle(
        "Below 1 is faster near center, above 1 is slower near center"
    )
    gamepad_output_response_curve_row.connect(
        "notify::value",
        curve_changed,
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
        gamepad_output_invert_row=gamepad_output_invert_row,
        gamepad_output_invert_x_btn=gamepad_output_invert_x_btn,
        gamepad_output_invert_y_btn=gamepad_output_invert_y_btn,
        gamepad_output_sensitivity_row=gamepad_output_sensitivity_row,
        gamepad_output_response_curve_row=gamepad_output_response_curve_row,
        gamepad_output_curve_row=gamepad_output_curve_row,
        gamepad_output_curve_graph=gamepad_output_curve_graph,
        gamepad_output_warning_row=warning_row,
        gamepad_output_warning_label=warning,
    )
