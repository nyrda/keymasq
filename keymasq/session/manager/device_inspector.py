import asyncio
import logging
from typing import TYPE_CHECKING, cast

from keymasq.common.ipc import Command, CommandType
from keymasq.common.models import (
    ActionType,
    AnalogAxisDefinition,
    AnalogInputDefinition,
    ButtonDefinition,
    MappingAction,
    profile_deactivation_policy_to_dict,
)
from keymasq.common.security import PeerCredentials
from keymasq.session.profiles import ResolvedDeviceProfile

from . import profiles as runtime_profiles
from .common import JsonObject, json_object, str_value

if TYPE_CHECKING:
    from .core import SessionManager

log = logging.getLogger("keymasq-session.device_inspector")


def _normalize_hardware_id(hardware_id: object) -> str:
    return str(hardware_id or "").strip()


def _writer_id(writer: asyncio.StreamWriter) -> int:
    return id(writer)


def device_inspector_active(manager: "SessionManager", hardware_id: str) -> bool:
    normalized = _normalize_hardware_id(hardware_id)
    return normalized in manager.device_inspector_state.active_hardware_ids


def device_inspector_suppressed(manager: "SessionManager", hardware_id: str) -> bool:
    normalized = _normalize_hardware_id(hardware_id)
    return normalized in manager.device_inspector_state.suppressed_hardware_ids


def update_status_from_daemon_event(manager: "SessionManager", data: JsonObject) -> None:
    hardware_id = _normalize_hardware_id(data.get("hardware_id"))
    if not hardware_id:
        return

    if bool(data.get("active", False)):
        manager.device_inspector_state.active_hardware_ids.add(hardware_id)
    else:
        manager.device_inspector_state.active_hardware_ids.discard(hardware_id)
        manager.device_inspector_state.owners_by_hardware_id.pop(hardware_id, None)

    if bool(data.get("suppressed", False)):
        manager.device_inspector_state.suppressed_hardware_ids.add(hardware_id)
    else:
        manager.device_inspector_state.suppressed_hardware_ids.discard(hardware_id)


def broadcast_event_to_owners(manager: "SessionManager", data: JsonObject) -> None:
    hardware_id = _normalize_hardware_id(data.get("hardware_id"))
    if not hardware_id:
        return
    owner_ids = manager.device_inspector_state.owners_by_hardware_id.get(hardware_id, set())
    if not owner_ids:
        return

    manager.broadcast_to_session_client_ids({"event": "device_inspector_event", **data}, owner_ids)


async def start_device_inspector(
    manager: "SessionManager",
    hardware_id: str,
    peer: PeerCredentials,
    writer: asyncio.StreamWriter,
) -> JsonObject:
    normalized = _normalize_hardware_id(hardware_id)
    if not normalized:
        return {"status": "error", "message": "missing hardware_id"}
    if manager.hardware.get_hardware(normalized) is None:
        return {"status": "error", "message": f"Unknown hardware_id: {normalized}"}

    owners = manager.device_inspector_state.owners_by_hardware_id.setdefault(normalized, set())
    owners.add(_writer_id(writer))
    manager.device_inspector_state.active_hardware_ids.add(normalized)

    result = await _send_inspector_command(
        manager,
        CommandType.DEVICE_INSPECTOR_START,
        {"hardware_id": normalized},
    )
    if result.get("status") != "ok":
        owners.discard(_writer_id(writer))
        if not owners:
            manager.device_inspector_state.owners_by_hardware_id.pop(normalized, None)
            manager.device_inspector_state.active_hardware_ids.discard(normalized)
            manager.device_inspector_state.suppressed_hardware_ids.discard(normalized)
        return result

    update_status_from_daemon_event(manager, result)
    await runtime_profiles.reevaluate_profiles(
        manager,
        reason=f"device inspector start {normalized}",
    )
    snapshot = build_device_inspector_snapshot(manager, normalized)
    snapshot["owner_pid"] = int(peer.pid)
    snapshot["owner_uid"] = int(peer.uid)
    return snapshot


