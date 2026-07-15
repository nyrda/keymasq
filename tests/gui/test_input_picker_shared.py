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
