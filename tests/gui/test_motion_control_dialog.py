# ruff: noqa: E402

from types import SimpleNamespace
from typing import cast

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from gi.repository import Gtk

from keymasq.common.model.analog import SAME_DEVICE_OUTPUT_ID, AnalogControlConfig
from keymasq.common.model.core import DeviceType
from keymasq.common.model.hardware import (
    AnalogAxisDefinition,
    AnalogInputDefinition,
    EvdevDevice,
    HardwareConfig,
)
from keymasq.common.model.motion import (
    MotionAnalogConfig,
    MotionControlConfig,
    MotionGamepadConfig,
    MotionMouseConfig,
    MotionTiltConfig,
)
from keymasq.gui.widgets.gamepad_output_choices import GamepadOutputChoiceSet
from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog
from keymasq.gui.widgets.managed_editor.shell import ManagedEditorShell
from keymasq.gui.widgets.managed_editor.state import EditorSelection
from keymasq.gui.widgets.motion_control.dialog import MotionControlDialog
from keymasq.gui.widgets.motion_control.draft import MotionControlDraft
from keymasq.gui.widgets.motion_control.persistence import (
    MotionControlPersistence,
    MotionControlStore,
    ProfileReferences,
)
from keymasq.gui.widgets.motion_control.view import MotionControlEditorView
from keymasq.session.motion_controls import MotionControlManager


def test_motion_control_dialog_uses_managed_editor_shell() -> None:
    dialog = MotionControlDialog(Gtk.Window())

    assert isinstance(dialog.shell, ManagedEditorShell)
    assert dialog.state.active_selection == EditorSelection.new_item()
    assert dialog.state.is_dirty is True
    assert dialog.shell.save_button.get_sensitive() is True
    assert dialog.shell.revert_button.get_sensitive() is True
    assert dialog.shell.delete_button.get_sensitive() is False
    assert (
        dialog.shell.documentation_button.get_tooltip_text() == "Open Motion Controls documentation"
    )


def test_motion_control_dialog_guards_switch_with_unsaved_changes() -> None:
    manager = MotionControlManager()
    manager.save_motion_control(MotionControlConfig(name="First"))
    manager.save_motion_control(MotionControlConfig(name="Second"))
    dialog = MotionControlDialog(Gtk.Window(), manager=manager)
    dialog.editor.name_entry.set_text("Changed")

    dialog.select_control_by_name("Second")

    assert dialog.state.active_selection == EditorSelection.saved_item("First")
    assert dialog.state.pending_transition is not None
    assert dialog.state.pending_transition.selection == EditorSelection.saved_item("Second")


def test_motion_control_dialog_saves_through_persistence() -> None:
    manager = MotionControlManager()
    dialog = MotionControlDialog(Gtk.Window(), manager=manager)
    saved: list[str] = []
    dialog.connect("motion-control-saved", lambda _dialog, name: saved.append(name))
    dialog.editor.name_entry.set_text("Gyro Aim")
    dialog.editor.description_entry.set_text("Low-deadzone aiming")

    assert dialog._save_current() is True

    config = manager.get_motion_control("Gyro Aim")
    assert config is not None
    assert config.description == "Low-deadzone aiming"
    assert dialog.state.is_dirty is False
    assert saved == ["Gyro Aim"]


def test_motion_control_view_preserves_each_output_mode_while_switching() -> None:
    modified: list[bool] = []
    view = MotionControlEditorView(on_modified=lambda: modified.append(True))
    view.load(
        MotionControlDraft.from_config(
            MotionControlConfig(
                name="Mixed",
                mouse=MotionMouseConfig(sensitivity_x=3.0),
                gamepad=MotionGamepadConfig(max_rate_dps=720.0),
            )
        )
    )
    view.sensitivity_x.set_value(9.0)
    view.yaw_output.set_selected(0)
    view.pitch_output.set_selected(1)
    view.roll_output.set_selected(2)
    view.mode_dropdown.set_selected(1)
    view.max_rate.set_value(900.0)

    draft = view.draft()

    assert draft.mode == "gamepad"
    assert draft.mouse.sensitivity_x == 9.0
    assert draft.gamepad.max_rate_dps == 900.0
    assert draft.axis_routing.yaw == "none"
    assert draft.axis_routing.pitch == "horizontal"
    assert draft.axis_routing.roll == "vertical"
    assert modified


def test_motion_axis_routing_defaults_to_yaw_and_roll_horizontal() -> None:
    view = MotionControlEditorView(on_modified=lambda: None)

    draft = view.draft()

    assert draft.axis_routing.yaw == "horizontal"
    assert draft.axis_routing.pitch == "vertical"
    assert draft.axis_routing.roll == "horizontal"


