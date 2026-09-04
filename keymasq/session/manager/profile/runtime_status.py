"""Runtime status queries and GUI-facing profile/device presentation payloads."""

import logging
from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING, cast

from keymasq.common.ipc import Command, CommandType
from keymasq.common.model.actions import profile_deactivation_policy_to_dict
from keymasq.common.model.hardware import HardwareConfig
from keymasq.session.profile.types import ResolvedDeviceProfile

from ..common import JsonObject, json_list, json_object
from ..payload import mapping
from .grab_plan import all_configured_interfaces, get_interfaces_to_grab

if TYPE_CHECKING:
    from ..core import SessionManager

log = logging.getLogger("keymasq-session")
DEVICE_RUNTIME_STATUS_TIMEOUT_S = 1.0


def build_active_profiles_payload(manager: "SessionManager") -> JsonObject:
    """Present the currently resolved profile and per-device runtime state."""
    devices: dict[str, JsonObject] = {}
    for hardware_id, resolved in sorted(manager.profile_state.resolved_devices.items()):
        hardware = manager.hardware.get_hardware(hardware_id)
        inspector_state = getattr(manager, "device_inspector_state", None)
        active_hardware_ids = (
            getattr(inspector_state, "active_hardware_ids", set[str]())
            if inspector_state is not None
            else set[str]()
        )
        suppressed_hardware_ids = (
            getattr(inspector_state, "suppressed_hardware_ids", set[str]())
            if inspector_state is not None
            else set[str]()
        )
        inspector_active = bool(inspector_state is not None and hardware_id in active_hardware_ids)
        inspector_suppressed = bool(
            inspector_state is not None and hardware_id in suppressed_hardware_ids
        )
        mapping_count = int(getattr(resolved, "mapping_count", 0))
        if hasattr(resolved, "mappings"):
            expected_mapping_signature = mapping.signature(
                manager,
                resolved,
                hardware_id,
            )
            mapping_applied = (
                mapping_count <= 0
                or manager.profile_state.last_sent_mapping_signatures.get(hardware_id)
                == expected_mapping_signature
            )
        else:
            mapping_applied = (
                mapping_count <= 0
                or hardware_id in manager.profile_state.last_sent_mapping_signatures
            )
        devices[hardware_id] = {
            "device_name": hardware.name if hardware else hardware_id,
            "profiles": list(resolved.active_profile_names),
            "mapping_count": mapping_count,
            "always_grab_all": resolved.always_grab_all,
            "grabbed": hardware_id in manager.profile_state.grabbed_devices,
            "waiting_for_device": hardware_id in manager.profile_state.grab_waiting_devices,
            "grabbed_interfaces": dict(
                manager.profile_state.grabbed_interfaces.get(hardware_id, {})
            ),
            "device_status": build_device_status_payload(
                manager,
                hardware_id,
                hardware,
                resolved,
                inspector_active=inspector_active,
            ),
            "mapping_applied": mapping_applied,
            "device_inspector_active": inspector_active,
            "device_inspector_suppressed": inspector_suppressed,
        }

    runtime_activations: dict[str, JsonObject] = {}
    for name, activation in sorted(manager.profile_state.runtime_profile_activations.items()):
        runtime_activations[name] = {
            "activation_id": activation.activation_id,
            "tracked": activation.tracked,
            "deactivation": profile_deactivation_policy_to_dict(activation.deactivation),
        }

    return {
        "status": "ok",
        "active_profiles": list(manager.profile_state.active_profile_names),
        "devices": devices,
        "runtime_profile_activations": runtime_activations,
    }


