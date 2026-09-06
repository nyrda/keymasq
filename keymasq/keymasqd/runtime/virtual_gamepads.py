from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

from keymasq.common.model.core import DeviceType
from keymasq.common.output_axes import STANDARD_OUTPUT_AXES, OutputAxis, learned_output_axes
from keymasq.common.types import JsonObject
from keymasq.common.virtual_devices import is_virtual_gamepad_output_id
from keymasq.keymasqd.runtime.stick_output import StickOutputState


@dataclass(frozen=True)
class GamepadOutputTarget:
    output_id: str
    uinput: object
    bucket: str
    is_virtual: bool
    analog_inputs: dict[str, object] = field(default_factory=dict)
    stick_output: StickOutputState = field(default_factory=StickOutputState)
    output_axes: tuple[OutputAxis, ...] | None = None


type ClearComboRuntime = Callable[[], Awaitable[None]]
type ConfigureVirtualGamepads = Callable[[int], None]


def _resolved_hardware_analog_inputs(device: object) -> dict[str, object]:
    """Combine saved metadata with cached grab-time calibration without mutating either."""
    raw_inputs = getattr(device, "analog_inputs", None)
    if not isinstance(raw_inputs, dict):
        return {}
    inputs = dict(cast(dict[str, object], raw_inputs))
    raw_calibrations = getattr(device, "analog_axis_calibrations", None)
    calibrations = (
        cast(dict[tuple[str, str], dict[str, object]], raw_calibrations)
        if isinstance(raw_calibrations, dict)
        else {}
    )
    raw_ranges = getattr(device, "analog_axis_ranges", None)
    ranges = (
        cast(dict[tuple[str, str], tuple[int, int]], raw_ranges)
        if isinstance(raw_ranges, dict)
        else {}
    )
    for analog_id, raw_input in inputs.items():
        if not isinstance(raw_input, dict):
            continue
        analog = cast(dict[str, object], raw_input)
        raw_axes = analog.get("axes")
        if not isinstance(raw_axes, list):
            continue
        axes: list[object] = []
        for raw_axis in cast(list[object], raw_axes):
            if not isinstance(raw_axis, dict):
                axes.append(raw_axis)
                continue
            axis = dict(cast(dict[str, object], raw_axis))
            key = (analog_id, str(axis.get("role", "") or "").strip().lower())
            calibration = calibrations.get(key, {})
            for field_name in ("minimum", "maximum", "center", "rest"):
                if axis.get(field_name) is None and calibration.get(field_name) is not None:
                    axis[field_name] = calibration[field_name]
            axis_range = ranges.get(key)
            if axis_range is not None:
                for field_name, value in zip(("minimum", "maximum"), axis_range, strict=True):
                    if axis.get(field_name) is None:
                        axis[field_name] = value
            axes.append(axis)
        inputs[analog_id] = {**analog, "axes": axes}
    return inputs


async def reconfigure_virtual_gamepads(
    *,
    count: int,
    current_count: int,
    output_devices_active: bool,
    grabbed_devices: Mapping[str, Sequence[object]],
    clear_combo_runtime: ClearComboRuntime,
    configure_outputs: ConfigureVirtualGamepads,
    set_inactive_count: Callable[[int], None],
    logger: logging.Logger,
) -> JsonObject:
    """Reset dependent output state before replacing virtual gamepads."""
    if count == current_count:
        return {"status": "ok", "count": count}

    cancelled_tasks: list[asyncio.Task[None]] = []
    await clear_combo_runtime()
    for devices in grabbed_devices.values():
        for device in devices:
            state = getattr(device, "state", None)
            rapidfire_tasks = getattr(state, "rapidfire_tasks", {})
            if isinstance(rapidfire_tasks, dict):
                for task in cast(dict[object, object], rapidfire_tasks).values():
                    if isinstance(task, asyncio.Task) and not task.done():
                        cancelled_tasks.append(cast(asyncio.Task[None], task))
            release_outputs = getattr(device, "release_tracked_outputs", None)
            if callable(release_outputs):
                release_outputs()
            reset_runtime = getattr(device, "reset_mapping_runtime_state", None)
            if callable(reset_runtime):
                await cast(Awaitable[object], reset_runtime())

    if cancelled_tasks:
        unique_tasks = list(dict.fromkeys(cancelled_tasks))
        for task in unique_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*unique_tasks, return_exceptions=True)

    if output_devices_active:
        configure_outputs(count)
    else:
        set_inactive_count(count)
    logger.info("Configured %d virtual gamepad output(s)", count)
    return {"status": "ok", "count": count}


