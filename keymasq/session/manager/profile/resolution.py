"""Profile resolution orchestration across configured hardware devices."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from keymasq.common.model.profiles import ProfileConfig
from keymasq.session.profile.types import (
    ResolvedCombo,
    ResolvedDeviceProfile,
)

from ..common import JsonObject
from .lifecycle import runtime_profile_names
from .reconciliation import raise_if_stale_profile_apply

if TYPE_CHECKING:
    from ..core import SessionManager

log = logging.getLogger("keymasq-session")


class DeviceProfileApplier(Protocol):
    async def __call__(
        self,
        manager: "SessionManager",
        hardware_id: str,
        resolved: ResolvedDeviceProfile,
        *,
        generation: int | None = None,
    ) -> None: ...


class DeviceProfileDeactivator(Protocol):
    async def __call__(
        self,
        manager: "SessionManager",
        hardware_id: str,
        immediate: bool = False,
        *,
        generation: int | None = None,
    ) -> bool: ...


class ComboApplier(Protocol):
    async def __call__(
        self,
        manager: "SessionManager",
        combos: list[ResolvedCombo],
        *,
        generation: int | None = None,
    ) -> None: ...


class LifecycleMacroDispatcher(Protocol):
    async def __call__(
        self,
        manager: "SessionManager",
        old_active_profile_names: list[str],
        new_active_profiles: list[ProfileConfig],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ProfileResolutionOperations:
    """Explicit side effects used by the resolution orchestration pass."""

    apply_device: DeviceProfileApplier
    deactivate_device: DeviceProfileDeactivator
    update_combos: ComboApplier
    clear_hardware_state: Callable[["SessionManager", str], None]
    refresh_device_status: Callable[["SessionManager"], Awaitable[None]]
    build_active_payload: Callable[["SessionManager"], JsonObject]
    play_lifecycle_macros: LifecycleMacroDispatcher


async def reconcile_resolved_profiles(
    manager: "SessionManager",
    generation: int | None,
    reason: str,
    operations: ProfileResolutionOperations,
) -> None:
    """Resolve profiles and apply the newest complete generation to the daemon."""
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
        await operations.apply_device(
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
            release_succeeded = await operations.deactivate_device(
                manager,
                hardware_id,
                immediate=True,
                generation=generation,
            )
            raise_if_stale_profile_apply(manager, generation)
        if not release_succeeded:
            continue
        manager.profile_state.resolved_devices.pop(hardware_id, None)
        operations.clear_hardware_state(manager, hardware_id)

    await operations.update_combos(
        manager,
        resolved.combos,
        generation=generation,
    )
    raise_if_stale_profile_apply(manager, generation)
    if manager.session_clients:
        await operations.refresh_device_status(manager)
        raise_if_stale_profile_apply(manager, generation)
    manager.broadcast_to_session_clients(
        {
            "event": "profiles_changed",
            **operations.build_active_payload(manager),
        }
    )
    await operations.play_lifecycle_macros(
        manager,
        old_active_profile_names,
        resolved.active_profiles,
    )
