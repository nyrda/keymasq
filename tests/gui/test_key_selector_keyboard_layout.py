import pytest

pytest.importorskip("gi")

from keymasq.common import xkb  # noqa: E402

requires_xkb = pytest.mark.skipif(not xkb.is_available(), reason="libxkbcommon unavailable")


def _keyboard_buttons(widget):
    from gi.repository import Gtk

    buttons = {}
    if isinstance(widget, Gtk.Button) and hasattr(widget, "_keycap_label"):
        buttons[widget._evdev_name] = widget
    child = widget.get_first_child()
    while child is not None:
        buttons.update(_keyboard_buttons(child))
        child = child.get_next_sibling()
    return buttons


def test_keyboard_rows_follow_the_layout_country() -> None:
    from keymasq.gui.widgets.key_selector.targets import (
        ANSI_KEYBOARD_ROWS,
        ISO_KEYBOARD_ROWS,
        keyboard_rows_for_layout,
    )

    assert keyboard_rows_for_layout(None) is ANSI_KEYBOARD_ROWS
    assert keyboard_rows_for_layout("us(dvorak)") is ANSI_KEYBOARD_ROWS
    assert keyboard_rows_for_layout("not a layout") is ANSI_KEYBOARD_ROWS
    assert keyboard_rows_for_layout("de(nodeadkeys)") is ISO_KEYBOARD_ROWS
    assert keyboard_rows_for_layout("gb") is ISO_KEYBOARD_ROWS

    def evdev_names(rows):
        return {key.evdev_name for row in rows for key in row}

    assert evdev_names(ISO_KEYBOARD_ROWS) - evdev_names(ANSI_KEYBOARD_ROWS) == {"key_102nd"}
    assert evdev_names(ANSI_KEYBOARD_ROWS) <= evdev_names(ISO_KEYBOARD_ROWS)
    for rows in (ANSI_KEYBOARD_ROWS, ISO_KEYBOARD_ROWS):
        # Rows 1 to 5 are as wide as the function row; the ISO Enter covers two rows.
        for row in rows:
            assert sum(key.gap + key.width for key in row) in {13.75, 15}


@requires_xkb
def test_key_selector_keyboard_follows_configured_layout(monkeypatch) -> None:
    from gi.repository import Gtk

    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.core import ActionType
    from keymasq.gui.session_client import GuiTaskResult
    from keymasq.gui.widgets import type_macro_layout
    from keymasq.gui.widgets.key_selector import tabs
    from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog

    monkeypatch.setattr(type_macro_layout, "session_request_async", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        tabs,
        "run_gui_task",
        lambda worker, callback: callback(GuiTaskResult(value=worker())),
    )
    dialog = KeySelectorDialog(
        Gtk.Box(),
        "Source",
        MappingAction(action_type=ActionType.KEYBOARD, target="key_y"),
        allowed_tabs={"keyboard"},
    )
    buttons = _keyboard_buttons(dialog._keyboard_grid)
    assert "key_102nd" not in buttons
    assert buttons["key_y"].get_label() == "Y"

    dialog._keyboard_layout_state._on_settings_changed({"keyboard_layout": "de"})
    buttons = _keyboard_buttons(dialog._keyboard_grid)
    assert buttons["key_y"].get_label() == "Z"
    assert buttons["key_y"].has_css_class("bound-target")
    assert buttons["key_102nd"].get_label() == ">\n<"

    # An unusable layout falls back to the US keyboard the keys then type.
    dialog._keyboard_layout_state._on_settings_changed({"keyboard_layout": "nope"})
    buttons = _keyboard_buttons(dialog._keyboard_grid)
    assert "key_102nd" not in buttons
    assert buttons["key_y"].get_label() == "Y"

    dialog.emit("closed")
    dialog._keyboard_layout_state._on_settings_changed({"keyboard_layout": "de"})
    assert "key_102nd" not in _keyboard_buttons(dialog._keyboard_grid)