def test_motion_view_exposes_separate_tilt_and_area_modes() -> None:
    choices = GamepadOutputChoiceSet(
        choices=[(None, "Virtual Gamepad 1")],
        count=1,
        hardware_configs=[],
    )
    view = MotionControlEditorView(
        on_modified=lambda: None,
        output_choices_loader=lambda _selected: choices,
    )
    view.load(
        MotionControlDraft.from_config(
            MotionControlConfig(
                name="Tilt Mouse",
                mode="tilt_mouse",
                tilt=MotionTiltConfig(
                    reference="gravity",
                    speed_x=700.0,
                    speed_y=600.0,
                ),
            )
        )
    )

    assert view.yaw_output.get_visible() is False
    assert view.reference_dropdown.get_visible() is True
    assert view.tilt_mouse_box.get_visible() is True
    assert view.area_mouse_box.get_visible() is False
    assert view.reference_dropdown.get_selected() == 1

    view.tilt_speed_x.set_value(800.0)
    view.mode_dropdown.set_selected(3)
    view.gamepad_output.gamepad_output_dropdown.set_selected(1)
    tilt_stick = view.draft()

    assert tilt_stick.mode == "tilt_gamepad"
    assert tilt_stick.tilt.speed_x == 800.0
    assert tilt_stick.gamepad.output_id == "virtual-gamepad-1"
    assert view.gamepad_box.get_visible() is True
    assert view.max_rate.get_visible() is False

    view.mode_dropdown.set_selected(4)
    view.area_radius_x.set_value(640.0)
    area_mouse = view.draft()

    assert area_mouse.mode == "area_mouse"
    assert area_mouse.tilt.area_radius_x == 640.0
    assert view.area_mouse_box.get_visible() is True
    assert view.gamepad_box.get_visible() is False


def test_motion_controller_output_defaults_to_origin_device() -> None:
    view = MotionControlEditorView(
        on_modified=lambda: None,
        output_choices_loader=lambda _selected: GamepadOutputChoiceSet(
            choices=[(None, "Virtual Gamepad 1")],
            count=1,
            hardware_configs=[],
        ),
    )

    view.load(
        MotionControlDraft.from_config(MotionControlConfig(name="Right Stick", mode="gamepad"))
    )

    assert view.selected_output_id == SAME_DEVICE_OUTPUT_ID
    assert view.gamepad_output.gamepad_output_dropdown.get_selected() == 0
    assert view.output_target_buttons["right"].get_active() is True
    assert view.draft().gamepad.output_id == SAME_DEVICE_OUTPUT_ID


def test_motion_virtual_gamepad_selection_survives_save_and_reload(temp_config_dir) -> None:
    choices = GamepadOutputChoiceSet(
        choices=[(None, "Virtual Gamepad 1")],
        count=1,
        hardware_configs=[],
    )
    view = MotionControlEditorView(
        on_modified=lambda: None,
        output_choices_loader=lambda _selected: choices,
    )
    view.load(
        MotionControlDraft.from_config(MotionControlConfig(name="Right Stick", mode="gamepad"))
    )
    view.gamepad_output.gamepad_output_dropdown.set_selected(1)
    manager = MotionControlManager()

    manager.save_motion_control(view.draft().to_config())

    loaded = MotionControlManager().get_motion_control("Right Stick")
    assert loaded is not None
    assert loaded.gamepad.output_id == "virtual-gamepad-1"
    content = (temp_config_dir / "motion_controls" / "right_stick.toml").read_text()
    assert 'output_id = "virtual-gamepad-1"' in content

    reopened = MotionControlEditorView(
        on_modified=lambda: None,
        output_choices_loader=lambda _selected: choices,
    )
    reopened.load(MotionControlDraft.from_config(loaded))
    assert reopened.gamepad_output.gamepad_output_dropdown.get_selected() == 1
    assert reopened.selected_output_id == "virtual-gamepad-1"


def test_motion_controller_output_routes_to_a_learned_physical_stick() -> None:
    hardware = HardwareConfig(
        vendor_id="1234",
        product_id="5678",
        name="Target Pad",
        evdev_devices=[
            EvdevDevice(
                path="/dev/input/event1",
                device_type=DeviceType.GAMEPAD,
                id="gamepad",
            )
        ],
        buttons=[],
        analog_inputs=[
            AnalogInputDefinition(
                id="left_stick",
                label="Left Stick",
                type="stick",
                axes=[AnalogAxisDefinition(role="x", evdev="abs_x")],
            ),
            AnalogInputDefinition(
                id="right_stick",
                label="Right Stick",
                type="stick",
                axes=[AnalogAxisDefinition(role="x", evdev="abs_rx")],
            ),
        ],
    )
    choices = GamepadOutputChoiceSet(
        choices=[
            (None, "Virtual Gamepad 1"),
            ("virtual-gamepad-2", "Virtual Gamepad 2"),
            (hardware.hardware_id, "Target Pad (1234:5678)"),
        ],
        count=2,
        hardware_configs=[hardware],
    )
    view = MotionControlEditorView(
        on_modified=lambda: None,
        output_choices_loader=lambda _selected: choices,
        output_count_loader=lambda: 2,
    )
    view.load(
        MotionControlDraft.from_config(MotionControlConfig(name="Right Stick", mode="gamepad"))
    )

    view.gamepad_output.gamepad_output_dropdown.set_selected(3)
    view.output_target_buttons["analog:right_stick"].set_active(True)
    draft = view.draft()

    assert draft.gamepad.output_id == "1234:5678"
    assert draft.gamepad.target == "analog"
    assert draft.gamepad.target_analog_id == "right_stick"


