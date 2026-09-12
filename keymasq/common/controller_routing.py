"""Controller interface selection for hardware default output routes."""

from keymasq.common.devices import (
    detect_input_classes_from_capabilities,
    resolve_evdev_code,
    resolve_evdev_event_type,
)
from keymasq.common.model.core import DeviceType
from keymasq.common.model.hardware import EvdevDevice


def is_controller_interface(device: EvdevDevice) -> bool:
    if device.device_type == DeviceType.GAMEPAD:
        return True
    if device.device_type == DeviceType.MOTION:
        return False
    capabilities: dict[int, list[object]] = {}
    for name in device.capabilities:
        event_type = resolve_evdev_event_type(name)
        code = resolve_evdev_code(name)
        if event_type is not None and code is not None:
            capabilities.setdefault(event_type, []).append(code)
    return "gamepad" in detect_input_classes_from_capabilities(capabilities)
