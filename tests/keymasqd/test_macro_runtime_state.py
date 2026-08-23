import pytest

from keymasq.keymasqd.runtime.macro.loops import (
    MacroLoopStateMachine,
    normalize_loop_mode,
    plan_loop_stop,
)
from keymasq.keymasqd.runtime.macro.state import MacroRuntimeState
from keymasq.keymasqd.runtime.macro.timing import MacroPlaybackTimeline


@pytest.mark.parametrize("value", [None, "", "unsupported", object()])
def test_normalize_loop_mode_falls_back_to_single_run(value: object) -> None:
    assert normalize_loop_mode(value) == "none"


def test_count_loop_state_finishes_after_requested_iterations() -> None:
    loop = MacroLoopStateMachine("count", count=3)

    loop.begin_iteration()
    assert loop.should_continue() is True
    loop.begin_iteration()
    assert loop.should_continue() is True
    loop.begin_iteration()
    assert loop.should_continue() is False


@pytest.mark.parametrize("mode", ["hold", "toggle"])
def test_open_ended_loop_state_stops_only_when_requested(mode: str) -> None:
    loop = MacroLoopStateMachine(mode)

    loop.begin_iteration()
    assert loop.should_continue() is True
    loop.request_stop()
    assert loop.should_continue() is False


def test_loop_stop_plan_separates_cancel_from_finish_current_run() -> None:
    state = MacroRuntimeState(
        instance_meta={
            1: {"loop_stop_behavior": "cancel_run"},
            2: {"loop_stop_behavior": "finish_run"},
        }
    )

    plan = plan_loop_stop(state, [1, 2])

    assert plan.cancel_instance_ids == (1,)
    assert plan.finish_instance_ids == (2,)


def test_macro_runtime_state_allocates_explicit_instance_metadata() -> None:
    state = MacroRuntimeState()

    instance_id = state.allocate_instance(
        loop_mode="hold",
        source_key=("mouse", "BTN_SIDE"),
        macro_name="hold action",
        loop_stop_behavior="finish_run",
    )

    assert instance_id == 1
    assert state.instance_held == {1: set()}
    assert state.instance_held_abs == {1: set()}
    assert state.instance_meta[1] == {
        "loop_mode": "hold",
        "source_device": "mouse",
        "source_button": "BTN_SIDE",
        "macro_name": "hold action",
        "loop_active": True,
        "loop_stop_behavior": "finish_run",
        "parent_instance_id": None,
        "root_instance_id": 1,
        "source_lifecycle_available": True,
        "source_lifecycle_active": True,
    }


def test_playback_timeline_anchors_events_and_tracks_blocking_actions() -> None:
    timeline = MacroPlaybackTimeline(anchor_s=10.0, speed_factor=2.0)

    assert timeline.event_deadline(2_000_000) == pytest.approx(11.0)
    timeline.extend_for_blocking_action(0.25)
    assert timeline.event_deadline(2_000_000) == pytest.approx(11.25)
    assert timeline.event_delay(2_000_000, now_s=10.75) == pytest.approx(0.5)


def test_playback_timeline_does_not_repeat_control_wait_at_nominal_end() -> None:
    timeline = MacroPlaybackTimeline(anchor_s=20.0, speed_factor=1.0)
    timeline.extend_for_blocking_action(2.5)

    assert timeline.nominal_end_deadline(1_000_000) == pytest.approx(21.0)
    assert timeline.nominal_end_delay(1_000_000, now_s=22.5) == 0.0