class GamepadOutputRouter:
    """Resolves virtual or grabbed-hardware gamepad output targets."""

    def __init__(
        self,
        logger: logging.Logger,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._logger = logger
        self._monotonic = monotonic
        self._warning_at: dict[tuple[str, str], float] = {}
        self._virtual_stick_outputs: dict[str, tuple[object, StickOutputState]] = {}

    def resolve(
        self,
        output_state: object,
        grabbed_devices: Mapping[str, Sequence[object]],
        output_id: str | None,
        *,
        context: str = "",
    ) -> GamepadOutputTarget | None:
        explicit = output_id is not None
        resolved_id = str(output_id or "virtual-gamepad-1").strip() or "virtual-gamepad-1"

        if is_virtual_gamepad_output_id(resolved_id):
            raw_outputs = getattr(output_state, "virtual_gamepad_uinputs", {})
            outputs = cast(dict[str, object], raw_outputs) if isinstance(raw_outputs, dict) else {}
            uinput = outputs.get(resolved_id)
            if uinput is None:
                self._virtual_stick_outputs.pop(resolved_id, None)
                self._warn(resolved_id, "virtual output is not configured", context, explicit)
                return None
            previous = self._virtual_stick_outputs.get(resolved_id)
            if previous is None or previous[0] is not uinput:
                previous = (uinput, StickOutputState())
                self._virtual_stick_outputs[resolved_id] = previous
            return GamepadOutputTarget(
                output_id=resolved_id,
                uinput=uinput,
                bucket=f"gamepad:{resolved_id}",
                is_virtual=True,
                stick_output=previous[1],
                output_axes=STANDARD_OUTPUT_AXES,
            )

        devices = grabbed_devices.get(resolved_id)
        if not devices:
            self._warn(resolved_id, "target hardware is not grabbed", context, explicit)
            return None

        for device in devices:
            device_type = getattr(device, "device_type", None)
            raw_types = getattr(device, "device_types", None)
            device_types: set[object] = (
                set(cast(Sequence[object], raw_types)) if isinstance(raw_types, Sequence) else set()
            )
            if device_type != DeviceType.GAMEPAD and DeviceType.GAMEPAD not in device_types:
                continue
            uinput = getattr(device, "uinput", None)
            if uinput is not None:
                stick_output = getattr(
                    getattr(device, "state", None), "passthrough_stick_output", None
                )
                analog_inputs = _resolved_hardware_analog_inputs(device)
                return GamepadOutputTarget(
                    output_id=resolved_id,
                    uinput=uinput,
                    bucket=f"gamepad:{resolved_id}",
                    is_virtual=False,
                    analog_inputs=analog_inputs,
                    output_axes=learned_output_axes(analog_inputs.values()),
                    stick_output=stick_output
                    if isinstance(stick_output, StickOutputState)
                    else StickOutputState(),
                )
        self._warn(
            resolved_id,
            "target hardware has no grabbed gamepad passthrough output",
            context,
            explicit,
        )
        return None

    def _warn(self, output_id: str, reason: str, context: str, explicit: bool) -> None:
        key = (output_id, reason)
        now = self._monotonic()
        last = self._warning_at.get(key)
        if last is not None and now - last < 5.0:
            return
        self._warning_at[key] = now
        context_text = f" for {context}" if context else ""
        mode_text = "" if explicit else " default"
        self._logger.warning(
            "Gamepad output target %s unavailable%s%s: %s; dropping output",
            output_id,
            mode_text,
            context_text,
            reason,
        )