async def refresh_device_runtime_status(manager: "SessionManager") -> None:
    """Refresh the daemon's live interface state used by presentation payloads."""
    if not manager.connected:
        manager.profile_state.device_runtime_status.clear()
        return

    try:
        result = await manager.client.send_command(
            Command(command=CommandType.DEVICE_RUNTIME_STATUS),
            timeout=DEVICE_RUNTIME_STATUS_TIMEOUT_S,
        )
    except (OSError, TimeoutError) as exc:
        log.debug("Failed to refresh device runtime status: %s", exc)
        manager.profile_state.device_runtime_status.clear()
        return
    except Exception:
        log.exception("Unexpected failure refreshing device runtime status")
        manager.profile_state.device_runtime_status.clear()
        return

    if result.status != "ok":
        log.debug("Device runtime status query failed: %s", result.error)
        manager.profile_state.device_runtime_status.clear()
        return

    status = json_object(result.data)
    if status is None:
        manager.profile_state.device_runtime_status.clear()
        return
    manager.profile_state.device_runtime_status = dict(status)


def build_device_status_payload(
    manager: "SessionManager",
    hardware_id: str,
    hardware: HardwareConfig | None,
    resolved: object,
    *,
    inspector_active: bool,
) -> JsonObject:
    """Summarize configured, connected, requested, and grabbed interfaces."""
    configured_devices = list(getattr(hardware, "evdev_devices", []) or []) if hardware else []
    configured_count = len(configured_devices)
    runtime_status = manager.profile_state.device_runtime_status
    runtime_ready = bool(manager.connected and runtime_status.get("status") == "ok")
    requested_interfaces = _requested_interfaces_for_device(
        hardware,
        resolved,
        inspector_active=inspector_active,
        manager=manager,
    )
    grab_status = dict(manager.profile_state.grab_status.get(hardware_id, {}))

    if not runtime_ready:
        return {
            "state": "unknown",
            "configured_count": configured_count,
            "connected_count": 0,
            "requested_count": len(requested_interfaces),
            "grabbed_count": 0,
            "interfaces": _unknown_configured_interface_payloads(
                configured_devices,
                requested_interfaces,
            ),
            "runtime_ready": False,
            "grab_status": grab_status,
        }

    live_interfaces = _runtime_interfaces_for_hardware(
        runtime_status.get("interfaces"),
        hardware_id,
    )
    grabbed_interfaces = _runtime_interfaces_for_hardware(
        runtime_status.get("grabbed_interfaces"),
        hardware_id,
        exact_hardware_id=True,
    )
    interface_statuses = [
        _configured_interface_status(
            configured,
            live_interfaces,
            grabbed_interfaces,
            requested_interfaces,
        )
        for configured in configured_devices
    ]
    connected_count = sum(1 for item in interface_statuses if bool(item.get("connected")))
    requested_count = sum(1 for item in interface_statuses if bool(item.get("requested")))
    grabbed_count = sum(1 for item in interface_statuses if bool(item.get("grabbed")))
    state = _device_connection_state(
        configured_count=configured_count,
        connected_count=connected_count,
        requested_count=requested_count,
        grabbed_count=grabbed_count,
        inspector_active=inspector_active,
        waiting=hardware_id in manager.profile_state.grab_waiting_devices,
        grab_status=grab_status,
    )
    return {
        "state": state,
        "configured_count": configured_count,
        "connected_count": connected_count,
        "requested_count": requested_count,
        "grabbed_count": grabbed_count,
        "interfaces": interface_statuses,
        "runtime_ready": True,
        "grab_status": grab_status,
    }


def _requested_interfaces_for_device(
    hardware: HardwareConfig | None,
    resolved: object,
    *,
    inspector_active: bool,
    manager: "SessionManager",
) -> dict[str, str]:
    if hardware is None:
        return {}
    if inspector_active:
        return all_configured_interfaces(hardware)
    if not hasattr(resolved, "mappings"):
        return {}
    return get_interfaces_to_grab(hardware, cast(ResolvedDeviceProfile, resolved), manager=manager)


