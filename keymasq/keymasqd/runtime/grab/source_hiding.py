"""Source-hiding policy for desired device grabs."""

import logging
from collections.abc import Sequence
from typing import cast

from keymasq.common.devices import (
    hardware_model_id_key,
    input_classes_include_gamepad,
    is_keymasq_device_path,
)
from keymasq.common.types import JsonObject
from keymasq.keymasqd.runtime import source_hiding
from keymasq.keymasqd.runtime.grab.state import GrabManager

log = logging.getLogger("keymasqd.devices")


def interfaces_request_gamepad_source_hiding(
    raw_interfaces: Sequence[JsonObject],
) -> bool:
    """Return whether interfaces represent a model-wide Keymasq gamepad source."""

    if not raw_interfaces:
        return False
    return any(
        input_classes_include_gamepad(primary=descriptor.get("type"))
        and is_keymasq_device_path(str(descriptor.get("path", "") or "").strip())
        for descriptor in raw_interfaces
    )


def desired_grab_requests_gamepad_source_hiding(
    desired_config: object | None,
) -> bool:
    raw_interfaces = getattr(desired_config, "evdev_interfaces", None)
    if not isinstance(raw_interfaces, list):
        return False
    return interfaces_request_gamepad_source_hiding(cast(Sequence[JsonObject], raw_interfaces))


async def disable_hardware_hotplug_hiding_if_unused(
    manager: GrabManager,
    hardware_id: str,
) -> None:
    """Disable a model's hiding flag when no sibling instance is awaiting a grab."""

    flag_name = hardware_model_id_key(hardware_id)
    if flag_name is None:
        return

    normalized_hardware_id = str(hardware_id or "").strip().lower()
    for other_hardware_id, desired_config in manager.grab_state.desired_grabs.items():
        if str(other_hardware_id or "").strip().lower() == normalized_hardware_id:
            continue
        if not desired_grab_requests_gamepad_source_hiding(desired_config):
            continue
        if not hardware_waiting_for_grab(manager, other_hardware_id):
            continue
        if hardware_model_id_key(other_hardware_id) == flag_name:
            return

    await source_hiding.disable_hardware_hotplug_hiding(hardware_id)


async def enable_hardware_hotplug_hiding_best_effort(
    manager: GrabManager,
    hardware_id: str,
) -> None:
    try:
        await source_hiding.enable_hardware_hotplug_hiding(hardware_id)
    except Exception:
        log.exception(
            "Failed to enable source-hiding hotplug state hardware_id=%s manager=%s",
            hardware_id,
            manager_log_context(manager),
        )


async def disable_hardware_hotplug_hiding_if_unused_best_effort(
    manager: GrabManager,
    hardware_id: str,
) -> None:
    try:
        await disable_hardware_hotplug_hiding_if_unused(manager, hardware_id)
    except Exception:
        log.exception(
            "Failed to disable source-hiding hotplug state hardware_id=%s manager=%s",
            hardware_id,
            manager_log_context(manager),
        )


def manager_log_context(manager: GrabManager) -> str:
    return f"{type(manager).__name__}@0x{id(manager):x}"


def hardware_waiting_for_grab(manager: GrabManager, hardware_id: str) -> bool:
    return not bool(manager.grabbed_devices.get(hardware_id))
