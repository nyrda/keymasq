import asyncio
import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, cast

from keymasq.common.coercion import coerce_int
from keymasq.common.ipc import Command, CommandType
from keymasq.common.models import (
    ActionType,
    HardwareConfig,
    ProfileConfig,
    profile_deactivation_policy_to_dict,
)
from keymasq.session.profiles import ResolvedCombo, ResolvedDeviceProfile

from . import payloads as runtime_payloads
from .common import JsonObject
from .common import json_list as _json_list
from .common import json_object as _json_object
from .constants import GRAB_DEVICE_TIMEOUT_S, GRAB_RETRY_DELAY_S

if TYPE_CHECKING:
    from .core import SessionManager

log = logging.getLogger("keymasq-session")
DEVICE_RUNTIME_STATUS_TIMEOUT_S = 1.0


async def activate_initial_profiles(manager: "SessionManager") -> None:
    hardware_ids = manager.hardware.list_hardware_ids()
    log.info("Found %d hardware config(s): %s", len(hardware_ids), hardware_ids)
    await reevaluate_profiles(manager, reason="initial activation")


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
            "Keymasq: Profile Not Found",
            f"Profile '{profile_name}' was not found.",
        )
        return {
            "status": "error",
            "message": f"Profile '{profile_name}' not found",
        }

    if profile.enabled is False:
        await cancel_runtime_profile_activation(
            manager,
            profile.name,
            reevaluate=False,
        )

    await reevaluate_profiles(manager, reason=f"profile {profile_name} enabled={profile.enabled}")
    return {
        "status": "ok",
        "profile_name": profile.name,
        "enabled": profile.enabled,
        "active_profiles": list(manager.profile_state.active_profile_names),
    }


async def cancel_runtime_profile_activation(
    manager: "SessionManager",
    profile_name: str,
    *,
    reevaluate: bool = True,
) -> bool:
    activation = manager.profile_state.runtime_profile_activations.pop(profile_name, None)
    if activation is None:
        return False
    try:
        await manager.client.send_command(
            Command(
                command=CommandType.CANCEL_PROFILE_ACTIVATION,
                data={
                    "profile_name": profile_name,
                    "activation_id": activation.activation_id,
                },
            )
        )
    except OSError as exc:
        log.debug(
            "Failed to cancel runtime profile activation profile=%s activation=%s: %s",
            profile_name,
            activation.activation_id,
            exc,
        )
    except Exception:
        log.exception(
            "Unexpected failure cancelling runtime profile activation profile=%s activation=%s",
            profile_name,
            activation.activation_id,
        )
    if reevaluate:
        await reevaluate_profiles(
            manager,
            reason=f"runtime profile activation cancelled {profile_name}",
        )
    return True


