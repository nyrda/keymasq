import asyncio
import logging
from typing import TYPE_CHECKING, cast

from keymasq.common.coercion import coerce_str
from keymasq.common.ipc import Command, CommandType
from keymasq.session.profile.types import ResolvedDeviceProfile

from .common import JsonObject, json_list, json_object
from .profile import application, coordinator

if TYPE_CHECKING:
    from .core import SessionManager

log = logging.getLogger("keymasq-session")


async def _begin_capture(
    manager: "SessionManager",
    hardware_id: str,
) -> JsonObject:
    manager.capture_state.locks.add(hardware_id)

    current_profiles = list(
        manager.profile_state.resolved_devices.get(
            hardware_id,
            ResolvedDeviceProfile(hardware_id),
        ).active_profile_names
    )
    manager.capture_state.resume_profiles[hardware_id] = current_profiles

    released = False
    try:
        if hardware_id in manager.profile_state.grabbed_devices:
            released = await application.deactivate_profile(
                manager,
                hardware_id,
                immediate=True,
            )
            if not released:
                raise RuntimeError(f"Failed to release {hardware_id} for capture")
    except BaseException:
        await _rollback_capture_begin(manager, hardware_id)
        raise

    return {
        "status": "ok",
        "hardware_id": hardware_id,
        "released": released,
        "profiles": current_profiles,
    }


async def capture_begin(manager: "SessionManager", hardware_id: str) -> JsonObject:
    return await capture_begin_for_paths(manager, hardware_id, [])


async def capture_begin_for_paths(
    manager: "SessionManager",
    hardware_id: str,
    evdev_paths: list[str],
    *,
    evdev_interfaces: list[JsonObject] | None = None,
    mode: str = "button",
    owner_writer: asyncio.StreamWriter | None = None,
) -> JsonObject:
    if hardware_id in manager.capture_state.tokens or hardware_id in manager.capture_state.locks:
        return {
            "status": "error",
            "error_code": "capture_already_active",
            "message": f"capture already active for {hardware_id}",
        }

    explicit_evdev_paths = bool(evdev_paths)
    if not explicit_evdev_paths:
        evdev_paths = _hardware_evdev_paths(manager, hardware_id)
    if evdev_interfaces is None and explicit_evdev_paths:
        evdev_interfaces = _evdev_interfaces_for_paths(manager, hardware_id, evdev_paths)
    elif evdev_interfaces is None:
        evdev_interfaces = _hardware_evdev_interfaces(manager, hardware_id)
    if not evdev_paths and _requires_explicit_evdev_paths(hardware_id):
        return {
            "status": "error",
            "message": f"Hardware config for {hardware_id} has no evdev paths",
        }
    try:
        lock_result = await _begin_capture(manager, hardware_id)
        request_task = asyncio.create_task(
            manager.client.send_command(
                Command(
                    command=CommandType.CAPTURE_BEGIN,
                    data={
                        "hardware_id": hardware_id,
                        **({"mode": mode} if mode != "button" else {}),
                        **({"evdev_paths": evdev_paths} if evdev_paths else {}),
                        **({"evdev_interfaces": evdev_interfaces} if evdev_interfaces else {}),
                    },
                )
            )
        )
        cancelled = False
        try:
            result = await asyncio.shield(request_task)
        except asyncio.CancelledError:
            cancelled = True
            result = await asyncio.shield(request_task)
    except asyncio.CancelledError:
        await _rollback_capture_begin(manager, hardware_id)
        raise
    except OSError:
        await _rollback_capture_begin(manager, hardware_id)
        return {"status": "error", "message": "Daemon unavailable"}
    except Exception:
        log.exception("Unexpected failure beginning capture for hardware_id=%s", hardware_id)
        await _rollback_capture_begin(manager, hardware_id)
        return {"status": "error", "message": "Failed to begin capture"}

    result_data = json_object(result.data)
    if result.status != "ok" or result_data is None:
        await _rollback_capture_begin(manager, hardware_id)
        if cancelled:
            raise asyncio.CancelledError
        return {"status": "error", "message": result.error or "Failed to begin capture"}

    token = coerce_str(result_data.get("token"), "")
    if not token:
        await _rollback_capture_begin(manager, hardware_id)
        if cancelled:
            raise asyncio.CancelledError
        return {"status": "error", "message": "Missing capture token"}

    manager.capture_state.tokens[hardware_id] = token
    if owner_writer is not None:
        manager.capture_state.owner_writer_ids[hardware_id] = id(owner_writer)
    if cancelled:
        await capture_end(manager, hardware_id)
        raise asyncio.CancelledError
    response = {
        "status": "ok",
        "hardware_id": hardware_id,
        "token": token,
        "warnings": result_data.get("warnings", []),
    }
    response.update(lock_result)
    return response


