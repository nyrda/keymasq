"""Pure grab planning and daemon payload construction for resolved profiles."""

import json
import logging
from typing import TYPE_CHECKING, cast

from keymasq.common.model.core import ActionType
from keymasq.common.model.hardware import HardwareConfig
from keymasq.session.profile.types import ResolvedDeviceProfile

from ..common import JsonObject, json_list

if TYPE_CHECKING:
    from ..core import SessionManager

log = logging.getLogger("keymasq-session")


def get_interfaces_to_grab(
    hardware_config: HardwareConfig,
    resolved: ResolvedDeviceProfile,
) -> dict[str, str]:
    """Select the configured interfaces needed by mappings and combo sources."""
    interface_to_path = all_configured_interfaces(hardware_config)

    if resolved.always_grab_all:
        return interface_to_path

    button_to_source: dict[str, str] = {
        button.id: button.source for button in hardware_config.buttons if button.source
    }
    analog_inputs = getattr(hardware_config, "analog_inputs", []) or []
    button_to_source.update({analog.id: analog.source for analog in analog_inputs if analog.source})

    sources_to_grab: set[str] = set()
    for button_id, action in resolved.mappings.items():
        if action.action_type != ActionType.PASSTHROUGH:
            source = button_to_source.get(button_id)
            if source:
                sources_to_grab.add(source)

    if resolved.combo_event_count:
        if resolved.combo_sources:
            sources_to_grab.update(resolved.combo_sources)
        else:
            return interface_to_path

    log.debug(
        (
            "Interface selection for %s profile=%s: total_ifaces=%d "
            "mapped_buttons=%d resolved_sources=%d"
        ),
        hardware_config.hardware_id,
        resolved.active_profile_names,
        len(interface_to_path),
        len(resolved.mappings),
        len(sources_to_grab),
    )

    return {
        source: interface_to_path[source]
        for source in sources_to_grab
        if source in interface_to_path
    }


def all_configured_interfaces(hardware_config: HardwareConfig) -> dict[str, str]:
    """Return configured interface IDs that have usable device paths."""
    return {
        device.id: device.path
        for device in hardware_config.evdev_devices
        if device.id and str(device.path or "").strip()
    }


def configured_interface_descriptors(
    hardware_config: HardwareConfig,
    selected_sources: set[str] | None,
) -> list[JsonObject]:
    """Serialize configured interface metadata for the daemon grab command."""
    descriptors: list[JsonObject] = []
    for device in hardware_config.evdev_devices:
        interface_id = str(getattr(device, "id", "") or "")
        path = str(getattr(device, "path", "") or "").strip()
        if not interface_id or not path:
            continue
        if selected_sources is not None and interface_id not in selected_sources:
            continue
        descriptors.append(
            {
                "id": interface_id,
                "path": path,
                "type": getattr(getattr(device, "device_type", None), "value", "other"),
                "phys": str(getattr(device, "phys", "") or ""),
                "capabilities": list(getattr(device, "capabilities", []) or []),
            }
        )
    return descriptors


def device_inspector_active(manager: "SessionManager", hardware_id: str) -> bool:
    """Return whether the inspector currently requires a full-device grab."""
    inspector_state = getattr(manager, "device_inspector_state", None)
    active_hardware_ids = (
        getattr(inspector_state, "active_hardware_ids", set[str]())
        if inspector_state is not None
        else set[str]()
    )
    return bool(
        inspector_state is not None and str(hardware_id or "").strip() in active_hardware_ids
    )


def build_grab_device_payload(
    manager: "SessionManager",
    hardware_id: str,
    hardware_config: HardwareConfig,
    resolved: ResolvedDeviceProfile,
    interfaces: dict[str, str],
    *,
    force_grab_unmapped: bool = False,
) -> JsonObject:
    """Build the complete daemon grab payload for a resolved device profile."""
    analog_inputs = getattr(hardware_config, "analog_inputs", []) or []
    selected_sources = set(interfaces.keys())
    return {
        "hardware_id": hardware_id,
        "evdev_paths": list(interfaces.values()),
        "evdev_interfaces": configured_interface_descriptors(
            hardware_config,
            selected_sources,
        ),
        "button_map": {button.id: button.evdev for button in hardware_config.buttons},
        "button_codes": manager.resolved_button_codes(hardware_config.buttons),
        "button_values": {
            button.id: int(evdev_value)
            for button in hardware_config.buttons
            if (evdev_value := getattr(button, "evdev_value", None)) is not None
        },
        "button_sources": {
            button.id: button.source for button in hardware_config.buttons if button.source
        },
        "analog_inputs": {
            analog.id: {
                "label": analog.label,
                "type": analog.type,
                **({"source": analog.source} if analog.source else {}),
                "axes": [
                    {
                        "role": axis.role,
                        "evdev": axis.evdev,
                        **(
                            {"evdev_code": int(axis.evdev_code)}
                            if axis.evdev_code is not None
                            else {}
                        ),
                        **({"minimum": int(axis.minimum)} if axis.minimum is not None else {}),
                        **({"maximum": int(axis.maximum)} if axis.maximum is not None else {}),
                        **({"center": int(axis.center)} if axis.center is not None else {}),
                        **({"rest": int(axis.rest)} if axis.rest is not None else {}),
                        **({"invert": True} if axis.invert else {}),
                    }
                    for axis in analog.axes
                ],
            }
            for analog in analog_inputs
        },
        "force_grab_unmapped": bool(force_grab_unmapped) or bool(resolved.combo_event_count),
    }


def grab_device_payload_signature(payload: JsonObject) -> str:
    """Create a stable signature for fields that affect an active grab."""
    signature_payload = {
        "evdev_paths": sorted(str(path) for path in json_list(payload.get("evdev_paths"))),
        "evdev_interfaces": _signature_evdev_interfaces(payload.get("evdev_interfaces")),
        "button_map": payload.get("button_map", {}),
        "button_codes": payload.get("button_codes", {}),
        "button_values": payload.get("button_values", {}),
        "analog_inputs": payload.get("analog_inputs", {}),
        "force_grab_unmapped": bool(payload.get("force_grab_unmapped", False)),
    }
    return json.dumps(signature_payload, sort_keys=True, separators=(",", ":"))


def _signature_evdev_interfaces(value: object) -> list[object]:
    interfaces: list[object] = []
    for item in json_list(value):
        if not isinstance(item, dict):
            interfaces.append(item)
            continue

        interface = dict(cast(JsonObject, item))
        capabilities = interface.get("capabilities")
        if isinstance(capabilities, list):
            interface["capabilities"] = sorted(
                str(capability) for capability in cast(list[object], capabilities)
            )
        interfaces.append(interface)

    return sorted(
        interfaces,
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
    )
