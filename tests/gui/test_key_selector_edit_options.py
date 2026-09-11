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


@pytest.mark.parametrize("template_axis", [False, True])
def test_axis_commit_saves_displayed_value(monkeypatch, template_axis):
    from gi.repository import Gtk

    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.core import ActionType
    from keymasq.common.virtual_device_templates import (
        LOGITECH_EXTREME_3D_TEMPLATE_ID,
        VirtualDeviceConfig,
        VirtualDeviceInstance,
    )
    from keymasq.gui.widgets.key_selector import tabs
    from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog
    from tests.gui.support import collect_widgets

    config = (
        VirtualDeviceConfig(
            devices=(
                VirtualDeviceInstance(
                    "flight-stick",
                    LOGITECH_EXTREME_3D_TEMPLATE_ID,
                ),
            )
        )
        if template_axis
        else VirtualDeviceConfig()
    )
    monkeypatch.setattr(tabs, "virtual_device_config", lambda: config)
    monkeypatch.setattr(tabs, "virtual_gamepad_count", lambda: 1)
    original = MappingAction(
        action_type=ActionType.GAMEPAD_AXIS,
        target="abs_x",
        axis_value=100,
        rapidfire_enabled=True,
        output_id="flight-stick" if template_axis else None,
    )
    dialog = KeySelectorDialog(Gtk.Box(), "Source", original)
    selected = []
    dialog.connect("key-selected", lambda _dialog, action: selected.append(action))
    assert not dialog.save_changes_btn.get_visible()
    if template_axis:
        picker = dialog._template_gamepad_picker.get_first_child()
        picker.value_spin.set_value(200)
        label = "Map axis"
    else:
        dialog.gamepad_axis_value.set_value(200)
        label = "Map Analog"
    dialog.hold_spin.set_value(45)
    next(
        button
        for button in collect_widgets(dialog.stack, Gtk.Button)
        if button.get_label() == label
    ).emit("clicked")
    assert len(selected) == 1
    assert selected[0].axis_value == 200
    assert selected[0].target == "abs_x"
    assert selected[0].rapidfire_hold_ms == 45
    assert original.axis_value == 100


def test_save_disables_rapidfire_and_enables_tap():
    from gi.repository import Gtk

    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.core import ActionType
    from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog

    original = MappingAction(
        action_type=ActionType.KEYBOARD,
        target="key_a",
        rapidfire_enabled=True,
    )
    dialog = KeySelectorDialog(Gtk.Box(), "Source", original)
    results = []
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))
    dialog.rapidfire_check.set_active(False)
    dialog.tap_check.set_active(True)
    dialog.tap_spin.set_value(230)
    dialog.save_changes_btn.emit("clicked")
    assert not results[0].rapidfire_enabled
    assert results[0].tap_enabled
    assert results[0].tap_hold_ms == 230
    assert original.rapidfire_enabled


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
