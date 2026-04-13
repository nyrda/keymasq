import asyncio
import logging
import traceback
from datetime import datetime
from typing import TYPE_CHECKING

from keyforge.common.ipc import Command, CommandType
from keyforge.common.models import ActionType, HardwareConfig
from keyforge.session.profiles import ResolvedCombo, ResolvedDeviceProfile

from . import payloads as runtime_payloads
from .common import JsonObject
from .common import int_value as _int_value
from .common import json_object as _json_object

if TYPE_CHECKING:
    from .core import SessionManager

log = logging.getLogger("keyforge-session")
GRAB_DEVICE_TIMEOUT_S = 330.0
GRAB_RETRY_DELAY_S = 5.0


async def activate_initial_profiles(manager: "SessionManager") -> None:
    hardware_ids = manager.hardware.list_hardware_ids()
    log.info("Found %d hardware config(s): %s", len(hardware_ids), hardware_ids)
    await reevaluate_profiles(manager)


async def set_profile_enabled(
    manager: "SessionManager",
    profile_name: str,
    enabled: bool | None,
) -> JsonObject:
    profile = await asyncio.to_thread(
        manager.profiles.set_profile_enabled,
        profile_name,
        enabled,
    )
    if profile is None:
        manager.send_notification(
            "Keyforge: Profile Not Found",
            f"Profile '{profile_name}' was not found.",
        )
        return {
            "status": "error",
            "message": f"Profile '{profile_name}' not found",
        }

    await reevaluate_profiles(manager)
    return {
        "status": "ok",
        "profile_name": profile.name,
        "enabled": profile.enabled,
        "active_profiles": list(manager.profile_state.active_profile_names),
    }


def build_active_profiles_payload(manager: "SessionManager") -> JsonObject:
    return {
        "status": "ok",
        "active_profiles": list(manager.profile_state.active_profile_names),
        "devices": {
            hardware_id: {
                "profiles": list(resolved.active_profile_names),
                "mapping_count": resolved.mapping_count,
                "always_grab_all": resolved.always_grab_all,
            }
            for hardware_id, resolved in sorted(manager.profile_state.resolved_devices.items())
        },
    }


