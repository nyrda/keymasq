from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk, Pango  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.core import ActionType
from keymasq.gui.widgets.action_labels import describe_mapping_action_compact
from keymasq.gui.widgets.action_payloads import mapping_action_from_payload
from keymasq.gui.widgets.device_control_layout import group_pointer_controls, label_sort_key

from .model import Payload, ellipsize_middle, int_or_none, list_of_dicts, text

MAPPING_CARD_WIDTH = 122
MAPPING_NAME_CHARS = 14
MAPPING_ACTION_CHARS = 15
MAPPING_GRID_GAP = 6

KEYBOARD_SECTIONS: tuple[tuple[str, tuple[tuple[str, ...], ...], bool, int], ...] = (
    (
        "Number Keys",
        (
            ("key_1", "key_2", "key_3", "key_4", "key_5", "key_minus"),
            ("key_6", "key_7", "key_8", "key_9", "key_0", "key_equal"),
        ),
        True,
        6,
    ),
    (
        "Keyboard (Left)",
        (
            ("key_esc", "key_grave"),
            ("key_tab", "key_q", "key_w", "key_e", "key_r", "key_t"),
            ("key_capslock", "key_a", "key_s", "key_d", "key_f", "key_g"),
            ("key_leftshift", "key_z", "key_x", "key_c", "key_v", "key_b"),
            ("key_leftctrl", "key_leftmeta", "key_leftalt", "key_space"),
        ),
        True,
        6,
    ),
    (
        "Keyboard (Right)",
        (
            ("key_backspace", "key_y", "key_u", "key_i", "key_o", "key_p"),
            ("key_enter", "key_h", "key_j", "key_k", "key_l"),
            ("key_n", "key_m", "key_rightshift"),
            ("key_rightmeta", "key_rightalt", "key_rightctrl"),
        ),
        False,
        6,
    ),
    (
        "Symbols",
        (
            (
                "key_leftbrace",
                "key_rightbrace",
                "key_backslash",
                "key_semicolon",
                "key_apostrophe",
            ),
            ("key_comma", "key_dot", "key_slash"),
        ),
        False,
        5,
    ),
    (
        "F Row",
        (
            ("key_f1", "key_f2", "key_f3", "key_f4", "key_f5", "key_f6"),
            ("key_f7", "key_f8", "key_f9", "key_f10", "key_f11", "key_f12"),
        ),
        False,
        6,
    ),
    (
        "Navigation",
        (
            ("key_up", "key_down"),
            ("key_left", "key_right"),
            ("key_sysrq", "key_scrolllock", "key_pause"),
            ("key_insert", "key_home", "key_pageup"),
            ("key_delete", "key_end", "key_pagedown"),
        ),
        False,
        4,
    ),
    (
        "Special",
        (
            ("key_numlock", "key_kpslash", "key_kpasterisk", "key_kpminus"),
            ("key_kp7", "key_kp8", "key_kp9", "key_kpplus"),
            ("key_kp4", "key_kp5", "key_kp6"),
            ("key_kp1", "key_kp2", "key_kp3", "key_kpenter"),
            ("key_kp0", "key_kpdot"),
        ),
        False,
        4,
    ),
)