def _unknown_configured_interface_payloads(
    configured_devices: list[object],
    requested_interfaces: dict[str, str],
) -> list[JsonObject]:
    payloads: list[JsonObject] = []
    for configured in configured_devices:
        payload: JsonObject = {
            "id": _configured_interface_id(configured),
            "configured_path": str(getattr(configured, "path", "") or ""),
            "type": _configured_interface_type(configured),
            "connected": False,
            "requested": _configured_interface_requested(
                configured,
                requested_interfaces,
            ),
            "grabbed": False,
            "current_path": "",
            "stable_path": "",
        }
        _append_interface_selectors(
            payload,
            phys=str(getattr(configured, "phys", "") or ""),
            capabilities=getattr(configured, "capabilities", []),
        )
        payloads.append(payload)
    return payloads


def _runtime_interfaces_for_hardware(
    raw_interfaces: object,
    hardware_id: str,
    *,
    exact_hardware_id: bool = False,
) -> list[JsonObject]:
    interfaces: list[JsonObject] = []
    configured_exact_id = str(hardware_id or "").strip().lower()
    for raw in json_list(raw_interfaces):
        if not isinstance(raw, dict):
            continue
        item = cast(JsonObject, raw)
        runtime_hardware_id = str(item.get("hardware_id", "") or "")
        matches = (
            runtime_hardware_id.strip().lower() == configured_exact_id
            if exact_hardware_id
            else _runtime_hardware_matches(
                hardware_id,
                runtime_hardware_id,
                interface_id=str(item.get("interface_id", "") or ""),
            )
        )
        if matches:
            interfaces.append(item)
    return interfaces


def _runtime_hardware_matches(
    configured_hardware_id: str,
    runtime_hardware_id: str,
    *,
    interface_id: str = "",
) -> bool:
    configured_base, configured_suffix = _split_hardware_id(configured_hardware_id)
    runtime_base, _runtime_suffix = _split_hardware_id(runtime_hardware_id)
    if configured_base != runtime_base:
        return False
    if not configured_suffix or configured_suffix.isdecimal():
        return True
    return configured_suffix == str(interface_id or "").strip().lower()


def _split_hardware_id(hardware_id: object) -> tuple[str, str]:
    normalized = str(hardware_id or "").strip().lower()
    base, separator, suffix = normalized.partition("@")
    if not separator:
        return base, ""
    return base, suffix


def _configured_interface_status(
    configured: object,
    live_interfaces: list[JsonObject],
    grabbed_interfaces: list[JsonObject],
    requested_interfaces: dict[str, str],
) -> JsonObject:
    live = _match_configured_interface(configured, live_interfaces)
    grabbed = _match_configured_interface(configured, grabbed_interfaces)
    matched = grabbed or live or {}
    payload: JsonObject = {
        "id": _configured_interface_id(configured),
        "configured_path": str(getattr(configured, "path", "") or ""),
        "type": _configured_interface_type(configured),
        "connected": live is not None or grabbed is not None,
        "requested": _configured_interface_requested(configured, requested_interfaces),
        "grabbed": grabbed is not None,
        "current_path": str(matched.get("resolved_path") or matched.get("path") or ""),
        "stable_path": str(matched.get("stable_path") or ""),
    }
    _append_interface_selectors(
        payload,
        phys=str(
            (live or {}).get("phys") or matched.get("phys") or getattr(configured, "phys", "") or ""
        ),
        capabilities=(
            (live or {}).get("capabilities")
            or matched.get("capabilities")
            or getattr(configured, "capabilities", [])
        ),
    )
    return payload


def _append_interface_selectors(
    payload: JsonObject,
    *,
    phys: str,
    capabilities: object,
) -> None:
    if phys:
        payload["phys"] = phys
    capability_values = _interface_capabilities_payload(capabilities)
    if capability_values:
        payload["capabilities"] = capability_values


def _interface_capabilities_payload(value: object) -> list[str]:
    if isinstance(value, dict):
        items = [
            nested
            for item in cast(dict[object, object], value).values()
            for nested in _interface_capability_items(item)
        ]
        return [str(item) for item in items if str(item)]
    if not isinstance(value, list | tuple | set):
        return []
    items = cast(Iterable[object], value)
    return [str(item) for item in items if str(item)]


