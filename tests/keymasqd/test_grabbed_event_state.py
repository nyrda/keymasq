from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import evdev
import pytest

from keymasq.keymasqd.combo_engine import ComboDecision
from keymasq.keymasqd.runtime.grabbed_device import outputs
from keymasq.keymasqd.runtime.grabbed_device.event.classification import (
    EventClass,
    classify_event,
)
from keymasq.keymasqd.runtime.grabbed_device.event.combo import (
    ComboCallbackRoute,
    finish_combo_passthrough_held_event,
    intercept_recalled_event,
    route_combo_callback_result,
)
from keymasq.keymasqd.runtime.grabbed_device.event.passthrough import (
    process_syn_event,
)
from keymasq.keymasqd.runtime.grabbed_device.types import (
    GrabbedDeviceState,
    InputEventLike,
)
from tests.keymasqd.device_manager_support import FakeUInput, make_grabbed_device


def _event(event_type: int, event_code: int = 0) -> InputEventLike:
    return cast(
        InputEventLike,
        SimpleNamespace(type=event_type, code=event_code, value=0),
    )


def test_event_classifier_exposes_pipeline_routes_without_a_device_runtime() -> None:
    analog_binding = (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X)

    assert (
        classify_event(
            _event(91),
            evdev_mod=evdev,
            analog_axis_bindings=set(),
            passthrough_echo_event_types={91},
        )
        is EventClass.PASSTHROUGH_ECHO
    )
    assert (
        classify_event(
            _event(evdev.ecodes.EV_SYN),
            evdev_mod=evdev,
            analog_axis_bindings=set(),
        )
        is EventClass.SYNCHRONIZATION
    )
    assert (
        classify_event(
            _event(*analog_binding),
            evdev_mod=evdev,
            analog_axis_bindings={analog_binding},
        )
        is EventClass.ANALOG_AXIS
    )
    assert (
        classify_event(
            _event(evdev.ecodes.EV_KEY),
            evdev_mod=evdev,
            analog_axis_bindings=set(),
        )
        is EventClass.KEY
    )
    assert (
        classify_event(
            _event(evdev.ecodes.EV_REL),
            evdev_mod=evdev,
            analog_axis_bindings=set(),
        )
        is EventClass.RELATIVE
    )
    assert (
        classify_event(
            _event(evdev.ecodes.EV_MSC),
            evdev_mod=evdev,
            analog_axis_bindings=set(),
        )
        is EventClass.OTHER
    )


def test_recalled_key_transition_suppresses_repeat_then_clears_on_release() -> None:
    state = GrabbedDeviceState(
        combo_recalled_bindings={"key_a"},
        combo_passthrough_held={"key_a"},
    )

    repeat = intercept_recalled_event(
        state,
        event_is_key=True,
        event_name="key_a",
        event_value=2,
    )

    assert repeat.stop_processing is True
    assert repeat.diagnostic_label == "combo_recalled_repeat_suppressed"
    assert state.combo_recalled_bindings == {"key_a"}
    assert state.combo_passthrough_held == {"key_a"}

    release = intercept_recalled_event(
        state,
        event_is_key=True,
        event_name="key_a",
        event_value=0,
    )

    assert release.stop_processing is False
    assert release.suppress_release_after_callback is True
    assert state.combo_recalled_bindings == set()
    assert state.combo_passthrough_held == set()


def test_combo_callback_route_preserves_an_existing_release_route() -> None:
    consumed_press = route_combo_callback_result(
        ComboDecision(consume_current_event=True),
        event_is_key=True,
        event_value=1,
        event_name="key_a",
        held_source_actions={},
        combo_passthrough_held=set(),
    )
    consumed_release = route_combo_callback_result(
        ComboDecision(consume_current_event=True, passthrough_current_event=True),
        event_is_key=True,
        event_value=0,
        event_name="key_a",
        held_source_actions={"key_a": None},
        combo_passthrough_held=set(),
    )

    assert consumed_press == ComboCallbackRoute(stop_processing=True)
    assert consumed_release == ComboCallbackRoute(
        combo_consumed=True,
        passthrough_requested=True,
    )
    assert route_combo_callback_result(
        True,
        event_is_key=True,
        event_value=0,
        event_name="key_a",
        held_source_actions={},
        combo_passthrough_held=set(),
    ) == ComboCallbackRoute(
        stop_processing=True,
        clear_released_source_action=True,
    )


def test_combo_passthrough_held_transition_clears_only_on_release() -> None:
    state = GrabbedDeviceState(combo_passthrough_held={"key_a"})

    assert (
        finish_combo_passthrough_held_event(
            state,
            event_name="key_a",
            event_value=2,
        )
        is False
    )
    assert state.combo_passthrough_held == {"key_a"}

    assert (
        finish_combo_passthrough_held_event(
            state,
            event_name="key_a",
            event_value=0,
        )
        is True
    )
    assert state.combo_passthrough_held == set()


@pytest.mark.parametrize("event_code", [evdev.ecodes.SYN_CONFIG, evdev.ecodes.SYN_DROPPED])
def test_other_syn_events_close_frame_without_flushing(
    monkeypatch: pytest.MonkeyPatch, event_code: int
) -> None:
    passthrough = FakeUInput()
    syn = Mock()
    monkeypatch.setattr(passthrough, "syn", syn)
    device = make_grabbed_device(monkeypatch, passthrough_uinput=passthrough)
    outputs.mark_passthrough_frame_open(device, passthrough)

    assert process_syn_event(
        device, _event(evdev.ecodes.EV_SYN, event_code), evdev_mod=evdev
    ) == "syn"
    assert not outputs.passthrough_frame_open(device, passthrough)
    assert process_syn_event(
        device, _event(evdev.ecodes.EV_SYN, evdev.ecodes.SYN_REPORT), evdev_mod=evdev
    ) == "syn"
    assert passthrough.writes == []
    syn.assert_not_called()
