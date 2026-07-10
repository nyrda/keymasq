from types import SimpleNamespace
from typing import Any, cast

from keymasq.keymasqd.runtime.analog import thresholds
from keymasq.keymasqd.runtime.combo import actions
from keymasq.keymasqd.runtime.combo.state import ComboRuntimeState
from keymasq.keymasqd.runtime.grabbed_device import outputs
from keymasq.keymasqd.runtime.grabbed_device.types import GrabbedDeviceState


def test_refcounted_held_output_balances_shared_references() -> None:
    refcounts: dict[int, int] = {}
    held: set[int] = set()

    assert outputs.track_refcounted_held_output(
        refcounts,
        held,
        30,
        pressed=True,
        released=False,
    )
    assert not outputs.track_refcounted_held_output(
        refcounts,
        held,
        30,
        pressed=True,
        released=False,
    )
    assert refcounts == {30: 2}
    assert held == {30}

    assert outputs.track_refcounted_held_output(
        refcounts,
        held,
        30,
        pressed=False,
        released=False,
    )
    assert refcounts == {30: 2}
    assert held == {30}

    assert not outputs.track_refcounted_held_output(
        refcounts,
        held,
        30,
        pressed=False,
        released=True,
    )
    assert refcounts == {30: 1}
    assert held == {30}

    assert outputs.track_refcounted_held_output(
        refcounts,
        held,
        30,
        pressed=False,
        released=True,
    )
    assert refcounts == {}
    assert held == set()

    assert not outputs.track_refcounted_held_output(
        refcounts,
        held,
        30,
        pressed=False,
        released=True,
    )


def test_refcounted_output_bucket_tracks_key_and_abs_styles() -> None:
    refcount_buckets: dict[str, dict[int, int]] = {}
    held_buckets: dict[str, set[int]] = {}

    assert outputs.track_refcounted_output_bucket(
        refcount_buckets,
        held_buckets,
        "keyboard",
        30,
        1,
    )
    assert not outputs.track_refcounted_output_bucket(
        refcount_buckets,
        held_buckets,
        "keyboard",
        30,
        1,
    )
    assert refcount_buckets["keyboard"] == {30: 2}
    assert held_buckets["keyboard"] == {30}

    assert not outputs.track_refcounted_output_bucket(
        refcount_buckets,
        held_buckets,
        "keyboard",
        30,
        0,
    )
    assert outputs.track_refcounted_output_bucket(
        refcount_buckets,
        held_buckets,
        "keyboard",
        30,
        0,
    )
    assert refcount_buckets["keyboard"] == {}
    assert held_buckets["keyboard"] == set()

    assert outputs.track_refcounted_output_bucket(
        refcount_buckets,
        held_buckets,
        "gamepad",
        2,
        255,
        pressed_value=None,
    )
    assert not outputs.track_refcounted_output_bucket(
        refcount_buckets,
        held_buckets,
        "gamepad",
        2,
        -255,
        pressed_value=None,
    )
    assert refcount_buckets["gamepad"] == {2: 2}
    assert held_buckets["gamepad"] == {2}

    assert not outputs.track_refcounted_output_bucket(
        refcount_buckets,
        held_buckets,
        "gamepad",
        2,
        0,
        pressed_value=None,
    )
    assert outputs.track_refcounted_output_bucket(
        refcount_buckets,
        held_buckets,
        "gamepad",
        2,
        0,
        pressed_value=None,
    )
    assert refcount_buckets["gamepad"] == {}
    assert held_buckets["gamepad"] == set()


def test_analog_threshold_key_output_uses_refcount_lifecycle() -> None:
    state = GrabbedDeviceState()
    device = cast(Any, SimpleNamespace(state=state))

    assert thresholds.track_threshold_output(device, "keyboard", 30, 1)
    assert not thresholds.track_threshold_output(device, "keyboard", 30, 1)
    assert state.analog_threshold_output_refcounts["keyboard"] == {30: 2}
    assert state.held_output_keys["keyboard"] == {30}

    assert not thresholds.track_threshold_output(device, "keyboard", 30, 0)
    assert state.analog_threshold_output_refcounts["keyboard"] == {30: 1}
    assert state.held_output_keys["keyboard"] == {30}

    assert thresholds.track_threshold_output(device, "keyboard", 30, 0)
    assert state.analog_threshold_output_refcounts["keyboard"] == {}
    assert state.held_output_keys["keyboard"] == set()


def test_analog_threshold_abs_output_uses_refcount_lifecycle() -> None:
    state = GrabbedDeviceState()
    device = cast(Any, SimpleNamespace(state=state))

    assert thresholds.track_threshold_abs_output(device, "gamepad", 2, 255)
    assert not thresholds.track_threshold_abs_output(device, "gamepad", 2, -255)
    assert state.analog_threshold_abs_refcounts["gamepad"] == {2: 2}
    assert state.held_output_abs["gamepad"] == {2}

    assert not thresholds.track_threshold_abs_output(device, "gamepad", 2, 0)
    assert state.analog_threshold_abs_refcounts["gamepad"] == {2: 1}
    assert state.held_output_abs["gamepad"] == {2}

    assert thresholds.track_threshold_abs_output(device, "gamepad", 2, 0)
    assert state.analog_threshold_abs_refcounts["gamepad"] == {}
    assert state.held_output_abs["gamepad"] == set()


def test_combo_superkey_output_uses_refcount_lifecycle() -> None:
    combo_state = ComboRuntimeState()
    manager = cast(Any, SimpleNamespace(combo_state=combo_state))

    assert actions.track_combo_superkey_output(manager, "keyboard", 30, 1)
    assert not actions.track_combo_superkey_output(manager, "keyboard", 30, 1)
    assert combo_state.superkey_output_refcounts["keyboard"] == {30: 2}
    assert combo_state.held_output_keys["keyboard"] == {30}

    assert not actions.track_combo_superkey_output(manager, "keyboard", 30, 0)
    assert combo_state.superkey_output_refcounts["keyboard"] == {30: 1}
    assert combo_state.held_output_keys["keyboard"] == {30}

    assert actions.track_combo_superkey_output(manager, "keyboard", 30, 0)
    assert combo_state.superkey_output_refcounts["keyboard"] == {}
    assert combo_state.held_output_keys["keyboard"] == set()
