"""Preserve exact touchpad release coordinates through the kernel input filter."""

import asyncio
import logging

from keymasq.common.model.analog import analog_control_primary_mode
from keymasq.keymasqd.runtime.analog.binding_state import action_analog_control_configs
from keymasq.keymasqd.runtime.grabbed_device.types import GrabbedDeviceRuntime, InputAccessMode

log = logging.getLogger("keymasqd.devices")


async def update_touchpad_fuzz(runtime: GrabbedDeviceRuntime, *, releasing: bool = False) -> None:
    device = runtime.device
    if device is None:
        return
    touchpads = {
        analog_id
        for analog_id, action in runtime.mapping_getter().items()
        if any(
            analog_control_primary_mode(config) == "mouse"
            and config.mouse_motion.mode == "area"
            and config.mouse_motion.area_input_style == "touchpad"
            for config in action_analog_control_configs(action)
        )
    }
    desired = {
        code
        for (_, code), (analog_id, _) in runtime.analog_axis_bindings.items()
        if analog_id in touchpads
    }
    if releasing or runtime.access_mode is not InputAccessMode.EXCLUSIVE:
        desired.clear()
    original = runtime.state.analog_original_fuzz

    def configure() -> None:
        for code in original.keys() - desired:
            try:
                if original[code]:
                    device.set_absinfo(code, fuzz=original[code])
            except OSError:
                log.warning(
                    "Failed to restore axis %s fuzz on %s", code, runtime.path, exc_info=True
                )
            else:
                del original[code]
        for code in desired - original.keys():
            fuzz = getattr(device.absinfo(code), "fuzz", None)
            if not isinstance(fuzz, int):
                raise OSError(f"Cannot read axis {code} fuzz on {runtime.path}")
            if fuzz:
                device.set_absinfo(code, fuzz=0)
            original[code] = fuzz

    # A cancelled grab must not restore fuzz while its worker is still changing it.
    task = asyncio.create_task(asyncio.to_thread(configure))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