async def stop_device_inspector(
    manager: "SessionManager",
    hardware_id: str,
    writer: asyncio.StreamWriter,
) -> JsonObject:
    normalized = _normalize_hardware_id(hardware_id)
    if not normalized:
        return {"status": "error", "message": "missing hardware_id"}

    removed_last_owner = _drop_owner(manager, normalized, _writer_id(writer))
    if not removed_last_owner:
        snapshot = build_device_inspector_snapshot(manager, normalized)
        if snapshot.get("status") != "error":
            snapshot["status"] = "ok"
        return snapshot

    return await _stop_device_inspector_unlocked(
        manager,
        normalized,
        reason=f"device inspector stop {normalized}",
    )


async def enable_device_inspector_suppression(
    manager: "SessionManager",
    hardware_id: str,
    writer: asyncio.StreamWriter,
) -> JsonObject:
    normalized = _normalize_hardware_id(hardware_id)
    if not normalized:
        return {"status": "error", "message": "missing hardware_id"}
    if manager.hardware.get_hardware(normalized) is None:
        return {"status": "error", "message": f"Unknown hardware_id: {normalized}"}

    writer_id = _writer_id(writer)
    owners = manager.device_inspector_state.owners_by_hardware_id.setdefault(normalized, set())
    had_owner = writer_id in owners
    was_active = normalized in manager.device_inspector_state.active_hardware_ids
    owners.add(writer_id)
    manager.device_inspector_state.active_hardware_ids.add(normalized)

    await runtime_profiles.reevaluate_profiles(
        manager,
        reason=f"device inspector suppression grab {normalized}",
    )
    if normalized not in manager.profile_state.grabbed_devices:
        await _rollback_suppression_enable_state(
            manager,
            normalized,
            writer_id,
            had_owner=had_owner,
            was_active=was_active,
        )
        return {
            "status": "error",
            "message": f"Device inspector could not grab {normalized} for suppression",
        }

    result = await _send_inspector_command(
        manager,
        CommandType.DEVICE_INSPECTOR_ENABLE_SUPPRESSION,
        {"hardware_id": normalized},
    )
    if result.get("status") != "ok":
        await _rollback_suppression_enable_state(
            manager,
            normalized,
            writer_id,
            had_owner=had_owner,
            was_active=was_active,
        )
        return result

    update_status_from_daemon_event(manager, result)
    snapshot = build_device_inspector_snapshot(manager, normalized)
    snapshot["reason"] = "enable_suppression"
    return snapshot


async def disable_device_inspector_suppression(
    manager: "SessionManager",
    hardware_id: str,
    reason: str = "manual",
) -> JsonObject:
    normalized = _normalize_hardware_id(hardware_id)
    if not normalized:
        return {"status": "error", "message": "missing hardware_id"}

    result = await _send_inspector_command(
        manager,
        CommandType.DEVICE_INSPECTOR_DISABLE_SUPPRESSION,
        {"hardware_id": normalized, "reason": str(reason or "manual")},
    )
    if result.get("status") != "ok":
        return result

    update_status_from_daemon_event(manager, result)
    snapshot = build_device_inspector_snapshot(manager, normalized)
    if snapshot.get("status") != "ok":
        return result
    snapshot["reason"] = str(reason or "manual")
    return snapshot


async def clear_device_inspectors_for_writer(
    manager: "SessionManager",
    writer: asyncio.StreamWriter,
) -> None:
    writer_id = _writer_id(writer)
    for hardware_id, owners in list(manager.device_inspector_state.owners_by_hardware_id.items()):
        if writer_id not in owners:
            continue
        owners.discard(writer_id)
        if owners:
            continue
        try:
            await _stop_device_inspector_unlocked(
                manager,
                hardware_id,
                reason=f"device inspector owner disconnected {hardware_id}",
            )
        except Exception as exc:
            log.warning(
                "Failed to stop device inspector for disconnected owner hardware_id=%s: %s",
                hardware_id,
                exc,
            )


async def _stop_device_inspector_unlocked(
    manager: "SessionManager",
    hardware_id: str,
    *,
    reason: str,
) -> JsonObject:
    manager.device_inspector_state.owners_by_hardware_id.pop(hardware_id, None)
    manager.device_inspector_state.active_hardware_ids.discard(hardware_id)
    manager.device_inspector_state.suppressed_hardware_ids.discard(hardware_id)

    result = await _send_inspector_command(
        manager,
        CommandType.DEVICE_INSPECTOR_STOP,
        {"hardware_id": hardware_id},
    )
    await runtime_profiles.reevaluate_profiles(manager, reason=reason)
    if result.get("status") == "ok":
        update_status_from_daemon_event(manager, result)
        return {
            "status": "ok",
            "hardware_id": hardware_id,
            "active": False,
            "suppressed": False,
        }
    return result


