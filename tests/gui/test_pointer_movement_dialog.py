# ruff: noqa: E402

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.common.model.pointer import PointerMovementConfig
from keymasq.gui.widgets.gamepad_output_choices import GamepadOutputChoiceSet
from keymasq.gui.widgets.pointer_movement_dialog import PointerMovementDialog
from keymasq.gui.widgets.spin_inputs import apply_spin_secondary_step


def _choices(_selected: str | None) -> GamepadOutputChoiceSet:
    return GamepadOutputChoiceSet(
        choices=[(None, "Virtual Gamepad 1"), ("virtual-gamepad-2", "Virtual Gamepad 2")],
        count=2,
        hardware_configs=[],
    )


def test_dialog_saves_every_setting_it_loaded_including_unavailable_axes() -> None:
    config = PointerMovementConfig(
        mode="axes",
        factor_x=0.25,
        factor_y=3.0,
        invert_y=True,
        swap_axes=True,
        output_id="virtual-gamepad-2",
        behavior="position",
        full_speed=2500,
        window_ms=40,
        radius=750,
        overshoot="keep",
        recenter_ms=300,
        x_axis="abs_x",
        y_axis="abs_throttle",
        x_direction="min",
        y_direction="max",
        deadzone=0.1,
        minimum_output=0.2,
        response_curve=1.5,
    )
    saved: list[MappingAction | None] = []
    dialog = PointerMovementDialog(
        "Mouse",
        MappingAction(action_type=ActionType.POINTER_MOVEMENT, pointer_movement=config),
        on_save=saved.append,
        output_choices_loader=_choices,
    )

    dialog.save_button.emit("clicked")
    dialog.remove_button.emit("clicked")

    assert saved[0] is not None
    assert saved[0].pointer_movement == config
    assert saved[1] is None


def test_mouse_mode_keeps_axis_settings_without_loading_outputs_and_offers_overrides() -> None:
    config = PointerMovementConfig(factor_y=2.0, x_axis="abs_throttle", y_axis=None)

    def unused_loader(_selected: str | None) -> GamepadOutputChoiceSet:
        raise AssertionError("mouse mode must not load gamepad outputs")

    saved: list[MappingAction | None] = []
    dialog = PointerMovementDialog(
        "Mouse",
        MappingAction(action_type=ActionType.POINTER_MOVEMENT, pointer_movement=config),
        on_save=saved.append,
        output_choices_loader=unused_loader,
    )
    dialog.save_button.emit("clicked")
    for index in (2, 3):
        dialog.mode_row.set_selected(index)
        dialog.save_button.emit("clicked")

    assert saved[0] is not None
    assert saved[0].pointer_movement == config
    assert [action.action_type for action in saved[1:] if action is not None] == [
        ActionType.PASSTHROUGH,
        ActionType.SUPPRESS,
    ]


def test_apply_reports_the_current_settings_and_enables_remove() -> None:
    saved: list[MappingAction | None] = []
    applied: list[MappingAction] = []
    dialog = PointerMovementDialog("Mouse", None, on_save=saved.append, on_apply=applied.append)
    assert not dialog.remove_button.get_sensitive()

    dialog.factor_y_row.set_value(2.0)
    dialog.apply_button.emit("clicked")
    dialog.factor_y_row.set_value(3.0)
    dialog.apply_button.emit("clicked")

    assert saved == []
    assert [action.pointer_movement.factor_y for action in applied if action.pointer_movement] == [
        2.0,
        3.0,
    ]
    assert dialog.remove_button.get_sensitive()


@pytest.mark.parametrize(
    ("value", "direction", "expected"),
    [
        (0.35, 1, 1.0),
        (1.0, 1, 2.0),
        (1.35, 1, 2.0),
        (1.35, -1, 1.0),
        (2.0, -1, 1.0),
        (3.5, -1, 3.0),
        (0.35, -1, 0.0),
        (49.5, 1, 50.0),
    ],
)
def test_factor_secondary_steps_land_on_whole_numbers(
    value: float, direction: int, expected: float
) -> None:
    dialog = PointerMovementDialog("Mouse", None, on_save=lambda _action: None)
    dialog.factor_x_row.set_value(value)

    apply_spin_secondary_step(dialog.factor_x_row, direction, 1.0, snap_to_step=True)

    assert dialog.factor_x_row.get_value() == pytest.approx(expected)


def test_untouched_fields_keep_precision_the_display_rounds_and_typed_text_still_saves() -> None:
    config = PointerMovementConfig(
        factor_x=0.004, factor_y=1.0, minimum_output=0.123, response_curve=1.234
    )
    saved: list[MappingAction | None] = []
    dialog = PointerMovementDialog(
        "Mouse",
        MappingAction(action_type=ActionType.POINTER_MOVEMENT, pointer_movement=config),
        on_save=saved.append,
        output_choices_loader=_choices,
    )

    dialog.apply_button.emit("clicked")
    for row in (dialog.factor_x_row, dialog.minimum_output_row, dialog.response_curve_row):
        row.update()
    dialog.factor_y_row.set_text("3")
    dialog.save_button.emit("clicked")

    assert saved[0] is not None and saved[0].pointer_movement == config
    assert saved[1] is not None and saved[1].pointer_movement is not None
    assert saved[1].pointer_movement.factor_y == 3.0
    assert saved[1].pointer_movement.factor_x == 0.004
