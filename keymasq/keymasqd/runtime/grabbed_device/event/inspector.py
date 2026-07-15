"""Device-inspector payload construction and suppression dispatch."""

from typing import cast

from keymasq.common.devices import normalize_evdev_binding_value
from keymasq.keymasqd.runtime.grabbed_device.types import (
    EvdevModule,
    GrabbedDeviceRuntime,
    InputEventLike,
)


def inspector_active(device_runtime: GrabbedDeviceRuntime) -> bool:
    getter = device_runtime.inspector_active_getter
    return bool(getter and getter(device_runtime.hardware_id))


def inspector_suppressed(device_runtime: GrabbedDeviceRuntime) -> bool:
    getter = device_runtime.inspector_suppression_getter
    return bool(getter and getter(device_runtime.hardware_id))


def inspector_suppressed_hardware_ids(
    device_runtime: GrabbedDeviceRuntime,
) -> set[str]:
    getter = device_runtime.inspector_suppressed_ids_getter
    if getter is None:
        return {device_runtime.hardware_id} if inspector_suppressed(device_runtime) else set()
    suppressed_ids: set[str] = set()
    for hardware_id in getter():
        normalized = str(hardware_id or "").strip()
        if normalized:
            suppressed_ids.add(normalized)
    return suppressed_ids


def event_time_us(event: InputEventLike) -> int:
    sec = cast(object, getattr(event, "sec", None))
    usec = cast(object, getattr(event, "usec", None))
    if sec is None or usec is None:
        return 0
    try:
        return int(cast(int | float | str | bytes, sec)) * 1_000_000 + int(
            cast(int | float | str | bytes, usec)
        )
    except (TypeError, ValueError):
        return 0


def event_type_name(event_type: int, *, evdev_mod: EvdevModule) -> str:
    raw_events_obj = cast(object, getattr(evdev_mod.ecodes, "EV", {}))
    if isinstance(raw_events_obj, dict):
        raw_events = cast(dict[object, object], raw_events_obj)
        value = raw_events.get(int(event_type))
        if value is not None:
            return str(value)
    return str(int(event_type))


def inspector_control_id(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    *,
    evdev_mod: EvdevModule,
) -> str:
    normalized_value = normalize_evdev_binding_value(int(event.type), int(event.value))
    button_id = device_runtime.event_binding_to_button.get(
        (int(event.type), int(event.code), normalized_value)
    )
    if button_id:
        return button_id
    button_id = device_runtime.event_code_to_button.get((int(event.type), int(event.code)))
    if button_id:
        return button_id
    if int(event.type) == int(evdev_mod.ecodes.EV_KEY):
        return device_runtime.evdev_to_button.get(event_name.lower(), "")
    return ""


def build_inspector_event_payload(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    *,
    evdev_mod: EvdevModule,
) -> dict[str, object]:
    """Build the GUI-facing payload without broadcasting it."""

    control_id = inspector_control_id(
        device_runtime,
        event,
        event_name,
        evdev_mod=evdev_mod,
    )
    analog_id = ""
    analog_role = ""
    analog_binding = device_runtime.analog_axis_bindings.get((int(event.type), int(event.code)))
    if analog_binding is not None:
        analog_id, analog_role = analog_binding

    action_type = ""
    mapping = device_runtime.mapping_getter()
    mapped_id = analog_id or control_id
    if mapped_id:
        action = mapping.get(mapped_id)
        if action is not None:
            action_type = action.action_type.value

    return {
        "hardware_id": device_runtime.hardware_id,
        "path": device_runtime.path,
        "stable_path": device_runtime.stable_path,
        "source": device_runtime.interface_id,
        "time_us": event_time_us(event),
        "type": int(event.type),
        "type_name": event_type_name(int(event.type), evdev_mod=evdev_mod),
        "code": int(event.code),
        "code_name": event_name,
        "value": int(event.value),
        "control_id": control_id,
        "analog_id": analog_id,
        "analog_role": analog_role,
        "action_type": action_type,
        "suppressed": inspector_suppressed(device_runtime),
    }


def broadcast_inspector_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    *,
    evdev_mod: EvdevModule,
) -> None:
    callback = device_runtime.inspector_event_callback
    if callback is None or not inspector_active(device_runtime):
        return
    callback(
        build_inspector_event_payload(
            device_runtime,
            event,
            event_name,
            evdev_mod=evdev_mod,
        )
    )


def is_inspector_escape_press(
    event: InputEventLike,
    event_name: str,
    *,
    evdev_mod: EvdevModule,
) -> bool:
    return (
        int(event.type) == int(evdev_mod.ecodes.EV_KEY)
        and int(event.value) == 1
        and (
            event_name == "key_esc"
            or int(event.code) == int(getattr(evdev_mod.ecodes, "KEY_ESC", -1))
        )
    )


async def intercept_inspector_suppression(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    *,
    evdev_mod: EvdevModule,
) -> str | None:
    """Handle suppression control, returning a diagnostic label when consumed."""

    suppressed_hardware_ids = inspector_suppressed_hardware_ids(device_runtime)
    if suppressed_hardware_ids and is_inspector_escape_press(
        event,
        event_name,
        evdev_mod=evdev_mod,
    ):
        disabler = device_runtime.inspector_suppression_disabler
        if disabler is not None:
            for hardware_id in sorted(suppressed_hardware_ids):
                await disabler(hardware_id, "key_esc")
        return "inspector_escape_key"
    if inspector_suppressed(device_runtime):
        return "inspector_suppressed"
    return None
