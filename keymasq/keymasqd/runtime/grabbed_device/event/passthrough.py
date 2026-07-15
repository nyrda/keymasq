"""Passthrough emission and synchronization-frame handling."""

from dataclasses import dataclass
from enum import Enum

from keymasq.keymasqd.runtime import repeat
from keymasq.keymasqd.runtime.adapters import identity_uinput_writer
from keymasq.keymasqd.runtime.grabbed_device import outputs
from keymasq.keymasqd.runtime.grabbed_device.types import (
    EvdevModule,
    GrabbedDeviceRuntime,
    InputEventLike,
)


class SynFrameAction(Enum):
    FLUSH_REPORT = "flush_report"
    FORWARD_MT_REPORT = "forward_mt_report"
    CLOSE_FRAME = "close_frame"


@dataclass(frozen=True)
class SynFrameDecision:
    action: SynFrameAction
    diagnostic_label: str


def classify_syn_frame(
    event_code: int,
    *,
    syn_report_code: int,
    syn_mt_report_code: int,
    passthrough_frame_open: bool,
) -> SynFrameDecision:
    """Classify a SYN event without touching runtime or output state."""

    if event_code == syn_report_code:
        return SynFrameDecision(
            action=SynFrameAction.FLUSH_REPORT,
            diagnostic_label="passthrough_syn" if passthrough_frame_open else "syn",
        )
    if event_code == syn_mt_report_code:
        return SynFrameDecision(
            action=SynFrameAction.FORWARD_MT_REPORT,
            diagnostic_label="syn",
        )
    return SynFrameDecision(
        action=SynFrameAction.CLOSE_FRAME,
        diagnostic_label="syn",
    )


def process_syn_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    *,
    evdev_mod: EvdevModule,
) -> str:
    event_code = int(event.code)
    syn_report = int(getattr(evdev_mod.ecodes, "SYN_REPORT", 0))
    syn_mt_report = int(getattr(evdev_mod.ecodes, "SYN_MT_REPORT", 0))
    passthrough_frame_open = (
        outputs.passthrough_frame_open(
            device_runtime,
            device_runtime.uinput,
        )
        if event_code == syn_report
        else False
    )
    decision = classify_syn_frame(
        event_code,
        syn_report_code=syn_report,
        syn_mt_report_code=syn_mt_report,
        passthrough_frame_open=passthrough_frame_open,
    )

    if decision.action is SynFrameAction.FLUSH_REPORT:
        outputs.flush_passthrough_frame(
            device_runtime,
            device_runtime.uinput,
            uinput_writer=identity_uinput_writer,
        )
    elif decision.action is SynFrameAction.FORWARD_MT_REPORT:
        writer = identity_uinput_writer(device_runtime.uinput)
        if writer is not None:
            writer.write(evdev_mod.ecodes.EV_SYN, event_code, int(event.value))
            outputs.mark_passthrough_frame_open(
                device_runtime,
                device_runtime.uinput,
            )
    else:
        outputs.mark_passthrough_frame_closed(
            device_runtime,
            device_runtime.uinput,
        )
    return decision.diagnostic_label


def emit_passthrough_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    *,
    evdev_mod: EvdevModule,
    event_name: str | None = None,
    remember_for_repeat: bool = False,
    remember_after_emit: bool = False,
) -> None:
    """Emit one non-SYN event through the device passthrough output."""

    if remember_for_repeat and not remember_after_emit:
        if event_name is None:
            raise ValueError("event_name is required when remembering passthrough repeat state")
        repeat.remember_passthrough_event(
            device_runtime.repeat_state,
            device_runtime,
            event,
            event_name,
            evdev_mod=evdev_mod,
        )
    outputs.passthrough(
        device_runtime,
        event,
        evdev_mod=evdev_mod,
        uinput_writer=identity_uinput_writer,
        sync=False,
    )
    if remember_for_repeat and remember_after_emit:
        if event_name is None:
            raise ValueError("event_name is required when remembering passthrough repeat state")
        repeat.remember_passthrough_event(
            device_runtime.repeat_state,
            device_runtime,
            event,
            event_name,
            evdev_mod=evdev_mod,
        )
