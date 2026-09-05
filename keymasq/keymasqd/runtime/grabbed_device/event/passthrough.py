"""Passthrough emission and synchronization-frame handling."""

from keymasq.keymasqd.runtime import repeat
from keymasq.keymasqd.runtime.adapters import identity_uinput_writer
from keymasq.keymasqd.runtime.grabbed_device import outputs
from keymasq.keymasqd.runtime.grabbed_device.types import (
    EvdevModule,
    GrabbedDeviceRuntime,
    InputEventLike,
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
    if event_code == syn_report:
        passthrough_frame_open = outputs.passthrough_frame_open(
            device_runtime,
            device_runtime.uinput,
        )
        outputs.flush_passthrough_frame(
            device_runtime,
            device_runtime.uinput,
            uinput_writer=identity_uinput_writer,
        )
        return "passthrough_syn" if passthrough_frame_open else "syn"
    if event_code == syn_mt_report:
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
    return "syn"


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
