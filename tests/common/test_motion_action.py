import pytest

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType


@pytest.mark.parametrize(
    "singular,plural,expected",
    [
        (" Aim ", [], ["Aim"]),
        (" Aim ", [" ", ""], ["Aim"]),
        ("Other", [" Aim ", "Aim", " Tilt ", ""], ["Aim", "Tilt"]),
        (" ", [" "], []),
        (None, [], []),
    ],
)
def test_motion_action_normalizes_named_references(singular, plural, expected):
    action = MappingAction(
        action_type=ActionType.MOTION_CONTROL,
        motion_control_name=singular,
        motion_control_names=plural,
    )

    assert action.motion_control_names == expected
    assert action.motion_control_name == (expected[0] if expected else None)
