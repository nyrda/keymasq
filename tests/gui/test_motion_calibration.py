import math
from typing import Any, cast

import evdev
import pytest

from keymasq.common.model.hardware import HardwareConfig
from keymasq.common.model.motion import MotionAxisDefinition, MotionSensorDefinition
from keymasq.gui.widgets.device_tab.motion_calibration import (
    MAX_STATIONARY_RANGE_DPS,
    GyroCalibrationFrame,
    infer_stationary_gyro_calibration,
)


def _gyro_axes() -> list[MotionAxisDefinition]:
    scale = math.radians(1 / 16)
    return [
        MotionAxisDefinition("pitch", "abs_rx", evdev.ecodes.ABS_RX, scale=scale),
        MotionAxisDefinition("yaw", "abs_ry", evdev.ecodes.ABS_RY, scale=scale),
        MotionAxisDefinition("roll", "abs_rz", evdev.ecodes.ABS_RZ, scale=scale),
    ]


def _frames(
    values: list[tuple[float, float, float]],
    *,
    interval_seconds: float = 0.1,
) -> list[GyroCalibrationFrame]:
    return [
        GyroCalibrationFrame(
            timestamp_ns=int(index * interval_seconds * 1_000_000_000),
            values={"pitch": pitch, "yaw": yaw, "roll": roll},
        )
        for index, (pitch, yaw, roll) in enumerate(values)
    ]


class _TestHardwareManager:
    def __init__(self) -> None:
        self.saved: list[HardwareConfig] = []

    def save_hardware(self, hardware: HardwareConfig) -> None:
        self.saved.append(hardware)


def _guided_dialog(
    gtk: Any,
    dialog_module: Any,
) -> tuple[
    list[MotionAxisDefinition],
    HardwareConfig,
    _TestHardwareManager,
    Any,
]:
    axes = _gyro_axes()
    sensor = MotionSensorDefinition("imu", "Motion Sensor", source="imu", gyro_axes=axes)
    hardware = HardwareConfig("057e", "2009", "Controller", [], [], motion_sensors=[sensor])
    manager = _TestHardwareManager()
    dialog = dialog_module.MotionCalibrationDialog(
        gtk.Window(), hardware, sensor, cast(Any, manager)
    )
    return axes, hardware, manager, dialog


def test_stationary_gyro_calibration_infers_bias_and_noise() -> None:
    axes = _gyro_axes()
    values = [
        (100 + pitch, -50 + yaw, 25 + roll)
        for pitch, yaw, roll in zip(
            (-1, 0, 1, 0) * 10,
            (0, 1, 0, -1) * 10,
            (1, 0, -1, 0) * 10,
            strict=True,
        )
    ]

    result = infer_stationary_gyro_calibration(axes, _frames(values))

    assert result.sample_count == 36
    assert result.sample_duration_seconds == pytest.approx(3.5)
    assert [axis.offset for axis in result.axes] == [100.0, -50.0, 25.0]
    assert [math.degrees(axis.noise) for axis in result.axes] == pytest.approx(
        [0.09375, 0.09375, 0.09375]
    )
    assert result.maximum_noise_dps == pytest.approx(0.09375)


def test_stationary_gyro_calibration_rejects_movement() -> None:
    axes = _gyro_axes()
    moving_range = math.ceil(MAX_STATIONARY_RANGE_DPS * 16) + 1
    values = [(0.0, 0.0, 0.0)] * 40
    for index in range(20, 24):
        values[index] = (float(moving_range), 0.0, 0.0)

    with pytest.raises(ValueError, match="moved during calibration"):
        infer_stationary_gyro_calibration(axes, _frames(values))


def test_stationary_gyro_calibration_tolerates_one_outlier() -> None:
    values = [(10.0, 20.0, 30.0)] * 40
    values[20] = (1000.0, 20.0, 30.0)

    result = infer_stationary_gyro_calibration(_gyro_axes(), _frames(values))

    assert [axis.offset for axis in result.axes] == [10.0, 20.0, 30.0]


def test_stationary_gyro_calibration_requires_complete_frames() -> None:
    frames = _frames([(0.0, 0.0, 0.0)] * 40)
    incomplete = [
        GyroCalibrationFrame(frame.timestamp_ns, {"pitch": 0.0, "roll": 0.0})
        for frame in frames
    ]

    with pytest.raises(ValueError, match="No complete gyroscope frames"):
        infer_stationary_gyro_calibration(_gyro_axes(), incomplete)


def test_stationary_gyro_calibration_requires_measurement_duration() -> None:
    with pytest.raises(ValueError, match="covered only"):
        infer_stationary_gyro_calibration(
            _gyro_axes(),
            _frames([(0.0, 0.0, 0.0)] * 60, interval_seconds=0.03),
        )


