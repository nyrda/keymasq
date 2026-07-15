"""Mapping replacement for an already grabbed device."""

import logging

from keymasq.common.model.actions import MappingAction
from keymasq.keymasqd.runtime import action_parser
from keymasq.keymasqd.runtime.grab.release import cancel_pending_hardware_release
from keymasq.keymasqd.runtime.grab.state import GrabManager, JsonObjectFn


async def set_mapping(
    manager: GrabManager,
    hardware_id: str,
    mapping: dict[str, object],
    *,
    json_object_fn: JsonObjectFn,
    log: logging.Logger,
) -> dict[str, object]:
    """Parse and atomically replace active actions for a grabbed device."""

    async with manager._op_lock:
        cancel_pending_hardware_release(manager, hardware_id)
        if hardware_id not in manager.grabbed_devices:
            raise ValueError(f"Device {hardware_id} not grabbed")

        parsed_mapping: dict[str, MappingAction] = {}
        for button_id, action_data in mapping.items():
            action_dict = json_object_fn(action_data)
            if isinstance(action_data, str):
                parsed_mapping[button_id] = action_parser.parse_action(
                    manager,
                    action_data,
                )
            elif action_dict is not None:
                parsed_mapping[button_id] = action_parser.parse_action(
                    manager,
                    action_dict,
                )

        previous_mapping = dict(manager.active_mappings.get(hardware_id, {}))
        manager.active_mappings[hardware_id] = parsed_mapping
        for device in manager.grabbed_devices.get(hardware_id, []):
            await device.reset_mapping_runtime_state(previous_mapping=previous_mapping)
        log.info("Updated mapping for %s (%d buttons)", hardware_id, len(parsed_mapping))
        return {"updated": True, "hardware_id": hardware_id}
