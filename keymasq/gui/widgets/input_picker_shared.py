from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gtk  # pyright: ignore[reportAttributeAccessIssue]


def _get_gamepad_svg_path() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "assets", "gamepad.svg")


def build_keyboard_tab(
    owner,
    *,
    keyboard_layout: list[list[str]],
    key_to_evdev: Mapping[str, str | None],
    key_widths: Mapping[str, float],
) -> Gtk.ScrolledWindow:
    scrolled = Gtk.ScrolledWindow()
    scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    box.set_margin_top(8)
    box.set_margin_bottom(8)
    box.set_margin_start(8)
    box.set_margin_end(8)

    for row in keyboard_layout:
        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        row_box.set_halign(Gtk.Align.CENTER)

        for key in row:
            evdev_name = key_to_evdev.get(key)
            if evdev_name is None:
                continue

            width = key_widths.get(key, 1)
            btn = owner._create_key_button(key, evdev_name, width=width)
            btn.connect("clicked", owner._on_keyboard_clicked, evdev_name)
            row_box.append(btn)

        box.append(row_box)

    scrolled.set_child(box)
    return scrolled


def build_navigation_tab(
    owner,
    *,
    f_extra: list[str],
) -> Gtk.Box:
    square_size = 50

    outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
    outer.set_margin_top(16)
    outer.set_margin_bottom(16)
    outer.set_margin_start(16)
    outer.set_margin_end(16)
    outer.set_halign(Gtk.Align.CENTER)
    outer.set_valign(Gtk.Align.CENTER)

    left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    left_box.set_valign(Gtk.Align.CENTER)

    f_extra_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

    owner.f_dropdown = Gtk.DropDown()
    f_model = Gtk.StringList()
    for f in f_extra:
        f_model.append(f)
    owner.f_dropdown.set_model(f_model)
    f_extra_box.append(owner.f_dropdown)

    f_btn = Gtk.Button(label=f"Map {f_extra[0]}")
    f_btn.add_css_class("suggested-action")
    f_btn.connect("clicked", owner._on_f_key_selected)
    owner.f_dropdown.connect("notify::selected", owner._on_f_dropdown_changed, f_btn)
    f_extra_box.append(f_btn)

    left_box.append(f_extra_box)

    nav_grid = Gtk.Grid()
    nav_grid.set_column_spacing(4)
    nav_grid.set_row_spacing(4)
    nav_grid.set_halign(Gtk.Align.CENTER)

    nav_keys = [
        (0, 0, "Ins", "key_insert"),
        (1, 0, "Home", "key_home"),
        (2, 0, "PgUp", "key_pageup"),
        (0, 1, "Del", "key_delete"),
        (1, 1, "End", "key_end"),
        (2, 1, "PgDn", "key_pagedown"),
    ]
    for col, row, label, evdev_id in nav_keys:
        btn = owner._create_key_button(label, evdev_id)
        btn.add_css_class("square-key-button")
        btn.set_can_shrink(True)
        btn.set_size_request(square_size, square_size)
        btn.connect("clicked", owner._on_keyboard_clicked, evdev_id)
        nav_grid.attach(btn, col, row, 1, 1)

    left_box.append(nav_grid)

    arrows_grid = Gtk.Grid()
    arrows_grid.set_column_spacing(4)
    arrows_grid.set_row_spacing(4)
    arrows_grid.set_halign(Gtk.Align.CENTER)

    up_btn = owner._create_key_button("↑", "key_up")
    up_btn.add_css_class("square-key-button")
    up_btn.set_can_shrink(True)
    up_btn.set_size_request(square_size, square_size)
    up_btn.connect("clicked", owner._on_keyboard_clicked, "key_up")
    arrows_grid.attach(up_btn, 1, 0, 1, 1)

    arrow_keys = [("←", "key_left"), ("↓", "key_down"), ("→", "key_right")]
    for col, (label, evdev_id) in enumerate(arrow_keys):
        btn = owner._create_key_button(label, evdev_id)
        btn.add_css_class("square-key-button")
        btn.set_can_shrink(True)
        btn.set_size_request(square_size, square_size)
        btn.connect("clicked", owner._on_keyboard_clicked, evdev_id)
        arrows_grid.attach(btn, col, 1, 1, 1)

    left_box.append(arrows_grid)
    outer.append(left_box)

    sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
    outer.append(sep)

    right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    right_box.set_valign(Gtk.Align.CENTER)

    numpad_label = Gtk.Label(label="Numpad")
    numpad_label.add_css_class("dim-label")
    numpad_label.set_halign(Gtk.Align.CENTER)
    right_box.append(numpad_label)

    right_box.append(build_numpad_grid(owner))
    outer.append(right_box)

    return outer