def test_guided_dialog_applies_only_matching_gyro_samples() -> None:
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.device_tab.motion_calibration_dialog import MotionCalibrationDialog

    class _HardwareManager:
        def __init__(self) -> None:
            self.saved: list[HardwareConfig] = []

        def save_hardware(self, hardware: HardwareConfig) -> None:
            self.saved.append(hardware)

    axes = _gyro_axes()
    sensor = MotionSensorDefinition(
        id="imu",
        label="Nintendo Motion Sensor",
        source="imu",
        gyro_axes=axes,
    )
    hardware = HardwareConfig("057e", "2009", "Pro Controller", [], [], motion_sensors=[sensor])
    manager = _HardwareManager()
    dialog = MotionCalibrationDialog(
        Gtk.Window(),
        hardware,
        sensor,
        cast(Any, manager),
    )
    for index in range(40):
        dialog._record_frame(
            {
                "source": "gamepad",
                "timestamp_ns": (index + 1) * 100_000_000,
                "values": {
                    str(evdev.ecodes.ABS_RX): 999,
                    str(evdev.ecodes.ABS_RY): 999,
                    str(evdev.ecodes.ABS_RZ): 999,
                },
            }
        )
        dialog._record_frame(
            {
                "source": "imu",
                "timestamp_ns": (index + 1) * 100_000_000,
                "values": {
                    str(axis.evdev_code): bias
                    for axis, bias in zip(axes, (120, -35, 8), strict=True)
                },
            }
        )

    dialog._apply_capture()

    assert [axis.offset for axis in axes] == [120.0, -35.0, 8.0]
    assert sensor.calibration_samples == 36
    assert sensor.calibrated_at is not None
    assert manager.saved == [hardware]


def test_guided_dialog_does_not_apply_until_capture_cleanup_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.device_tab import motion_calibration_dialog as dialog_module

    class _HardwareManager:
        def __init__(self) -> None:
            self.saved: list[HardwareConfig] = []

        def save_hardware(self, hardware: HardwareConfig) -> None:
            self.saved.append(hardware)

    callbacks: list[object] = []

    def request_async(_payload: object, callback: object) -> None:
        callbacks.append(callback)

    monkeypatch.setattr(dialog_module, "session_request_async", request_async)
    axes = _gyro_axes()
    sensor = MotionSensorDefinition("imu", "Motion Sensor", source="imu", gyro_axes=axes)
    hardware = HardwareConfig("057e", "2009", "Controller", [], [], motion_sensors=[sensor])
    manager = _HardwareManager()
    dialog = dialog_module.MotionCalibrationDialog(
        Gtk.Window(), hardware, sensor, cast(Any, manager)
    )
    dialog._frames = _frames([(120.0, -35.0, 8.0)] * 40)
    dialog._capture_generation = 1
    dialog._capture_state = dialog_module._CaptureState.ENDING
    dialog._capture_active = True
    dialog._end_capture_attempts = dialog_module.MAX_END_CAPTURE_ATTEMPTS

    dialog._on_capture_ended(
        {"status": "error", "message": "daemon unavailable"},
        dialog._capture_generation,
    )

    assert dialog._capture_state is dialog_module._CaptureState.CLEANUP_FAILED
    assert dialog._capture_active is True
    assert dialog._start_button.get_label() == "Retry cleanup"
    assert [axis.offset for axis in axes] == [0.0, 0.0, 0.0]
    assert manager.saved == []

    dialog._on_start_clicked(dialog._start_button)
    assert len(callbacks) == 1
    callback = cast(Any, callbacks.pop())
    callback({"status": "ok"})

    assert dialog._capture_state is dialog_module._CaptureState.IDLE
    assert dialog._capture_active is False
    assert [axis.offset for axis in axes] == [120.0, -35.0, 8.0]
    assert manager.saved == [hardware]


def test_guided_dialog_applies_finished_sample_after_it_closes() -> None:
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.device_tab import motion_calibration_dialog as dialog_module

    axes, hardware, manager, dialog = _guided_dialog(Gtk, dialog_module)
    dialog._frames = _frames([(120.0, -35.0, 8.0)] * 40)
    dialog._capture_generation = 1
    dialog._capture_state = dialog_module._CaptureState.ENDING
    dialog._capture_active = True

    dialog._on_closed(dialog)
    dialog._on_capture_ended({"status": "ok"}, dialog._capture_generation)

    assert dialog._capture_active is False
    assert manager.saved == [hardware]
    assert [axis.offset for axis in axes] == [120.0, -35.0, 8.0]


