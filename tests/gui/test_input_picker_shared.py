# ruff: noqa: F403, F405, I001, E402
import gi

from tests.gui.support import *

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]


class _GamepadOwner:
    def __init__(self) -> None:
        self.clicked: list[str] = []

    def _create_key_button(
        self,
        label: str,
        evdev: str,
        width: float = 1,
        large: bool = False,
        protected: bool = False,
    ) -> Gtk.Button:
        _ = width, large, protected
        return Gtk.Button(label=label)

    def _on_gamepad_clicked(self, _button: Gtk.Button, evdev_id: str) -> None:
        self.clicked.append(evdev_id)

    def _on_gamepad_axis_clicked(
        self,
        _button: Gtk.Button,
        _target: str,
        _value: int,
    ) -> None:
        return


def _collect_buttons(widget: Gtk.Widget) -> list[Gtk.Button]:
    buttons: list[Gtk.Button] = []
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Button):
            buttons.append(child)
        buttons.extend(_collect_buttons(child))
        child = child.get_next_sibling()
    return buttons


def test_gamepad_face_buttons_emit_positional_targets() -> None:
    from keymasq.gui.widgets.input_picker_shared import build_gamepad_tab
    from keymasq.gui.widgets.input_picker_shared import GAMEPAD_BUTTONS

    expected = {
        "A": "btn_south",
        "B": "btn_east",
        "X": "btn_north",
        "Y": "btn_west",
    }
    owner = _GamepadOwner()
    tab = build_gamepad_tab(owner)
    face_buttons = {
        button.get_label() or "": button
        for button in _collect_buttons(tab)
        if (button.get_label() or "") in expected
    }

    assert set(face_buttons) == set(expected)
    assert {label: GAMEPAD_BUTTONS[label] for label in expected} == expected

    for label, _evdev_id in expected.items():
        face_buttons[label].emit("clicked")

    assert owner.clicked == list(expected.values())


class _KeyboardOwner:
    def __init__(self) -> None:
        self.clicked: list[str] = []

    def _create_key_button(
        self,
        label: str,
        evdev: str,
        width: float = 1,
        large: bool = False,
        protected: bool = False,
    ) -> Gtk.Button:
        _ = width, large, protected
        button = Gtk.Button(label=label)
        button._evdev_name = evdev
        return button

    def _on_keyboard_clicked(self, _button: Gtk.Button | None, evdev_id: str) -> None:
        self.clicked.append(evdev_id)


def test_keyboard_grid_places_keys_on_quarter_key_columns() -> None:
    from keymasq.gui.widgets.input_picker_shared import KeyCap, build_keyboard_grid

    grid = build_keyboard_grid(
        _KeyboardOwner(),
        [
            [KeyCap("Tab", "key_tab", 1.5), KeyCap("Enter", "key_enter", 1.25, height=2, gap=0.25)],
            [KeyCap("Caps", "key_capslock", 1.75)],
        ],
    )
    enter = next(b for b in _collect_buttons(grid) if b._evdev_name == "key_enter")

    assert grid.query_child(enter) == (7, 0, 5, 2)
    assert enter.get_tooltip_text() == "KEY_ENTER"


def test_key_legends_relabel_keys_and_restore_fixed_names() -> None:
    import evdev

    from keymasq.common.keyboard_layouts import KeyLegend
    from keymasq.gui.widgets.input_picker_shared import (
        KeyCap,
        apply_key_legends,
        build_keyboard_grid,
    )

    owner = _KeyboardOwner()
    grid = build_keyboard_grid(
        owner,
        [[KeyCap("Y", "key_y"), KeyCap("-", "key_minus"), KeyCap("Tab", "key_tab")]],
    )
    buttons = {b._evdev_name: b for b in _collect_buttons(grid)}

    apply_key_legends(
        grid,
        {evdev.ecodes.KEY_Y: KeyLegend("Z"), evdev.ecodes.KEY_MINUS: KeyLegend("ß", "?")},
    )
    assert buttons["key_y"].get_label() == "Z"
    assert buttons["key_minus"].get_label() == "?\nß"
    assert buttons["key_minus"].has_css_class("dual-legend")
    assert buttons["key_tab"].get_label() == "Tab"

    apply_key_legends(grid, {})
    assert buttons["key_y"].get_label() == "Y"
    assert buttons["key_minus"].get_label() == "-"
    assert not buttons["key_minus"].has_css_class("dual-legend")

    buttons["key_y"].emit("clicked")
    assert owner.clicked == ["key_y"]
