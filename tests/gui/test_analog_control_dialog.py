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


def test_new_analog_control_output_deadzone_defaults_to_zero(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control_dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())

    assert dialog.gamepad_output_deadzone_row.get_value() == 0


def test_analog_control_dialog_docs_button_links_to_analog_controls_docs(
    temp_config_dir,
    monkeypatch,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    import keymasq.gui.widgets.analog_control_dialog as analog_dialog

    monkeypatch.setattr(analog_dialog, "__version__", "1.2.3")

    dialog = analog_dialog.AnalogControlDialog(Gtk.Window())

    assert dialog.analog_controls_docs_btn.get_label() == "?"
    assert (
        dialog.analog_controls_docs_btn.get_tooltip_text()
        == "Open Analog Controls documentation"
    )
    assert analog_dialog._analog_controls_docs_url() == (
        "https://keymasq.tools/docs/v1.2.3/ANALOG_CONTROLS/"
    )

    monkeypatch.setattr(analog_dialog, "__version__", "1.2.3.dev1")
    assert analog_dialog._analog_controls_docs_url() == (
        "https://keymasq.tools/docs/master/ANALOG_CONTROLS/"
    )


def test_analog_control_dialog_unsaved_close_warns_and_can_discard(
    temp_config_dir,
    monkeypatch,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, Gtk

    import keymasq.gui.widgets.analog_control_dialog as analog_dialog

    dialog = analog_dialog.AnalogControlDialog(Gtk.Window())
    closed: list[bool] = []
    alerts: list[tuple[object, object]] = []
    monkeypatch.setattr(dialog, "force_close", lambda: closed.append(True))
    monkeypatch.setattr(
        analog_dialog.Adw.AlertDialog,
        "present",
        lambda alert, parent: alerts.append((alert, parent)),
    )

    assert dialog.new_control_row is not None
    assert dialog.list_box.get_selected_row() is dialog.new_control_row
    assert dialog.get_can_close() is False

    dialog.close_btn.emit("clicked")
    assert closed == []
    assert len(alerts) == 1
    assert alerts[0][1] is dialog

    dialog._on_unsaved_close_response(alerts[0][0], "cancel")
    assert closed == []

    assert dialog._on_key_pressed(None, Gdk.KEY_Escape, 0, 0) is True
    assert closed == []
    assert len(alerts) == 2

    dialog._on_unsaved_close_response(alerts[1][0], "discard")
    assert closed == [True]
    assert dialog.get_can_close() is True


def test_analog_control_dialog_unsaved_close_save_response_saves_and_closes(
    temp_config_dir,
    monkeypatch,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    import keymasq.gui.widgets.analog_control_dialog as analog_dialog

    dialog = analog_dialog.AnalogControlDialog(Gtk.Window())
    closed: list[bool] = []
    alerts: list[tuple[object, object]] = []
    saved: list[str] = []
    monkeypatch.setattr(dialog, "force_close", lambda: closed.append(True))
    monkeypatch.setattr(
        analog_dialog.Adw.AlertDialog,
        "present",
        lambda alert, parent: alerts.append((alert, parent)),
    )
    dialog.connect("analog-control-saved", lambda _dialog, name: saved.append(name))

    dialog.name_entry.set_text("close_saved")
    dialog._request_close()

    assert closed == []
    assert len(alerts) == 1

    dialog._on_unsaved_close_response(alerts[0][0], "save")

    assert closed == [True]
    assert saved == ["close_saved"]
    assert dialog.manager.get_analog_control("close_saved") is not None
    assert dialog.get_can_close() is True


def test_axis_analog_output_exposes_curve_controls(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.models import SAME_DEVICE_OUTPUT_ID
    from keymasq.gui.widgets.analog_control_dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())
    dialog.name_entry.set_text("Axis Output")
    dialog.input_type_dropdown.set_selected(1)
    dialog.mode_dropdown.set_selected(1)
    dialog.gamepad_output_sensitivity_row.set_value(1.5)
    dialog.gamepad_output_response_curve_row.set_value(0.75)

    assert dialog.gamepad_output_sensitivity_row.get_visible() is True
    assert dialog.gamepad_output_response_curve_row.get_visible() is True
    assert dialog.gamepad_output_curve_row.get_visible() is True
    assert dialog._save_current_control() is True

    saved = dialog.manager.get_analog_control("Axis Output")
    assert saved is not None
    assert saved.gamepad_output.output_id == SAME_DEVICE_OUTPUT_ID
    assert saved.gamepad_output.sensitivity == 1.5
    assert saved.gamepad_output.response_curve == 0.75


def test_axis_mouse_movement_exposes_direction_and_curve_controls(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control_dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())
    dialog.name_entry.set_text("Axis Mouse")
    dialog.input_type_dropdown.set_selected(1)
    dialog.mode_dropdown.set_selected(2)
    dialog.speed_row.set_value(1200)
    dialog.mouse_sensitivity_row.set_value(1.5)
    dialog.mouse_response_curve_row.set_value(0.75)
    dialog._mouse_direction_buttons["vertical"].set_active(True)

    assert dialog.mouse_group.get_visible() is True
    assert dialog.speed_row.get_visible() is True
    assert dialog.speed_x_row.get_visible() is False
    assert dialog.speed_y_row.get_visible() is False
    assert dialog.mouse_direction_row.get_visible() is True
    assert dialog.invert_x_row.get_visible() is False
    assert dialog.invert_y_row.get_visible() is False
    assert dialog._save_current_control() is True

    saved = dialog.manager.get_analog_control("Axis Mouse")
    assert saved is not None
    assert saved.input_type == "axis"
    assert saved.mouse_motion.enabled is True
    assert saved.mouse_motion.speed == 1200
    assert saved.mouse_motion.sensitivity == 1.5
    assert saved.mouse_motion.response_curve == 0.75
    assert saved.mouse_motion.direction == "vertical"


def test_stick_mouse_movement_exposes_split_speed_controls(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control_dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())
    dialog.name_entry.set_text("Stick Mouse")
    dialog.speed_x_row.set_value(700)

    assert dialog.mouse_group.get_visible() is True
    assert dialog.speed_row.get_visible() is False
    assert dialog.speed_x_row.get_visible() is True
    assert dialog.speed_y_row.get_visible() is True
    assert dialog.speed_y_row.get_value() == 700
    assert dialog.mouse_direction_row.get_visible() is False
    assert dialog._save_current_control() is True

    saved = dialog.manager.get_analog_control("Stick Mouse")
    assert saved is not None
    assert saved.mouse_motion.enabled is True
    assert saved.mouse_motion.speed_x == 700
    assert saved.mouse_motion.speed_y == 700


def test_mouse_speed_spin_rows_use_500_page_steps(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control_dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())

    for row in (dialog.speed_row, dialog.speed_x_row, dialog.speed_y_row):
        adjustment = row.get_adjustment()
        assert adjustment.get_step_increment() == 25
        assert adjustment.get_page_increment() == 500


def test_mouse_movement_tuning_spin_rows_use_expected_page_steps(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control_dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())

    assert dialog.deadzone_row.get_adjustment().get_page_increment() == pytest.approx(0.05)
    assert dialog.mouse_sensitivity_row.get_adjustment().get_page_increment() == 0.25
    assert dialog.mouse_response_curve_row.get_adjustment().get_page_increment() == 0.25


def test_mouse_speed_secondary_steps_apply_500_and_keep_sync(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control_dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())

    dialog._apply_spin_secondary_step(dialog.speed_row, 1, 500)
    assert dialog.speed_row.get_value() == 1400

    dialog._apply_spin_secondary_step(dialog.speed_x_row, 1, 500)
    assert dialog.speed_x_row.get_value() == 1400
    assert dialog.speed_y_row.get_value() == 1400

    dialog._apply_spin_secondary_step(dialog.speed_y_row, -1, 500)
    assert dialog.speed_x_row.get_value() == 900
    assert dialog.speed_y_row.get_value() == 900


def test_mouse_movement_tuning_secondary_steps_apply_requested_values(
    temp_config_dir,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control_dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())

    dialog.deadzone_row.set_value(0.15)
    dialog._apply_spin_secondary_step(dialog.deadzone_row, 1, 0.05)
    assert dialog.deadzone_row.get_value() == pytest.approx(0.20)

    dialog._apply_spin_secondary_step(dialog.deadzone_row, -1, 0.05)
    assert dialog.deadzone_row.get_value() == pytest.approx(0.15)

    dialog._apply_spin_secondary_step(dialog.mouse_sensitivity_row, 1, 0.25)
    assert dialog.mouse_sensitivity_row.get_value() == 1.25

    dialog._apply_spin_secondary_step(dialog.mouse_response_curve_row, -1, 0.25)
    assert dialog.mouse_response_curve_row.get_value() == 0.75


def test_analog_output_spin_rows_use_expected_page_steps(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control_dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())

    assert dialog.gamepad_output_deadzone_row.get_adjustment().get_page_increment() == 5
    assert (
        dialog.gamepad_output_sensitivity_row.get_adjustment().get_page_increment()
        == 0.25
    )
    assert (
        dialog.gamepad_output_response_curve_row.get_adjustment().get_page_increment()
        == 0.25
    )


def test_analog_output_rest_secondary_click_resets_to_zero(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control_dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())
    dialog.gamepad_output_rest_row.set_value(100)

    dialog._apply_spin_secondary_step(
        dialog.gamepad_output_rest_row,
        1,
        None,
        reset_value=0,
    )

    assert dialog.gamepad_output_rest_row.get_value() == 0


def test_analog_output_secondary_steps_apply_requested_values(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control_dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())

    dialog.gamepad_output_deadzone_row.set_value(10)
    dialog._apply_spin_secondary_step(dialog.gamepad_output_deadzone_row, 1, 5)
    assert dialog.gamepad_output_deadzone_row.get_value() == 15

    dialog._apply_spin_secondary_step(dialog.gamepad_output_deadzone_row, -1, 5)
    assert dialog.gamepad_output_deadzone_row.get_value() == 10

    dialog._apply_spin_secondary_step(dialog.gamepad_output_sensitivity_row, 1, 0.25)
    assert dialog.gamepad_output_sensitivity_row.get_value() == 1.25

    dialog._apply_spin_secondary_step(
        dialog.gamepad_output_response_curve_row,
        -1,
        0.25,
    )
    assert dialog.gamepad_output_response_curve_row.get_value() == 0.75


def test_stick_mouse_movement_keeps_different_split_speeds_independent(
    temp_config_dir,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.models import AnalogControlConfig, AnalogMouseMotionConfig
    from keymasq.gui.widgets.analog_control_dialog import AnalogControlDialog
    from keymasq.session.analog_controls import AnalogControlManager

    manager = AnalogControlManager()
    manager.save_analog_control(
        AnalogControlConfig(
            name="Split Mouse",
            mouse_motion=AnalogMouseMotionConfig(
                enabled=True,
                speed_x=700,
                speed_y=1100,
            ),
        )
    )

    dialog = AnalogControlDialog(Gtk.Window())
    assert dialog.speed_x_row.get_value() == 700
    assert dialog.speed_y_row.get_value() == 1100

    dialog.speed_x_row.set_value(800)

    assert dialog.speed_x_row.get_value() == 800
    assert dialog.speed_y_row.get_value() == 1100


def test_stick_mouse_movement_modifier_desyncs_equal_split_speeds(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control_dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())
    assert dialog.speed_x_row.get_value() == dialog.speed_y_row.get_value()

    dialog._request_split_mouse_speed_desync("x")
    dialog.speed_x_row.set_value(700)

    assert dialog.speed_x_row.get_value() == 700
    assert dialog.speed_y_row.get_value() == 900


def test_stick_mouse_movement_held_modifier_desyncs_equal_split_speeds(
    temp_config_dir,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, Gtk

    from keymasq.gui.widgets.analog_control_dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())

    assert dialog._on_key_pressed(None, Gdk.KEY_Control_L, 0, 0) is False
    dialog.speed_y_row.set_value(700)
    dialog._on_key_released(None, Gdk.KEY_Control_L, 0, 0)

    assert dialog.speed_x_row.get_value() == 900
    assert dialog.speed_y_row.get_value() == 700


def test_axis_control_can_select_mouse_mode(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control_dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())
    dialog.name_entry.set_text("Axis Mouse")
    dialog.input_type_dropdown.set_selected(1)
    dialog.mode_dropdown.set_selected(2)

    assert dialog._current_mode() == "mouse"
    assert dialog._save_current_control() is True

    saved = dialog.manager.get_analog_control("Axis Mouse")
    assert saved is not None
    assert saved.input_type == "axis"
    assert saved.mouse_motion.enabled is True


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

    dialog.name_entry.set_text("Axis Control")
    dialog.input_type_dropdown.set_selected(1)
    dialog.mode_dropdown.set_selected(0)
    dialog._on_add_range_clicked()

    assert dialog.mouse_group.get_visible() is False
    assert dialog.digital_group.get_visible() is True
    assert dialog.template_group.get_visible() is False
    assert dialog._thresholds[0].axis == "x"
    assert dialog._thresholds[0].trigger_min >= 0.0
    assert dialog._save_current_control() is True

    saved = dialog.manager.get_analog_control("Axis Control")
    assert saved is not None
    assert saved.input_type == "axis"
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
    dialog._gamepad_output_dropdown.set_selected(2)
    dialog.gamepad_output_deadzone_row.set_value(20)
    dialog.gamepad_output_sensitivity_row.set_value(1.5)
    dialog.gamepad_output_response_curve_row.set_value(0.75)

    assert dialog._save_current_control() is True
    saved = dialog.manager.get_analog_control("Route Stick")
    assert saved is not None
    assert saved.gamepad_output.output_id == "virtual-gamepad-2"
    assert saved.gamepad_output.target == "right"
    assert saved.gamepad_output.deadzone == 0.2
    assert saved.gamepad_output.sensitivity == 1.5
    assert saved.gamepad_output.response_curve == 0.75

    reloaded = analog_dialog.AnalogControlDialog(parent)
    assert reloaded._current_name == "Route Stick"
    assert reloaded._selected_gamepad_output_id == "virtual-gamepad-2"
    assert reloaded._gamepad_output_target_buttons["right"].get_active() is True
    assert reloaded._gamepad_output_dropdown is not None
    assert reloaded._gamepad_output_dropdown.get_selected() == 2
    assert reloaded.gamepad_output_deadzone_row.get_value() == 20
    assert reloaded.gamepad_output_sensitivity_row.get_value() == 1.5
    assert reloaded.gamepad_output_response_curve_row.get_value() == 0.75


def test_analog_output_controls_use_learned_hardware_targets(temp_config_dir, monkeypatch) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.models import (
        AnalogAxisDefinition,
        AnalogInputDefinition,
        DeviceType,
        EvdevDevice,
        HardwareConfig,
    )
    import keymasq.gui.widgets.analog_control_dialog as analog_dialog

    hardware = HardwareConfig(
        vendor_id="1234",
        product_id="5678",
        name="Wheel",
        evdev_devices=[
            EvdevDevice(
                path="/dev/input/event0",
                device_type=DeviceType.GAMEPAD,
                id="wheel",
            )
        ],
        buttons=[],
        analog_inputs=[
            AnalogInputDefinition(
                id="gas",
                label="Gas",
                type="axis",
                axes=[AnalogAxisDefinition(role="x", evdev="abs_gas", evdev_code=9)],
            ),
            AnalogInputDefinition(
                id="brake",
                label="Brake",
                type="axis",
                axes=[AnalogAxisDefinition(role="x", evdev="abs_brake", evdev_code=10)],
            ),
        ],
    )

    class _HardwareManager:
        def list_hardware(self):
            return [hardware]

    monkeypatch.setattr(analog_dialog, "HardwareManager", _HardwareManager)
    monkeypatch.setattr(analog_dialog, "_virtual_gamepad_count", lambda: 1)

    dialog = analog_dialog.AnalogControlDialog(Gtk.Window())
    dialog.name_entry.set_text("Route Pedal")
    dialog.input_type_dropdown.set_selected(1)
    dialog.mode_dropdown.set_selected(1)
    assert dialog._gamepad_output_dropdown is not None
    dialog._gamepad_output_dropdown.set_selected(2)

    assert "analog:brake" in dialog._gamepad_output_target_buttons
    dialog._gamepad_output_target_buttons["analog:brake"].set_active(True)
    dialog.gamepad_output_rest_row.set_value(100)
    dialog.gamepad_output_direction_both_btn.set_active(True)

    assert dialog._save_current_control() is True
    saved = dialog.manager.get_analog_control("Route Pedal")
    assert saved is not None
    assert saved.gamepad_output.output_id == "1234:5678"
    assert saved.gamepad_output.target == "analog"
    assert saved.gamepad_output.target_analog_id == "brake"
    assert saved.gamepad_output.output_rest == 100
    assert saved.gamepad_output.output_direction == "both"
    assert saved.gamepad_output.output_invert is False


def test_gamepad_mode_save_drops_hidden_combined_settings(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.models import (
        ActionType,
        AnalogActionThreshold,
        AnalogControlConfig,
        AnalogGamepadOutputConfig,
        AnalogMouseMotionConfig,
        MappingAction,
    )
    from keymasq.gui.widgets.analog_control_dialog import AnalogControlDialog
    from keymasq.session.analog_controls import AnalogControlManager

    manager = AnalogControlManager()
    manager.save_analog_control(
        AnalogControlConfig(
            name="Combined",
            mouse_motion=AnalogMouseMotionConfig(enabled=True, tick_ms=12),
            gamepad_output=AnalogGamepadOutputConfig(enabled=True),
            thresholds=[
                AnalogActionThreshold(
                    axis="x",
                    trigger_min=0.65,
                    trigger_max=1.0,
                    release_min=0.55,
                    release_max=1.0,
                    actions=[MappingAction(action_type=ActionType.KEYBOARD, target="key_e")],
                )
            ],
        )
    )

    dialog = AnalogControlDialog(Gtk.Window())
    assert dialog._current_mode() == "gamepad"
    dialog.description_entry.set_text("Edited")

    assert dialog._save_current_control() is True
    saved = dialog.manager.get_analog_control("Combined")
    assert saved is not None
    assert saved.mouse_motion.enabled is False
    assert saved.gamepad_output.enabled is True
    assert saved.thresholds == []


def test_analog_selector_filters_controls_by_source_input_type(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.models import AnalogControlConfig
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog
    from keymasq.session.analog_controls import AnalogControlManager

    manager = AnalogControlManager()
    manager.save_analog_control(AnalogControlConfig(name="Stick Control"))
    manager.save_analog_control(AnalogControlConfig(name="Axis Control", input_type="axis"))

    dialog = KeySelectorDialog(
        Gtk.Window(),
        "Left Trigger",
        source_type="analog",
        analog_input_type="axis",
    )

    assert [config.name for config in dialog._analog_control_list] == ["Axis Control"]


def test_analog_selector_docs_button_links_to_analog_controls_docs(
    temp_config_dir,
    monkeypatch,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets import key_selector_dialog as dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    monkeypatch.setattr(dialog_module, "__version__", "1.2.3")

    dialog = KeySelectorDialog(Gtk.Window(), "Left Stick", source_type="analog")

    assert dialog.stack.get_visible_child_name() == "analog_control"
    assert dialog.actions_docs_btn.get_visible() is True
    assert (
        dialog.actions_docs_btn.get_tooltip_text()
        == "Open Analog Controls documentation"
    )
    assert dialog._active_actions_docs_link() == ("analog-controls", "Analog Controls")
    assert dialog_module._actions_docs_url("analog-controls") == (
        "https://keymasq.tools/docs/v1.2.3/ANALOG_CONTROLS/"
    )


def test_analog_control_dialog_groups_saved_controls_by_input_type() -> None:
    from keymasq.common.models import AnalogControlConfig
    from keymasq.gui.widgets.analog_control_dialog import _group_analog_control_names

    configs = {
        "Stick Control": AnalogControlConfig(name="Stick Control"),
        "Axis Control": AnalogControlConfig(name="Axis Control", input_type="axis"),
    }

    assert _group_analog_control_names(
        ["Stick Control", "Axis Control"],
        configs,
    ) == [
        ("1D Axes / Triggers", ["Axis Control"]),
        ("Sticks", ["Stick Control"]),
    ]