async def _rollback_capture_begin(manager: "SessionManager", hardware_id: str) -> None:
    """Drop a provisional lock and restore normal profile reconciliation."""

    manager.capture_state.tokens.pop(hardware_id, None)
    try:
        await _end_capture(manager, hardware_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Failed to roll back capture begin for hardware_id=%s", hardware_id)


async def capture_read(manager: "SessionManager", hardware_id: str) -> JsonObject:
    token = manager.capture_state.tokens.get(hardware_id, "")
    if not token:
        return {"status": "error", "message": "capture not active"}

    try:
        result = await manager.client.send_command(
            Command(command=CommandType.CAPTURE_READ, data={"token": token})
        )
    except OSError:
        return {"status": "error", "message": "Daemon unavailable"}
    except Exception:
        log.exception("Unexpected failure reading capture for hardware_id=%s", hardware_id)
        return {"status": "error", "message": "Failed to read capture"}

    result_data = json_object(result.data)
    if result.status == "ok" and result_data is not None:
        return {"status": "ok", "captured": result_data.get("captured")}
    return {"status": "error", "message": result.error or "Failed to read capture"}


async def capture_end(manager: "SessionManager", hardware_id: str) -> JsonObject:
    token = manager.capture_state.tokens.get(hardware_id, "")
    if token:
        try:
            result = await manager.client.send_command(
                Command(command=CommandType.CAPTURE_END, data={"token": token})
            )
        except OSError as exc:
            log.debug("Failed to end capture for hardware_id=%s: %s", hardware_id, exc)
            return {"status": "error", "message": "Daemon unavailable"}
        except Exception:
            log.exception("Unexpected failure ending capture for hardware_id=%s", hardware_id)
            return {"status": "error", "message": "Failed to end capture"}
        if result.status != "ok":
            return {"status": "error", "message": result.error or "Failed to end capture"}
        manager.capture_state.tokens.pop(hardware_id, None)
    return await _end_capture(manager, hardware_id)


async def clear_captures_for_writer(
    manager: "SessionManager",
    writer: asyncio.StreamWriter,
) -> None:
    writer_id = id(writer)
    hardware_ids = [
        hardware_id
        for hardware_id, owner_writer_id in manager.capture_state.owner_writer_ids.items()
        if owner_writer_id == writer_id
    ]
    for hardware_id in hardware_ids:
        try:
            result = await capture_end(manager, hardware_id)
            if result.get("status") == "error":
                log.warning(
                    "Capture end failed for disconnected owner hardware_id=%s: %s",
                    hardware_id,
                    result,
                )
                manager.capture_state.tokens.pop(hardware_id, None)
                await _end_capture(manager, hardware_id)
        except OSError as exc:
            log.warning(
                "Failed to end capture for disconnected owner hardware_id=%s: %s",
                hardware_id,
                exc,
            )
        except Exception:
            log.exception(
                "Unexpected failure ending capture for disconnected owner hardware_id=%s",
                hardware_id,
            )


async def _end_capture(manager: "SessionManager", hardware_id: str) -> JsonObject:
    was_locked = hardware_id in manager.capture_state.locks
    manager.capture_state.locks.discard(hardware_id)
    manager.capture_state.owner_writer_ids.pop(hardware_id, None)

    previous_profile_names = manager.capture_state.resume_profiles.pop(hardware_id, [])
    if not was_locked:
        return {"status": "ok", "hardware_id": hardware_id, "resumed": False}

    await coordinator.reevaluate_profiles(manager, reason=f"capture ended for {hardware_id}")
    active_names = list(
        manager.profile_state.resolved_devices.get(
            hardware_id,
            ResolvedDeviceProfile(hardware_id),
        ).active_profile_names
    )
    return {
        "status": "ok",
        "hardware_id": hardware_id,
        "resumed": bool(active_names),
        "profiles": active_names or previous_profile_names,
    }


async def capture_combo(
    manager: "SessionManager",
    profile_name: str,
    timeout_s: float,
) -> JsonObject:
    profile = manager.profiles.get_profile(profile_name)
    if profile is None:
        return {"status": "error", "message": f"Unknown profile '{profile_name}'"}

    hardware_ids = sorted(
        {
            *manager.hardware.list_hardware_ids(),
            *profile.config.device_layers.keys(),
            *(
                event.hardware_id
                for combo in getattr(profile.config, "combos", [])
                for step in combo.steps
                for event in step.events
                if event.hardware_id
            ),
        }
    )
    if not hardware_ids:
        return {
            "status": "error",
            "message": "No known devices available for combo capture",
        }

    try:
        hardware_paths = {
            hardware_id: paths
            for hardware_id in hardware_ids
            if (paths := _hardware_evdev_paths(manager, hardware_id))
        }
        hardware_interfaces = {
            hardware_id: interfaces
            for hardware_id in hardware_ids
            if (interfaces := _hardware_evdev_interfaces(manager, hardware_id))
        }
        result = await manager.client.send_command(
            Command(
                command=CommandType.CAPTURE_COMBO,
                data={
                    "hardware_ids": hardware_ids,
                    "hardware_paths": hardware_paths,
                    "hardware_interfaces": hardware_interfaces,
                    "timeout_s": float(timeout_s),
                },
            )
        )
    except OSError:
        return {"status": "error", "message": "Daemon unavailable"}
    except Exception:
        log.exception("Unexpected failure capturing combo for profile '%s'", profile_name)
        return {"status": "error", "message": "Combo capture failed"}

    result_data = json_object(result.data)
    if result.status != "ok" or result_data is None:
        return {"status": "error", "message": result.error or "Combo capture failed"}

    events_raw = result_data.get("events")
    if not isinstance(events_raw, list):
        return {"status": "error", "message": "Combo capture returned no events"}
    event_items = cast(list[object], events_raw)

    events: list[JsonObject] = []
    for raw_event in event_items:
        event = json_object(raw_event)
        if event is None:
            continue
        events.append(
            {
                "evdev": coerce_str(event.get("evdev"), ""),
                "hardware_id": coerce_str(event.get("hardware_id"), ""),
                "source": coerce_str(event.get("source"), ""),
            }
        )

    return {
        "status": "ok",
        "events": events,
        "warnings": json_list(result_data.get("warnings")),
    }


def _hardware_evdev_paths(manager: "SessionManager", hardware_id: str) -> list[str]:
    hardware = manager.hardware.get_hardware(hardware_id)
    if hardware is None:
        return []
    return [
        path
        for device in getattr(hardware, "evdev_devices", [])
        if (path := coerce_str(getattr(device, "path", ""), ""))
    ]


def _hardware_evdev_interfaces(manager: "SessionManager", hardware_id: str) -> list[JsonObject]:
    hardware = manager.hardware.get_hardware(hardware_id)
    if hardware is None:
        return []
    interfaces: list[JsonObject] = []
    for device in getattr(hardware, "evdev_devices", []):
        device_id = coerce_str(getattr(device, "id", ""), "")
        if not device_id:
            continue
        path = coerce_str(getattr(device, "path", ""), "")
        if not path:
            continue
        interfaces.append(
            {
                "id": device_id,
                "path": path,
                "type": getattr(getattr(device, "device_type", None), "value", "other"),
                "phys": coerce_str(getattr(device, "phys", ""), ""),
                "capabilities": list(getattr(device, "capabilities", []) or []),
            }
        )
    return interfaces


def _evdev_interfaces_for_paths(
    manager: "SessionManager",
    hardware_id: str,
    evdev_paths: list[str],
) -> list[JsonObject]:
    configured_by_path: dict[str, list[JsonObject]] = {}
    for interface in _hardware_evdev_interfaces(manager, hardware_id):
        path = coerce_str(interface.get("path"), "")
        if not path:
            continue
        configured_by_path.setdefault(path, []).append(interface)

    interfaces: list[JsonObject] = []
    for path in evdev_paths:
        normalized_path = coerce_str(path, "")
        if not normalized_path:
            continue
        configured = configured_by_path.get(normalized_path, [])
        if configured:
            interfaces.append(configured.pop(0))
    return interfaces


def _requires_explicit_evdev_paths(hardware_id: str) -> bool:
    return "@" in str(hardware_id or "")
