import pytest

pytest.importorskip("gi")


@pytest.mark.parametrize(
    ("kind", "target", "tab"),
    [
        ("keyboard", "key_space", "keyboard"),
        ("keyboard", "key_kp7", "navigation"),
        ("mouse", "btn_left", "mouse"),
        ("gamepad", "btn_south", "gamepad"),
    ],
)
@pytest.mark.parametrize("macro_edit", [False, True])
def test_save_existing_target_options(kind, target, tab, macro_edit):
    from gi.repository import Gtk

    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.core import ActionType
    from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog

    original = MappingAction(
        action_type=ActionType(kind),
        target=target,
        rapidfire_enabled=True,
        rapidfire_hold_ms=20,
        rapidfire_wait_ms=30,
    )
    dialog = KeySelectorDialog(
        Gtk.Box(),
        "Source",
        original,
        initial_tab=tab,
        allow_tap=not macro_edit,
        allow_gamepad_axis_rapidfire=not macro_edit,
        allowed_tabs={tab} if macro_edit else None,
    )
    results = []
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))

    def marked_targets(widget):
        marked = []
        if isinstance(widget, Gtk.Button) and widget.has_css_class("bound-target"):
            marked.append(widget._evdev_name)
        child = widget.get_first_child()
        while child is not None:
            marked.extend(marked_targets(child))
            child = child.get_next_sibling()
        return marked

    assert target in marked_targets(dialog.stack)
    assert dialog.save_changes_btn.get_visible()
    dialog.hold_spin.set_value(45)
    dialog.wait_spin.set_value(65)
    assert original.rapidfire_hold_ms == 20
    assert results == []
    dialog.save_changes_btn.emit("clicked")

    assert len(results) == 1
    saved = results[0]
    assert saved.target == target
    assert saved.action_type == original.action_type
    assert saved.rapidfire_enabled
    assert saved.rapidfire_hold_ms == 45
    assert saved.rapidfire_wait_ms == 65
    assert original.rapidfire_wait_ms == 30


def test_cancel_option_changes_keeps_mapping():
    from gi.repository import Gtk

    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.core import ActionType
    from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog

    original = MappingAction(action_type=ActionType.KEYBOARD, target="key_a")
    dialog = KeySelectorDialog(Gtk.Box(), "Source", original)
    results = []
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))
    dialog.rapidfire_check.set_active(True)
    dialog.hold_spin.set_value(80)
    dialog.close()
    assert not original.rapidfire_enabled
    assert results == []