def _drop_owner(manager: "SessionManager", hardware_id: str, writer_id: int) -> bool:
    owners = manager.device_inspector_state.owners_by_hardware_id.get(hardware_id)
    if owners is None:
        return True
    owners.discard(writer_id)
    if owners:
        return False
    manager.device_inspector_state.owners_by_hardware_id.pop(hardware_id, None)
    return True


async def _rollback_suppression_enable_state(
    manager: "SessionManager",
    hardware_id: str,
    writer_id: int,
    *,
    had_owner: bool,
    was_active: bool,
) -> None:
    if not had_owner:
        _drop_owner(manager, hardware_id, writer_id)
    if not was_active:
        manager.device_inspector_state.active_hardware_ids.discard(hardware_id)
        manager.device_inspector_state.suppressed_hardware_ids.discard(hardware_id)
        await runtime_profiles.reevaluate_profiles(
            manager,
            reason=f"device inspector suppression rollback {hardware_id}",
        )


def clear_all_device_inspector_state(manager: "SessionManager") -> None:
    manager.device_inspector_state.active_hardware_ids.clear()
    manager.device_inspector_state.suppressed_hardware_ids.clear()
    manager.device_inspector_state.owners_by_hardware_id.clear()


async def _send_inspector_command(
    manager: "SessionManager",
    command_type: CommandType,
    data: JsonObject,
) -> JsonObject:
    try:
        result = await manager.client.send_command(Command(command=command_type, data=data))
    except Exception:
        return {"status": "error", "message": "Daemon unavailable"}

    result_data = json_object(result.data)
    if result.status == "ok":
        return {"status": "ok", **(result_data or {})}
    return {"status": "error", "message": result.error or "device inspector command failed"}


def build_device_inspector_snapshot(
    manager: "SessionManager",
    hardware_id: str,
) -> JsonObject:
    normalized = _normalize_hardware_id(hardware_id)
    hardware = manager.hardware.get_hardware(normalized)
    if hardware is None:
        return {"status": "error", "message": f"Unknown hardware_id: {normalized}"}

    resolved = manager.profile_state.resolved_devices.get(
        normalized,
        ResolvedDeviceProfile(normalized),
    )
    mapping_profile_names = cast(
        dict[str, str],
        getattr(resolved, "mapping_profile_names", {}) or {},
    )

    return {
        "status": "ok",
        "hardware_id": normalized,
        "device_name": hardware.name,
        "model_id": hardware.model_id,
        "active": device_inspector_active(manager, normalized),
        "suppressed": device_inspector_suppressed(manager, normalized),
        "active_profiles": list(resolved.active_profile_names),
        "interfaces": [_serialize_interface(device) for device in hardware.evdev_devices],
        "buttons": [
            _serialize_button(button, resolved, mapping_profile_names)
            for button in hardware.buttons
        ],
        "analog_inputs": [
            _serialize_analog_input(analog, resolved, mapping_profile_names)
            for analog in hardware.analog_inputs
        ],
    }


def _serialize_interface(device: object) -> JsonObject:
    device_type = getattr(device, "device_type", "")
    device_type_value = getattr(device_type, "value", str(device_type or ""))
    return {
        "id": str_value(getattr(device, "id", ""), ""),
        "path": str_value(getattr(device, "path", ""), ""),
        "type": str(device_type_value or ""),
        "phys": str_value(getattr(device, "phys", ""), ""),
        "capabilities": [
            str(item)
            for item in cast(list[object], getattr(device, "capabilities", []) or [])
        ],
    }


def _serialize_button(
    button: ButtonDefinition,
    resolved: ResolvedDeviceProfile,
    mapping_profile_names: dict[str, str],
) -> JsonObject:
    mapping = resolved.mappings.get(button.id)
    return {
        "id": button.id,
        "label": button.label,
        "kind": "button",
        "evdev": button.evdev,
        "evdev_code": button.evdev_code,
        "evdev_value": button.evdev_value,
        "source": button.source or "",
        "zone": button.zone or "",
        "row": button.row,
        "col": button.col,
        "type": button.type or "",
        "profile_name": mapping_profile_names.get(button.id, ""),
        "action": _serialize_action(mapping) if mapping is not None else None,
    }


