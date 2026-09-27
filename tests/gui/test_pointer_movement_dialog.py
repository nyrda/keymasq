# ruff: noqa: E402

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.common.model.pointer import PointerMovementConfig
from keymasq.gui.widgets.gamepad_output_choices import GamepadOutputChoiceSet
from keymasq.gui.widgets.pointer_movement_dialog import PointerMovementDialog


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
