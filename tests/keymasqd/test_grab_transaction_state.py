from types import SimpleNamespace

from keymasq.keymasqd.runtime.grab.release import hardware_release_decision
from keymasq.keymasqd.runtime.grab.state import (
    GrabAcquisitionState,
    GrabPlan,
)


def _plan(*, requested: bool = False, existing: list[object] | None = None) -> GrabPlan:
    return GrabPlan(
        hardware_id="hw",
        raw_interfaces=[{"path": "keymasq:1234:5678"}] if requested else [],
        evdev_interfaces_provided=requested,
        resolved_interfaces=[],
        requested_paths=set(),
        requested_claim_paths=set(),
        resolved_by_claim_path={},
        desired_paths=set(),
        mapped_evdev_names=set(),
        resolved_button_codes={},
        resolved_button_values={},
        button_mapped_bindings=set(),
        mapped_bindings=set(),
        analog_inputs={},
        existing_devices=existing or [],
        existing_by_claim_path={},
        previous_desired_paths=None,
        previous_desired_config=None,
        requests_gamepad_source_hiding=False,
    )


def test_acquisition_state_reports_waiting_only_before_an_interface_is_available() -> None:
    state = GrabAcquisitionState(devices=[])
    plan = _plan(requested=True)

    assert state.is_waiting_for_device(plan) is True

    state.available_count = 1
    assert state.is_waiting_for_device(plan) is False


def test_acquisition_state_tracks_transaction_owned_devices_by_identity() -> None:
    existing = SimpleNamespace(path="existing")
    equivalent_but_new = SimpleNamespace(path="existing")
    plan = _plan(existing=[existing])
    state = GrabAcquisitionState(devices=[existing, equivalent_but_new])

    assert state.owns_device(existing, plan) is False
    assert state.owns_device(equivalent_but_new, plan) is True


class _HeldDevice:
    def __init__(self, held: bool) -> None:
        self.held = held

    def has_held_source_inputs(self) -> bool:
        return self.held


def _release_manager(*, desired: bool, held: bool) -> SimpleNamespace:
    return SimpleNamespace(
        grabbed_devices={"hw": [_HeldDevice(held)]},
        grab_state=SimpleNamespace(
            desired_paths={"hw": {"/dev/input/event1"} if desired else set()},
            held_release_retry_s=2.5,
        ),
    )


def test_hardware_release_decision_cancels_when_hardware_is_desired_again() -> None:
    decision = hardware_release_decision(
        _release_manager(desired=True, held=True),
        "hw",
    )

    assert decision.action == "cancel"
    assert decision.next_delay is None


def test_hardware_release_decision_defers_held_input_then_releases() -> None:
    manager = _release_manager(desired=False, held=True)

    decision = hardware_release_decision(manager, "hw")
    assert decision.action == "defer"
    assert decision.next_delay == 2.5

    manager.grabbed_devices["hw"][0].held = False
    decision = hardware_release_decision(manager, "hw")
    assert decision.action == "release"
    assert decision.next_delay is None
