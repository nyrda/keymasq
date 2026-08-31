from collections.abc import Callable
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gtk, Pango  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.actions import is_protected_button
from keymasq.common.model.core import DeviceType
from keymasq.common.model.hardware import AnalogInputDefinition, ButtonDefinition, HardwareConfig
from keymasq.common.model.motion import MotionSensorDefinition
from keymasq.gui.widgets.device_control_layout import (
    group_pointer_controls,
    label_sort_key,
    resolve_device_layout_kind,
)

_ADD_INPUTS_TOOLTIP = "Capture additional physical buttons or keys for this device"
_MAPPING_BUTTON_CARD_WIDTH = 122
_MAPPING_LABEL_CHARS = 14
_MAPPING_ACTION_SUMMARY_CHARS = 15
_MOUSE_MAPPING_BUTTON_CARD_WIDTH = 187
_MOUSE_MAPPING_LABEL_CHARS = 20
_MOUSE_MAPPING_ACTION_SUMMARY_CHARS = 24
_ANALOG_LAYOUT_ORDER = {
    "left_trigger": 0,
    "right_trigger": 1,
    "left_stick": 2,
    "right_stick": 3,
}


@dataclass(frozen=True)
class DeviceGridCallbacks:
    on_add_inputs_clicked: Callable[..., None]
    on_learn_analog_clicked: Callable[..., None]
    on_mapping_button_clicked: Callable[..., None]
    on_analog_mapping_clicked: Callable[..., None]
    on_motion_mapping_clicked: Callable[..., None]
    on_motion_action_right_clicked: Callable[..., None]
    on_name_label_right_clicked: Callable[..., None]
    on_action_label_right_clicked: Callable[..., None]
    on_analog_name_right_clicked: Callable[..., None]


@dataclass(frozen=True)
class DeviceGridResult:
    widget: Gtk.ScrolledWindow
    button_widgets: dict[str, Gtk.Button]
    keyboard_layout_mode: bool


def _ordered_analog_inputs(
    analog_inputs: list[AnalogInputDefinition],
) -> list[AnalogInputDefinition]:
    return sorted(
        analog_inputs,
        key=lambda analog: (
            _ANALOG_LAYOUT_ORDER.get(analog.id, len(_ANALOG_LAYOUT_ORDER)),
            analog.label.lower(),
            analog.id,
        ),
    )


def _grouped_analog_inputs(
    analog_inputs: list[AnalogInputDefinition],
) -> list[tuple[str, list[AnalogInputDefinition]]]:
    ordered = _ordered_analog_inputs(analog_inputs)
    groups: list[tuple[str, list[AnalogInputDefinition]]] = []
    for analog_type, title in (("axis", "1D Axes / Triggers"), ("stick", "Sticks")):
        matching = [analog for analog in ordered if str(analog.type).lower() == analog_type]
        if matching:
            groups.append((title, matching))

    other = [analog for analog in ordered if str(analog.type).lower() not in {"axis", "stick"}]
    if other:
        groups.append(("Other", other))
    return groups


def button_card_width(kind: str) -> int:
    if kind == "mouse":
        return _MOUSE_MAPPING_BUTTON_CARD_WIDTH
    return _MAPPING_BUTTON_CARD_WIDTH


def mapping_label_chars(kind: str) -> int:
    if kind == "mouse":
        return _MOUSE_MAPPING_LABEL_CHARS
    return _MAPPING_LABEL_CHARS


def mapping_action_summary_chars(kind: str) -> int:
    if kind == "mouse":
        return _MOUSE_MAPPING_ACTION_SUMMARY_CHARS
    return _MAPPING_ACTION_SUMMARY_CHARS


def supports_analog_learning(device: HardwareConfig) -> bool:
    if device.analog_inputs:
        return True
    return any(
        evdev_device.device_type not in {DeviceType.MOUSE, DeviceType.KEYBOARD}
        for evdev_device in device.evdev_devices
    )