def build_profile_overview(manager: "SessionManager") -> JsonObject:
    known_hardware_ids = set(manager.hardware.list_hardware_ids())
    for info in manager.profiles.list_profiles():
        known_hardware_ids.update(info.config.device_layers.keys())

    profiles = sorted(
        manager.profiles.list_profiles(),
        key=lambda p: (p.config.name.casefold(), p.config.created_at or datetime.min),
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


def cancel_grab_retry(manager: "SessionManager", hardware_id: str) -> None:
    task = manager.profile_state.grab_retry_tasks.pop(hardware_id, None)
    if task is not None and not task.done():
        task.cancel()


def schedule_grab_retry(
    manager: "SessionManager",
    hardware_id: str,
    delay_s: float,
) -> None:
    if not hardware_id:
        return
    existing = manager.profile_state.grab_retry_tasks.get(hardware_id)
    if existing is not None and not existing.done():
        return

    async def _retry() -> None:
        try:
            await asyncio.sleep(delay_s)
            await reevaluate_profiles(manager)
        except asyncio.CancelledError:
            pass
        finally:
            task = manager.profile_state.grab_retry_tasks.get(hardware_id)
            if task is asyncio.current_task():
                manager.profile_state.grab_retry_tasks.pop(hardware_id, None)

    manager.profile_state.grab_retry_tasks[hardware_id] = asyncio.create_task(_retry())


def invalidate_grabbed_state(manager: "SessionManager") -> None:
    runtime_payloads.clear_all_exec_refs(manager)
    for task in list(manager.profile_state.grab_retry_tasks.values()):
        if not task.done():
            task.cancel()
    manager.profile_state.grab_retry_tasks.clear()
    manager.profile_state.grabbed_devices.clear()
    manager.profile_state.grabbed_interfaces.clear()
    manager.profile_state.grab_waiting_devices.clear()
    manager.profile_state.last_sent_mapping_signatures.clear()
    manager.profile_state.last_sent_combo_signature = ""


def schedule_topology_refresh(
    manager: "SessionManager",
    debounce_s: float,
    retry_s: float,
) -> None:
    existing = manager.profile_state.topology_refresh_task
    if existing is not None and not existing.done():
        existing.cancel()

    async def _run() -> None:
        try:
            delay = debounce_s
            while True:
                await asyncio.sleep(delay)
                try:
                    invalidate_grabbed_state(manager)
                    await reevaluate_profiles(manager)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("Topology refresh failed: %s", e)
                    delay = retry_s
        except asyncio.CancelledError:
            raise
        finally:
            task = manager.profile_state.topology_refresh_task
            if task is asyncio.current_task():
                manager.profile_state.topology_refresh_task = None

    manager.profile_state.topology_refresh_task = asyncio.create_task(_run())


async def reevaluate_profiles(manager: "SessionManager") -> None:
    hardware_ids = manager.hardware.list_hardware_ids()
    resolved = manager.profiles.resolve_active_profiles(
        manager.compositor_state.current_window,
        manager.compositor_state.compositor_capabilities,
        hardware_ids=hardware_ids,
    )
    manager.profile_state.active_profile_names = [
        profile.name for profile in resolved.active_profiles
    ]

    for hardware_id in hardware_ids:
        if hardware_id in manager.capture_state.locks:
            log.info("Reevaluate skipped for %s: capture lock active", hardware_id)
            continue
        device_resolution = resolved.devices.get(
            hardware_id,
            ResolvedDeviceProfile(hardware_id),
        )
        await apply_resolved_device_profile(manager, hardware_id, device_resolution)

    stale_ids = [
        hardware_id
        for hardware_id in list(manager.profile_state.resolved_devices)
        if hardware_id not in set(hardware_ids)
    ]
    for hardware_id in stale_ids:
        manager.profile_state.resolved_devices.pop(hardware_id, None)
        manager.profile_state.last_sent_mapping_signatures.pop(hardware_id, None)

    await update_combos(manager, resolved.combos)
    manager.broadcast_to_session_clients(
        {"event": "profiles_changed", **build_active_profiles_payload(manager)}
    )


async def apply_resolved_device_profile(
    manager: "SessionManager",
    hardware_id: str,
    resolved: ResolvedDeviceProfile,
) -> None:
    if hardware_id in manager.capture_state.locks:
        log.debug("Skipping activation for %s while capture is active", hardware_id)
        return

    hardware_config = manager.hardware.get_hardware(hardware_id)
    if not hardware_config:
        log.warning("No hardware config for %s", hardware_id)
        return

    old_resolved = manager.profile_state.resolved_devices.get(hardware_id)
    old_profile_names = old_resolved.active_profile_names if old_resolved else []
    manager.profile_state.resolved_devices[hardware_id] = resolved

    if not resolved.has_effective_mapping:
        cancel_grab_retry(manager, hardware_id)
        manager.profile_state.grab_waiting_devices.discard(hardware_id)
        if hardware_id in manager.profile_state.grabbed_devices:
            await deactivate_profile(manager, hardware_id, immediate=True)
        return

    new_interfaces = get_interfaces_to_grab(hardware_config, resolved)
    current_interfaces = manager.profile_state.grabbed_interfaces.get(hardware_id, {})

    if hardware_id in manager.profile_state.grabbed_devices:
        if set(current_interfaces.keys()) == set(new_interfaces.keys()):
            if not runtime_payloads.mapping_update_needed(manager, hardware_id, resolved):
                log.debug("Skipping unchanged mapping for %s", hardware_id)
                maybe_notify_profile_activation(
                    manager,
                    hardware_config.name,
                    old_profile_names,
                    resolved,
                )
                return
            if old_profile_names == resolved.active_profile_names and manager.verbosity >= 1:
                log.debug(
                    "Resolved profile set already active for %s, updating mapping only",
                    hardware_id,
                )
            elif old_profile_names != resolved.active_profile_names:
                log.info(
                    "Same interfaces for %s, updating mapping only (old=%s new=%s)",
                    hardware_id,
                    old_profile_names,
                    resolved.active_profile_names,
                )
            updated = await update_mapping(manager, hardware_id, resolved)
            if updated:
                maybe_notify_profile_activation(
                    manager,
                    hardware_config.name,
                    old_profile_names,
                    resolved,
                )
                return
            log.warning(
                "Mapping update failed for %s with same interfaces; forcing re-grab",
                hardware_id,
            )
            await deactivate_profile(manager, hardware_id)

        log.info(
            "Interfaces changed for %s, reconfiguring in keyforged (old: %s -> new: %s)",
            hardware_id,
            list(current_interfaces.keys()),
            list(new_interfaces.keys()),
        )

    log.info("Grabbing device %s (interfaces: %s)", hardware_id, list(new_interfaces.keys()))
    try:
        result = await manager.client.send_command(
            Command(
                command=CommandType.GRAB_DEVICE,
                data={
                    "hardware_id": hardware_id,
                    "evdev_paths": list(new_interfaces.values()),
                    "button_map": {b.id: b.evdev for b in hardware_config.buttons},
                    "button_codes": manager.resolved_button_codes(hardware_config.buttons),
                    "button_values": {
                        b.id: int(evdev_value)
                        for b in hardware_config.buttons
                        if (evdev_value := getattr(b, "evdev_value", None)) is not None
                    },
                    "button_sources": {b.id: b.source for b in hardware_config.buttons if b.source},
                    "force_grab_unmapped": bool(resolved.combo_event_count),
                },
            ),
            timeout=GRAB_DEVICE_TIMEOUT_S,
        )
        if result.status == "ok":
            result_data = _json_object(result.data)
            grabbed_count = (
                _int_value(result_data.get("grabbed_count"), 0)
                if result_data is not None
                else 0
            )
            cancel_grab_retry(manager, hardware_id)
            manager.profile_state.grab_waiting_devices.discard(hardware_id)
            log.info("keyforged: Grabbed device %s: %s", hardware_id, result.data)
            if grabbed_count > 0:
                manager.profile_state.grabbed_devices.add(hardware_id)
                manager.profile_state.grabbed_interfaces[hardware_id] = new_interfaces
            else:
                manager.profile_state.grabbed_devices.discard(hardware_id)
                manager.profile_state.grabbed_interfaces.pop(hardware_id, None)
                log.warning(
                    (
                        "keyforged grab returned zero interfaces for %s "
                        "(requested=%s, mappings=%d)"
                    ),
                    hardware_id,
                    list(new_interfaces.keys()),
                    len(resolved.mappings),
                )
                manager.profile_state.last_sent_mapping_signatures.pop(hardware_id, None)
                return
        else:
            log.error("keyforged: Failed to grab device %s: %s", hardware_id, result.error)
            if "timed out waiting" in str(result.error or "").lower():
                schedule_grab_retry(manager, hardware_id, delay_s=GRAB_RETRY_DELAY_S)
            return
    except Exception as e:
        log.error(
            "keyforged: Exception grabbing device %s: %s: %s",
            hardware_id,
            type(e).__name__,
            e,
        )
        traceback.print_exc()
        if isinstance(e, TimeoutError):
            manager.send_notification(
                "Keyforge: Grab Timed Out",
                (
                    f"{device_name_for_hardware(manager, hardware_id)}: grab timed out while "
                    "waiting for keys to be released. Retrying automatically."
                ),
            )
            schedule_grab_retry(manager, hardware_id, delay_s=GRAB_RETRY_DELAY_S)
        return

    log.info(
        "Setting mapping for %s with %d buttons from profiles=%s",
        hardware_id,
        len(resolved.mappings),
        resolved.active_profile_names,
    )
    try:
        mapping = runtime_payloads.profile_to_mapping(manager, resolved, hardware_id)
        log.debug("Mapping data: %s", runtime_payloads.mapping_log_view(mapping))

        result = await manager.client.send_command(
            Command(
                command=CommandType.SET_MAPPING,
                data={
                    "hardware_id": hardware_id,
                    "mapping": mapping,
                },
            )
        )

        if result.status == "ok":
            manager.profile_state.last_sent_mapping_signatures[hardware_id] = (
                runtime_payloads.resolved_mapping_signature(manager, resolved, hardware_id)
            )
            log.info(
                "Activated resolved profiles %s for %s",
                resolved.active_profile_names,
                hardware_id,
            )
            maybe_notify_profile_activation(
                manager,
                hardware_config.name,
                old_profile_names,
                resolved,
            )
        else:
            log.error("Failed to set mapping: %s", result.error)

    except Exception as e:
        log.error("Exception setting mapping: %s: %s", type(e).__name__, e)
        traceback.print_exc()


def get_interfaces_to_grab(
    hardware_config: HardwareConfig,
    resolved: ResolvedDeviceProfile,
) -> dict[str, str]:
    interface_to_path: dict[str, str] = {}
    for dev in hardware_config.evdev_devices:
        if dev.id:
            interface_to_path[dev.id] = dev.path

    if resolved.always_grab_all:
        return interface_to_path

    button_to_source: dict[str, str] = {
        b.id: b.source for b in hardware_config.buttons if b.source
    }

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

    log.info(
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


async def update_combos(manager: "SessionManager", combos: list[ResolvedCombo]) -> None:
    signature = runtime_payloads.resolved_combos_signature(manager, combos)
    if signature == manager.profile_state.last_sent_combo_signature:
        log.debug("Skipping unchanged combo payload")
        return
    runtime_payloads.clear_combo_exec_refs(manager)
    payload = runtime_payloads.resolved_combos_payload(manager, combos)
    try:
        result = await manager.client.send_command(
            Command(
                command=CommandType.SET_COMBOS,
                data={"combos": payload},
            )
        )
        if result.status != "ok":
            log.error("Failed to update combos: %s", result.error)
            return
        manager.profile_state.last_sent_combo_signature = signature
    except Exception as e:
        log.error("Exception updating combos: %s: %s", type(e).__name__, e)


async def update_mapping(
    manager: "SessionManager",
    hardware_id: str,
    resolved: ResolvedDeviceProfile,
) -> bool:
    if hardware_id not in manager.profile_state.grabbed_devices:
        return False

    signature = runtime_payloads.resolved_mapping_signature(manager, resolved, hardware_id)
    runtime_payloads.clear_exec_refs(manager, hardware_id)

    log.info("Updating mapping for %s with %d buttons", hardware_id, len(resolved.mappings))
    try:
        mapping = runtime_payloads.profile_to_mapping(manager, resolved, hardware_id)
        result = await manager.client.send_command(
            Command(
                command=CommandType.SET_MAPPING,
                data={
                    "hardware_id": hardware_id,
                    "mapping": mapping,
                },
            )
        )
        if result.status == "ok":
            log.info("Updated mapping for %s", hardware_id)
            manager.profile_state.last_sent_mapping_signatures[hardware_id] = signature
            return True
        log.error("Failed to update mapping: %s", result.error)
        return False
    except Exception as e:
        log.error("Exception updating mapping: %s: %s", type(e).__name__, e)
        return False


async def deactivate_profile(
    manager: "SessionManager",
    hardware_id: str,
    immediate: bool = False,
) -> None:
    cancel_grab_retry(manager, hardware_id)
    manager.profile_state.grab_waiting_devices.discard(hardware_id)
    if hardware_id not in manager.profile_state.grabbed_devices:
        return

    try:
        result = await manager.client.send_command(
            Command(
                command=CommandType.RELEASE_DEVICE,
                data={"hardware_id": hardware_id, "immediate": bool(immediate)},
            )
        )
        if result.status != "ok":
            log.error("Failed to release device %s: %s", hardware_id, result.error)
            return
        manager.profile_state.grabbed_devices.discard(hardware_id)
        manager.profile_state.grabbed_interfaces.pop(hardware_id, None)
        manager.profile_state.last_sent_mapping_signatures.pop(hardware_id, None)
    except Exception as e:
        log.error("Failed to release device %s: %s", hardware_id, e)

    runtime_payloads.clear_exec_refs(manager, hardware_id)
    log.info("Deactivated grabbed mapping for %s", hardware_id)


def maybe_notify_profile_activation(
    manager: "SessionManager",
    device_name: str,
    old_profile_names: list[str],
    resolved: ResolvedDeviceProfile,
) -> None:
    if old_profile_names == resolved.active_profile_names:
        return
    if not resolved.notify_profiles:
        return
    profile_list = ", ".join(resolved.active_profile_names) or "passthrough"
    manager.send_notification("Profile Activated", f"{device_name}: {profile_list}")


def device_name_for_hardware(manager: "SessionManager", hardware_id: str) -> str:
    hardware = manager.hardware.get_hardware(hardware_id)
    if hardware is None:
        return hardware_id
    return str(getattr(hardware, "name", "") or hardware_id)
