import pytest

gi = pytest.importorskip("gi")

from keymasq.gui.widgets.managed_editor.state import EditorSelection  # noqa: E402
from keymasq.gui.widgets.spin_inputs import (  # noqa: E402
    apply_spin_secondary_step,
    int_entry_key_pressed,
)


def _new_row(dialog):
    row = dialog.shell.row_for_selection(EditorSelection.new_item())
    assert row is not None
    return row


def _saved_row(dialog, name: str):
    row = dialog.shell.row_for_selection(EditorSelection.saved_item(name))
    assert row is not None
    return row


def _select_input_type(dialog, input_type: str) -> None:
    from keymasq.gui.widgets.analog_control.options import input_type_index

    dialog.editor.input_type_dropdown.set_selected(input_type_index(input_type))


def _select_mode(dialog, mode: str) -> None:
    from keymasq.gui.widgets.analog_control.options import mode_index_for_input_type

    dialog.editor.mode_dropdown.set_selected(
        mode_index_for_input_type(dialog.editor.current_input_type(), mode)
    )


def test_new_analog_control_keeps_draft_when_add_row_reselected(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.core import ActionType
    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    parent = Gtk.Window()
    dialog = AnalogControlDialog(parent)

    _select_mode(dialog, "digital")
    dialog.editor.apply_wasd_template()
    assert len(dialog.editor.thresholds.thresholds) == 4

    action = MappingAction(action_type=ActionType.KEYBOARD, target="key_e")
    dialog.editor.thresholds.actions_selected([action], 0)

    dialog._on_selection_changed(EditorSelection.new_item())

    assert dialog._current_name is None
    assert len(dialog.editor.thresholds.thresholds) == 4
    assert dialog.editor.thresholds.thresholds[0].actions == [action]


def test_threshold_actions_use_neutral_mapping_sequence_dialog(
    temp_config_dir,
    monkeypatch,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    import keymasq.gui.widgets.analog_control.dialog as dialog_module
    from keymasq.gui.widgets.action_sequence import ActionSequenceMode
    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    captured: dict[str, object] = {}

    class SequenceDialog:
        def __init__(self, parent, title, mode, *, current_actions, action_key):
            captured.update(
                parent=parent,
                title=title,
                mode=mode,
                current_actions=current_actions,
                action_key=action_key,
            )

        def connect(self, signal_name, callback, index):
            captured["signal_name"] = signal_name
            captured["index"] = index

        def present(self, parent):
            captured["present_parent"] = parent

    monkeypatch.setattr(dialog_module, "ActionSequenceDialog", SequenceDialog)
    parent = Gtk.Window()
    dialog = AnalogControlDialog(parent)
    _select_mode(dialog, "digital")
    dialog.editor.apply_wasd_template()

    dialog._open_threshold_actions(0)

    assert captured["parent"] is parent
    assert captured["title"] == "Edit Range 1 Actions"
    assert captured["mode"] is ActionSequenceMode.MAPPING
    assert captured["action_key"] == "analog_threshold"
    assert captured["signal_name"] == "actions-selected"
    assert captured["index"] == 0
    assert captured["present_parent"] is parent


def test_new_analog_control_output_deadzone_defaults_to_zero(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())

    assert dialog.editor.gamepad.gamepad_output_deadzone_row.get_value() == 0


def test_analog_control_dialog_docs_button_links_to_analog_controls_docs(
    temp_config_dir,
    monkeypatch,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    import keymasq.gui.widgets.analog_control.dialog as dialog_module
    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    monkeypatch.setattr(dialog_module, "__version__", "1.2.3")

    dialog = AnalogControlDialog(Gtk.Window())

    assert dialog.shell.documentation_button.get_label() == "?"
    assert (
        dialog.shell.documentation_button.get_tooltip_text() == "Open Analog Controls documentation"
    )
    assert dialog_module.analog_controls_docs_url() == (
        "https://keymasq.tools/docs/v1.2.3/ANALOG_CONTROLS/"
    )

    monkeypatch.setattr(dialog_module, "__version__", "1.2.3.dev1")
    assert dialog_module.analog_controls_docs_url() == (
        "https://keymasq.tools/docs/master/ANALOG_CONTROLS/"
    )


def test_analog_control_dialog_unsaved_close_warns_and_can_discard(
    temp_config_dir,
    monkeypatch,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, Gtk

    import keymasq.gui.widgets.analog_control.dialog as analog_dialog

    dialog = analog_dialog.AnalogControlDialog(Gtk.Window())
    closed: list[bool] = []
    alerts: list[tuple[object, object]] = []
    monkeypatch.setattr(dialog, "force_close", lambda: closed.append(True))
    monkeypatch.setattr(
        analog_dialog.Adw.AlertDialog,
        "present",
        lambda alert, parent: alerts.append((alert, parent)),
    )

    assert _new_row(dialog) is not None
    assert dialog.shell.list_box.get_selected_row() is _new_row(dialog)
    assert dialog.get_can_close() is False

    dialog.shell.close_button.emit("clicked")
    assert closed == []
    assert len(alerts) == 1
    assert alerts[0][1] is dialog

    alerts[0][0].emit("response", "cancel")
    assert closed == []

    assert dialog._on_key_pressed(None, Gdk.KEY_Escape, 0, 0) is True
    assert closed == []
    assert len(alerts) == 2

    alerts[1][0].emit("response", "discard")
    assert closed == [True]
    assert dialog.get_can_close() is True


def test_analog_control_dialog_unsaved_close_save_response_saves_and_closes(
    temp_config_dir,
    monkeypatch,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    import keymasq.gui.widgets.analog_control.dialog as analog_dialog

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

    dialog.editor.name_entry.set_text("close_saved")
    dialog.unsaved.request_close()

    assert closed == []
    assert len(alerts) == 1

    alerts[0][0].emit("response", "save")

    assert closed == [True]
    assert saved == ["close_saved"]
    assert dialog.manager.get_analog_control("close_saved") is not None
    assert dialog.get_can_close() is True


def test_analog_control_dialog_unsaved_selection_warns_and_can_discard(
    temp_config_dir,
    monkeypatch,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    import keymasq.gui.widgets.analog_control.dialog as analog_dialog
    from keymasq.common.model.analog import AnalogControlConfig
    from keymasq.session.analog_controls import AnalogControlManager

    manager = AnalogControlManager()
    manager.save_analog_control(AnalogControlConfig(name="Alpha"))
    manager.save_analog_control(AnalogControlConfig(name="Beta"))

    alerts: list[tuple[object, object]] = []
    monkeypatch.setattr(
        analog_dialog.Adw.AlertDialog,
        "present",
        lambda alert, parent: alerts.append((alert, parent)),
    )

    dialog = analog_dialog.AnalogControlDialog(Gtk.Window())

    alpha_row = _saved_row(dialog, "Alpha")
    beta_row = _saved_row(dialog, "Beta")
    assert dialog.shell.list_box.get_selected_row() is alpha_row

    dialog.editor.description_entry.set_text("dirty")
    dialog.shell.list_box.select_row(beta_row)

    assert len(alerts) == 1
    assert alerts[0][1] is dialog
    assert dialog.shell.list_box.get_selected_row() is alpha_row
    assert dialog._current_name == "Alpha"
    assert dialog.editor.description_entry.get_text() == "dirty"

    alerts[0][0].emit("response", "discard")

    assert dialog.shell.list_box.get_selected_row() is beta_row
    assert dialog._current_name == "Beta"
    assert dialog.state.is_dirty is False


def test_analog_control_dialog_add_button_warns_before_resetting_dirty_new_draft(
    temp_config_dir,
    monkeypatch,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    import keymasq.gui.widgets.analog_control.dialog as analog_dialog

    alerts: list[tuple[object, object]] = []
    monkeypatch.setattr(
        analog_dialog.Adw.AlertDialog,
        "present",
        lambda alert, parent: alerts.append((alert, parent)),
    )

    dialog = analog_dialog.AnalogControlDialog(Gtk.Window())
    assert dialog.shell.list_box.get_selected_row() is _new_row(dialog)

    dialog.editor.name_entry.set_text("Edited Draft")
    dialog.shell.add_button.emit("clicked")

    assert len(alerts) == 1
    alert = alerts[0][0]
    assert alerts[0][1] is dialog
    assert alert.get_heading() == "Unsaved Analog Control Changes"
    assert alert.get_body() == (
        "Save your changes before starting a new Analog Control, or discard them?"
    )
    assert dialog.editor.name_entry.get_text() == "Edited Draft"

    alert.emit("response", "discard")

    assert dialog.shell.list_box.get_selected_row() is _new_row(dialog)
    assert dialog.editor.name_entry.get_text() == "New Analog Control"
    assert dialog.state.is_dirty is True


def test_analog_control_dialog_failed_delete_keeps_state(
    temp_config_dir,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.model.analog import AnalogControlConfig
    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog
    from keymasq.session.analog_controls import AnalogControlManager

    AnalogControlManager().save_analog_control(AnalogControlConfig(name="Alpha"))
    dialog = AnalogControlDialog(Gtk.Window())
    emitted: list[str] = []
    replaced: list[str] = []
    dialog.connect("analog-control-deleted", lambda _dialog, name: emitted.append(name))
    dialog.manager.delete_analog_control = lambda _name: False  # type: ignore[method-assign]
    dialog.profile_manager = type(
        "ProfileManager",
        (),
        {"replace_analog_control_with_suppress": lambda _self, name: replaced.append(name)},
    )()

    dialog.shell.delete_button.emit("clicked")

    assert dialog._current_name == "Alpha"
    assert dialog._current_config is not None
    assert dialog.shell.editor_container.get_sensitive() is True
    assert dialog.shell.delete_button.get_sensitive() is True
    assert emitted == []
    assert replaced == []


def test_analog_control_dialog_successful_delete_replaces_profile_references(
    temp_config_dir,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.model.analog import AnalogControlConfig
    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog
    from keymasq.session.analog_controls import AnalogControlManager

    AnalogControlManager().save_analog_control(AnalogControlConfig(name="Alpha"))
    dialog = AnalogControlDialog(Gtk.Window())
    emitted: list[str] = []
    replaced: list[str] = []
    dialog.connect("analog-control-deleted", lambda _dialog, name: emitted.append(name))
    dialog.profile_manager = type(
        "ProfileManager",
        (),
        {"replace_analog_control_with_suppress": lambda _self, name: replaced.append(name)},
    )()

    dialog.shell.delete_button.emit("clicked")

    assert dialog.manager.get_analog_control("Alpha") is None
    assert dialog._current_name != "Alpha"
    assert emitted == ["Alpha"]
    assert replaced == ["Alpha"]


def test_axis_analog_output_exposes_curve_controls(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.model.analog import SAME_DEVICE_OUTPUT_ID
    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())
    dialog.editor.name_entry.set_text("Axis Output")
    _select_input_type(dialog, "axis")
    _select_mode(dialog, "gamepad")
    dialog.editor.gamepad.gamepad_output_sensitivity_row.set_value(1.5)
    dialog.editor.gamepad.gamepad_output_response_curve_row.set_value(0.75)

    assert dialog.editor.gamepad.gamepad_output_sensitivity_row.get_visible() is True
    assert dialog.editor.gamepad.gamepad_output_response_curve_row.get_visible() is True
    assert dialog.editor.gamepad.gamepad_output_curve_row.get_visible() is True
    assert dialog.editor.gamepad.gamepad_output_invert_row.get_visible() is False
    assert dialog._save_current() is True

    saved = dialog.manager.get_analog_control("Axis Output")
    assert saved is not None
    assert saved.gamepad_output.output_id == SAME_DEVICE_OUTPUT_ID
    assert saved.gamepad_output.sensitivity == 1.5
    assert saved.gamepad_output.response_curve == 0.75


def test_axis_analog_output_both_direction_exposes_invert_toggle(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())
    dialog.editor.name_entry.set_text("Axis Output Invert")
    _select_input_type(dialog, "axis")
    _select_mode(dialog, "gamepad")
    dialog.editor.gamepad.gamepad_output_direction_both_btn.set_active(True)
    dialog.editor.gamepad.gamepad_output_invert_x_btn.set_active(True)

    # For 1D both-direction output, the single visible X toggle stores the combined
    # invert flag, not the stick-style per-axis X flag.
    assert dialog.editor.gamepad.gamepad_output_invert_row.get_title() == "Invert Output Axis"
    assert dialog.editor.gamepad.gamepad_output_invert_row.get_visible() is True
    assert dialog.editor.gamepad.gamepad_output_invert_x_btn.get_visible() is True
    assert dialog.editor.gamepad.gamepad_output_invert_y_btn.get_visible() is False
    assert dialog._save_current() is True

    saved = dialog.manager.get_analog_control("Axis Output Invert")
    assert saved is not None
    assert saved.gamepad_output.output_direction == "both"
    assert saved.gamepad_output.output_invert is True
    assert saved.gamepad_output.output_invert_x is False
    assert saved.gamepad_output.output_invert_y is False


def test_axis_mouse_movement_exposes_direction_and_curve_controls(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())
    dialog.editor.name_entry.set_text("Axis Mouse")
    _select_input_type(dialog, "axis")
    _select_mode(dialog, "mouse")
    dialog.editor.mouse.speed_row.set_value(1200)
    dialog.editor.mouse.mouse_sensitivity_row.set_value(1.5)
    dialog.editor.mouse.mouse_response_curve_row.set_value(0.75)
    dialog.editor.mouse.mouse_direction_buttons["vertical"].set_active(True)
    dialog.editor.mouse.invert_x_btn.set_active(True)

    assert dialog.editor.mouse.group.get_visible() is True
    assert dialog.editor.mouse.speed_row.get_visible() is True
    assert dialog.editor.mouse.speed_x_row.get_visible() is False
    assert dialog.editor.mouse.speed_y_row.get_visible() is False
    assert dialog.editor.mouse.mouse_direction_row.get_visible() is True
    assert dialog.editor.mouse.invert_axes_row.get_title() == "Invert Axis"
    assert dialog.editor.mouse.invert_axes_row.get_visible() is True
    assert dialog.editor.mouse.invert_x_btn.get_visible() is True
    assert dialog.editor.mouse.invert_y_btn.get_visible() is False
    assert dialog._save_current() is True

    saved = dialog.manager.get_analog_control("Axis Mouse")
    assert saved is not None
    assert saved.input_type == "axis"
    assert saved.mouse_motion.enabled is True
    assert saved.mouse_motion.speed == 1200
    assert saved.mouse_motion.sensitivity == 1.5
    assert saved.mouse_motion.response_curve == 0.75
    assert saved.mouse_motion.direction == "vertical"
    assert saved.mouse_motion.invert_x is True
    assert saved.mouse_motion.invert_y is False


def test_stick_mouse_movement_exposes_split_speed_controls(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())
    dialog.editor.name_entry.set_text("Stick Mouse")
    dialog.editor.mouse.speed_x_row.set_value(700)

    assert dialog.editor.mouse.group.get_visible() is True
    assert dialog.editor.mouse.speed_row.get_visible() is False
    assert dialog.editor.mouse.speed_x_row.get_visible() is True
    assert dialog.editor.mouse.speed_y_row.get_visible() is True
    assert dialog.editor.mouse.speed_y_row.get_value() == 700
    assert dialog.editor.mouse.mouse_direction_row.get_visible() is False
    assert dialog.editor.mouse.invert_axes_row.get_title() == "Invert Axes"
    assert dialog.editor.mouse.invert_axes_row.get_visible() is True
    assert dialog._save_current() is True

    saved = dialog.manager.get_analog_control("Stick Mouse")
    assert saved is not None
    assert saved.mouse_motion.enabled is True
    assert saved.mouse_motion.speed_x == 700
    assert saved.mouse_motion.speed_y == 700


def test_stick_mouse_invert_axes_use_compact_toggle_row(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())
    dialog.editor.name_entry.set_text("Invert Stick")
    dialog.editor.mouse.invert_x_btn.set_active(True)
    dialog.editor.mouse.invert_y_btn.set_active(True)

    assert dialog.editor.mouse.invert_axes_row.get_title() == "Invert Axes"
    assert dialog._save_current() is True

    saved = dialog.manager.get_analog_control("Invert Stick")
    assert saved is not None
    assert saved.mouse_motion.invert_x is True
    assert saved.mouse_motion.invert_y is True


def test_stick_mouse_invert_axes_are_independent(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())

    dialog.editor.mouse.invert_x_btn.set_active(True)
    assert dialog.editor.mouse.invert_x_btn.get_active() is True
    assert dialog.editor.mouse.invert_y_btn.get_active() is False

    dialog.editor.mouse.invert_y_btn.set_active(True)
    assert dialog.editor.mouse.invert_x_btn.get_active() is True
    assert dialog.editor.mouse.invert_y_btn.get_active() is True

    dialog.editor.mouse.invert_x_btn.set_active(False)
    assert dialog.editor.mouse.invert_y_btn.get_active() is True

    _select_mode(dialog, "mouse_area")
    dialog.editor.mouse.invert_y_btn.set_active(False)
    dialog.editor.mouse.invert_x_btn.set_active(True)

    assert dialog.editor.mouse.invert_x_btn.get_active() is True
    assert dialog.editor.mouse.invert_y_btn.get_active() is False


def test_stick_mouse_area_exposes_radius_and_start_capture(
    temp_config_dir,
    monkeypatch,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control import dialog as dialog_module
    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    class _Result:
        def __init__(self, x: int, y: int) -> None:
            self.x = x
            self.y = y

    class _SlurpCapture:
        available = True

        def set_compositor(self, compositor: str) -> None:
            self.compositor = compositor

        def capture_point(self, callback) -> None:
            callback(_Result(640, 480))

    monkeypatch.setattr(dialog_module, "get_slurp_capture", lambda: _SlurpCapture())
    monkeypatch.setattr(dialog_module, "session_compositor_id", lambda: "hyprland")

    dialog = AnalogControlDialog(Gtk.Window())
    dialog.editor.name_entry.set_text("Stick Area")
    _select_mode(dialog, "mouse_area")
    dialog.editor.mouse.area_radius_x_row.set_value(640)
    dialog.editor.mouse.area_start_enabled_row.set_active(True)
    dialog.editor.begin_area_capture()

    assert dialog.editor.mouse.speed_x_row.get_visible() is False
    assert dialog.editor.mouse.area_radius_x_row.get_visible() is True
    assert dialog.editor.mouse.area_start_x_entry.get_text() == "640"
    assert dialog.editor.mouse.area_start_y_entry.get_text() == "480"
    assert dialog.editor.mouse.area_start_capture_status.get_text() == ""
    assert dialog._save_current() is True

    saved = dialog.manager.get_analog_control("Stick Area")
    assert saved is not None
    assert saved.mouse_motion.enabled is True
    assert saved.mouse_motion.mode == "area"
    assert saved.mouse_motion.area_radius_x == 640
    assert saved.mouse_motion.area_radius_y == 640
    assert saved.mouse_motion.area_start_enabled is True
    assert saved.mouse_motion.area_start_x == 640
    assert saved.mouse_motion.area_start_y == 480


def test_mouse_area_capture_is_cancelled_when_selection_changes(
    temp_config_dir,
    monkeypatch,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.model.analog import AnalogControlConfig, AnalogMouseMotionConfig
    from keymasq.gui.widgets.analog_control import dialog as dialog_module
    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog
    from keymasq.session.analog_controls import AnalogControlManager

    class _Result:
        def __init__(self, x: int, y: int) -> None:
            self.x = x
            self.y = y

    callbacks = []

    class _SlurpCapture:
        available = True

        def set_compositor(self, compositor: str) -> None:
            self.compositor = compositor

        def capture_point(self, callback) -> None:
            callbacks.append(callback)

    monkeypatch.setattr(dialog_module, "get_slurp_capture", lambda: _SlurpCapture())
    monkeypatch.setattr(dialog_module, "session_compositor_id", lambda: "hyprland")

    manager = AnalogControlManager()
    manager.save_analog_control(
        AnalogControlConfig(
            name="Alpha",
            mouse_motion=AnalogMouseMotionConfig(
                enabled=True,
                mode="area",
                area_start_enabled=True,
            ),
        )
    )
    manager.save_analog_control(
        AnalogControlConfig(
            name="Beta",
            mouse_motion=AnalogMouseMotionConfig(
                enabled=True,
                mode="area",
                area_start_enabled=True,
                area_start_x=10,
                area_start_y=20,
            ),
        )
    )

    dialog = AnalogControlDialog(Gtk.Window())

    beta_row = _saved_row(dialog, "Beta")

    dialog.editor.begin_area_capture()
    request_id = dialog.editor.capture.request_id
    assert callbacks
    assert dialog.editor.capture.pending is True

    dialog.shell.list_box.select_row(beta_row)

    assert dialog._current_name == "Beta"
    assert dialog.editor.capture.pending is False
    assert dialog.editor.capture.apply is None
    assert dialog.editor.capture.request_id != request_id

    callbacks[0](_Result(640, 480))

    assert dialog.editor.mouse.area_start_x_entry.get_text() == "10"
    assert dialog.editor.mouse.area_start_y_entry.get_text() == "20"


@pytest.mark.parametrize(
    ("transition", "expected_name", "expected_position"),
    (
        ("revert", "Alpha", (10, 20)),
        ("save", "Alpha", (30, 40)),
        ("delete", "Beta", (50, 60)),
    ),
)
def test_mouse_area_capture_is_cancelled_before_document_replacement(
    temp_config_dir,
    transition: str,
    expected_name: str,
    expected_position: tuple[int, int],
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.model.analog import AnalogControlConfig, AnalogMouseMotionConfig
    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog
    from keymasq.gui.widgets.position_capture import PositionCaptureController
    from keymasq.session.analog_controls import AnalogControlManager

    class _Result:
        def __init__(self, x: int, y: int) -> None:
            self.x = x
            self.y = y

    callbacks = []

    class _SlurpCapture:
        def capture_point(self, callback) -> None:
            callbacks.append(callback)

    manager = AnalogControlManager()
    for name, x, y in (("Alpha", 10, 20), ("Beta", 50, 60)):
        manager.save_analog_control(
            AnalogControlConfig(
                name=name,
                mouse_motion=AnalogMouseMotionConfig(
                    enabled=True,
                    mode="area",
                    area_start_enabled=True,
                    area_start_x=x,
                    area_start_y=y,
                ),
            )
        )
    capture = PositionCaptureController(
        slurp_capture=_SlurpCapture(),
        slurp_available=True,
    )
    dialog = AnalogControlDialog(
        Gtk.Window(),
        manager=manager,
        position_capture=capture,
    )

    if transition in {"revert", "save"}:
        dialog.editor.mouse.area_start_x_entry.set_text("30")
        dialog.editor.mouse.area_start_y_entry.set_text("40")
        dialog.editor.description_entry.set_text("Changed")
    dialog.editor.begin_area_capture()
    request_id = capture.request_id

    assert callbacks
    assert capture.pending is True
    if transition == "revert":
        assert dialog.shell.revert_button.get_sensitive() is True
        dialog.shell.revert_button.emit("clicked")
    elif transition == "save":
        assert dialog.shell.save_button.get_sensitive() is True
        dialog.shell.save_button.emit("clicked")
    else:
        dialog.shell.delete_button.emit("clicked")

    assert dialog._current_name == expected_name
    assert capture.pending is False
    assert capture.apply is None
    assert capture.request_id != request_id
    assert dialog.state.is_dirty is False
    assert dialog.editor.mouse.area_start_x_entry.get_text() == str(expected_position[0])
    assert dialog.editor.mouse.area_start_y_entry.get_text() == str(expected_position[1])

    callbacks[0](_Result(640, 480))

    assert dialog.state.is_dirty is False
    assert dialog.editor.mouse.area_start_x_entry.get_text() == str(expected_position[0])
    assert dialog.editor.mouse.area_start_y_entry.get_text() == str(expected_position[1])


def test_mouse_area_start_entries_are_centered_and_numeric(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, GLib, Gtk

    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())

    assert dialog.editor.mouse.area_start_x_entry.get_alignment() == 0.5
    assert int_entry_key_pressed(
        Gtk.EventControllerKey(),
        Gdk.KEY_a,
        0,
        Gdk.ModifierType(0),
        dialog.editor.mouse.area_start_x_entry,
    )
    dialog.editor.mouse.area_start_x_entry.set_text("12a")
    while GLib.MainContext.default().pending():
        GLib.MainContext.default().iteration(False)
    assert dialog.editor.mouse.area_start_x_entry.get_text() == "12"

    dialog.editor.mouse.area_start_x_entry.set_text("-12")
    assert dialog.editor.mouse.area_start_x_entry.get_text() == "-12"


def test_mouse_area_radius_rows_sync_and_can_desync(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())
    _select_mode(dialog, "mouse_area")

    dialog.editor.mouse.area_radius_x_row.set_value(700)
    assert dialog.editor.mouse.area_radius_y_row.get_value() == 700

    dialog.editor.request_axis_desync("y")
    dialog.editor.mouse.area_radius_y_row.set_value(300)

    assert dialog.editor.mouse.area_radius_x_row.get_value() == 700
    assert dialog.editor.mouse.area_radius_y_row.get_value() == 300


def test_loading_mouse_area_preserves_different_radii(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.model.analog import AnalogControlConfig, AnalogMouseMotionConfig
    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog
    from keymasq.session.analog_controls import AnalogControlManager

    AnalogControlManager().save_analog_control(
        AnalogControlConfig(
            name="Area",
            mouse_motion=AnalogMouseMotionConfig(
                enabled=True,
                mode="area",
                area_radius_x=400,
                area_radius_y=500,
            ),
        )
    )

    dialog = AnalogControlDialog(Gtk.Window())

    assert dialog.editor.mouse.area_radius_x_row.get_value() == 400
    assert dialog.editor.mouse.area_radius_y_row.get_value() == 500


def test_mouse_area_capture_failure_uses_error_status(temp_config_dir, monkeypatch) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control import dialog as dialog_module
    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    class _SlurpCapture:
        available = True

        def set_compositor(self, compositor: str) -> None:
            self.compositor = compositor

        def capture_point(self, callback) -> None:
            callback(None)

    monkeypatch.setattr(dialog_module, "get_slurp_capture", lambda: _SlurpCapture())
    monkeypatch.setattr(dialog_module, "session_compositor_id", lambda: "hyprland")

    dialog = AnalogControlDialog(Gtk.Window())
    _select_mode(dialog, "mouse_area")
    dialog.editor.mouse.area_start_enabled_row.set_active(True)
    dialog.editor.begin_area_capture()

    assert dialog.editor.mouse.area_start_capture_row.get_title() == ""
    assert dialog.editor.mouse.area_start_capture_status.get_xalign() == 1.0
    assert dialog.editor.mouse.area_start_capture_status.get_text() == "Capture cancelled or failed"
    assert (
        dialog.editor.mouse.area_start_capture_status.get_next_sibling()
        is dialog.editor.mouse.area_start_capture_btn
    )
    assert any(
        css_class == "capture-error-label"
        for css_class in dialog.editor.mouse.area_start_capture_status.get_css_classes()
    )


def test_mouse_speed_spin_rows_use_500_page_steps(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())

    for row in (
        dialog.editor.mouse.speed_row,
        dialog.editor.mouse.speed_x_row,
        dialog.editor.mouse.speed_y_row,
    ):
        adjustment = row.get_adjustment()
        assert adjustment.get_step_increment() == 25
        assert adjustment.get_page_increment() == 500


def test_mouse_movement_tuning_spin_rows_use_expected_page_steps(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())

    assert dialog.editor.mouse.deadzone_row.get_adjustment().get_page_increment() == pytest.approx(
        0.05
    )
    assert dialog.editor.mouse.mouse_sensitivity_row.get_adjustment().get_page_increment() == 0.25
    assert (
        dialog.editor.mouse.mouse_response_curve_row.get_adjustment().get_page_increment() == 0.25
    )


def test_analog_curve_graph_clamps_to_editor_tuning_limits(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog
    from keymasq.gui.widgets.analog_curve_graph import AnalogCurveGraph

    dialog = AnalogControlDialog(Gtk.Window())
    graph = AnalogCurveGraph()

    graph.set_curve(deadzone=10.0, sensitivity=10.0, response_curve=10.0)
    assert graph._deadzone == pytest.approx(
        dialog.editor.mouse.deadzone_row.get_adjustment().get_upper()
    )
    assert graph._deadzone == pytest.approx(
        dialog.editor.gamepad.gamepad_output_deadzone_row.get_adjustment().get_upper() / 100.0
    )
    assert graph._sensitivity == pytest.approx(
        dialog.editor.mouse.mouse_sensitivity_row.get_adjustment().get_upper()
    )
    assert graph._sensitivity == pytest.approx(
        dialog.editor.gamepad.gamepad_output_sensitivity_row.get_adjustment().get_upper()
    )
    assert graph._response_curve == pytest.approx(
        dialog.editor.mouse.mouse_response_curve_row.get_adjustment().get_upper()
    )
    assert graph._response_curve == pytest.approx(
        dialog.editor.gamepad.gamepad_output_response_curve_row.get_adjustment().get_upper()
    )

    graph.set_curve(deadzone=-10.0, sensitivity=-10.0, response_curve=-10.0)
    assert graph._deadzone == pytest.approx(
        dialog.editor.mouse.deadzone_row.get_adjustment().get_lower()
    )
    assert graph._deadzone == pytest.approx(
        dialog.editor.gamepad.gamepad_output_deadzone_row.get_adjustment().get_lower() / 100.0
    )
    assert graph._sensitivity == pytest.approx(
        dialog.editor.mouse.mouse_sensitivity_row.get_adjustment().get_lower()
    )
    assert graph._sensitivity == pytest.approx(
        dialog.editor.gamepad.gamepad_output_sensitivity_row.get_adjustment().get_lower()
    )
    assert graph._response_curve == pytest.approx(
        dialog.editor.mouse.mouse_response_curve_row.get_adjustment().get_lower()
    )
    assert graph._response_curve == pytest.approx(
        dialog.editor.gamepad.gamepad_output_response_curve_row.get_adjustment().get_lower()
    )


def test_mouse_speed_secondary_steps_apply_500_and_keep_sync(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())

    apply_spin_secondary_step(dialog.editor.mouse.speed_row, 1, 500)
    assert dialog.editor.mouse.speed_row.get_value() == 1400

    apply_spin_secondary_step(dialog.editor.mouse.speed_x_row, 1, 500)
    assert dialog.editor.mouse.speed_x_row.get_value() == 1400
    assert dialog.editor.mouse.speed_y_row.get_value() == 1400

    apply_spin_secondary_step(dialog.editor.mouse.speed_y_row, -1, 500)
    assert dialog.editor.mouse.speed_x_row.get_value() == 900
    assert dialog.editor.mouse.speed_y_row.get_value() == 900

    apply_spin_secondary_step(dialog.editor.mouse.area_radius_x_row, 1, 100)
    assert dialog.editor.mouse.area_radius_x_row.get_value() == 500
    assert dialog.editor.mouse.area_radius_y_row.get_value() == 500


def test_mouse_movement_tuning_secondary_steps_apply_requested_values(
    temp_config_dir,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())

    dialog.editor.mouse.deadzone_row.set_value(0.15)
    apply_spin_secondary_step(dialog.editor.mouse.deadzone_row, 1, 0.05)
    assert dialog.editor.mouse.deadzone_row.get_value() == pytest.approx(0.20)

    apply_spin_secondary_step(dialog.editor.mouse.deadzone_row, -1, 0.05)
    assert dialog.editor.mouse.deadzone_row.get_value() == pytest.approx(0.15)

    apply_spin_secondary_step(dialog.editor.mouse.mouse_sensitivity_row, 1, 0.25)
    assert dialog.editor.mouse.mouse_sensitivity_row.get_value() == 1.25

    apply_spin_secondary_step(dialog.editor.mouse.mouse_response_curve_row, -1, 0.25)
    assert dialog.editor.mouse.mouse_response_curve_row.get_value() == 0.75


def test_analog_output_spin_rows_use_expected_page_steps(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())

    assert (
        dialog.editor.gamepad.gamepad_output_deadzone_row.get_adjustment().get_page_increment() == 5
    )
    assert (
        dialog.editor.gamepad.gamepad_output_sensitivity_row.get_adjustment().get_page_increment()
        == 0.25
    )
    assert (
        dialog.editor.gamepad.gamepad_output_response_curve_row.get_adjustment().get_page_increment()
        == 0.25
    )


def test_analog_output_rest_secondary_click_resets_to_zero(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())
    dialog.editor.gamepad.gamepad_output_rest_row.set_value(100)

    apply_spin_secondary_step(
        dialog.editor.gamepad.gamepad_output_rest_row,
        1,
        None,
        reset_value=0,
    )

    assert dialog.editor.gamepad.gamepad_output_rest_row.get_value() == 0


def test_analog_output_secondary_steps_apply_requested_values(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())

    dialog.editor.gamepad.gamepad_output_deadzone_row.set_value(10)
    apply_spin_secondary_step(dialog.editor.gamepad.gamepad_output_deadzone_row, 1, 5)
    assert dialog.editor.gamepad.gamepad_output_deadzone_row.get_value() == 15

    apply_spin_secondary_step(dialog.editor.gamepad.gamepad_output_deadzone_row, -1, 5)
    assert dialog.editor.gamepad.gamepad_output_deadzone_row.get_value() == 10

    apply_spin_secondary_step(dialog.editor.gamepad.gamepad_output_sensitivity_row, 1, 0.25)
    assert dialog.editor.gamepad.gamepad_output_sensitivity_row.get_value() == 1.25

    apply_spin_secondary_step(
        dialog.editor.gamepad.gamepad_output_response_curve_row,
        -1,
        0.25,
    )
    assert dialog.editor.gamepad.gamepad_output_response_curve_row.get_value() == 0.75


def test_stick_mouse_movement_keeps_different_split_speeds_independent(
    temp_config_dir,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.model.analog import AnalogControlConfig, AnalogMouseMotionConfig
    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog
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
    assert dialog.editor.mouse.speed_x_row.get_value() == 700
    assert dialog.editor.mouse.speed_y_row.get_value() == 1100

    dialog.editor.mouse.speed_x_row.set_value(800)

    assert dialog.editor.mouse.speed_x_row.get_value() == 800
    assert dialog.editor.mouse.speed_y_row.get_value() == 1100


def test_stick_mouse_movement_modifier_desyncs_equal_split_speeds(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())
    assert (
        dialog.editor.mouse.speed_x_row.get_value() == dialog.editor.mouse.speed_y_row.get_value()
    )

    dialog.editor.request_axis_desync("x")
    dialog.editor.mouse.speed_x_row.set_value(700)

    assert dialog.editor.mouse.speed_x_row.get_value() == 700
    assert dialog.editor.mouse.speed_y_row.get_value() == 900


def test_stick_mouse_movement_held_modifier_desyncs_equal_split_speeds(
    temp_config_dir,
) -> None:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, Gtk

    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())

    assert dialog._on_key_pressed(None, Gdk.KEY_Control_L, 0, 0) is False
    dialog.editor.mouse.speed_y_row.set_value(700)
    dialog._on_key_released(None, Gdk.KEY_Control_L, 0, 0)

    assert dialog.editor.mouse.speed_x_row.get_value() == 900
    assert dialog.editor.mouse.speed_y_row.get_value() == 700


def test_stick_mouse_movement_tracks_multiple_held_desync_modifiers(
    temp_config_dir,
) -> None:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, Gtk

    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())

    assert dialog._on_key_pressed(None, Gdk.KEY_Control_L, 0, 0) is False
    assert dialog._on_key_pressed(None, Gdk.KEY_Shift_L, 0, 0) is False
    dialog._on_key_released(None, Gdk.KEY_Control_L, 0, 0)

    dialog.editor.mouse.speed_y_row.set_value(700)
    dialog._on_key_released(None, Gdk.KEY_Shift_L, 0, 0)

    assert dialog.editor.mouse.speed_x_row.get_value() == 900
    assert dialog.editor.mouse.speed_y_row.get_value() == 700


def test_axis_control_can_select_mouse_mode(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())
    dialog.editor.name_entry.set_text("Axis Mouse")
    _select_input_type(dialog, "axis")
    _select_mode(dialog, "mouse")

    assert dialog.editor.current_mode() == "mouse"
    assert dialog._save_current() is True

    saved = dialog.manager.get_analog_control("Axis Mouse")
    assert saved is not None
    assert saved.input_type == "axis"
    assert saved.mouse_motion.enabled is True


def test_saved_analog_control_keeps_action_edits_when_current_row_reselected(
    temp_config_dir,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.core import ActionType
    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog
    from keymasq.session.analog_controls import analog_control_wasd_template

    parent = Gtk.Window()
    dialog = AnalogControlDialog(parent)

    dialog.editor.name_entry.set_text("Saved Control")
    _select_mode(dialog, "digital")
    dialog.editor.thresholds.apply_template(analog_control_wasd_template())
    assert dialog._save_current() is True

    action = MappingAction(action_type=ActionType.KEYBOARD, target="key_e")
    dialog.editor.thresholds.actions_selected([action], 0)

    selected_row = dialog.shell.list_box.get_selected_row()
    assert selected_row is not None
    dialog._on_selection_changed(dialog.shell.selection_for_row(selected_row))

    assert dialog._current_name == "Saved Control"
    assert len(dialog.editor.thresholds.thresholds) == 4
    assert dialog.editor.thresholds.thresholds[0].actions == [action]


def test_trigger_analog_control_saves_digital_only_positive_ranges(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    parent = Gtk.Window()
    dialog = AnalogControlDialog(parent)

    dialog.editor.name_entry.set_text("Axis Control")
    _select_input_type(dialog, "axis")
    _select_mode(dialog, "digital")
    dialog.editor.thresholds.add_range()

    assert dialog.editor.mouse.group.get_visible() is False
    assert dialog.editor.digital.group.get_visible() is True
    assert dialog.editor.templates.group.get_visible() is False
    assert dialog.editor.thresholds.thresholds[0].axis == "x"
    assert dialog.editor.thresholds.thresholds[0].trigger_min >= 0.0
    row = dialog.editor.thresholds.rows[0]
    assert row.trigger_min.get_adjustment().get_lower() == -100.0
    assert dialog._save_current() is True

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

    import keymasq.gui.widgets.analog_control.view as view_module
    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    monkeypatch.setattr(view_module, "virtual_gamepad_count", lambda: 2)

    parent = Gtk.Window()
    dialog = AnalogControlDialog(parent)
    dialog.editor.name_entry.set_text("Route Stick")
    _select_mode(dialog, "gamepad")
    dialog.editor.output_target_buttons["right"].set_active(True)
    dialog.editor.gamepad.gamepad_output_invert_x_btn.set_active(True)
    dialog.editor.gamepad.gamepad_output_invert_y_btn.set_active(True)
    assert dialog.editor.gamepad.gamepad_output_dropdown is not None
    dialog.editor.gamepad.gamepad_output_dropdown.set_selected(2)
    dialog.editor.gamepad.gamepad_output_deadzone_row.set_value(20)
    dialog.editor.gamepad.gamepad_output_sensitivity_row.set_value(1.5)
    dialog.editor.gamepad.gamepad_output_response_curve_row.set_value(0.75)

    assert dialog.editor.gamepad.gamepad_output_invert_row.get_title() == "Invert Output Axes"
    assert dialog.editor.gamepad.gamepad_output_invert_row.get_visible() is True
    assert dialog.editor.gamepad.gamepad_output_invert_x_btn.get_visible() is True
    assert dialog.editor.gamepad.gamepad_output_invert_y_btn.get_visible() is True
    assert dialog._save_current() is True
    saved = dialog.manager.get_analog_control("Route Stick")
    assert saved is not None
    assert saved.gamepad_output.output_id == "virtual-gamepad-2"
    assert saved.gamepad_output.target == "right"
    assert saved.gamepad_output.deadzone == 0.2
    assert saved.gamepad_output.output_invert_x is True
    assert saved.gamepad_output.output_invert_y is True
    assert saved.gamepad_output.sensitivity == 1.5
    assert saved.gamepad_output.response_curve == 0.75

    reloaded = AnalogControlDialog(parent)
    assert reloaded._current_name == "Route Stick"
    assert reloaded.editor.selected_output_id == "virtual-gamepad-2"
    assert reloaded.editor.output_target_buttons["right"].get_active() is True
    assert reloaded.editor.gamepad.gamepad_output_dropdown.get_selected() == 2
    assert reloaded.editor.gamepad.gamepad_output_deadzone_row.get_value() == 20
    assert reloaded.editor.gamepad.gamepad_output_invert_x_btn.get_active() is True
    assert reloaded.editor.gamepad.gamepad_output_invert_y_btn.get_active() is True
    assert reloaded.editor.gamepad.gamepad_output_sensitivity_row.get_value() == 1.5
    assert reloaded.editor.gamepad.gamepad_output_response_curve_row.get_value() == 0.75


def test_analog_output_controls_use_learned_hardware_targets(temp_config_dir, monkeypatch) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    import keymasq.gui.widgets.analog_control.view as view_module
    from keymasq.common.model.core import DeviceType
    from keymasq.common.model.hardware import (
        AnalogAxisDefinition,
        AnalogInputDefinition,
        EvdevDevice,
        HardwareConfig,
    )
    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

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

    monkeypatch.setattr(view_module, "HardwareManager", _HardwareManager)
    monkeypatch.setattr(view_module, "virtual_gamepad_count", lambda: 1)

    dialog = AnalogControlDialog(Gtk.Window())
    dialog.editor.name_entry.set_text("Route Pedal")
    _select_input_type(dialog, "axis")
    _select_mode(dialog, "gamepad")
    assert dialog.editor.gamepad.gamepad_output_dropdown is not None
    dialog.editor.gamepad.gamepad_output_dropdown.set_selected(2)

    assert "analog:brake" in dialog.editor.output_target_buttons
    dialog.editor.output_target_buttons["analog:brake"].set_active(True)
    dialog.editor.gamepad.gamepad_output_rest_row.set_value(100)
    dialog.editor.gamepad.gamepad_output_direction_both_btn.set_active(True)

    assert dialog._save_current() is True
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

    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.analog import (
        AnalogActionThreshold,
        AnalogControlConfig,
        AnalogGamepadOutputConfig,
        AnalogMouseMotionConfig,
    )
    from keymasq.common.model.core import ActionType
    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog
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
    assert dialog.editor.current_mode() == "gamepad"
    dialog.editor.description_entry.set_text("Edited")

    assert dialog._save_current() is True
    saved = dialog.manager.get_analog_control("Combined")
    assert saved is not None
    assert saved.mouse_motion.enabled is False
    assert saved.gamepad_output.enabled is True
    assert saved.thresholds == []


def test_analog_control_dialog_does_not_offer_mouse_plus_digital_mode(
    temp_config_dir,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())

    def mode_labels() -> list[str]:
        model = dialog.editor.mode_dropdown.get_model()
        assert isinstance(model, Gtk.StringList)
        return [model.get_string(index) or "" for index in range(model.get_n_items())]

    assert "Mouse + Digital" not in mode_labels()

    _select_input_type(dialog, "axis")

    assert "Mouse + Digital" not in mode_labels()


def test_analog_control_dialog_loads_old_mouse_plus_digital_as_digital(
    temp_config_dir,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.analog import (
        AnalogActionThreshold,
        AnalogControlConfig,
        AnalogMouseMotionConfig,
    )
    from keymasq.common.model.core import ActionType
    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog
    from keymasq.session.analog_controls import AnalogControlManager

    AnalogControlManager().save_analog_control(
        AnalogControlConfig(
            name="Old Combined",
            mouse_motion=AnalogMouseMotionConfig(enabled=True),
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

    assert dialog.editor.current_mode() == "digital"


def test_analog_selector_filters_controls_by_source_input_type(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.model.analog import AnalogControlConfig
    from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog
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


def test_analog_selector_emits_selected_control_names(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.analog import AnalogControlConfig
    from keymasq.common.model.core import ActionType
    from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog
    from keymasq.session.analog_controls import AnalogControlManager

    manager = AnalogControlManager()
    manager.save_analog_control(AnalogControlConfig(name="Mouse"))
    manager.save_analog_control(AnalogControlConfig(name="WASD"))

    results: list[MappingAction] = []
    dialog = KeySelectorDialog(
        Gtk.Window(),
        "Left Stick",
        current_action=MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_names=["Mouse", "WASD"],
        ),
        source_type="analog",
        analog_input_type="stick",
    )
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))

    assert dialog._selected_analog_controls == ["Mouse", "WASD"]

    dialog._on_analog_control_map_clicked(dialog.map_btn)

    assert results[0].action_type == ActionType.ANALOG_CONTROL
    assert results[0].analog_control_names == ["Mouse", "WASD"]


def test_analog_selector_falls_back_to_singular_control_name(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.analog import AnalogControlConfig
    from keymasq.common.model.core import ActionType
    from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog
    from keymasq.session.analog_controls import AnalogControlManager

    AnalogControlManager().save_analog_control(AnalogControlConfig(name="Mouse"))
    action = MappingAction(
        action_type=ActionType.ANALOG_CONTROL,
        analog_control_name="Mouse",
    )
    action.analog_control_names = []

    dialog = KeySelectorDialog(
        Gtk.Window(),
        "Left Stick",
        current_action=action,
        source_type="analog",
        analog_input_type="stick",
    )

    assert dialog._selected_analog_control == "Mouse"
    assert dialog._selected_analog_controls == ["Mouse"]


def test_analog_selector_clicking_selected_control_deselects_it(temp_config_dir) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk

    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.analog import AnalogControlConfig
    from keymasq.common.model.core import ActionType
    from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog
    from keymasq.session.analog_controls import AnalogControlManager

    AnalogControlManager().save_analog_control(AnalogControlConfig(name="Mouse"))

    dialog = KeySelectorDialog(
        Gtk.Window(),
        "Left Stick",
        current_action=MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_names=["Mouse"],
        ),
        source_type="analog",
        analog_input_type="stick",
    )
    row = dialog._analog_control_listbox.get_row_at_index(0)

    assert row is not None
    assert row.is_selected()
    assert dialog.map_btn.get_sensitive() is True

    dialog._on_analog_control_row_pressed(Gtk.GestureClick(), 1, 0.0, 0.0, row)
    while GLib.main_context_default().iteration(False):
        pass

    assert not row.is_selected()
    assert dialog._selected_analog_controls == []
    assert dialog.map_btn.get_sensitive() is False


def test_analog_selector_presets_tab_hides_rapidfire_tap_footer(
    temp_config_dir,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog

    dialog = KeySelectorDialog(Gtk.Window(), "Left Stick", source_type="analog")

    assert dialog.stack.get_visible_child_name() == "analog_presets"
    assert dialog.options_box.get_visible() is False


def test_analog_manager_changed_switches_presets_to_control_picker(
    temp_config_dir,
    monkeypatch,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.model.analog import AnalogControlConfig
    from keymasq.gui.widgets.key_selector import analog_tab as dialog_module
    from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog
    from keymasq.session.analog_controls import AnalogControlManager

    monkeypatch.setattr(dialog_module, "notify_session_reload_async", lambda: None)

    dialog = KeySelectorDialog(Gtk.Window(), "Left Stick", source_type="analog")
    assert dialog.stack.get_visible_child_name() == "analog_presets"

    AnalogControlManager().save_analog_control(AnalogControlConfig(name="Mouse"))
    dialog._on_analog_control_manager_changed(dialog, "Mouse")

    assert dialog.stack.get_visible_child_name() == "analog_control"
    assert dialog.map_btn.get_sensitive() is False


def test_analog_manager_changed_clears_deleted_control_selection(
    temp_config_dir,
    monkeypatch,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.analog import AnalogControlConfig
    from keymasq.common.model.core import ActionType
    from keymasq.gui.widgets.key_selector import analog_tab as dialog_module
    from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog
    from keymasq.session.analog_controls import AnalogControlManager

    monkeypatch.setattr(dialog_module, "notify_session_reload_async", lambda: None)

    manager = AnalogControlManager()
    manager.save_analog_control(AnalogControlConfig(name="Mouse"))
    dialog = KeySelectorDialog(
        Gtk.Window(),
        "Left Stick",
        current_action=MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_names=["Mouse"],
        ),
        source_type="analog",
        analog_input_type="stick",
    )

    assert dialog.stack.get_visible_child_name() == "analog_control"
    assert dialog.map_btn.get_sensitive() is True

    manager.delete_analog_control("Mouse")
    dialog._on_analog_control_manager_changed(dialog, "Mouse")

    assert dialog._selected_analog_controls == []
    assert dialog._selected_analog_control is None
    assert dialog.map_btn.get_sensitive() is False


def test_analog_control_dialog_select_control_by_name_selects_saved_control(
    temp_config_dir,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.model.analog import AnalogControlConfig
    from keymasq.gui.widgets.analog_control.dialog import AnalogControlDialog
    from keymasq.session.analog_controls import AnalogControlManager

    manager = AnalogControlManager()
    manager.save_analog_control(AnalogControlConfig(name="Alpha"))
    manager.save_analog_control(AnalogControlConfig(name="Beta"))

    dialog = AnalogControlDialog(Gtk.Window())

    dialog.select_control_by_name("Beta")

    assert dialog._current_name == "Beta"
    assert dialog.editor.name_entry.get_text() == "Beta"


def test_analog_selector_right_click_opens_manager_for_control(
    temp_config_dir,
    monkeypatch,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.model.analog import AnalogControlConfig
    from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog
    from keymasq.session.analog_controls import AnalogControlManager

    AnalogControlManager().save_analog_control(AnalogControlConfig(name="Mouse"))
    dialog = KeySelectorDialog(
        Gtk.Window(),
        "Left Stick",
        source_type="analog",
        analog_input_type="stick",
    )
    opened: list[str | None] = []
    monkeypatch.setattr(dialog, "_open_analog_control_manager", opened.append)

    dialog._on_analog_control_row_right_pressed(
        Gtk.GestureClick(),
        1,
        0.0,
        0.0,
        "Mouse",
    )

    assert opened == ["Mouse"]


def test_analog_selector_open_manager_presents_and_selects_requested_control(
    temp_config_dir,
    monkeypatch,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    import keymasq.gui.widgets.analog_control.dialog as analog_dialog_module
    from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog

    captured: dict[str, object] = {}
    parent = Gtk.Window()
    profile_manager = object()
    parent.profile_manager = profile_manager

    class DummyAnalogControlDialog:
        def __init__(self, root, profile_manager_arg):
            captured["root"] = root
            captured["profile_manager"] = profile_manager_arg
            captured["signals"] = []

        def connect(self, signal_name, callback):
            captured["signals"].append(signal_name)
            captured[signal_name] = callback

        def present(self, root):
            captured["present_root"] = root

        def select_control_by_name(self, name):
            captured["selected_name"] = name

    monkeypatch.setattr(
        analog_dialog_module,
        "AnalogControlDialog",
        DummyAnalogControlDialog,
    )

    dialog = KeySelectorDialog(
        parent,
        "Left Stick",
        source_type="analog",
        analog_input_type="stick",
    )
    monkeypatch.setattr(dialog, "get_root", lambda: parent)

    dialog._open_analog_control_manager("Mouse")

    assert captured["root"] is parent
    assert captured["profile_manager"] is profile_manager
    assert captured["present_root"] is parent
    assert captured["signals"] == ["analog-control-saved", "analog-control-deleted"]
    assert captured["selected_name"] == "Mouse"


def test_analog_selector_docs_button_links_to_analog_controls_docs(
    temp_config_dir,
    monkeypatch,
) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.key_selector import targets as dialog_module
    from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog

    monkeypatch.setattr(dialog_module, "__version__", "1.2.3")

    dialog = KeySelectorDialog(Gtk.Window(), "Left Stick", source_type="analog")

    # With no saved controls the dialog opens on the Presets tab, which still
    # links to the Analog Controls docs.
    assert dialog.stack.get_visible_child_name() == "analog_presets"
    assert dialog.actions_docs_btn.get_visible() is True
    assert dialog.actions_docs_btn.get_tooltip_text() == "Open Analog Controls documentation"
    assert dialog._active_actions_docs_link() == ("analog-controls", "Analog Controls")
    assert dialog_module._actions_docs_url("analog-controls") == (
        "https://keymasq.tools/docs/v1.2.3/ANALOG_CONTROLS/"
    )


def test_analog_control_dialog_groups_saved_controls_by_input_type() -> None:
    from keymasq.common.model.analog import AnalogControlConfig
    from keymasq.gui.widgets.analog_control.options import group_analog_control_names

    configs = {
        "Stick Control": AnalogControlConfig(name="Stick Control"),
        "Axis Control": AnalogControlConfig(name="Axis Control", input_type="axis"),
    }

    assert group_analog_control_names(
        ["Stick Control", "Axis Control"],
        configs,
    ) == [
        ("1D Axes / Triggers", ["Axis Control"]),
        ("Sticks", ["Stick Control"]),
    ]
