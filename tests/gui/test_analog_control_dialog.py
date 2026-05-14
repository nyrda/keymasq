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