def build_active_profiles_payload(manager: "SessionManager") -> JsonObject:
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
            mapping_signature = runtime_payloads.resolved_mapping_signature(
                manager,
                resolved,
                hardware_id,
            )
            mapping_applied = (
                mapping_count <= 0
                or manager.profile_state.last_sent_mapping_signatures.get(hardware_id)
                == mapping_signature
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

    status = _json_object(result.data)
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
    configured_devices = list(getattr(hardware, "evdev_devices", []) or []) if hardware else []
    configured_count = len(configured_devices)
    runtime_status = manager.profile_state.device_runtime_status
    runtime_ready = bool(
        manager.connected and runtime_status.get("status") == "ok"
    )
    requested_interfaces = _requested_interfaces_for_device(
        hardware,
        resolved,
        inspector_active=inspector_active,
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
) -> dict[str, str]:
    if hardware is None:
        return {}
    if inspector_active:
        return all_configured_interfaces(hardware)
    if not hasattr(resolved, "mappings"):
        return {}
    return get_interfaces_to_grab(hardware, cast(ResolvedDeviceProfile, resolved))


def _unknown_configured_interface_payloads(
    configured_devices: list[object],
    requested_interfaces: dict[str, str],
) -> list[JsonObject]:
    return [
        {
            "id": _configured_interface_id(configured),
            "configured_path": str(getattr(configured, "path", "") or ""),
            "type": _configured_interface_type(configured),
            "connected": False,
            "requested": _configured_interface_requested(configured, requested_interfaces),
            "grabbed": False,
            "current_path": "",
            "stable_path": "",
        }
        for configured in configured_devices
    ]


def _runtime_interfaces_for_hardware(raw_interfaces: object, hardware_id: str) -> list[JsonObject]:
    interfaces: list[JsonObject] = []
    for raw in _json_list(raw_interfaces):
        if not isinstance(raw, dict):
            continue
        item = cast(JsonObject, raw)
        if _runtime_hardware_matches(
            hardware_id,
            str(item.get("hardware_id", "") or ""),
            interface_id=str(item.get("interface_id", "") or ""),
        ):
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
    return {
        "id": _configured_interface_id(configured),
        "configured_path": str(getattr(configured, "path", "") or ""),
        "type": _configured_interface_type(configured),
        "connected": live is not None or grabbed is not None,
        "requested": _configured_interface_requested(configured, requested_interfaces),
        "grabbed": grabbed is not None,
        "current_path": str(
            matched.get("resolved_path")
            or matched.get("path")
            or ""
        ),
        "stable_path": str(matched.get("stable_path") or ""),
    }


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


async def cancel_all_grab_retries(manager: "SessionManager") -> None:
    tasks = list(manager.profile_state.grab_retry_tasks.values())
    if not tasks:
        return
    for task in tasks:
        if not task.done():
            task.cancel()
    manager.profile_state.grab_retry_tasks.clear()
    await asyncio.gather(*tasks, return_exceptions=True)


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
            await reevaluate_profiles(manager, reason=f"grab retry for {hardware_id}")
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
    manager.profile_state.grab_status.clear()
    manager.profile_state.device_runtime_status.clear()
    manager.profile_state.last_sent_grab_signatures.clear()
    manager.profile_state.last_sent_mapping_signatures.clear()
    manager.profile_state.last_sent_combo_signature = ""
    manager.profile_state.resolved_combos.clear()


def clear_hardware_runtime_state(manager: "SessionManager", hardware_id: str) -> None:
    cancel_grab_retry(manager, hardware_id)
    manager.profile_state.grabbed_devices.discard(hardware_id)
    manager.profile_state.grabbed_interfaces.pop(hardware_id, None)
    manager.profile_state.grab_waiting_devices.discard(hardware_id)
    manager.profile_state.grab_status.pop(hardware_id, None)
    manager.profile_state.last_sent_grab_signatures.pop(hardware_id, None)
    manager.profile_state.last_sent_mapping_signatures.pop(hardware_id, None)
    runtime_payloads.clear_exec_refs(manager, hardware_id)


def invalidate_runtime_payload_signatures(manager: "SessionManager") -> None:
    manager.profile_state.last_sent_mapping_signatures.clear()
    manager.profile_state.last_sent_combo_signature = ""


def runtime_profile_names(manager: "SessionManager") -> list[str]:
    return [
        activation.profile_name
        for activation in sorted(
            manager.profile_state.runtime_profile_activations.values(),
            key=lambda item: item.sequence,
        )
    ]


async def clear_runtime_profile_activations(
    manager: "SessionManager",
    *,
    reason: str,
) -> None:
    if not manager.profile_state.runtime_profile_activations:
        return
    manager.profile_state.runtime_profile_activations.clear()
    await reevaluate_profiles(manager, reason=reason)


async def refresh_macro_bindings(manager: "SessionManager") -> None:
    invalidate_runtime_payload_signatures(manager)
    await reevaluate_profiles(manager, reason="macro bindings refreshed")


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
                    await reevaluate_profiles(manager, reason="topology refresh")
                    return
                except asyncio.CancelledError:
                    raise
                except OSError as exc:
                    log.warning("Topology refresh failed: %s", exc)
                    delay = retry_s
                except Exception:
                    log.exception("Unexpected topology refresh failure")
                    delay = retry_s
        except asyncio.CancelledError:
            raise
        finally:
            task = manager.profile_state.topology_refresh_task
            if task is asyncio.current_task():
                manager.profile_state.topology_refresh_task = None

    manager.profile_state.topology_refresh_task = asyncio.create_task(_run())


async def request_profile_reevaluation(
    manager: "SessionManager",
    *,
    reason: str = "",
    wait: bool = False,
) -> asyncio.Task[None]:
    manager.profile_state.apply_generation += 1
    generation = manager.profile_state.apply_generation
    previous = manager.profile_state.apply_task
    current = asyncio.current_task()
    if previous is not None and previous is not current and not previous.done():
        previous.cancel()

    task = asyncio.create_task(
        _reevaluate_profiles_for_generation(manager, generation, reason=reason)
    )
    manager.profile_state.apply_task = task
    manager.profile_state.apply_reason = reason

    def _clear_current_apply(done: asyncio.Task[None]) -> None:
        if manager.profile_state.apply_task is done:
            manager.profile_state.apply_task = None

    task.add_done_callback(_clear_current_apply)
    if wait:
        await task
        latest = cast(
            asyncio.Task[None] | None,
            manager.profile_state.__dict__.get("apply_task"),
        )
        if (
            not profile_apply_is_current(manager, generation)
            and latest is not None
            and latest is not task
        ):
            await latest
    return task


async def reevaluate_profiles(manager: "SessionManager", *, reason: str = "") -> None:
    await request_profile_reevaluation(manager, reason=reason, wait=True)


def profile_apply_is_current(manager: "SessionManager", generation: int | None) -> bool:
    return generation is None or generation == manager.profile_state.apply_generation


def raise_if_stale_profile_apply(
    manager: "SessionManager",
    generation: int | None,
) -> None:
    if not profile_apply_is_current(manager, generation):
        raise asyncio.CancelledError


async def _reevaluate_profiles_for_generation(
    manager: "SessionManager",
    generation: int,
    *,
    reason: str = "",
) -> None:
    try:
        await _reevaluate_profiles(manager, generation=generation, reason=reason)
    except asyncio.CancelledError:
        if manager.verbosity >= 1:
            log.debug(
                "Profile reevaluation interrupted: generation=%s reason=%s",
                generation,
                reason,
            )
    except Exception:
        log.exception("Profile reevaluation failed: generation=%s reason=%s", generation, reason)
        raise


async def _reevaluate_profiles(
    manager: "SessionManager",
    *,
    generation: int | None,
    reason: str = "",
) -> None:
    raise_if_stale_profile_apply(manager, generation)
    hardware_ids = manager.hardware.list_hardware_ids()
    resolved = manager.profiles.resolve_active_profiles(
        manager.compositor_state.current_window,
        manager.compositor_state.compositor_capabilities,
        hardware_ids=hardware_ids,
        runtime_profile_names=runtime_profile_names(manager),
    )
    raise_if_stale_profile_apply(manager, generation)
    old_active_profile_names = list(manager.profile_state.active_profile_names)
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
        await apply_resolved_device_profile(
            manager,
            hardware_id,
            device_resolution,
            generation=generation,
        )
        raise_if_stale_profile_apply(manager, generation)

    active_hardware_ids = set(hardware_ids)
    stale_ids = sorted(
        (
            set(manager.profile_state.resolved_devices)
            | set(manager.profile_state.grabbed_devices)
            | set(manager.profile_state.grabbed_interfaces)
            | set(manager.profile_state.grab_waiting_devices)
            | set(manager.profile_state.last_sent_grab_signatures)
            | set(manager.profile_state.last_sent_mapping_signatures)
        )
        - active_hardware_ids
    )
    for hardware_id in stale_ids:
        release_succeeded = True
        if hardware_id in manager.profile_state.grabbed_devices:
            release_succeeded = await deactivate_profile(
                manager,
                hardware_id,
                immediate=True,
                generation=generation,
            )
            raise_if_stale_profile_apply(manager, generation)
        if not release_succeeded:
            continue
        manager.profile_state.resolved_devices.pop(hardware_id, None)
        clear_hardware_runtime_state(manager, hardware_id)

    await update_combos(manager, resolved.combos, generation=generation)
    raise_if_stale_profile_apply(manager, generation)
    if manager.session_clients:
        await refresh_device_runtime_status(manager)
        raise_if_stale_profile_apply(manager, generation)
    manager.broadcast_to_session_clients(
        {"event": "profiles_changed", **build_active_profiles_payload(manager)}
    )
    await play_profile_lifecycle_macros(
        manager,
        old_active_profile_names,
        resolved.active_profiles,
    )


async def play_profile_lifecycle_macros(
    manager: "SessionManager",
    old_active_profile_names: list[str],
    new_active_profiles: list[ProfileConfig],
) -> None:
    old_names = set(old_active_profile_names)
    new_names = {profile.name for profile in new_active_profiles}

    deactivated_names = [name for name in old_active_profile_names if name not in new_names]
    activated_profiles = [
        profile for profile in new_active_profiles if profile.name not in old_names
    ]

    for profile_name in deactivated_names:
        profile_info = manager.profiles.get_profile(profile_name)
        if profile_info is None:
            continue
        await play_profile_lifecycle_macro(
            manager,
            profile_info.config.deactivation_macro_name,
            profile_name=profile_name,
            transition="deactivation",
        )

    for profile in activated_profiles:
        await play_profile_lifecycle_macro(
            manager,
            profile.activation_macro_name,
            profile_name=profile.name,
            transition="activation",
        )


async def play_profile_lifecycle_macro(
    manager: "SessionManager",
    macro_name: str | None,
    *,
    profile_name: str,
    transition: str,
) -> None:
    if not macro_name:
        return
    try:
        result = await manager.client.send_command(
            Command(
                command=CommandType.MACRO_PLAY_BY_NAME,
                data={"name": macro_name},
            )
        )
    except OSError as exc:
        log.warning(
            "Failed to play %s macro '%s' for profile '%s': %s",
            transition,
            macro_name,
            profile_name,
            exc,
        )
        return
    except Exception:
        log.exception(
            "Unexpected failure playing %s macro '%s' for profile '%s'",
            transition,
            macro_name,
            profile_name,
        )
        return

    if result.status != "ok":
        log.warning(
            "Failed to play %s macro '%s' for profile '%s': %s",
            transition,
            macro_name,
            profile_name,
            result.error or result.data or "playback failed",
        )


async def apply_resolved_device_profile(
    manager: "SessionManager",
    hardware_id: str,
    resolved: ResolvedDeviceProfile,
    *,
    generation: int | None = None,
) -> None:
    raise_if_stale_profile_apply(manager, generation)
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
    inspector_active = _device_inspector_active(manager, hardware_id)

    if not resolved.has_effective_mapping and not inspector_active:
        cancel_grab_retry(manager, hardware_id)
        if hardware_id in manager.profile_state.grabbed_devices:
            await deactivate_profile(
                manager,
                hardware_id,
                immediate=True,
                generation=generation,
            )
        elif hardware_id not in manager.profile_state.last_sent_grab_signatures:
            manager.profile_state.grab_waiting_devices.discard(hardware_id)
        return

    new_interfaces = (
        all_configured_interfaces(hardware_config)
        if inspector_active
        else get_interfaces_to_grab(hardware_config, resolved)
    )
    current_interfaces = manager.profile_state.grabbed_interfaces.get(hardware_id, {})
    grab_payload = build_grab_device_payload(
        manager,
        hardware_id,
        hardware_config,
        resolved,
        new_interfaces,
        force_grab_unmapped=inspector_active,
    )
    grab_signature = grab_device_payload_signature(grab_payload)
    if not new_interfaces and not inspector_active:
        if manager.profile_state.last_sent_grab_signatures.get(hardware_id) != grab_signature:
            log.warning(
                (
                    "No configured interfaces selected for %s "
                    "(mappings=%d combo_sources=%s configured_interfaces=%s); "
                    "skipping daemon grab"
                ),
                hardware_id,
                len(resolved.mappings),
                sorted(resolved.combo_sources),
                sorted(all_configured_interfaces(hardware_config)),
            )
            manager.profile_state.last_sent_grab_signatures[hardware_id] = grab_signature
        return

    if (
        hardware_id in manager.profile_state.grab_waiting_devices
        and hardware_id not in manager.profile_state.grabbed_devices
        and manager.profile_state.last_sent_grab_signatures.get(hardware_id) == grab_signature
    ):
        log.debug("Skipping pending grab for unavailable device %s", hardware_id)
        maybe_notify_profile_activation(
            manager,
            hardware_config.name,
            old_profile_names,
            resolved,
        )
        return

    if hardware_id in manager.profile_state.grabbed_devices:
        if set(current_interfaces.keys()) == set(new_interfaces.keys()):
            grab_update_needed = (
                manager.profile_state.last_sent_grab_signatures.get(hardware_id, "")
                != grab_signature
            )
            mapping_update_needed = runtime_payloads.mapping_update_needed(
                manager,
                hardware_id,
                resolved,
            )
            if not grab_update_needed and not mapping_update_needed:
                log.debug("Skipping unchanged mapping for %s", hardware_id)
                maybe_notify_profile_activation(
                    manager,
                    hardware_config.name,
                    old_profile_names,
                    resolved,
                )
                return
            if grab_update_needed:
                updated_grab = await update_grab_device_payload(
                    manager,
                    hardware_id,
                    grab_payload,
                    grab_signature,
                    generation=generation,
                )
                raise_if_stale_profile_apply(manager, generation)
                if not updated_grab:
                    log.warning(
                        "Grab update failed for %s with same interfaces; forcing re-grab",
                        hardware_id,
                    )
                    await deactivate_profile(manager, hardware_id, generation=generation)
                elif not mapping_update_needed:
                    maybe_notify_profile_activation(
                        manager,
                        hardware_config.name,
                        old_profile_names,
                        resolved,
                    )
                    return
            if hardware_id not in manager.profile_state.grabbed_devices:
                log.info(
                    "Grab config refresh deactivated %s; reconfiguring in keymasqd",
                    hardware_id,
                )
            elif mapping_update_needed:
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
                updated = await update_mapping(
                    manager,
                    hardware_id,
                    resolved,
                    generation=generation,
                )
                raise_if_stale_profile_apply(manager, generation)
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
                await deactivate_profile(manager, hardware_id, generation=generation)
                manager.profile_state.last_sent_grab_signatures.pop(hardware_id, None)
            else:
                maybe_notify_profile_activation(
                    manager,
                    hardware_config.name,
                    old_profile_names,
                    resolved,
                )
                return

        log.info(
            "Interfaces changed for %s, reconfiguring in keymasqd (old: %s -> new: %s)",
            hardware_id,
            list(current_interfaces.keys()),
            list(new_interfaces.keys()),
        )

    log.info("Grabbing device %s (interfaces: %s)", hardware_id, list(new_interfaces.keys()))
    try:
        result = await manager.client.send_command(
            Command(
                command=CommandType.GRAB_DEVICE,
                data=grab_payload,
            ),
            timeout=GRAB_DEVICE_TIMEOUT_S,
        )
        raise_if_stale_profile_apply(manager, generation)
        if result.status == "ok":
            result_data = _json_object(result.data)
            grabbed_count = (
                coerce_int(result_data.get("grabbed_count"), 0)
                if result_data is not None
                else 0
            )
            cancel_grab_retry(manager, hardware_id)
            manager.profile_state.grab_waiting_devices.discard(hardware_id)
            log.info("keymasqd: Grabbed device %s: %s", hardware_id, result.data)
            if grabbed_count > 0:
                manager.profile_state.grabbed_devices.add(hardware_id)
                manager.profile_state.grabbed_interfaces[hardware_id] = new_interfaces
                manager.profile_state.last_sent_grab_signatures[hardware_id] = grab_signature
                manager.profile_state.grab_status.pop(hardware_id, None)
            else:
                manager.profile_state.grabbed_devices.discard(hardware_id)
                manager.profile_state.grabbed_interfaces.pop(hardware_id, None)
                waiting_for_device = bool(
                    result_data is not None and result_data.get("waiting_for_device")
                )
                if waiting_for_device:
                    manager.profile_state.grab_waiting_devices.add(hardware_id)
                    manager.profile_state.grab_status[hardware_id] = {
                        "state": "waiting_for_device",
                        "path": next(iter(new_interfaces.values()), ""),
                    }
                    manager.profile_state.last_sent_grab_signatures[hardware_id] = grab_signature
                else:
                    manager.profile_state.grab_waiting_devices.discard(hardware_id)
                    manager.profile_state.grab_status.pop(hardware_id, None)
                    manager.profile_state.last_sent_grab_signatures.pop(hardware_id, None)
                log.warning(
                    ("keymasqd grab returned zero interfaces for %s (requested=%s, mappings=%d)"),
                    hardware_id,
                    list(new_interfaces.keys()),
                    len(resolved.mappings),
                )
                manager.profile_state.last_sent_mapping_signatures.pop(hardware_id, None)
                return
        else:
            log.error("keymasqd: Failed to grab device %s: %s", hardware_id, result.error)
            if "timed out waiting" in str(result.error or "").lower():
                manager.profile_state.grab_status[hardware_id] = {
                    "state": "timed_out",
                    "path": next(iter(new_interfaces.values()), ""),
                }
                schedule_grab_retry(manager, hardware_id, delay_s=GRAB_RETRY_DELAY_S)
            return
    except TimeoutError as exc:
        log.error(
            "keymasqd: Exception grabbing device %s: %s: %s",
            hardware_id,
            type(exc).__name__,
            exc,
        )
        manager.send_notification(
            "Keymasq: Grab Timed Out",
            (
                f"{device_name_for_hardware(manager, hardware_id)}: grab timed out while "
                "waiting for keys to be released. Retrying automatically."
            ),
        )
        manager.profile_state.grab_status[hardware_id] = {
            "state": "timed_out",
            "path": next(iter(new_interfaces.values()), ""),
        }
        schedule_grab_retry(manager, hardware_id, delay_s=GRAB_RETRY_DELAY_S)
        return
    except OSError as exc:
        log.error(
            "keymasqd: Exception grabbing device %s: %s: %s",
            hardware_id,
            type(exc).__name__,
            exc,
        )
        return
    except Exception:
        log.exception("Unexpected failure grabbing device %s", hardware_id)
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
        raise_if_stale_profile_apply(manager, generation)

        result = await manager.client.send_command(
            Command(
                command=CommandType.SET_MAPPING,
                data={
                    "hardware_id": hardware_id,
                    "mapping": mapping,
                },
            )
        )
        raise_if_stale_profile_apply(manager, generation)

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

    except OSError as exc:
        log.error("Exception setting mapping: %s: %s", type(exc).__name__, exc)
    except Exception:
        log.exception("Unexpected failure setting mapping for %s", hardware_id)


def get_interfaces_to_grab(
    hardware_config: HardwareConfig,
    resolved: ResolvedDeviceProfile,
) -> dict[str, str]:
    interface_to_path = all_configured_interfaces(hardware_config)

    if resolved.always_grab_all:
        return interface_to_path

    button_to_source: dict[str, str] = {b.id: b.source for b in hardware_config.buttons if b.source}
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
    return {
        dev.id: dev.path
        for dev in hardware_config.evdev_devices
        if dev.id and str(dev.path or "").strip()
    }


def configured_interface_descriptors(
    hardware_config: HardwareConfig,
    selected_sources: set[str] | None,
) -> list[JsonObject]:
    descriptors: list[JsonObject] = []
    for dev in hardware_config.evdev_devices:
        interface_id = str(getattr(dev, "id", "") or "")
        path = str(getattr(dev, "path", "") or "").strip()
        if not interface_id or not path:
            continue
        if selected_sources is not None and interface_id not in selected_sources:
            continue
        descriptors.append(
            {
                "id": interface_id,
                "path": path,
                "type": getattr(getattr(dev, "device_type", None), "value", "other"),
                "phys": str(getattr(dev, "phys", "") or ""),
                "capabilities": list(getattr(dev, "capabilities", []) or []),
            }
        )
    return descriptors


def _device_inspector_active(manager: "SessionManager", hardware_id: str) -> bool:
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
    analog_inputs = getattr(hardware_config, "analog_inputs", []) or []
    selected_sources = set(interfaces.keys())
    return {
        "hardware_id": hardware_id,
        "evdev_paths": list(interfaces.values()),
        "evdev_interfaces": configured_interface_descriptors(
            hardware_config,
            selected_sources,
        ),
        "button_map": {b.id: b.evdev for b in hardware_config.buttons},
        "button_codes": manager.resolved_button_codes(hardware_config.buttons),
        "button_values": {
            b.id: int(evdev_value)
            for b in hardware_config.buttons
            if (evdev_value := getattr(b, "evdev_value", None)) is not None
        },
        "button_sources": {b.id: b.source for b in hardware_config.buttons if b.source},
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
    signature_payload = {
        "evdev_paths": sorted(str(path) for path in _json_list(payload.get("evdev_paths"))),
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
    for item in _json_list(value):
        if not isinstance(item, dict):
            interfaces.append(item)
            continue

        iface = dict(cast(JsonObject, item))
        capabilities = iface.get("capabilities")
        if isinstance(capabilities, list):
            iface["capabilities"] = sorted(
                str(capability) for capability in cast(list[object], capabilities)
            )
        interfaces.append(iface)

    return sorted(
        interfaces,
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
    )


async def update_grab_device_payload(
    manager: "SessionManager",
    hardware_id: str,
    payload: JsonObject,
    signature: str,
    *,
    generation: int | None = None,
) -> bool:
    try:
        raise_if_stale_profile_apply(manager, generation)
        result = await manager.client.send_command(
            Command(
                command=CommandType.GRAB_DEVICE,
                data=payload,
            ),
            timeout=GRAB_DEVICE_TIMEOUT_S,
        )
        raise_if_stale_profile_apply(manager, generation)
        if result.status != "ok":
            log.error("Failed to update grab config for %s: %s", hardware_id, result.error)
            return False
        result_data = _json_object(result.data)
        grabbed_count = (
            coerce_int(result_data.get("grabbed_count"), 0) if result_data is not None else 0
        )
        if grabbed_count <= 0:
            log.error("Grab config update for %s returned zero grabbed interfaces", hardware_id)
            return False
        manager.profile_state.last_sent_grab_signatures[hardware_id] = signature
        return True
    except OSError as exc:
        log.error(
            "Exception updating grab config for %s: %s: %s",
            hardware_id,
            type(exc).__name__,
            exc,
        )
        return False
    except Exception:
        log.exception("Unexpected failure updating grab config for %s", hardware_id)
        return False


async def update_combos(
    manager: "SessionManager",
    combos: list[ResolvedCombo],
    *,
    generation: int | None = None,
) -> None:
    raise_if_stale_profile_apply(manager, generation)
    signature = runtime_payloads.resolved_combos_signature(manager, combos)
    if signature == manager.profile_state.last_sent_combo_signature:
        log.debug("Skipping unchanged combo payload")
        return
    runtime_payloads.clear_combo_exec_refs(manager)
    payload: list[JsonObject] = []
    active_combos: list[ResolvedCombo] = []
    for combo in combos:
        combo_payload = runtime_payloads.resolved_combo_payload(manager, combo)
        if combo_payload is None:
            continue
        payload.append(combo_payload)
        active_combos.append(combo)
    try:
        raise_if_stale_profile_apply(manager, generation)
        result = await manager.client.send_command(
            Command(
                command=CommandType.SET_COMBOS,
                data={"combos": payload},
            )
        )
        raise_if_stale_profile_apply(manager, generation)
        if result.status != "ok":
            log.error("Failed to update combos: %s", result.error)
            return
        manager.profile_state.last_sent_combo_signature = signature
        manager.profile_state.resolved_combos = list(active_combos)
    except OSError as exc:
        log.error("Exception updating combos: %s: %s", type(exc).__name__, exc)
    except Exception:
        log.exception("Unexpected failure updating combos")


async def update_mapping(
    manager: "SessionManager",
    hardware_id: str,
    resolved: ResolvedDeviceProfile,
    *,
    generation: int | None = None,
) -> bool:
    raise_if_stale_profile_apply(manager, generation)
    if hardware_id not in manager.profile_state.grabbed_devices:
        return False

    signature = runtime_payloads.resolved_mapping_signature(manager, resolved, hardware_id)
    runtime_payloads.clear_exec_refs(manager, hardware_id)

    log.info("Updating mapping for %s with %d buttons", hardware_id, len(resolved.mappings))
    try:
        mapping = runtime_payloads.profile_to_mapping(manager, resolved, hardware_id)
        raise_if_stale_profile_apply(manager, generation)
        result = await manager.client.send_command(
            Command(
                command=CommandType.SET_MAPPING,
                data={
                    "hardware_id": hardware_id,
                    "mapping": mapping,
                },
            )
        )
        raise_if_stale_profile_apply(manager, generation)
        if result.status == "ok":
            log.info("Updated mapping for %s", hardware_id)
            manager.profile_state.last_sent_mapping_signatures[hardware_id] = signature
            return True
        log.error("Failed to update mapping: %s", result.error)
        return False
    except OSError as exc:
        log.error("Exception updating mapping: %s: %s", type(exc).__name__, exc)
        return False
    except Exception:
        log.exception("Unexpected failure updating mapping for %s", hardware_id)
        return False


async def deactivate_profile(
    manager: "SessionManager",
    hardware_id: str,
    immediate: bool = False,
    *,
    generation: int | None = None,
) -> bool:
    raise_if_stale_profile_apply(manager, generation)
    cancel_grab_retry(manager, hardware_id)
    manager.profile_state.grab_waiting_devices.discard(hardware_id)
    if hardware_id not in manager.profile_state.grabbed_devices:
        return True

    try:
        result = await manager.client.send_command(
            Command(
                command=CommandType.RELEASE_DEVICE,
                data={"hardware_id": hardware_id, "immediate": bool(immediate)},
            )
        )
        raise_if_stale_profile_apply(manager, generation)
        if result.status != "ok":
            log.error("Failed to release device %s: %s", hardware_id, result.error)
            return False
        clear_hardware_runtime_state(manager, hardware_id)
    except OSError as exc:
        log.error("Failed to release device %s: %s", hardware_id, exc)
        return False
    except Exception:
        log.exception("Unexpected failure releasing device %s", hardware_id)
        return False

    runtime_payloads.clear_exec_refs(manager, hardware_id)
    log.info("Deactivated grabbed mapping for %s", hardware_id)
    return True


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