def _serialize_analog_input(
    analog: AnalogInputDefinition,
    resolved: ResolvedDeviceProfile,
    mapping_profile_names: dict[str, str],
) -> JsonObject:
    mapping = resolved.mappings.get(analog.id)
    return {
        "id": analog.id,
        "label": analog.label,
        "kind": "analog",
        "type": analog.type,
        "source": analog.source or "",
        "profile_name": mapping_profile_names.get(analog.id, ""),
        "action": _serialize_action(mapping) if mapping is not None else None,
        "axes": [_serialize_axis(axis) for axis in analog.axes],
    }


def _serialize_axis(axis: AnalogAxisDefinition) -> JsonObject:
    return {
        "role": axis.role,
        "evdev": axis.evdev,
        "evdev_code": axis.evdev_code,
        "minimum": axis.minimum,
        "maximum": axis.maximum,
        "center": axis.center,
        "rest": axis.rest,
        "invert": bool(axis.invert),
    }


def _serialize_action(action: MappingAction | None) -> JsonObject | None:
    if action is None:
        return None

    action_data: JsonObject = {"action": action.action_type.value}
    _set_optional(action_data, "target", action.target)
    _set_optional(action_data, "output_id", action.output_id)
    if action.keys:
        action_data["keys"] = list(action.keys)
    _set_optional(action_data, "cmd", action.cmd)
    _set_optional(action_data, "superkey_name", action.superkey_name)
    if action.analog_control_names:
        action_data["analog_control_names"] = list(action.analog_control_names)
    if action.action_type == ActionType.MACRO:
        action_data["target"] = action.macro_name or ""
        action_data["replay_mouse_movement"] = bool(action.macro_replay_mouse_movement)
        action_data["replay_mouse_clicks"] = bool(action.macro_replay_mouse_clicks)
        action_data["speed"] = float(action.macro_speed)
        action_data["loop_mode"] = action.macro_loop_mode
        action_data["loop_count"] = int(action.macro_loop_count)
        action_data["loop_stop_behavior"] = action.macro_loop_stop_behavior
        action_data["move_to_start"] = bool(action.macro_move_to_start)
        action_data["start_x"] = int(action.macro_start_x)
        action_data["start_y"] = int(action.macro_start_y)
        action_data["block_mouse_movement"] = bool(action.macro_block_mouse_movement)
    if action.action_type in (ActionType.MOUSE_MOVE_REL, ActionType.MOUSE_MOVE_ABS):
        action_data["x"] = int(action.move_x)
        action_data["y"] = int(action.move_y)
    if action.action_type == ActionType.GAMEPAD_AXIS:
        action_data["value"] = int(action.axis_value)
    if action.action_type in (
        ActionType.PROFILE_ENABLE,
        ActionType.PROFILE_DISABLE,
        ActionType.PROFILE_TOGGLE,
    ):
        action_data["profile_name"] = action.profile_name or ""
        action_data["target"] = action.profile_name or ""
        deactivation = profile_deactivation_policy_to_dict(action.profile_deactivation)
        if deactivation is not None and action.action_type != ActionType.PROFILE_DISABLE:
            action_data["deactivation"] = deactivation
    if action.action_type == ActionType.COMPOSITOR_DISPATCH:
        _set_optional(action_data, "compositor", action.compositor_id)
        action_data["dispatcher"] = action.compositor_dispatcher or ""
        action_data["args"] = action.compositor_args or ""
    if action.rapidfire_enabled:
        action_data["rapidfire_enabled"] = True
        action_data["rapidfire_hold_ms"] = int(action.rapidfire_hold_ms)
        action_data["rapidfire_wait_ms"] = int(action.rapidfire_wait_ms)
    if action.tap_enabled:
        action_data["tap_enabled"] = True
        action_data["tap_hold_ms"] = int(action.tap_hold_ms)
    return action_data


def serialize_mapping_action(action: MappingAction | None) -> JsonObject | None:
    return _serialize_action(action)


def _set_optional(action_data: JsonObject, key: str, value: object) -> None:
    if value is not None and str(value):
        action_data[key] = str(value)
