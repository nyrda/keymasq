"""Guided motion-sensor calibration dialog."""

import logging
from datetime import datetime
from enum import Enum
from typing import Any, cast

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.coercion import coerce_int
from keymasq.common.devices import resolve_evdev_code
from keymasq.common.model.hardware import HardwareConfig
from keymasq.common.model.motion import MotionAxisDefinition, MotionSensorDefinition
from keymasq.gui.session_client import JsonDict, session_request_async
from keymasq.session.hardware import HardwareManager

from .motion_calibration import (
    GYRO_CALIBRATION_WARMUP_SECONDS,
    GyroCalibrationFrame,
    infer_stationary_gyro_calibration,
)

CALIBRATION_DURATION_SECONDS = 3.0
CALIBRATION_TOTAL_SECONDS = CALIBRATION_DURATION_SECONDS + GYRO_CALIBRATION_WARMUP_SECONDS
CALIBRATION_POLL_INTERVAL_MS = 50
MAX_END_CAPTURE_ATTEMPTS = 3

log = logging.getLogger("keymasq.gui.widgets.device_tab.motion_calibration_dialog")


class _CaptureState(Enum):
    IDLE = "idle"
    STARTING = "starting"
    SAMPLING = "sampling"
    ENDING = "ending"
    CLEANUP_FAILED = "cleanup_failed"

AxisEditor = tuple[
    MotionAxisDefinition,
    Gtk.SpinButton,
    Gtk.SpinButton,
    Gtk.SpinButton,
    Gtk.CheckButton,
]


