"""Illustrated mapping layouts driven by a virtual device template."""

from collections.abc import Callable

import evdev
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.virtual_device_templates import (
    LOGITECH_EXTREME_3D_TEMPLATE,
    XBOX_360_TEMPLATE,
    VirtualAxis,
    VirtualDeviceTemplate,
)
from keymasq.gui.widgets.flight_stick_artwork import FlightStickDrawing
from keymasq.gui.widgets.input_picker_shared import _get_gamepad_image_path


def axis_percent_value(axis: VirtualAxis, percent: float) -> int:
    if axis.rest == axis.maximum:
        endpoint = axis.minimum
    else:
        endpoint = axis.maximum if percent >= 0 else axis.minimum
    return round(axis.rest + abs(percent) / 100 * (endpoint - axis.rest))


class VirtualDevicePicker(Gtk.Box):
    def __init__(
        self,
        template: VirtualDeviceTemplate,
        on_button: Callable[[Gtk.Button, str], None],
        on_axis: Callable[[Gtk.Button, str, int], None],
        *,
        current_target: str | None = None,
        current_value: int = 0,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.set_halign(Gtk.Align.CENTER)
        self._on_button = on_button
        self._on_axis = on_axis
        self._axes = {axis.evdev: axis for axis in template.axes}
        self._template = template
        self._buttons = {
            int(getattr(evdev.ecodes, item.evdev.upper())): item for item in template.buttons
        }
        self._shown_buttons: set[int] = set()
        self._syncing = False
        self._extras_jump = Gtk.Button(halign=Gtk.Align.END)
        self._extras_jump.add_css_class("flat")
        self._extras_jump.connect("clicked", self._jump_to_extras)
        self.append(self._extras_jump)
        layout = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        layout.set_halign(Gtk.Align.CENTER)
        self.append(layout)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, valign=Gtk.Align.CENTER)
        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.CENTER)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, valign=Gtk.Align.CENTER)
        layout.append(left)
        layout.append(center)
        layout.append(right)

        if template.layout == "flight-stick":
            self._flight_layout(left, center, right)
        else:
            self._gamepad_layout(left, center, right)

        editor = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        editor.append(Gtk.Label(label="Axis"))
        self.axis_names = list(self._axes)
        self.axis_choice = Gtk.DropDown.new_from_strings(
            [axis.label for axis in self._axes.values()]
        )
        editor.append(self.axis_choice)
        self.value_spin = Gtk.SpinButton(numeric=True, width_chars=5)
        self.value_spin.set_tooltip_text("Exact axis value")
        editor.append(self.value_spin)
        self.percent_spin = Gtk.SpinButton(numeric=True, width_chars=4)
        self.percent_spin.set_tooltip_text("Deflection from rest; 100% is full travel")
        editor.append(self.percent_spin)
        editor.append(Gtk.Label(label="%"))
        apply = Gtk.Button(label="Map axis")
        apply.add_css_class("suggested-action")
        apply.connect("clicked", self._map_axis)
        editor.append(apply)
        self.append(editor)
        self.range_label = Gtk.Label()
        self.range_label.add_css_class("dim-label")
        self.append(self.range_label)
        self.axis_choice.connect("notify::selected", self._axis_changed)
        self.value_spin.connect("value-changed", self._value_changed)
        self.percent_spin.connect("value-changed", self._percent_changed)
        self._axis_changed()
        if current_target in self.axis_names:
            self.axis_choice.set_selected(self.axis_names.index(current_target))
            self.value_spin.set_value(current_value)
        self._build_extras()

    def _flight_layout(self, left: Gtk.Box, center: Gtk.Box, right: Gtk.Box) -> None:
        left.append(self._direction_pad("Stick", "abs_x", "abs_y"))
        twist = self._section("Twist")
        turns = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        turns.append(self._axis_shortcut("↶ Left", "abs_rz", -100, "Twist left"))
        turns.append(self._axis_shortcut("Right ↷", "abs_rz", 100, "Twist right"))
        twist.append(turns)
        twist.set_visible("abs_rz" in self._axes)
        left.append(twist)
        throttle = self._section("Throttle")
        levels = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        for caption, percent in (("Idle", 0), ("Half", 50), ("Full", 100)):
            levels.append(
                self._axis_shortcut(caption, "abs_throttle", percent, f"Throttle {percent}%")
            )
        throttle.append(levels)
        throttle.set_visible("abs_throttle" in self._axes)
        left.append(throttle)

        center.append(FlightStickDrawing())
        base = self._section("Base buttons")
        base_buttons = Gtk.Grid(column_spacing=6, row_spacing=6, halign=Gtk.Align.CENTER)
        for index, control in enumerate(LOGITECH_EXTREME_3D_TEMPLATE.buttons[6:]):
            base_buttons.attach(
                self._button(f"{index + 7}", control.evdev, control.label),
                index % 3,
                index // 3,
                1,
                1,
            )
        base.append(base_buttons)
        base.set_visible(
            any(
                int(getattr(evdev.ecodes, item.evdev.upper())) in self._buttons
                for item in LOGITECH_EXTREME_3D_TEMPLATE.buttons[6:]
            )
        )
        center.append(base)
        grip = self._section("Grip buttons")
        grip_grid = Gtk.Grid(column_spacing=6, row_spacing=6, halign=Gtk.Align.CENTER)
        for index, control in enumerate(LOGITECH_EXTREME_3D_TEMPLATE.buttons[:6]):
            button = self._button(
                control.label, control.evdev, f"Button {index + 1}: {control.label}"
            )
            if index == 0:
                grip_grid.attach(button, 0, 0, 2, 1)
            elif index == 5:
                grip_grid.attach(button, 0, 3, 2, 1)
            else:
                grip_grid.attach(button, (index - 1) % 2, 1 + (index - 1) // 2, 1, 1)
        grip.append(grip_grid)
        grip.set_visible(
            any(
                int(getattr(evdev.ecodes, item.evdev.upper())) in self._buttons
                for item in LOGITECH_EXTREME_3D_TEMPLATE.buttons[:6]
            )
        )
        right.append(grip)
        right.append(self._direction_pad("Hat switch", "abs_hat0x", "abs_hat0y"))

    def _gamepad_layout(self, left: Gtk.Box, center: Gtk.Box, right: Gtk.Box) -> None:
        left.set_spacing(8)
        right.set_spacing(8)
        for column, axis, shoulder in (
            (left, "abs_z", "btn_tl"),
            (right, "abs_rz", "btn_tr"),
        ):
            column.append(
                self._axis_shortcut("LT" if column is left else "RT", axis, 100, "Trigger")
            )
            column.append(self._button("LB" if column is left else "RB", shoulder, "Shoulder"))
        left.append(self._direction_pad("Left stick", "abs_x", "abs_y", stick_button="btn_thumbl"))
        navigation = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        for label, code in (
            ("Select", "btn_select"),
            ("Guide", "btn_mode"),
            ("Start", "btn_start"),
        ):
            navigation.append(self._button(label, code, label))
        center.append(navigation)
        picture = Gtk.Picture.new_for_filename(_get_gamepad_image_path())
        picture.set_can_shrink(True)
        picture.set_size_request(220, 150)
        center.append(picture)
        face = Gtk.Grid(column_spacing=6, row_spacing=6, halign=Gtk.Align.CENTER)
        for label, code, x, y in (
            ("Y", "btn_north", 1, 0),
            ("X", "btn_west", 0, 1),
            ("B", "btn_east", 2, 1),
            ("A", "btn_south", 1, 2),
        ):
            face.attach(self._button(label, code, label), x, y, 1, 1)
        right.append(face)
        right.append(
            self._direction_pad("Right stick", "abs_rx", "abs_ry", stick_button="btn_thumbr")
        )
        left.append(self._direction_pad("D-pad axes", "abs_hat0x", "abs_hat0y"))
        dpad = self._section("D-pad buttons")
        grid = Gtk.Grid(column_spacing=6, row_spacing=6, halign=Gtk.Align.CENTER)
        codes = (
            ("↑", "btn_dpad_up", 1, 0),
            ("←", "btn_dpad_left", 0, 1),
            ("→", "btn_dpad_right", 2, 1),
            ("↓", "btn_dpad_down", 1, 2),
        )
        for label, code, x, y in codes:
            grid.attach(self._button(label, code, code), x, y, 1, 1)
        dpad.append(grid)
        dpad.set_visible(
            any(
                int(getattr(evdev.ecodes, code.upper())) in self._buttons for _, code, _, _ in codes
            )
        )
        center.append(dpad)

    def _axis_shortcut(self, label: str, code: str, percent: float, tooltip: str) -> Gtk.Widget:
        axis = self._axes.get(code)
        if axis is None:
            return Gtk.Box(visible=False)
        return self._axis_button(label, code, axis_percent_value(axis, percent), tooltip)

    def _build_extras(self) -> None:
        extra = [
            (index + 1, button)
            for index, button in enumerate(self._template.buttons)
            if int(getattr(evdev.ecodes, button.evdev.upper())) not in self._shown_buttons
        ]
        self._extras_jump.set_visible(bool(extra))
        self._extras_jump.set_label(f"Additional buttons ({len(extra)}) ↓")
        if not extra:
            return
        self.append(Gtk.Separator())
        header = Gtk.Box(spacing=12)
        title = Gtk.Label(label=f"Additional buttons · {len(extra)}", hexpand=True, xalign=0)
        title.add_css_class("heading")
        header.append(title)
        self.extra_search = Gtk.SearchEntry(placeholder_text="Search buttons", hexpand=True)
        self.extra_search.connect("search-changed", self._filter_extras)
        header.append(self.extra_search)
        self.append(header)
        self.extra_grid = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, column_spacing=6, row_spacing=6
        )
        self.extra_grid.set_min_children_per_line(2)
        self.extra_grid.set_max_children_per_line(3)
        self._extra_items: list[tuple[Gtk.FlowBoxChild, str]] = []
        for number, control in extra:
            label = (
                control.label
                if control.label == f"Button {number}"
                else f"{number} · {control.label}"
            )
            button = Gtk.Button(label=label, tooltip_text=f"{control.id} · {control.evdev}")
            button.set_size_request(-1, 40)
            button.set_hexpand(True)
            button.connect("clicked", self._on_button, control.evdev)
            child = Gtk.FlowBoxChild()
            child.set_child(button)
            self.extra_grid.append(child)
            self._extra_items.append(
                (child, f"{number} {control.label} {control.id} {control.evdev}".casefold())
            )
        self.extra_grid.set_filter_func(self._extra_matches)
        self.append(self.extra_grid)
        self._no_results = Gtk.Label(label="No matching buttons", visible=False)
        self._no_results.add_css_class("dim-label")
        self.append(self._no_results)

    def _extra_matches(self, child: Gtk.FlowBoxChild) -> bool:
        query = self.extra_search.get_text().strip().casefold()
        return any(
            item is child and all(token in text for token in query.split())
            for item, text in self._extra_items
        )

    def _filter_extras(self, *_args: object) -> None:
        self.extra_grid.invalidate_filter()
        self._no_results.set_visible(
            not any(self._extra_matches(child) for child, _ in self._extra_items)
        )

    def _jump_to_extras(self, _button: Gtk.Button) -> None:
        self.extra_search.grab_focus()
        ancestor = self.get_ancestor(Gtk.ScrolledWindow)
        if isinstance(ancestor, Gtk.ScrolledWindow):
            adjustment = ancestor.get_vadjustment()
            found, bounds = self.extra_search.compute_bounds(self)
            if found:
                adjustment.set_value(bounds.get_y())

    @staticmethod
    def _section(title: str) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        label = Gtk.Label(label=title)
        label.add_css_class("dim-label")
        box.append(label)
        return box

    def _button(self, label: str, code: str, tooltip: str) -> Gtk.Widget:
        numeric_code = int(getattr(evdev.ecodes, code.upper()))
        control = self._buttons.get(numeric_code)
        if control is None:
            return Gtk.Box(visible=False)
        self._shown_buttons.add(numeric_code)
        original = next(
            (
                item
                for item in (*XBOX_360_TEMPLATE.buttons, *LOGITECH_EXTREME_3D_TEMPLATE.buttons)
                if int(getattr(evdev.ecodes, item.evdev.upper())) == numeric_code
            ),
            None,
        )
        if original is None or control.label != original.label:
            label = control.label
        code = control.evdev
        button = Gtk.Button(label=label)
        button.add_css_class("key-button")
        button.set_size_request(36, 34)
        button.set_tooltip_text(f"{control.label} · {tooltip} · {code}")
        button.connect("clicked", self._on_button, code)
        return button

    def _axis_button(self, label: str, code: str, value: int, tooltip: str) -> Gtk.Button:
        button = Gtk.Button(label=label)
        button.add_css_class("key-button")
        button.set_size_request(36, 34)
        button.set_tooltip_text(f"{tooltip} · {code} = {value}")
        button.connect("clicked", self._on_axis, code, value)
        return button

    def _direction_pad(
        self, title: str, x_code: str, y_code: str, *, stick_button: str | None = None
    ) -> Gtk.Box:
        box = self._section(title)
        grid = Gtk.Grid(column_spacing=6, row_spacing=6, halign=Gtk.Align.CENTER)
        x_axis, y_axis = self._axes.get(x_code), self._axes.get(y_code)
        if x_axis is None and y_axis is None:
            box.set_visible(False)
            return box
        for label, code, value, col, row, direction in (
            (
                "↑",
                y_code,
                y_axis.minimum if y_axis else 0,
                1,
                0,
                "forward" if title == "Stick" else "up",
            ),
            ("←", x_code, x_axis.minimum if x_axis else 0, 0, 1, "left"),
            ("→", x_code, x_axis.maximum if x_axis else 0, 2, 1, "right"),
            (
                "↓",
                y_code,
                y_axis.maximum if y_axis else 0,
                1,
                2,
                "back" if title == "Stick" else "down",
            ),
        ):
            if code not in self._axes:
                continue
            button = self._axis_button(label, code, value, f"{title} {direction}")
            grid.attach(button, col, row, 1, 1)
        if stick_button is not None:
            center = self._button(
                "LS" if stick_button == "btn_thumbl" else "RS", stick_button, "Stick press"
            )
        else:
            center = Gtk.Label(label="✛")
            center.add_css_class("dim-label")
        grid.attach(center, 1, 1, 1, 1)
        box.append(grid)
        return box

    def _selected_axis(self) -> VirtualAxis:
        return self._axes[self.axis_names[int(self.axis_choice.get_selected())]]

    def _axis_changed(self, *_args: object) -> None:
        axis = self._selected_axis()
        self._syncing = True
        self.value_spin.set_adjustment(
            Gtk.Adjustment(
                lower=axis.minimum, upper=axis.maximum, step_increment=1, page_increment=10
            )
        )
        self.percent_spin.set_adjustment(
            Gtk.Adjustment(
                lower=-100 if axis.minimum < axis.rest < axis.maximum else 0,
                upper=100,
                step_increment=1,
                page_increment=10,
            )
        )
        self._syncing = False
        self.value_spin.set_value(axis_percent_value(axis, 100))
        self._value_changed()
        self.range_label.set_text(f"Range {axis.minimum} to {axis.maximum} · Rest {axis.rest}")

    def _value_changed(self, *_args: object) -> None:
        if self._syncing:
            return
        axis = self._selected_axis()
        value = self.value_spin.get_value()
        endpoint = axis.maximum if value >= axis.rest else axis.minimum
        distance = abs(endpoint - axis.rest)
        percent = 0 if distance == 0 else (value - axis.rest) / distance * 100
        if axis.rest == axis.maximum:
            percent = -percent
        self._syncing = True
        self.percent_spin.set_value(percent)
        self._syncing = False

    def _percent_changed(self, *_args: object) -> None:
        if self._syncing:
            return
        self._syncing = True
        self.value_spin.set_value(
            axis_percent_value(self._selected_axis(), self.percent_spin.get_value())
        )
        self._syncing = False

    def _map_axis(self, button: Gtk.Button) -> None:
        self.value_spin.update()
        self._on_axis(button, self._selected_axis().evdev, int(self.value_spin.get_value()))
