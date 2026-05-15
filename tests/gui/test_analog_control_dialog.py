# ruff: noqa: F403, F405, I001
from tests.gui.support import *


def test_new_analog_control_keeps_draft_when_add_row_reselected(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.analog_control_dialog import AnalogControlDialog

    parent = Gtk.Window()
    dialog = AnalogControlDialog(parent)

    dialog.mode_dropdown.set_selected(1)
    dialog._on_template_wasd()
    assert len(dialog._thresholds) == 4

    action = MappingAction(action_type=ActionType.KEYBOARD, target="key_e")
    dialog._on_threshold_actions_selected(dialog, [action], 0)

    dialog._on_control_selected(dialog.list_box, dialog.new_control_row)

    assert dialog._current_name is None
    assert len(dialog._thresholds) == 4
    assert dialog._thresholds[0].actions == [action]


def test_saved_analog_control_keeps_action_edits_when_current_row_reselected(
    temp_config_dir,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.analog_control_dialog import AnalogControlDialog
    from keymasq.session.analog_controls import analog_control_wasd_template

    parent = Gtk.Window()
    dialog = AnalogControlDialog(parent)

    dialog.name_entry.set_text("Saved Control")
    dialog.mode_dropdown.set_selected(1)
    dialog._apply_template(analog_control_wasd_template())
    assert dialog._save_current_control() is True

    action = MappingAction(action_type=ActionType.KEYBOARD, target="key_e")
    dialog._on_threshold_actions_selected(dialog, [action], 0)

    selected_row = dialog.list_box.get_selected_row()
    assert selected_row is not None
    dialog._on_control_selected(dialog.list_box, selected_row)

    assert dialog._current_name == "Saved Control"
    assert len(dialog._thresholds) == 4
    assert dialog._thresholds[0].actions == [action]


def test_trigger_analog_control_saves_digital_only_positive_ranges(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control_dialog import AnalogControlDialog

    parent = Gtk.Window()
    dialog = AnalogControlDialog(parent)

    dialog.name_entry.set_text("Trigger Control")
    dialog.input_type_dropdown.set_selected(1)
    dialog._on_add_range_clicked()

    assert dialog.mouse_group.get_visible() is False
    assert dialog.digital_group.get_visible() is True
    assert dialog.template_group.get_visible() is False
    assert dialog._thresholds[0].axis == "x"
    assert dialog._thresholds[0].trigger_min >= 0.0
    assert dialog._save_current_control() is True

    saved = dialog.manager.get_analog_control("Trigger Control")
    assert saved is not None
    assert saved.input_type == "trigger"
    assert saved.mouse_motion.enabled is False
    assert saved.thresholds[0].axis == "x"


def test_gamepad_output_dropdown_preserves_saved_selection(
    temp_config_dir,
    monkeypatch,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    import keymasq.gui.widgets.analog_control_dialog as analog_dialog

    monkeypatch.setattr(analog_dialog, "_virtual_gamepad_count", lambda: 2)

    parent = Gtk.Window()
    dialog = analog_dialog.AnalogControlDialog(parent)
    dialog.name_entry.set_text("Route Stick")
    dialog.mode_dropdown.set_selected(2)
    dialog._gamepad_output_target_buttons["right"].set_active(True)
    assert dialog._gamepad_output_dropdown is not None
    dialog._gamepad_output_dropdown.set_selected(1)

    assert dialog._save_current_control() is True
    saved = dialog.manager.get_analog_control("Route Stick")
    assert saved is not None
    assert saved.gamepad_output.output_id == "virtual-gamepad-2"
    assert saved.gamepad_output.target == "right"

    reloaded = analog_dialog.AnalogControlDialog(parent)
    assert reloaded._current_name == "Route Stick"
    assert reloaded._selected_gamepad_output_id == "virtual-gamepad-2"
    assert reloaded._gamepad_output_target_buttons["right"].get_active() is True
    assert reloaded._gamepad_output_dropdown is not None
    assert reloaded._gamepad_output_dropdown.get_selected() == 1


def test_analog_selector_filters_controls_by_source_input_type(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.models import AnalogControlConfig
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog
    from keymasq.session.analog_controls import AnalogControlManager

    manager = AnalogControlManager()
    manager.save_analog_control(AnalogControlConfig(name="Stick Control"))
    manager.save_analog_control(AnalogControlConfig(name="Trigger Control", input_type="trigger"))

    dialog = KeySelectorDialog(
        Gtk.Window(),
        "Left Trigger",
        source_type="analog",
        analog_input_type="trigger",
    )

    assert [config.name for config in dialog._analog_control_list] == ["Trigger Control"]


def test_analog_control_dialog_groups_saved_controls_by_input_type() -> None:
    from keymasq.common.models import AnalogControlConfig
    from keymasq.gui.widgets.analog_control_dialog import _group_analog_control_names

    configs = {
        "Stick Control": AnalogControlConfig(name="Stick Control"),
        "Trigger Control": AnalogControlConfig(name="Trigger Control", input_type="trigger"),
    }

    assert _group_analog_control_names(
        ["Stick Control", "Trigger Control"],
        configs,
    ) == [
        ("Triggers", ["Trigger Control"]),
        ("Sticks", ["Stick Control"]),
    ]