class MotionCalibrationDialog(Adw.Dialog):
    def __init__(
        self,
        parent: Gtk.Window | None,
        hardware_config: HardwareConfig,
        sensor: MotionSensorDefinition,
        hardware_manager: HardwareManager,
    ) -> None:
        super().__init__(
            title=f"{sensor.label} Calibration",
            content_width=580,
            content_height=560,
        )
        self._parent = parent
        self._hardware_config = hardware_config
        self._sensor = sensor
        self._hardware_manager = hardware_manager
        self._capture_state = _CaptureState.IDLE
        self._capture_active = False
        self._poll_inflight = False
        self._poll_id = 0
        self._cleanup_retry_id = 0
        self._deadline_us = 0
        self._capture_generation = 0
        self._end_capture_attempts = 0
        self._finish_after_read = False
        self._pending_capture_error: str | None = None
        self._closed = False
        self._frames: list[GyroCalibrationFrame] = []
        self._axis_by_code = {
            code: axis
            for axis in sensor.gyro_axes
            if (
                code := (
                    axis.evdev_code
                    if axis.evdev_code is not None
                    else resolve_evdev_code(axis.evdev)
                )
            )
            is not None
        }
        self._editors: list[AxisEditor] = []
        self._setup_ui()
        self.connect("closed", self._on_closed)

    def _setup_ui(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        content.set_margin_top(20)
        content.set_margin_bottom(20)
        content.set_margin_start(20)
        content.set_margin_end(20)

        title = Gtk.Label(label="Calibrate gyro drift")
        title.add_css_class("title-2")
        title.set_xalign(0.0)
        content.append(title)

        instructions = Gtk.Label(
            label=(
                "Place the controller on a stable surface and do not touch it. "
                "Keymasq will let the gyro settle briefly, then measure it for three seconds."
            )
        )
        instructions.set_wrap(True)
        instructions.set_xalign(0.0)
        content.append(instructions)

        self._status = Gtk.Label(label="Ready to calibrate.")
        self._status.add_css_class("dim-label")
        self._status.set_wrap(True)
        self._status.set_xalign(0.0)
        content.append(self._status)

        self._progress = Gtk.ProgressBar()
        self._progress.set_show_text(True)
        self._progress.set_text(f"{CALIBRATION_TOTAL_SECONDS:.1f} seconds")
        self._progress.set_visible(False)
        content.append(self._progress)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._start_button = Gtk.Button(label="Calibrate gyro")
        self._start_button.add_css_class("suggested-action")
        self._start_button.connect("clicked", self._on_start_clicked)
        actions.append(self._start_button)
        self._reset_button = Gtk.Button(label="Reset gyro bias")
        self._reset_button.connect("clicked", self._on_reset_clicked)
        actions.append(self._reset_button)
        content.append(actions)

        advanced = Gtk.Expander(label="Advanced manual normalization")
        advanced.set_child(self._build_advanced_editor())
        content.append(advanced)

        accel_note = Gtk.Label(
            label=(
                "A stationary sample cannot determine accelerometer bias because it also "
                "measures gravity. Accelerometer correction remains manual."
            )
        )
        accel_note.add_css_class("dim-label")
        accel_note.set_wrap(True)
        accel_note.set_xalign(0.0)
        content.append(accel_note)

        scrolled.set_child(content)
        outer.append(scrolled)
        outer.append(Gtk.Separator())

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END)
        footer.set_margin_top(8)
        footer.set_margin_bottom(8)
        footer.set_margin_end(12)
        close = Gtk.Button(label="Close")
        close.connect("clicked", self._on_close_clicked)
        footer.append(close)
        outer.append(footer)
        self.set_child(outer)

    def _build_advanced_editor(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(10)
        for title, axes in (
            ("Gyroscope", self._sensor.gyro_axes),
            ("Accelerometer", self._sensor.accelerometer_axes),
        ):
            heading = Gtk.Label(label=title)
            heading.add_css_class("heading")
            heading.set_xalign(0.0)
            box.append(heading)
            for axis in axes:
                row, editor = self._axis_editor(axis)
                box.append(row)
                self._editors.append(editor)
        self._manual_apply_button = Gtk.Button(label="Apply manual values")
        self._manual_apply_button.set_halign(Gtk.Align.START)
        self._manual_apply_button.connect("clicked", self._on_apply_manual_clicked)
        box.append(self._manual_apply_button)
        return box

    @staticmethod
    def _axis_editor(axis: MotionAxisDefinition) -> tuple[Gtk.Widget, AxisEditor]:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        label = Gtk.Label(label=axis.role.capitalize())
        label.set_width_chars(5)
        row.append(label)
        offset = Gtk.SpinButton.new_with_range(-1_000_000.0, 1_000_000.0, 0.01)
        offset.set_digits(4)
        offset.set_value(axis.offset)
        offset.set_tooltip_text("Raw stationary bias")
        row.append(offset)
        scale = Gtk.SpinButton.new_with_range(0.000000001, 1_000_000.0, 0.0001)
        scale.set_digits(8)
        scale.set_value(axis.scale)
        scale.set_tooltip_text("Canonical units per raw input unit")
        row.append(scale)
        noise = Gtk.SpinButton.new_with_range(0.0, 1000.0, 0.0001)
        noise.set_digits(6)
        noise.set_value(axis.noise)
        noise.set_tooltip_text("Canonical noise floor")
        row.append(noise)
        invert = Gtk.CheckButton(label="Invert")
        invert.set_active(axis.invert)
        row.append(invert)
        return row, (axis, offset, scale, noise, invert)

    def _on_start_clicked(self, _button: Gtk.Button) -> None:
        if self._capture_state is _CaptureState.CLEANUP_FAILED:
            self._end_capture_attempts = 0
            self._start_button.set_sensitive(False)
            self._progress.set_visible(True)
            self._progress.set_text("Retrying cleanup…")
            self._request_capture_end()
            return
        if self._capture_state is not _CaptureState.IDLE:
            return
        if not self._capture_is_unlocked():
            present_unlock = getattr(self._parent, "present_unlock_dialog", None)
            if callable(present_unlock):
                self._set_status("Authorize original-input capture to calibrate the gyro.")
                present_unlock(on_success=self._begin_capture)
                return
        self._begin_capture()

    def _capture_is_unlocked(self) -> bool:
        if self._parent is None:
            return True
        required = bool(getattr(self._parent, "_recording_unlock_required", False))
        if not required:
            return True
        return bool(getattr(self._parent, "_recording_unlocked", False)) and bool(
            getattr(self._parent, "_recording_refresh_owner", False)
        )

    def _begin_capture(self) -> None:
        if self._capture_state is not _CaptureState.IDLE or self._closed:
            return
        if not self._sensor.source:
            self._capture_failed(
                "This motion sensor has no event-device source. Reattach it in hardware settings."
            )
            return
        axis_codes = sorted(self._axis_by_code)
        if len(axis_codes) != len(self._sensor.gyro_axes):
            self._capture_failed(
                "One or more gyro axes have no evdev code. Check the hardware configuration."
            )
            return
        self._capture_state = _CaptureState.STARTING
        self._capture_generation += 1
        generation = self._capture_generation
        self._frames = []
        self._pending_capture_error = None
        self._finish_after_read = False
        self._end_capture_attempts = 0
        self._start_button.set_sensitive(False)
        self._set_editing_sensitive(False)
        self._progress.set_fraction(0.0)
        self._progress.set_visible(True)
        self._set_status("Preparing capture…")
        session_request_async(
            {
                "command": "begin_capture",
                "hardware_id": self._hardware_config.hardware_id,
                "mode": "motion",
                "source": self._sensor.source,
                "motion_axis_codes": axis_codes,
                "end_on_disconnect": True,
            },
            lambda result: self._on_capture_begun(result, generation),
        )

    def _on_capture_begun(self, result: JsonDict | None, generation: int) -> bool:
        if generation != self._capture_generation:
            return False
        if self._closed or self._capture_state is not _CaptureState.STARTING:
            if result and result.get("status") == "ok":
                self._capture_active = True
                self._pending_capture_error = "Calibration was closed."
                self._request_capture_end()
            return False
        if not result or result.get("status") != "ok":
            self._capture_failed((result or {}).get("message", "Could not start capture."))
            return False
        self._capture_active = True
        self._capture_state = _CaptureState.SAMPLING
        self._deadline_us = GLib.get_monotonic_time() + int(
            CALIBRATION_TOTAL_SECONDS * 1_000_000
        )
        self._set_status("Keep the controller still while the gyro settles and is measured…")
        self._poll_id = int(GLib.timeout_add(CALIBRATION_POLL_INTERVAL_MS, self._poll_capture))
        return False

    def _poll_capture(self) -> bool:
        if self._capture_state is not _CaptureState.SAMPLING:
            self._poll_id = 0
            return False
        remaining_us = self._deadline_us - GLib.get_monotonic_time()
        if remaining_us <= 0:
            if self._poll_inflight:
                self._finish_after_read = True
                return True
            self._poll_id = 0
            self._finish_capture()
            return False
        elapsed = CALIBRATION_TOTAL_SECONDS - remaining_us / 1_000_000.0
        self._progress.set_fraction(elapsed / CALIBRATION_TOTAL_SECONDS)
        self._progress.set_text(f"{max(0.0, remaining_us / 1_000_000.0):.1f} seconds")
        if self._poll_inflight:
            return True
        self._poll_inflight = True
        session_request_async(
            {
                "command": "capture_read",
                "hardware_id": self._hardware_config.hardware_id,
            },
            self._on_capture_read,
        )
        return True

    def _on_capture_read(self, result: JsonDict | None) -> bool:
        self._poll_inflight = False
        if self._capture_state is not _CaptureState.SAMPLING:
            return False
        if not result or result.get("status") != "ok":
            self._finish_capture(error=(result or {}).get("message", "Capture failed."))
            return False
        reader_error = str(result.get("reader_error", "") or "")
        if reader_error:
            self._finish_capture(error=reader_error)
            return False
        if coerce_int(result.get("dropped_frames"), 0) > 0:
            self._finish_capture(
                error="Motion frames were dropped during calibration. Try again."
            )
            return False
        if coerce_int(result.get("discontinuities"), 0) > 0:
            self._finish_capture(
                error="The motion event stream was interrupted during calibration. Try again."
            )
            return False
        frames = result.get("frames")
        if isinstance(frames, list):
            for frame in cast(list[object], frames):
                if isinstance(frame, dict):
                    self._record_frame(cast(JsonDict, frame))
        if self._finish_after_read or GLib.get_monotonic_time() >= self._deadline_us:
            self._finish_after_read = False
            self._finish_capture()
        return False

    def _record_frame(self, captured: JsonDict) -> None:
        source = str(captured.get("source", "") or "").strip()
        if self._sensor.source and source != self._sensor.source:
            return
        try:
            timestamp_ns = int(cast(Any, captured.get("timestamp_ns")))
        except (TypeError, ValueError):
            return
        raw_values = captured.get("values")
        if timestamp_ns <= 0 or not isinstance(raw_values, dict):
            return
        values: dict[str, float] = {}
        for code, axis in self._axis_by_code.items():
            raw_value = raw_values.get(str(code))
            if raw_value is None:
                raw_value = raw_values.get(code)
            try:
                values[axis.role] = float(cast(Any, raw_value))
            except (TypeError, ValueError):
                return
        if self._frames and timestamp_ns <= self._frames[-1].timestamp_ns:
            return
        self._frames.append(GyroCalibrationFrame(timestamp_ns=timestamp_ns, values=values))

    def _finish_capture(self, *, error: object | None = None) -> None:
        if self._capture_state is not _CaptureState.SAMPLING:
            return
        self._capture_state = _CaptureState.ENDING
        self._pending_capture_error = str(error) if error else None
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = 0
        self._progress.set_fraction(1.0)
        self._progress.set_text("Finishing…")
        if not self._capture_active:
            if error:
                self._capture_failed(str(error))
            else:
                self._apply_capture()
            return
        self._request_capture_end()

    def _request_capture_end(self) -> None:
        if not self._capture_active:
            self._complete_capture_end()
            return
        self._capture_state = _CaptureState.ENDING
        self._end_capture_attempts += 1
        generation = self._capture_generation
        session_request_async(
            {
                "command": "end_capture",
                "hardware_id": self._hardware_config.hardware_id,
            },
            lambda result: self._on_capture_ended(result, generation),
        )

    def _on_capture_ended(self, result: JsonDict | None, generation: int) -> bool:
        if generation != self._capture_generation or not self._capture_active:
            return False
        if result and result.get("status") == "ok":
            self._capture_active = False
            self._complete_capture_end()
            return False

        message = str((result or {}).get("message", "Session did not confirm capture cleanup."))
        if self._end_capture_attempts < MAX_END_CAPTURE_ATTEMPTS:
            self._set_status(f"Could not release capture: {message} Retrying…", error=True)
            delay_ms = 250 * self._end_capture_attempts
            self._cleanup_retry_id = int(
                GLib.timeout_add(delay_ms, self._retry_capture_end, generation)
            )
            return False

        self._capture_state = _CaptureState.CLEANUP_FAILED
        self._progress.set_visible(False)
        self._start_button.set_label("Retry cleanup")
        self._start_button.set_sensitive(not self._closed)
        self._set_status(
            f"Calibration was not saved because capture cleanup failed: {message} "
            "Profiles may remain paused. Retry cleanup before calibrating again.",
            error=True,
        )
        if self._closed:
            log.error(
                "Motion calibration capture cleanup failed after dialog closed hardware_id=%s: %s",
                self._hardware_config.hardware_id,
                message,
            )
        return False

    def _retry_capture_end(self, generation: int) -> bool:
        self._cleanup_retry_id = 0
        if (
            generation != self._capture_generation
            or not self._capture_active
            or self._capture_state is not _CaptureState.ENDING
        ):
            return False
        self._request_capture_end()
        return False

    def _complete_capture_end(self) -> None:
        self._capture_state = _CaptureState.IDLE
        self._end_capture_attempts = 0
        self._start_button.set_label("Calibrate gyro")
        if self._closed:
            return
        if self._pending_capture_error:
            self._capture_failed(self._pending_capture_error)
            return
        self._apply_capture()

    def _apply_capture(self) -> None:
        self._poll_inflight = False
        try:
            result = infer_stationary_gyro_calibration(self._sensor.gyro_axes, self._frames)
        except ValueError as exc:
            self._capture_failed(str(exc))
            return
        inferred = {axis.role: axis for axis in result.axes}
        for axis in self._sensor.gyro_axes:
            calibration = inferred[axis.role]
            axis.offset = calibration.offset
            axis.noise = calibration.noise
        self._sensor.calibration_samples = result.sample_count
        self._sensor.calibrated_at = datetime.now().astimezone().isoformat()
        self._hardware_manager.save_hardware(self._hardware_config)
        self._refresh_editors()
        self._progress.set_visible(False)
        self._start_button.set_sensitive(True)
        self._set_editing_sensitive(True)
        self._set_status(
            f"Calibrated from {result.sample_count} complete frames over "
            f"{result.sample_duration_seconds:.2f} seconds. "
            f"Measured noise was at most {result.maximum_noise_dps:.3f}°/s."
        )

    def _capture_failed(self, message: object) -> None:
        self._capture_state = _CaptureState.IDLE
        self._capture_active = False
        self._poll_inflight = False
        self._finish_after_read = False
        self._pending_capture_error = None
        self._progress.set_visible(False)
        self._start_button.set_label("Calibrate gyro")
        self._start_button.set_sensitive(True)
        self._set_editing_sensitive(True)
        self._set_status(str(message), error=True)

    def _set_editing_sensitive(self, sensitive: bool) -> None:
        self._reset_button.set_sensitive(sensitive)
        self._manual_apply_button.set_sensitive(sensitive)

    def _on_reset_clicked(self, _button: Gtk.Button) -> None:
        if self._capture_state is not _CaptureState.IDLE:
            return
        for axis in self._sensor.gyro_axes:
            axis.offset = 0.0
            axis.noise = 0.0
        self._sensor.calibration_samples = 0
        self._sensor.calibrated_at = None
        self._hardware_manager.save_hardware(self._hardware_config)
        self._refresh_editors()
        self._set_status("Reset gyro bias. Kernel scale values were kept.")

    def _on_apply_manual_clicked(self, _button: Gtk.Button) -> None:
        if self._capture_state is not _CaptureState.IDLE:
            return
        for axis, offset, scale, noise, invert in self._editors:
            axis.offset = offset.get_value()
            axis.scale = scale.get_value()
            axis.noise = noise.get_value()
            axis.invert = invert.get_active()
        self._sensor.calibration_samples = 0
        self._sensor.calibrated_at = None
        self._hardware_manager.save_hardware(self._hardware_config)
        self._set_status("Applied manual normalization values.")

    def _refresh_editors(self) -> None:
        for axis, offset, scale, noise, invert in self._editors:
            offset.set_value(axis.offset)
            scale.set_value(axis.scale)
            noise.set_value(axis.noise)
            invert.set_active(axis.invert)

    def _set_status(self, message: str, *, error: bool = False) -> None:
        self._status.set_label(message)
        if error:
            self._status.add_css_class("error")
            self._status.remove_css_class("dim-label")
        else:
            self._status.remove_css_class("error")
            self._status.add_css_class("dim-label")

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _on_closed(self, _dialog: Adw.Dialog) -> None:
        self._closed = True
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = 0
        if self._capture_state is _CaptureState.SAMPLING:
            self._finish_capture(error="Calibration was closed.")
        elif self._capture_state is _CaptureState.CLEANUP_FAILED:
            self._end_capture_attempts = 0
            self._request_capture_end()