def _interface_capability_items(value: object) -> Iterable[object]:
    if isinstance(value, list | tuple | set):
        return cast(Iterable[object], value)
    return (value,)


def _match_configured_interface(
    configured: object,
    runtime_interfaces: list[JsonObject],
) -> JsonObject | None:
    configured_id = _configured_interface_id(configured).lower()
    configured_path = str(getattr(configured, "path", "") or "").strip()
    for interface in runtime_interfaces:
        runtime_id = str(interface.get("interface_id", "") or "").strip().lower()
        if configured_id and runtime_id and configured_id == runtime_id:
            return interface
        runtime_paths = {
            str(interface.get("path", "") or ""),
            str(interface.get("resolved_path", "") or ""),
            str(interface.get("stable_path", "") or ""),
        }
        if configured_path and configured_path in runtime_paths:
            return interface
    return None


def _configured_interface_id(configured: object) -> str:
    return str(getattr(configured, "id", "") or "").strip()


def _configured_interface_type(configured: object) -> str:
    device_type = getattr(configured, "device_type", "")
    return str(getattr(device_type, "value", device_type or ""))


def _configured_interface_requested(
    configured: object,
    requested_interfaces: dict[str, str],
) -> bool:
    configured_id = _configured_interface_id(configured)
    configured_path = str(getattr(configured, "path", "") or "").strip()
    if configured_id and configured_id in requested_interfaces:
        return True
    return bool(configured_path and configured_path in set(requested_interfaces.values()))


def _device_connection_state(
    *,
    configured_count: int,
    connected_count: int,
    requested_count: int,
    grabbed_count: int,
    inspector_active: bool,
    waiting: bool,
    grab_status: JsonObject,
) -> str:
    if inspector_active:
        return "inspector"
    if configured_count <= 0:
        return "unknown"
    if connected_count <= 0:
        return "not_connected"
    grab_status_state = str(grab_status.get("state", "") or "").strip().lower()
    if requested_count > 0 and grabbed_count >= requested_count:
        return "grabbed"
    if requested_count > 0 and grabbed_count > 0:
        return "partial"
    if requested_count > 0 or waiting or grab_status_state in {"waiting", "timed_out"}:
        return "waiting"
    if grabbed_count > 0:
        return "grabbed"
    return "connected"


def build_profile_overview(manager: "SessionManager") -> JsonObject:
    """Build the profile/device overview returned to GUI and CLI clients."""
    known_hardware_ids = set(manager.hardware.list_hardware_ids())
    for info in manager.profiles.list_profiles():
        known_hardware_ids.update(info.config.device_layers.keys())

    profiles = sorted(
        manager.profiles.list_profiles(),
        key=lambda profile: (
            profile.config.name.casefold(),
            profile.config.created_at or datetime.min,
        ),
    )
    devices: list[JsonObject] = []
    for hardware_id in sorted(known_hardware_ids):
        hardware = manager.hardware.get_hardware(hardware_id)
        resolved = manager.profile_state.resolved_devices.get(
            hardware_id,
            ResolvedDeviceProfile(hardware_id),
        )
        devices.append(
            {
                "hardware_id": hardware_id,
                "device_name": hardware.name if hardware else hardware_id,
                "active_profiles": list(resolved.active_profile_names),
                "mapping_count": resolved.mapping_count,
                "always_grab_all": resolved.always_grab_all,
                "profile_count": sum(
                    1 for info in profiles if hardware_id in info.config.device_layers
                ),
            }
        )

    return {
        "status": "ok",
        "profiles": [
            {
                "name": info.config.name,
                "enabled": info.config.enabled,
                "is_permanent": info.config.is_permanent,
                "priority": info.config.priority,
                "window_rule_count": len(info.config.window_rules),
                "created_at": (
                    info.config.created_at.isoformat() if info.config.created_at else ""
                ),
                "devices": sorted(info.config.device_layers.keys()),
                "active": info.config.name in manager.profile_state.active_profile_names,
            }
            for info in profiles
        ],
        "devices": devices,
    }