class MappingMixin:
    def _render_mapping(self: Any, snapshot: Payload) -> None:
        self._clear_box(self._mapping_box)
        self._control_widgets.clear()
        buttons = list_of_dicts(snapshot.get("buttons"))
        analog_inputs = list_of_dicts(snapshot.get("analog_inputs"))

        title = Gtk.Label(label="Resolved Mapping")
        title.add_css_class("button-section-title")
        title.set_halign(Gtk.Align.START)
        self._mapping_box.append(title)

        profiles = snapshot.get("active_profiles")
        profile_label = Gtk.Label(
            label=", ".join(str(name) for name in profiles)
            if isinstance(profiles, list) and profiles
            else "No active profiles"
        )
        profile_label.add_css_class("caption")
        profile_label.add_css_class("dim-label")
        profile_label.set_halign(Gtk.Align.START)
        self._mapping_box.append(profile_label)

        if self._device_kind == "keyboard":
            self._render_keyboard_buttons(buttons)
        else:
            self._render_pointer_buttons(buttons)

        if analog_inputs:
            analog_label = Gtk.Label(label="Analog Inputs")
            analog_label.add_css_class("button-section-title")
            analog_label.set_halign(Gtk.Align.START)
            analog_label.set_margin_top(8)
            self._mapping_box.append(analog_label)
            grid = Gtk.Grid(column_spacing=MAPPING_GRID_GAP, row_spacing=MAPPING_GRID_GAP)
            grid.set_halign(Gtk.Align.START)
            for index, analog in enumerate(analog_inputs):
                widget = self._create_control_card(analog)
                grid.attach(widget, index % 2, index // 2, 1, 1)
                self._control_widgets[text(analog.get("id"))] = widget
            self._mapping_box.append(grid)

    def _render_keyboard_buttons(self: Any, buttons: list[Payload]) -> None:
        buttons_by_id = {text(button.get("id")): button for button in buttons}
        used_ids: set[str] = set()
        for title, layout_rows, expanded, max_cols in KEYBOARD_SECTIONS:
            grid = Gtk.Grid(column_spacing=MAPPING_GRID_GAP, row_spacing=MAPPING_GRID_GAP)
            grid.set_halign(Gtk.Align.START)
            for row_index, row_items in enumerate(layout_rows):
                for col_index, button_id in enumerate(row_items):
                    button = buttons_by_id.get(button_id)
                    if button is None:
                        spacer = Gtk.Box()
                        spacer.set_size_request(MAPPING_CARD_WIDTH, -1)
                        grid.attach(spacer, col_index, row_index, 1, 1)
                        continue
                    widget = self._create_control_card(button)
                    grid.attach(widget, col_index, row_index, 1, 1)
                    self._control_widgets[button_id] = widget
                    used_ids.add(button_id)
                for col_index in range(len(row_items), max_cols):
                    spacer = Gtk.Box()
                    spacer.set_size_request(MAPPING_CARD_WIDTH, -1)
                    grid.attach(spacer, col_index, row_index, 1, 1)
            expander = Gtk.Expander(label=title)
            expander.add_css_class("device-section-expander")
            expander.set_expanded(expanded)
            expander.set_child(grid)
            self._mapping_box.append(expander)

        extras = [button for button in buttons if text(button.get("id")) not in used_ids]
        if extras:
            self._append_flow_section("Extra Keys", extras, is_keyboard=True)

    def _render_pointer_buttons(self: Any, buttons: list[Payload]) -> None:
        main, scroll, side, extra = group_pointer_controls(
            buttons,
            id_for_control=lambda button: text(button.get("id")),
        )
        for title, group in (
            ("Extra Buttons", extra),
            ("Main Buttons", main),
            ("Scroll", scroll),
            ("Side Buttons", side),
        ):
            if group:
                self._append_flow_section(title, group, is_keyboard=False)

    def _append_flow_section(
        self: Any,
        title: str,
        controls: list[Payload],
        *,
        is_keyboard: bool,
    ) -> None:
        label = Gtk.Label(label=title)
        label.add_css_class("button-section-title")
        label.set_halign(Gtk.Align.START)
        self._mapping_box.append(label)

        grid = Gtk.Grid(column_spacing=MAPPING_GRID_GAP, row_spacing=MAPPING_GRID_GAP)
        grid.set_halign(Gtk.Align.START)
        max_cols = 4 if is_keyboard else 3
        sorted_controls = sorted(controls, key=lambda item: label_sort_key(item.get("label")))
        for index, control in enumerate(sorted_controls):
            widget = self._create_control_card(control)
            grid.attach(widget, index % max_cols, index // max_cols, 1, 1)
            self._control_widgets[text(control.get("id"))] = widget
        self._mapping_box.append(grid)

    def _create_control_card(
        self: Any,
        control: Payload,
    ) -> Gtk.Button:
        action = control.get("action")
        mapped = isinstance(action, dict)
        button = Gtk.Button()
        button.add_css_class("card")
        button.add_css_class("inspector-control-card")
        button.add_css_class("button-card-mapped-active" if mapped else "button-card-passthrough")
        button.set_focusable(False)
        button.set_size_request(MAPPING_CARD_WIDTH, -1)
        button.set_halign(Gtk.Align.START)
        button.set_hexpand(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(7)
        horizontal_padding = 7
        box.set_margin_start(horizontal_padding)
        box.set_margin_end(horizontal_padding)
        box.set_size_request(MAPPING_CARD_WIDTH - (horizontal_padding * 2), -1)

        name_label = Gtk.Label(label=text(control.get("label"), text(control.get("id"))))
        name_label.add_css_class("heading")
        name_label.set_xalign(0.0)
        name_label.set_ellipsize(Pango.EllipsizeMode.END)
        name_label.set_width_chars(1)
        name_label.set_max_width_chars(MAPPING_NAME_CHARS)
        box.append(name_label)

        action_text = self._action_label(action, control) or self._passthrough_label(control)
        action_label = Gtk.Label(label=ellipsize_middle(action_text, MAPPING_ACTION_CHARS))
        action_label.add_css_class("caption")
        action_label.add_css_class("button-card-action-label")
        action_label.set_xalign(0.0)
        action_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        action_label.set_width_chars(1)
        action_label.set_max_width_chars(MAPPING_ACTION_CHARS)
        if not mapped:
            action_label.add_css_class("dim-label")
        box.append(action_label)

        profile_name = text(control.get("profile_name"))
        tooltip = action_text
        if profile_name:
            tooltip = f"{tooltip}\nProfile: {profile_name}"
        source = text(control.get("source"))
        if source:
            tooltip = f"{tooltip}\nSource: {source}"
        button.set_tooltip_text(tooltip)
        button.set_child(box)
        return button

    def _action_label(self: Any, action: object, control: Payload) -> str | None:
        mapping = mapping_action_from_payload(action)
        if mapping is None:
            return None
        if mapping.action_type == ActionType.PASSTHROUGH:
            return self._passthrough_label(control)
        return describe_mapping_action_compact(mapping, include_state=True)

    def _passthrough_label(self: Any, control: Payload) -> str:
        if text(control.get("kind")) == "analog":
            if text(control.get("type")) == "axis":
                return "Axis passthrough"
            return "Analog passthrough"
        evdev = text(control.get("evdev"))
        evdev_value = int_or_none(control.get("evdev_value"))
        if evdev == "rel_wheel" and evdev_value is not None:
            return "Scroll Up" if evdev_value > 0 else "Scroll Down"
        if evdev == "rel_hwheel" and evdev_value is not None:
            return "Scroll Right" if evdev_value > 0 else "Scroll Left"
        return f"Passthrough: {evdev or text(control.get('id'), '?')}"