class DeviceGridBuilder:
    def __init__(
        self,
        *,
        device: HardwareConfig,
        demo_mode: bool,
        callbacks: DeviceGridCallbacks,
        describe_passthrough_output: Callable[[ButtonDefinition], str],
    ) -> None:
        self.device = device
        self.demo_mode = demo_mode
        self.callbacks = callbacks
        self.describe_passthrough_output = describe_passthrough_output
        self.button_widgets: dict[str, Gtk.Button] = {}

    def build(self) -> DeviceGridResult:
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_margin_top(12)

        kind = self.device_layout_kind()
        keyboard_layout_mode = kind == "keyboard"

        if keyboard_layout_mode:
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

            buttons_by_id = {b.id: b for b in self.device.buttons}
            used_ids: set[str] = set()

            self._append_keyboard_section(
                content,
                "Number Keys",
                [
                    [
                        "key_1",
                        "key_2",
                        "key_3",
                        "key_4",
                        "key_5",
                        "key_minus",
                    ],
                    [
                        "key_6",
                        "key_7",
                        "key_8",
                        "key_9",
                        "key_0",
                        "key_equal",
                    ],
                ],
                buttons_by_id,
                used_ids,
                max_cols=6,
                expanded=True,
            )

            self._append_keyboard_section(
                content,
                "Keyboard (Left)",
                [
                    ["key_esc", "key_grave"],
                    ["key_tab", "key_q", "key_w", "key_e", "key_r", "key_t"],
                    ["key_capslock", "key_a", "key_s", "key_d", "key_f", "key_g"],
                    ["key_leftshift", "key_z", "key_x", "key_c", "key_v", "key_b"],
                    [
                        "key_leftctrl",
                        "key_leftmeta",
                        "key_leftalt",
                        "key_space",
                    ],
                ],
                buttons_by_id,
                used_ids,
                max_cols=6,
                expanded=True,
            )

            self._append_keyboard_section(
                content,
                "Keyboard (Right)",
                [
                    ["key_backspace", "key_y", "key_u", "key_i", "key_o", "key_p"],
                    ["key_enter", "key_h", "key_j", "key_k", "key_l"],
                    ["key_n", "key_m", "key_rightshift"],
                    ["key_rightmeta", "key_rightalt", "key_rightctrl"],
                ],
                buttons_by_id,
                used_ids,
                max_cols=6,
            )

            self._append_keyboard_section(
                content,
                "Symbols",
                [
                    [
                        "key_leftbrace",
                        "key_rightbrace",
                        "key_backslash",
                        "key_semicolon",
                        "key_apostrophe",
                    ],
                    ["key_comma", "key_dot", "key_slash"],
                ],
                buttons_by_id,
                used_ids,
                max_cols=5,
            )

            self._append_keyboard_section(
                content,
                "F Row",
                [
                    [
                        "key_f1",
                        "key_f2",
                        "key_f3",
                        "key_f4",
                        "key_f5",
                        "key_f6",
                    ],
                    [
                        "key_f7",
                        "key_f8",
                        "key_f9",
                        "key_f10",
                        "key_f11",
                        "key_f12",
                    ],
                ],
                buttons_by_id,
                used_ids,
                max_cols=6,
            )

            self._append_keyboard_section(
                content,
                "Navigation",
                [
                    ["key_up", "key_down"],
                    ["key_left", "key_right"],
                    ["key_sysrq", "key_scrolllock", "key_pause"],
                    ["key_insert", "key_home", "key_pageup"],
                    ["key_delete", "key_end", "key_pagedown"],
                ],
                buttons_by_id,
                used_ids,
                max_cols=4,
            )

            self._append_keyboard_section(
                content,
                "Special",
                [
                    ["key_numlock", "key_kpslash", "key_kpasterisk", "key_kpminus"],
                    ["key_kp7", "key_kp8", "key_kp9", "key_kpplus"],
                    ["key_kp4", "key_kp5", "key_kp6"],
                    ["key_kp1", "key_kp2", "key_kp3", "key_kpenter"],
                    ["key_kp0", "key_kpdot"],
                ],
                buttons_by_id,
                used_ids,
                max_cols=4,
            )

            extras = [b for b in self.device.buttons if b.id not in used_ids]
            if extras:
                self._append_other_buttons_section(
                    content,
                    extras,
                    title="Extra Buttons",
                    expanded=True,
                    prepend=True,
                )

            self._append_analog_controls_section(content)
            self._append_motion_controls_section(content)
            self._append_learn_tile(content)
            scrolled.set_child(content)
            return DeviceGridResult(scrolled, self.button_widgets, keyboard_layout_mode)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        main_buttons, scroll_buttons, other_buttons, extra_buttons = group_pointer_controls(
            self.device.buttons,
            id_for_control=lambda button: button.id,
        )

        def add_section(
            title: str,
            buttons: list[ButtonDefinition],
            parent: Gtk.Box,
            max_cols: int = 4,
        ) -> None:
            if not buttons:
                return
            if title:
                label = Gtk.Label(label=title)
                label.add_css_class("button-section-title")
                label.set_halign(Gtk.Align.START)
                parent.append(label)
            grid = Gtk.Grid()
            grid.set_column_spacing(12)
            grid.set_row_spacing(12)
            col = 0
            row = 0
            for btn in buttons:
                widget = self._create_button_widget(btn)
                grid.attach(widget, col, row, 1, 1)
                self.button_widgets[btn.id] = widget
                col += 1
                if col >= max_cols:
                    col = 0
                    row += 1
            parent.append(grid)

        if kind == "gamepad":
            buttons_by_id = {b.id: b for b in self.device.buttons}
            for title, button_ids, max_cols in [
                ("Shoulders", ["btn_tl", "btn_tr"], 2),
                ("Menu Buttons", ["btn_select", "btn_mode", "btn_start"], 3),
                ("Face Buttons", ["btn_north", "btn_west", "btn_east", "btn_south"], 4),
                ("Stick Clicks", ["btn_thumbl", "btn_thumbr"], 2),
                (
                    "D-Pad",
                    ["btn_dpad_up", "btn_dpad_left", "btn_dpad_right", "btn_dpad_down"],
                    4,
                ),
            ]:
                section_buttons = []
                for button_id in button_ids:
                    button = buttons_by_id.pop(button_id, None)
                    if button is not None:
                        section_buttons.append(button)
                add_section(title, section_buttons, content, max_cols=max_cols)

            extras = sorted(
                buttons_by_id.values(),
                key=lambda button: label_sort_key(button.label),
            )
            if extras:
                self._append_other_buttons_section(
                    content,
                    extras,
                    title="Additional Controls",
                    expanded=True,
                    prepend=True,
                )

            self._append_analog_controls_section(content)
            self._append_motion_controls_section(content)
            self._append_learn_tile(content)
            scrolled.set_child(content)
            return DeviceGridResult(scrolled, self.button_widgets, keyboard_layout_mode)

        if extra_buttons:
            extra_expander = Gtk.Expander(label=f"Extra Buttons ({len(extra_buttons)})")
            extra_expander.set_expanded(True)
            extra_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            add_section(
                "",
                sorted(extra_buttons, key=lambda button: label_sort_key(button.label)),
                extra_box,
                max_cols=3,
            )
            extra_expander.set_child(extra_box)
            content.append(extra_expander)

        add_section("Main Buttons", main_buttons, content)
        add_section("Scroll", scroll_buttons, content)
        add_section("Side Buttons", other_buttons, content)

        self._append_analog_controls_section(content)
        self._append_motion_controls_section(content)
        self._append_learn_tile(content)
        scrolled.set_child(content)
        return DeviceGridResult(scrolled, self.button_widgets, keyboard_layout_mode)

    def device_layout_kind(self) -> str:
        return resolve_device_layout_kind(self.device)

    def _learn_label_noun(self) -> str:
        if self.device_layout_kind() == "keyboard":
            return "Keys"
        return "Buttons"

    def _learn_label_text(self) -> str:
        return f"Learn {self._learn_label_noun()}"

    def _make_icon_label_box(self, icon_name: str, label_text: str) -> Gtk.Box:
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        inner.set_halign(Gtk.Align.CENTER)
        inner.set_valign(Gtk.Align.CENTER)

        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(16)
        inner.append(icon)

        label = Gtk.Label(label=label_text)
        inner.append(label)
        return inner

    def create_learn_tile(self) -> Gtk.Button:
        btn = Gtk.Button()
        btn.add_css_class("button-card-learn")
        btn.set_halign(Gtk.Align.START)
        btn.set_tooltip_text(_ADD_INPUTS_TOOLTIP)
        btn.connect("clicked", self.callbacks.on_add_inputs_clicked)
        inner = self._make_icon_label_box("list-add-symbolic", self._learn_label_text())
        btn.set_child(inner)
        return btn

    def create_learn_analog_tile(self) -> Gtk.Button:
        btn = Gtk.Button()
        btn.add_css_class("button-card-learn")
        btn.set_halign(Gtk.Align.START)
        btn.set_tooltip_text("Capture a generic analog axis or stick for this device")
        btn.connect("clicked", self.callbacks.on_learn_analog_clicked)
        inner = self._make_icon_label_box("list-add-symbolic", "Learn Analog")
        btn.set_child(inner)
        return btn

    def _append_learn_tile(self, parent: Gtk.Box) -> None:
        if self.demo_mode:
            return
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_top(8)
        row.append(self.create_learn_tile())
        if supports_analog_learning(self.device):
            row.append(self.create_learn_analog_tile())
        parent.append(row)

    def _append_keyboard_section(
        self,
        parent: Gtk.Box,
        title: str,
        layout_rows: list[list[str]],
        buttons_by_id: dict[str, ButtonDefinition],
        used_ids: set[str],
        max_cols: int,
        expanded: bool = False,
    ) -> None:
        grid = self._build_keyboard_grid(layout_rows, buttons_by_id, used_ids, max_cols)

        expander = Gtk.Expander(label=title)
        expander.add_css_class("device-section-expander")
        expander.set_expanded(expanded)
        expander.set_child(grid)
        parent.append(expander)

    def _build_keyboard_grid(
        self,
        layout_rows: list[list[str]],
        buttons_by_id: dict[str, ButtonDefinition],
        used_ids: set[str],
        max_cols: int,
    ) -> Gtk.Grid:
        grid = Gtk.Grid()
        grid.set_column_spacing(4)
        grid.set_row_spacing(4)

        for row_i, row_items in enumerate(layout_rows):
            col_i = 0
            for button_id in row_items:
                button = buttons_by_id.get(button_id)
                if button is None:
                    spacer = Gtk.Box()
                    spacer.set_size_request(_MAPPING_BUTTON_CARD_WIDTH, -1)
                    grid.attach(spacer, col_i, row_i, 1, 1)
                else:
                    widget = self._create_button_widget(button)
                    grid.attach(widget, col_i, row_i, 1, 1)
                    self.button_widgets[button.id] = widget
                    used_ids.add(button.id)
                col_i += 1

            while col_i < max_cols:
                spacer = Gtk.Box()
                spacer.set_size_request(_MAPPING_BUTTON_CARD_WIDTH, -1)
                grid.attach(spacer, col_i, row_i, 1, 1)
                col_i += 1

        return grid

    def _append_other_buttons_section(
        self,
        parent: Gtk.Box,
        extras: list[ButtonDefinition],
        *,
        title: str = "Extra Keys",
        expanded: bool = False,
        prepend: bool = False,
    ) -> None:
        grid = Gtk.Grid()
        grid.set_column_spacing(4)
        grid.set_row_spacing(4)

        col = 0
        row = 0
        max_cols = 6
        for button in sorted(extras, key=lambda b: label_sort_key(b.label)):
            widget = self._create_button_widget(button)
            grid.attach(widget, col, row, 1, 1)
            self.button_widgets[button.id] = widget
            col += 1
            if col >= max_cols:
                col = 0
                row += 1

        expander = Gtk.Expander(label=f"{title} ({len(extras)})")
        expander.add_css_class("device-section-expander")
        expander.set_expanded(expanded)
        expander.set_child(grid)
        if prepend:
            parent.prepend(expander)
        else:
            parent.append(expander)

    def _button_card_width(self) -> int:
        return button_card_width(self.device_layout_kind())

    def _mapping_label_chars(self) -> int:
        return mapping_label_chars(self.device_layout_kind())

    def _mapping_action_summary_chars(self) -> int:
        return mapping_action_summary_chars(self.device_layout_kind())

    def _create_button_widget(self, button: ButtonDefinition) -> Gtk.Button:
        protected = is_protected_button(button.id)

        btn = Gtk.Button()
        btn.add_css_class("card")
        btn.add_css_class("button-card-passthrough")
        btn.set_margin_top(2)
        btn.set_margin_bottom(2)
        btn.set_margin_start(2)
        btn.set_margin_end(2)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_halign(Gtk.Align.FILL)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_top(6)
        box.set_margin_bottom(7)
        box.set_margin_start(8)
        box.set_margin_end(8)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)

        name_label = Gtk.Label(label=button.label)
        name_label.add_css_class("heading")
        name_label.set_xalign(0.0)
        name_label.set_ellipsize(Pango.EllipsizeMode.END)
        name_label.set_width_chars(1)
        name_label.set_max_width_chars(self._mapping_label_chars())
        header.append(name_label)

        name_right_click = Gtk.GestureClick()
        name_right_click.set_button(Gdk.BUTTON_SECONDARY)
        name_right_click.connect(
            "pressed",
            self.callbacks.on_name_label_right_clicked,
            button,
        )
        name_label.add_controller(name_right_click)

        if protected:
            info_icon = Gtk.Image(icon_name="help-about-symbolic")
            info_icon.set_pixel_size(10)
            info_icon.add_css_class("protected-button-info-icon")
            info_icon.set_tooltip_text("Remapping this button requires confirmation")
            header.append(info_icon)

        box.append(header)

        action_label = Gtk.Label(label=self.describe_passthrough_output(button))
        action_label.add_css_class("caption")
        action_label.add_css_class("button-card-action-label")
        action_label.set_halign(Gtk.Align.FILL)
        action_label.set_xalign(0.0)
        action_label.set_hexpand(True)
        action_label.set_single_line_mode(True)
        action_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        action_label.set_width_chars(1)
        action_label.set_max_width_chars(self._mapping_action_summary_chars())
        box.append(action_label)

        action_right_click = Gtk.GestureClick()
        action_right_click.set_button(Gdk.BUTTON_SECONDARY)
        action_right_click.connect(
            "pressed",
            self.callbacks.on_action_label_right_clicked,
            button,
        )
        action_label.add_controller(action_right_click)

        btn._action_label = action_label
        btn._name_label = name_label
        btn._button_id = button.id
        btn._protected = protected

        btn.set_size_request(self._button_card_width(), -1)
        btn.set_halign(Gtk.Align.START)
        btn.set_hexpand(False)
        btn.set_child(box)
        btn.connect("clicked", self.callbacks.on_mapping_button_clicked, button, protected)

        return btn

    def _append_analog_controls_section(self, parent: Gtk.Box) -> None:
        if not self.device.analog_inputs:
            return
        label = Gtk.Label(label="Analog Controls")
        label.add_css_class("button-section-title")
        label.set_halign(Gtk.Align.START)
        parent.append(label)

        for title, analogs in _grouped_analog_inputs(self.device.analog_inputs):
            group_label = Gtk.Label(label=title)
            group_label.add_css_class("caption")
            group_label.add_css_class("dim-label")
            group_label.set_halign(Gtk.Align.START)
            group_label.set_margin_top(2)
            parent.append(group_label)

            grid = Gtk.Grid()
            grid.set_column_spacing(12)
            grid.set_row_spacing(12)
            for index, analog in enumerate(analogs):
                widget = self._create_analog_widget(analog)
                grid.attach(widget, index % 2, index // 2, 1, 1)
                self.button_widgets[analog.id] = widget
            parent.append(grid)

    def _create_analog_widget(self, analog: AnalogInputDefinition) -> Gtk.Button:
        btn = Gtk.Button()
        btn.add_css_class("card")
        btn.add_css_class("button-card-passthrough")
        btn.set_margin_top(2)
        btn.set_margin_bottom(2)
        btn.set_margin_start(2)
        btn.set_margin_end(2)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_halign(Gtk.Align.FILL)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_top(6)
        box.set_margin_bottom(7)
        box.set_margin_start(8)
        box.set_margin_end(8)

        name_label = Gtk.Label(label=analog.label)
        name_label.add_css_class("heading")
        name_label.set_xalign(0.0)
        name_label.set_ellipsize(Pango.EllipsizeMode.END)
        name_label.set_width_chars(1)
        name_label.set_max_width_chars(self._mapping_label_chars())
        box.append(name_label)
        name_right_click = Gtk.GestureClick()
        name_right_click.set_button(Gdk.BUTTON_SECONDARY)
        name_right_click.connect(
            "pressed",
            self.callbacks.on_analog_name_right_clicked,
            analog,
        )
        name_label.add_controller(name_right_click)

        passthrough_label = "Axis passthrough" if analog.type == "axis" else "Analog passthrough"
        action_label = Gtk.Label(label=passthrough_label)
        action_label.add_css_class("caption")
        action_label.add_css_class("button-card-action-label")
        action_label.set_halign(Gtk.Align.FILL)
        action_label.set_xalign(0.0)
        action_label.set_hexpand(True)
        action_label.set_single_line_mode(True)
        action_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        action_label.set_width_chars(1)
        action_label.set_max_width_chars(self._mapping_action_summary_chars())
        box.append(action_label)

        btn._action_label = action_label
        btn._name_label = name_label
        btn._button_id = analog.id
        btn._protected = False
        btn._analog_source = True
        btn.set_size_request(self._button_card_width(), -1)
        btn.set_halign(Gtk.Align.START)
        btn.set_hexpand(False)
        btn.set_child(box)
        btn.connect("clicked", self.callbacks.on_analog_mapping_clicked, analog)
        return btn

    def _append_motion_controls_section(self, parent: Gtk.Box) -> None:
        if not self.device.motion_sensors:
            return
        label = Gtk.Label(label="Motion Sensors")
        label.add_css_class("button-section-title")
        label.set_halign(Gtk.Align.START)
        parent.append(label)
        grid = Gtk.Grid(column_spacing=12, row_spacing=12)
        for index, sensor in enumerate(self.device.motion_sensors):
            widget = self._create_motion_widget(sensor)
            grid.attach(widget, index % 2, index // 2, 1, 1)
            self.button_widgets[sensor.id] = widget
        parent.append(grid)

    def _create_motion_widget(self, sensor: MotionSensorDefinition) -> Gtk.Button:
        button = Gtk.Button()
        button.add_css_class("card")
        button.add_css_class("button-card-passthrough")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        content.set_margin_top(8)
        content.set_margin_bottom(8)
        content.set_margin_start(10)
        content.set_margin_end(10)
        name = Gtk.Label(label=sensor.label)
        name.add_css_class("heading")
        name.set_xalign(0.0)
        content.append(name)
        detail = Gtk.Label(
            label=(f"{len(sensor.gyro_axes)} gyro · {len(sensor.accelerometer_axes)} accel axes")
        )
        detail.add_css_class("caption")
        detail.add_css_class("dim-label")
        detail.set_xalign(0.0)
        content.append(detail)
        action = Gtk.Label(label="Motion passthrough")
        action.add_css_class("caption")
        action.add_css_class("button-card-action-label")
        action.set_halign(Gtk.Align.FILL)
        action.set_xalign(0.0)
        action.set_hexpand(True)
        action.set_single_line_mode(True)
        action.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        action.set_width_chars(1)
        action.set_max_width_chars(self._mapping_action_summary_chars())
        content.append(action)
        action_right_click = Gtk.GestureClick()
        action_right_click.set_button(Gdk.BUTTON_SECONDARY)
        action_right_click.connect(
            "pressed",
            self.callbacks.on_motion_action_right_clicked,
            sensor,
        )
        action.add_controller(action_right_click)
        button._action_label = action
        button._name_label = name
        button._button_id = sensor.id
        button._protected = False
        button._motion_source = True
        button.set_size_request(self._button_card_width(), -1)
        button.set_child(content)
        button.connect("clicked", self.callbacks.on_motion_mapping_clicked, sensor)
        return button