def test_guided_dialog_blocks_close_while_capture_is_ending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.device_tab import motion_calibration_dialog as dialog_module

    callbacks: list[object] = []

    def request_async(_payload: object, callback: object) -> None:
        callbacks.append(callback)

    monkeypatch.setattr(dialog_module, "session_request_async", request_async)
    _axes, hardware, manager, dialog = _guided_dialog(Gtk, dialog_module)
    dialog._frames = _frames([(120.0, -35.0, 8.0)] * 40)
    dialog._capture_generation = 1
    dialog._capture_state = dialog_module._CaptureState.SAMPLING
    dialog._capture_active = True

    dialog._finish_capture()

    assert dialog._capture_state is dialog_module._CaptureState.ENDING
    assert dialog.get_can_close() is False
    assert dialog._close_button.get_sensitive() is False
    callback = cast(Any, callbacks.pop())
    callback({"status": "ok"})

    assert dialog._capture_state is dialog_module._CaptureState.IDLE
    assert dialog.get_can_close() is True
    assert dialog._close_button.get_sensitive() is True
    assert manager.saved == [hardware]


def test_guided_dialog_keeps_retrying_cleanup_after_it_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.device_tab import motion_calibration_dialog as dialog_module

    callbacks: list[object] = []
    scheduled: dict[int, tuple[object, tuple[object, ...]]] = {}
    removed: list[int] = []
    next_source_id = 1

    def request_async(_payload: object, callback: object) -> None:
        callbacks.append(callback)

    def timeout_add(_delay_ms: int, callback: object, *args: object) -> int:
        nonlocal next_source_id
        source_id = next_source_id
        next_source_id += 1
        scheduled[source_id] = (callback, args)
        return source_id

    def source_remove(source_id: int) -> bool:
        removed.append(source_id)
        scheduled.pop(source_id, None)
        return True

    monkeypatch.setattr(dialog_module, "session_request_async", request_async)
    monkeypatch.setattr(dialog_module.GLib, "timeout_add", timeout_add)
    monkeypatch.setattr(dialog_module.GLib, "source_remove", source_remove)
    _axes, hardware, manager, dialog = _guided_dialog(Gtk, dialog_module)
    dialog._frames = _frames([(120.0, -35.0, 8.0)] * 40)
    dialog._capture_generation = 1
    dialog._set_capture_state(dialog_module._CaptureState.ENDING)
    dialog._capture_active = True
    dialog._end_capture_attempts = 1

    dialog._on_capture_ended(
        {"status": "error", "message": "daemon unavailable"},
        dialog._capture_generation,
    )
    pending_source_id = dialog._cleanup_retry_id
    assert pending_source_id in scheduled

    dialog._on_closed(dialog)

    assert removed == [pending_source_id]
    assert dialog._cleanup_retry_id == 0
    assert len(callbacks) == 1

    callback = cast(Any, callbacks.pop())
    callback({"status": "error", "message": "daemon unavailable"})
    retry_source_id = dialog._cleanup_retry_id
    retry, retry_args = scheduled.pop(retry_source_id)
    cast(Any, retry)(*retry_args)
    callback = cast(Any, callbacks.pop())
    callback({"status": "error", "message": "daemon unavailable"})

    assert dialog._end_capture_attempts == dialog_module.MAX_END_CAPTURE_ATTEMPTS
    assert dialog._capture_state is dialog_module._CaptureState.ENDING
    assert dialog._capture_active is True
    retry_source_id = dialog._cleanup_retry_id
    retry, retry_args = scheduled.pop(retry_source_id)
    cast(Any, retry)(*retry_args)
    callback = cast(Any, callbacks.pop())
    callback({"status": "ok"})

    assert dialog._capture_state is dialog_module._CaptureState.IDLE
    assert dialog._capture_active is False
    assert dialog._cleanup_retry_id == 0
    assert manager.saved == [hardware]


def test_guided_dialog_does_not_apply_interrupted_sample_after_it_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.device_tab import motion_calibration_dialog as dialog_module

    callbacks: list[object] = []

    def request_async(_payload: object, callback: object) -> None:
        callbacks.append(callback)

    monkeypatch.setattr(dialog_module, "session_request_async", request_async)
    axes, _hardware, manager, dialog = _guided_dialog(Gtk, dialog_module)
    dialog._frames = _frames([(120.0, -35.0, 8.0)] * 40)
    dialog._capture_generation = 1
    dialog._capture_state = dialog_module._CaptureState.SAMPLING
    dialog._capture_active = True

    dialog._on_closed(dialog)
    callback = cast(Any, callbacks.pop())
    callback({"status": "ok"})

    assert dialog._capture_state is dialog_module._CaptureState.IDLE
    assert dialog._capture_active is False
    assert manager.saved == []
    assert [axis.offset for axis in axes] == [0.0, 0.0, 0.0]