def test_motion_selector_allows_multiple_controls(temp_config_dir) -> None:
    manager = MotionControlManager()
    manager.save_motion_control(MotionControlConfig(name="Mouse"))
    manager.save_motion_control(MotionControlConfig(name="Right Stick", mode="gamepad"))
    dialog = KeySelectorDialog(Gtk.Window(), "Motion Sensor", source_type="motion")
    rows = [
        row
        for index in range(2)
        if (row := dialog._motion_control_listbox.get_row_at_index(index)) is not None
    ]

    dialog._motion_control_listbox.select_row(rows[0])
    dialog._motion_control_listbox.select_row(rows[1])

    assert dialog._motion_control_listbox.get_selection_mode() == Gtk.SelectionMode.MULTIPLE
    assert dialog._motion_control_listbox.get_selected_rows() == rows
    assert dialog._selected_motion_controls == [
        rows[0]._motion_control_name,
        rows[1]._motion_control_name,
    ]


def test_motion_to_analog_view_selects_one_matching_analog_control() -> None:
    edited: list[str] = []
    controls = {
        "Axis Actions": AnalogControlConfig(name="Axis Actions", input_type="axis"),
        "Stick Actions": AnalogControlConfig(name="Stick Actions", input_type="stick"),
    }
    view = MotionControlEditorView(
        on_modified=lambda: None,
        analog_controls_loader=lambda: controls,
        edit_analog_control=edited.append,
    )
    view.load(
        MotionControlDraft.from_config(
            MotionControlConfig(
                name="Tilt Directions",
                mode="analog",
                analog=MotionAnalogConfig(
                    analog_control_name="Stick Actions",
                    source="tilt",
                    x_axis="roll",
                    y_axis="pitch",
                    full_scale_deg=40.0,
                ),
            )
        )
    )

    assert view.mode_dropdown.get_selected() == 5
    assert view.motion_analog_box.get_visible() is True
    assert view.analog_control_listbox.get_selection_mode() == Gtk.SelectionMode.SINGLE
    assert view.analog_control_scrolled.get_min_content_height() == 176
    assert view.analog_control_scrolled.get_max_content_height() == 220
    selected_row = view.analog_control_listbox.get_selected_row()
    assert selected_row is not None
    assert selected_row._analog_control_name == "Stick Actions"
    assert view.analog_y_axis.get_visible() is True
    assert view.analog_full_scale_angle.get_value() == 40.0

    view.analog_control_search_entry.set_text("axis")
    assert view.analog_control_listbox.get_row_at_index(0).get_child_visible() is True
    assert view.analog_control_listbox.get_row_at_index(1).get_child_visible() is False
    view.analog_control_search_entry.set_text("")

    axis_row = view.analog_control_listbox.get_row_at_index(0)
    assert axis_row is not None
    assert axis_row._analog_control_name == "Axis Actions"
    view.analog_control_listbox.select_row(axis_row)
    view.analog_source_dropdown.set_selected(0)
    draft = view.draft()

    assert view.analog_y_axis.get_visible() is False
    assert view.analog_full_scale_rate.get_visible() is True
    assert draft.analog.analog_control_name == "Axis Actions"
    assert draft.analog.source == "gyro"

    gesture = SimpleNamespace(set_state=lambda _state: None)
    view._on_analog_control_row_right_pressed(
        cast(Gtk.GestureClick, gesture),
        1,
        0.0,
        0.0,
        "Axis Actions",
    )

    assert edited == ["Axis Actions"]


def test_motion_control_persistence_updates_profile_references() -> None:
    saved: list[tuple[str, str | None]] = []
    deleted: list[str] = []
    renamed: list[tuple[str, str]] = []
    suppressed: list[str] = []
    store = cast(
        MotionControlStore,
        SimpleNamespace(
            save_motion_control=lambda config, replacing_name=None: saved.append(
                (config.name, replacing_name)
            ),
            delete_motion_control=lambda name: deleted.append(name) or True,
        ),
    )
    profiles = cast(
        ProfileReferences,
        SimpleNamespace(
            rename_motion_control_references=lambda old, new: renamed.append((old, new)),
            replace_motion_control_with_suppress=lambda name: suppressed.append(name),
        ),
    )
    persistence = MotionControlPersistence(store)

    persistence.save(
        MotionControlConfig(name="New"),
        replacing_name="Old",
        profiles=profiles,
    )
    assert persistence.delete("New", profiles=profiles) is True

    assert saved == [("New", "Old")]
    assert renamed == [("Old", "New")]
    assert deleted == ["New"]
    assert suppressed == ["New"]
