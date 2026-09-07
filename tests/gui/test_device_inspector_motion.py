"""Motion preview follows sensor frames without changing remap state."""

import math

import pytest

from keymasq.gui.widgets.device_inspector.motion_model import GRAVITY, MotionState, rotate


def motion_sensor():
    return {
        "id": "motion",
        "label": "Nintendo Motion Sensor",
        "kind": "motion",
        "source": "imu",
        "profile_name": "Desktop",
        "action": {"action": "suppress"},
        "gyro_axes": [
            {"role": role, "evdev": f"abs_r{axis}", "evdev_code": code, "scale": 0.001}
            for role, axis, code in (("pitch", "x", 3), ("yaw", "y", 4), ("roll", "z", 5))
        ],
        "accelerometer_axes": [
            {"role": role, "evdev": f"abs_{role}", "evdev_code": code, "scale": GRAVITY / 1000}
            for code, role in enumerate(("x", "y", "z"))
        ],
    }


def sample(code, value, source="imu"):
    return {"type_name": "ev_abs", "code": code, "value": value, "source": source}


def frame(time_us):
    return {"type_name": "ev_syn", "code": 0, "time_us": time_us, "source": "imu"}


def test_motion_uses_calibration_and_ignores_the_gamepad_interface():
    sensor = motion_sensor()
    sensor["gyro_axes"][0].update(offset=10, scale=0.01, invert=True, noise=0.02)
    state = MotionState(sensor)
    state.accept(sample(3, 60, source="pad"))
    assert not state.raw
    state.accept(sample(3, 60))
    assert state.values["gyro", "pitch"] == pytest.approx(-0.5)
    state.accept(sample(3, 11))
    assert state.raw["gyro", "pitch"] == 11
    assert state.values["gyro", "pitch"] == 0.0


def test_motion_integrates_once_per_frame_and_recenter_only_changes_reference():
    sensor = motion_sensor()
    sensor["accelerometer_axes"] = []
    state = MotionState(sensor)
    state.accept(sample(4, 1000))
    assert not state.ready
    state.accept(frame(1_000_000))
    for index in range(1, 101):
        state.accept(frame(1_000_000 + 10_000 * index))
    assert rotate(state.orientation, (1.0, 0.0, 0.0)) == pytest.approx(
        (math.cos(1.0), math.sin(1.0), 0.0)
    )
    pose = state.orientation
    state.recenter()
    assert rotate(state.display_orientation, (1.0, 0.0, 0.0)) == pytest.approx((1, 0, 0))
    assert state.orientation == pose
    assert state.values["gyro", "yaw"] == 1.0
    # Never integrate seconds of stale velocity after a pause or clock jump.
    state.accept(frame(10_000_000))
    state.accept(frame(9_000_000))
    assert state.orientation == pose


@pytest.mark.parametrize("gravity", [(0, 0, 1000), (0, 600, 800), (600, 0, 800), (0, 0, -1000)])
def test_motion_gravity_orients_the_controller_including_upside_down(gravity):
    state = MotionState(motion_sensor())
    for code, value in enumerate(gravity):
        state.accept(sample(code, value))
    state.accept(frame(1_000_000))
    assert rotate(state.orientation, tuple(value / 1000 for value in gravity)) == pytest.approx(
        (0, 0, 1)
    )
    assert all(math.isfinite(value) for value in state.orientation)


def test_motion_drop_discards_partial_frames_and_waits_for_new_samples():
    state = MotionState(motion_sensor())
    state.accept(sample(4, 1000))
    state.accept(frame(1_000_000))
    state.accept({"type_name": "ev_syn", "code": 3, "source": "imu"})
    state.accept(sample(4, 2000))
    state.accept(frame(1_010_000))
    assert not state.ready
    assert not state.values
    state.accept(sample(4, 3000))
    state.accept(frame(1_020_000))
    assert state.ready
    assert state.values["gyro", "yaw"] == 3.0


def test_motion_shake_does_not_become_a_tilt_reference():
    state = MotionState(motion_sensor())
    for code, value in enumerate((3000, 0, 1000)):
        state.accept(sample(code, value))
    state.accept(frame(1_000_000))
    assert not state.gravity_initialized
    assert rotate(state.orientation, (0.0, 0.0, 1.0)) == pytest.approx((0, 0, 1))


def test_motion_unscoped_sensor_never_mixes_interfaces():
    sensor = motion_sensor()
    sensor["source"] = ""
    state = MotionState(sensor)
    state.accept(sample(4, 1000))
    state.accept(sample(4, 2000, source="pad"))
    assert state.values["gyro", "yaw"] == 1.0