def build_media_tab(
    owner,
    *,
    media_groups: Sequence[tuple[str, Sequence[tuple[str, str, str]]]],
) -> Gtk.ScrolledWindow:
    scrolled = Gtk.ScrolledWindow()
    scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    outer.set_margin_top(12)
    outer.set_margin_bottom(12)
    outer.set_margin_start(12)
    outer.set_margin_end(12)
    outer.set_halign(Gtk.Align.CENTER)
    outer.set_valign(Gtk.Align.CENTER)

    for title, buttons in media_groups:
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        section.set_halign(Gtk.Align.CENTER)

        title_label = Gtk.Label(label=title)
        title_label.add_css_class("button-section-title")
        title_label.set_halign(Gtk.Align.START)
        section.append(title_label)

        grid = Gtk.Grid()
        grid.set_column_spacing(6)
        grid.set_row_spacing(6)
        grid.set_column_homogeneous(True)
        grid.set_halign(Gtk.Align.CENTER)

        for index, (label, evdev_id, icon_name) in enumerate(buttons):
            btn = _create_media_key_button(label, evdev_id, icon_name)
            btn.connect("clicked", owner._on_keyboard_clicked, evdev_id)
            grid.attach(btn, index % 4, index // 4, 1, 1)

        section.append(grid)
        outer.append(section)

    scrolled.set_child(outer)
    return scrolled


def _create_media_key_button(label: str, evdev_id: str, icon_name: str) -> Gtk.Button:
    btn = Gtk.Button()
    btn.add_css_class("key-button")
    btn.add_css_class("media-key-button")
    btn.set_tooltip_text(evdev_id)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
    box.add_css_class("media-key-button-content")
    box.set_halign(Gtk.Align.CENTER)
    box.set_valign(Gtk.Align.CENTER)

    icon = Gtk.Image.new_from_icon_name(icon_name)
    icon.add_css_class("media-key-icon")
    icon.set_pixel_size(18)
    box.append(icon)

    label_widget = Gtk.Label(label=label)
    label_widget.add_css_class("media-key-label")
    label_widget.set_halign(Gtk.Align.CENTER)
    label_widget.set_justify(Gtk.Justification.CENTER)
    label_widget.set_wrap(True)
    box.append(label_widget)

    btn.set_child(box)
    btn.set_size_request(112, 58)
    btn._evdev_name = evdev_id
    return btn


def build_numpad_grid(owner) -> Gtk.Fixed:
    grid = Gtk.Fixed()

    key_size, gap = 50, 4

    def attach_key(
        label: str,
        evdev: str,
        col: int,
        row: int,
        col_span: int = 1,
        row_span: int = 1,
        tooltip: str | None = None,
    ) -> None:
        x = col * (key_size + gap)
        y = row * (key_size + gap)
        w = key_size * col_span + gap * (col_span - 1)
        h = key_size * row_span + gap * (row_span - 1)
        btn = Gtk.Button(label=label)
        btn.add_css_class("key-button")
        btn.add_css_class("square-key-button")
        btn.set_can_shrink(True)
        btn.set_size_request(w, h)
        if tooltip:
            btn.set_tooltip_text(tooltip)
        btn.connect("clicked", owner._on_keyboard_clicked, evdev)
        grid.put(btn, x, y)

    attach_key("Num", "key_numlock", 0, 0, tooltip="Num Lock")
    attach_key("/", "key_kpslash", 1, 0)
    attach_key("*", "key_kpasterisk", 2, 0)
    attach_key("-", "key_kpminus", 3, 0)
    attach_key("7", "key_kp7", 0, 1)
    attach_key("8", "key_kp8", 1, 1)
    attach_key("9", "key_kp9", 2, 1)
    attach_key("+", "key_kpplus", 3, 1, row_span=2)
    attach_key("4", "key_kp4", 0, 2)
    attach_key("5", "key_kp5", 1, 2)
    attach_key("6", "key_kp6", 2, 2)
    attach_key("1", "key_kp1", 0, 3)
    attach_key("2", "key_kp2", 1, 3)
    attach_key("3", "key_kp3", 2, 3)
    attach_key("↵", "key_kpenter", 3, 3, row_span=2)
    attach_key("0", "key_kp0", 0, 4, col_span=2)
    attach_key(".", "key_kpdot", 2, 4)

    grid.set_size_request(key_size * 4 + gap * 3, key_size * 5 + gap * 4)

    return grid


def build_mouse_tab(owner) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
    box.set_margin_top(16)
    box.set_margin_bottom(16)
    box.set_margin_start(16)
    box.set_margin_end(16)
    box.set_valign(Gtk.Align.CENTER)

    btn_row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    btn_row1.set_halign(Gtk.Align.CENTER)

    btn_label1 = Gtk.Label(label="Buttons:")
    btn_row1.append(btn_label1)

    for label, evdev_id in [
        ("Left", "btn_left"),
        ("Middle", "btn_middle"),
        ("Right", "btn_right"),
    ]:
        btn = owner._create_key_button(label, evdev_id)
        btn.connect("clicked", owner._on_mouse_clicked, evdev_id)
        btn_row1.append(btn)

    box.append(btn_row1)

    btn_row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    btn_row2.set_halign(Gtk.Align.CENTER)

    extras_label = Gtk.Label(label="Extras:")
    btn_row2.append(extras_label)

    for label, evdev_id in [("Forward", "btn_extra"), ("Back", "btn_side")]:
        btn = owner._create_key_button(label, evdev_id)
        btn.connect("clicked", owner._on_mouse_clicked, evdev_id)
        btn_row2.append(btn)

    box.append(btn_row2)

    scroll_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    scroll_row.set_halign(Gtk.Align.CENTER)

    scroll_label = Gtk.Label(label="Scroll:")
    scroll_row.append(scroll_label)

    for label, evdev_id in [
        ("↑", "rel_wheel:1"),
        ("↓", "rel_wheel:-1"),
        ("←", "rel_hwheel:-1"),
        ("→", "rel_hwheel:1"),
    ]:
        btn = owner._create_key_button(label, evdev_id)
        btn.connect("clicked", owner._on_mouse_clicked, evdev_id)
        scroll_row.append(btn)

    box.append(scroll_row)

    return box


def build_gamepad_tab(owner) -> Gtk.Box:
    outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
    outer.set_margin_top(12)
    outer.set_margin_bottom(12)
    outer.set_margin_start(12)
    outer.set_margin_end(12)
    outer.set_halign(Gtk.Align.CENTER)
    outer.set_valign(Gtk.Align.CENTER)

    outer.append(_build_gamepad_left_col(owner))
    outer.append(_build_gamepad_center_col(owner))
    outer.append(_build_gamepad_right_col(owner))

    return outer


def _build_gamepad_left_col(owner) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    box.set_valign(Gtk.Align.CENTER)

    for label, evdev_id in [("LT", "btn_tl2"), ("LB", "btn_tl")]:
        btn = owner._create_key_button(label, evdev_id, width=2)
        btn.connect("clicked", owner._on_gamepad_clicked, evdev_id)
        box.append(btn)

    ls_btn = owner._create_key_button("LS", "btn_thumbl", width=2)
    ls_btn.connect("clicked", owner._on_gamepad_clicked, "btn_thumbl")
    box.append(ls_btn)

    dpad = Gtk.Grid()
    dpad.set_column_spacing(4)
    dpad.set_row_spacing(4)
    dpad.set_halign(Gtk.Align.CENTER)

    for gc, gr, label, evdev_id in [
        (1, 0, "↑", "btn_dpad_up"),
        (0, 1, "←", "btn_dpad_left"),
        (2, 1, "→", "btn_dpad_right"),
        (1, 2, "↓", "btn_dpad_down"),
    ]:
        btn = owner._create_key_button(label, evdev_id)
        btn.connect("clicked", owner._on_gamepad_clicked, evdev_id)
        dpad.attach(btn, gc, gr, 1, 1)

    box.append(dpad)
    return box


def _build_gamepad_center_col(owner) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    box.set_valign(Gtk.Align.CENTER)
    box.set_hexpand(True)

    svg_pic = Gtk.Picture()
    svg_pic.set_halign(Gtk.Align.CENTER)
    svg_pic.set_can_shrink(True)
    svg_pic.set_size_request(260, 195)

    try:
        texture = Gdk.Texture.new_from_filename(_get_gamepad_svg_path())
        svg_pic.set_paintable(texture)
    except Exception:
        pass

    box.append(svg_pic)

    center_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    center_btns.set_halign(Gtk.Align.CENTER)

    for label, evdev_id in [
        ("Select", "btn_select"),
        ("Guide", "btn_mode"),
        ("Start", "btn_start"),
    ]:
        btn = owner._create_key_button(label, evdev_id, width=1.5)
        btn.connect("clicked", owner._on_gamepad_clicked, evdev_id)
        center_btns.append(btn)

    box.append(center_btns)
    return box


def _build_gamepad_right_col(owner) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    box.set_valign(Gtk.Align.CENTER)

    for label, evdev_id in [("RT", "btn_tr2"), ("RB", "btn_tr")]:
        btn = owner._create_key_button(label, evdev_id, width=2)
        btn.connect("clicked", owner._on_gamepad_clicked, evdev_id)
        box.append(btn)

    face = Gtk.Grid()
    face.set_column_spacing(4)
    face.set_row_spacing(4)
    face.set_halign(Gtk.Align.CENTER)

    for gc, gr, label, evdev_id in [
        (1, 0, "Y", "btn_west"),
        (0, 1, "X", "btn_north"),
        (2, 1, "B", "btn_east"),
        (1, 2, "A", "btn_south"),
    ]:
        btn = owner._create_key_button(label, evdev_id)
        btn.connect("clicked", owner._on_gamepad_clicked, evdev_id)
        face.attach(btn, gc, gr, 1, 1)

    box.append(face)

    rs_btn = owner._create_key_button("RS", "btn_thumbr", width=2)
    rs_btn.connect("clicked", owner._on_gamepad_clicked, "btn_thumbr")
    box.append(rs_btn)

    return box
