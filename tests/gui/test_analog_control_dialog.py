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


def test_axis_analog_output_exposes_curve_controls(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

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
    assert saved.gamepad_output.sensitivity == 1.5
    assert saved.gamepad_output.response_curve == 0.75


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
    dialog._gamepad_output_dropdown.set_selected(1)
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
    assert reloaded._gamepad_output_dropdown.get_selected() == 1
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
    dialog._gamepad_output_dropdown.set_selected(1)

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


def test_gamepad_mode_save_preserves_existing_combined_settings(temp_config_dir) -> None:
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
    assert saved.mouse_motion.enabled is True
    assert saved.mouse_motion.tick_ms == 12
    assert saved.gamepad_output.enabled is True
    assert saved.thresholds[0].actions[0].target == "key_e"


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
