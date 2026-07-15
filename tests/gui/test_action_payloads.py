import pytest

from keymasq.common.model.core import ActionType
from keymasq.gui.widgets.action_payloads import mapping_action_from_payload


@pytest.mark.parametrize(
    ("payload", "expected_type", "expected_fields", "profile_deactivates_on_end"),
    [
        pytest.param(
            {
                "action": "macro",
                "target": "Looped Macro",
                "loop_stop_behavior": "cancel_run",
                "keys": "not-a-list",
            },
            ActionType.MACRO,
            {
                "macro_name": "Looped Macro",
                "macro_loop_stop_behavior": "cancel_run",
                "keys": None,
            },
            False,
            id="macro-loop-stop",
        ),
        pytest.param(
            {
                "action": "future_action",
                "target": "future-target",
            },
            ActionType.PASSTHROUGH,
            {"target": "future-target"},
            False,
            id="unknown-action",
        ),
        pytest.param(
            {"action": "mpris", "command": "stop"},
            ActionType.MPRIS,
            {"mpris_command": "stop"},
            False,
            id="mpris",
        ),
        pytest.param(
            {
                "action": "profile_toggle",
                "profile_name": "Layer",
                "deactivation": {"on_trigger_end": True},
            },
            ActionType.PROFILE_TOGGLE,
            {"profile_name": "Layer"},
            True,
            id="profile-deactivation",
        ),
    ],
)
def test_mapping_action_from_payload_preserves_inspector_contract(
    payload: dict[str, object],
    expected_type: ActionType,
    expected_fields: dict[str, object],
    profile_deactivates_on_end: bool,
) -> None:
    action = mapping_action_from_payload(payload)

    assert action is not None
    assert action.action_type == expected_type
    for field, value in expected_fields.items():
        assert getattr(action, field) == value
    if profile_deactivates_on_end:
        assert action.profile_deactivation is not None
        assert action.profile_deactivation.on_trigger_end is True
    else:
        assert action.profile_deactivation is None
