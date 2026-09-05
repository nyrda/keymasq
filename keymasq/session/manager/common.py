from typing import TYPE_CHECKING

from keymasq.common.coercion import (
    json_list,
    json_object,
)
from keymasq.common.types import JsonObject
from keymasq.session.listeners.base import WindowListener

if TYPE_CHECKING:
    from .core import SessionManager

__all__ = [
    "JsonObject",
    "device_name_for_hardware",
    "json_list",
    "json_object",
    "merge_support_details",
]


def merge_support_details(
    base: dict[str, bool | str],
    listener: WindowListener | None,
) -> dict[str, bool | str | int]:
    merged: dict[str, bool | str | int] = dict(base)
    if listener is None:
        return merged
    runtime_details_getter = getattr(listener, "runtime_support_details", None)
    if callable(runtime_details_getter):
        runtime_details = json_object(runtime_details_getter())
        if runtime_details:
            for key, value in runtime_details.items():
                if isinstance(value, (bool, int, str)):
                    merged[key] = value
    return merged


def device_name_for_hardware(manager: "SessionManager", hardware_id: str) -> str:
    hardware = manager.hardware.get_hardware(hardware_id)
    if hardware is None:
        return hardware_id
    return str(getattr(hardware, "name", "") or hardware_id)
