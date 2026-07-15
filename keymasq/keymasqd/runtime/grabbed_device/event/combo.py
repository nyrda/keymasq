"""Explicit combo-interception transitions for grabbed input events."""

from collections.abc import Mapping, Set
from dataclasses import dataclass

from keymasq.common.combos import normalize_combo_evdev
from keymasq.common.model.actions import MappingAction
from keymasq.keymasqd.combo_engine import ComboDecision
from keymasq.keymasqd.runtime.grabbed_device.types import GrabbedDeviceState


@dataclass(frozen=True)
class RecalledEventInterception:
    """Decision made before a recalled event reaches the combo callback."""

    stop_processing: bool = False
    suppress_release_after_callback: bool = False
    diagnostic_label: str | None = None


@dataclass(frozen=True)
class ComboCallbackRoute:
    """Routing decision derived from the combo callback's response."""

    stop_processing: bool = False
    combo_consumed: bool = False
    passthrough_requested: bool = False
    clear_released_source_action: bool = False


def intercept_recalled_event(
    state: GrabbedDeviceState,
    *,
    event_is_key: bool,
    event_name: str,
    event_value: int,
) -> RecalledEventInterception:
    """Advance recalled-key state before normal combo matching.

    Recalled repeats remain pending and are suppressed.  A later press or
    release consumes the pending recall marker; releases are allowed to reach
    the combo callback before their passthrough is suppressed.
    """

    normalized_event_name = normalize_combo_evdev(event_name)
    if not event_is_key or normalized_event_name not in state.combo_recalled_bindings:
        return RecalledEventInterception()
    if event_value == 2:
        return RecalledEventInterception(
            stop_processing=True,
            diagnostic_label="combo_recalled_repeat_suppressed",
        )

    state.combo_recalled_bindings.discard(normalized_event_name)
    state.combo_passthrough_held.discard(event_name)
    return RecalledEventInterception(suppress_release_after_callback=event_value == 0)


def route_combo_callback_result(
    result: ComboDecision | bool | None,
    *,
    event_is_key: bool,
    event_value: int,
    event_name: str,
    held_source_actions: Mapping[str, MappingAction | None],
    combo_passthrough_held: Set[str],
) -> ComboCallbackRoute:
    """Convert the callback result into an event-loop routing decision."""

    if result is True:
        return ComboCallbackRoute(
            stop_processing=True,
            clear_released_source_action=True,
        )
    if not isinstance(result, ComboDecision):
        return ComboCallbackRoute()

    passthrough_requested = result.passthrough_current_event
    if not result.consume_current_event:
        return ComboCallbackRoute(passthrough_requested=passthrough_requested)

    release_has_existing_route = (
        event_is_key
        and event_value == 0
        and (event_name in held_source_actions or event_name in combo_passthrough_held)
    )
    if not release_has_existing_route:
        return ComboCallbackRoute(stop_processing=True)
    return ComboCallbackRoute(
        combo_consumed=True,
        passthrough_requested=passthrough_requested,
    )


def is_combo_passthrough_held_event(
    state: GrabbedDeviceState,
    *,
    event_is_key: bool,
    event_name: str,
) -> bool:
    return event_is_key and event_name in state.combo_passthrough_held


def finish_combo_passthrough_held_event(
    state: GrabbedDeviceState,
    *,
    event_name: str,
    event_value: int,
) -> bool:
    """Advance held-passthrough state and report whether this was a release."""

    if event_value != 0:
        return False
    state.combo_passthrough_held.discard(event_name)
    return True
