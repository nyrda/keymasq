from keymasq.common.model.actions import MappingAction, ProfileDeactivationPolicy
from keymasq.common.model.core import ActionType
from keymasq.keymasqd.runtime.combo.execution import (
    action_needs_release,
    profile_action_tracks_trigger,
    synthetic_event,
)


def test_release_policy_is_independent_of_combo_manager() -> None:
    assert action_needs_release(MappingAction(action_type=ActionType.KEYBOARD)) is True
    assert action_needs_release(MappingAction(action_type=ActionType.SUPPRESS)) is False
    assert (
        action_needs_release(
            MappingAction(action_type=ActionType.MOUSE_MOVE_NATURAL_ABS, tap_enabled=True)
        )
        is True
    )


def test_profile_trigger_tracking_uses_deactivation_policy() -> None:
    assert (
        profile_action_tracks_trigger(
            MappingAction(
                action_type=ActionType.PROFILE_ENABLE,
                profile_deactivation=ProfileDeactivationPolicy(on_trigger_end=True),
            )
        )
        is True
    )
    assert (
        profile_action_tracks_trigger(MappingAction(action_type=ActionType.PROFILE_DISABLE))
        is False
    )


def test_synthetic_event_tolerates_missing_trigger_binding() -> None:
    event = synthetic_event(None, 1)

    assert (event.type, event.code, event.value) == (0, 0, 1)
