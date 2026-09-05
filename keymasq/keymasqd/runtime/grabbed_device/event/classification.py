"""Pure classification and mapping lookup helpers for grabbed events.

This module deliberately knows nothing about event-loop orchestration.  It turns
evdev event metadata into stable names/classes and resolves configured actions
from the grabbed device's binding indexes.
"""

from collections.abc import Callable, Hashable, Mapping, Set
from enum import Enum
from typing import cast

import evdev

from keymasq.common.devices import (
    canonical_gamepad_button_name,
    evdev_alias_name,
    normalize_evdev_binding_value,
)
from keymasq.common.model.actions import MappingAction
from keymasq.keymasqd.runtime.grabbed_device.types import (
    EvdevModule,
    GrabbedDeviceRuntime,
    InputEventLike,
)


class EventClass(Enum):
    """Top-level routing class for a grabbed input event."""

    PASSTHROUGH_ECHO = "passthrough_echo"
    SYNCHRONIZATION = "synchronization"
    ANALOG_AXIS = "analog_axis"
    KEY = "key"
    RELATIVE = "relative"
    OTHER = "other"


PASSTHROUGH_ECHO_EVENT_TYPES = frozenset(
    int(code)
    for code in (
        getattr(evdev.ecodes, "EV_FF", None),
        getattr(evdev.ecodes, "EV_LED", None),
        getattr(evdev.ecodes, "EV_SND", None),
    )
    if isinstance(code, int)
)


def classify_event(
    event: InputEventLike,
    *,
    evdev_mod: EvdevModule,
    analog_axis_bindings: Set[tuple[int, int]],
    passthrough_echo_event_types: Set[int] = PASSTHROUGH_ECHO_EVENT_TYPES,
) -> EventClass:
    """Classify an event for the ordered routes used by ``process_event``."""

    event_type = int(event.type)
    event_code = int(event.code)
    if event_type in passthrough_echo_event_types:
        return EventClass.PASSTHROUGH_ECHO
    if event_type == int(evdev_mod.ecodes.EV_SYN):
        return EventClass.SYNCHRONIZATION
    if (
        event_type == int(evdev_mod.ecodes.EV_ABS)
        and (
            event_type,
            event_code,
        )
        in analog_axis_bindings
    ):
        return EventClass.ANALOG_AXIS
    if event_type == int(evdev_mod.ecodes.EV_KEY):
        return EventClass.KEY
    if event_type == int(evdev_mod.ecodes.EV_REL):
        return EventClass.RELATIVE
    return EventClass.OTHER


def _evdev_code_name(raw_name: object, fallback: int) -> str:
    return evdev_alias_name(raw_name, str(fallback)) or str(fallback)


def _event_code_int(value: object) -> int | None:
    try:
        return int(cast(int | float | str | bytes, value))
    except (TypeError, ValueError):
        return None


def _evdev_code_names(
    event_type: object, *, evdev_mod: EvdevModule
) -> Mapping[object, object] | None:
    if not isinstance(event_type, Hashable):
        return None
    ecodes = cast(object, getattr(evdev_mod, "ecodes", None))
    raw_bytype = cast(object, getattr(ecodes, "bytype", None))
    if not isinstance(raw_bytype, Mapping):
        return None
    bytype = cast(Mapping[object, object], raw_bytype)
    raw_names = bytype.get(event_type)
    if not isinstance(raw_names, Mapping):
        return None
    return cast(Mapping[object, object], raw_names)


def get_event_name(event: InputEventLike, *, evdev_mod: EvdevModule) -> str:
    raw_code: object = event.code
    code = _event_code_int(raw_code)
    if code is None:
        return str(raw_code)
    names = _evdev_code_names(event.type, evdev_mod=evdev_mod)
    if names is None:
        return str(event.code)
    raw_code_name = names.get(code, str(raw_code))
    return _evdev_code_name(raw_code_name, code)


def get_key_name(code: int, *, evdev_mod: EvdevModule) -> str | None:
    ecodes = cast(object, getattr(evdev_mod, "ecodes", None))
    ev_key = cast(object, getattr(ecodes, "EV_KEY", None))
    names = _evdev_code_names(ev_key, evdev_mod=evdev_mod)
    if names is None:
        return None
    raw_code_name = names.get(code, str(code))
    return _evdev_code_name(raw_code_name, code)


def find_action_for_code(
    device_runtime: GrabbedDeviceRuntime,
    event_type: int,
    event_code: int,
    event_value: int,
    event_name: str,
    mapping: dict[str, MappingAction],
) -> MappingAction | None:
    normalized_value = normalize_evdev_binding_value(int(event_type), int(event_value))
    button_id = device_runtime.event_binding_to_button.get(
        (int(event_type), int(event_code), normalized_value)
    )
    if button_id and button_id in mapping:
        return mapping[button_id]
    button_id = device_runtime.event_code_to_button.get((int(event_type), int(event_code)))
    if button_id and button_id in mapping:
        return mapping[button_id]
    if int(event_type) == evdev.ecodes.EV_REL:
        return None
    return find_action_for_name(
        device_runtime,
        event_name,
        mapping,
        canonical_gamepad_button_name_fn=canonical_gamepad_button_name,
    )


def find_button_id_for_code(
    device_runtime: GrabbedDeviceRuntime,
    event_type: int,
    event_code: int,
    event_value: int,
    mapping: dict[str, MappingAction],
) -> str | None:
    normalized_value = normalize_evdev_binding_value(int(event_type), int(event_value))
    button_id = device_runtime.event_binding_to_button.get(
        (int(event_type), int(event_code), normalized_value)
    )
    if button_id and button_id in mapping:
        return button_id
    button_id = device_runtime.event_code_to_button.get((int(event_type), int(event_code)))
    if button_id and button_id in mapping:
        return button_id
    return None


def find_action_for_name(
    device_runtime: GrabbedDeviceRuntime,
    event_name: str,
    mapping: dict[str, MappingAction],
    *,
    canonical_gamepad_button_name_fn: Callable[[str], str],
) -> MappingAction | None:
    button_id = device_runtime.evdev_to_button.get(event_name.lower())
    if not button_id:
        canonical_name = canonical_gamepad_button_name_fn(event_name)
        if canonical_name != event_name.lower():
            button_id = device_runtime.evdev_to_button.get(canonical_name)

    if button_id and button_id in mapping:
        return mapping[button_id]

    return None
