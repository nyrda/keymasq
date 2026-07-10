"""Profile-runtime orchestration across resolution and daemon application."""

import asyncio
from typing import TYPE_CHECKING

from keymasq.session.profile.types import ResolvedDeviceProfile

from ..common import JsonObject
from . import (
    application,
    lifecycle,
    reconciliation,
    resolution,
    runtime_state,
    runtime_status,
)

if TYPE_CHECKING:
    from ..core import SessionManager


async def activate_initial_profiles(manager: "SessionManager") -> None:
    hardware_ids = manager.hardware.list_hardware_ids()
    resolution.log.info(
        "Found %d hardware config(s): %s",
        len(hardware_ids),
        hardware_ids,
    )
    await reevaluate_profiles(manager, reason="initial activation")


async def set_profile_enabled(
    manager: "SessionManager",
    profile_name: str,
    enabled: bool | None,
) -> JsonObject:
    async def _reevaluate(reason: str) -> None:
        await reevaluate_profiles(manager, reason=reason)

    return await lifecycle.set_profile_enabled(
        manager,
        profile_name,
        enabled,
        reevaluate=_reevaluate,
    )


async def cancel_runtime_profile_activation(
    manager: "SessionManager",
    profile_name: str,
    *,
    reevaluate: bool = True,
) -> bool:
    async def _reevaluate(reason: str) -> None:
        await reevaluate_profiles(manager, reason=reason)

    return await lifecycle.cancel_runtime_profile_activation(
        manager,
        profile_name,
        reevaluate=_reevaluate if reevaluate else None,
    )


def schedule_grab_retry(
    manager: "SessionManager",
    hardware_id: str,
    delay_s: float,
) -> None:
    async def _reevaluate(reason: str) -> None:
        await reevaluate_profiles(manager, reason=reason)

    runtime_state.schedule_grab_retry(
        manager,
        hardware_id,
        delay_s,
        reevaluate=_reevaluate,
    )


async def refresh_macro_bindings(manager: "SessionManager") -> None:
    runtime_state.invalidate_runtime_payload_signatures(manager)
    await reevaluate_profiles(manager, reason="macro bindings refreshed")


def schedule_topology_refresh(
    manager: "SessionManager",
    debounce_s: float,
    retry_s: float,
) -> None:
    async def _reevaluate(reason: str) -> None:
        await reevaluate_profiles(manager, reason=reason)

    reconciliation.schedule_topology_refresh(
        manager,
        debounce_s,
        retry_s,
        invalidate=lambda: runtime_state.invalidate_grabbed_state(manager),
        reevaluate=_reevaluate,
    )


async def request_profile_reevaluation(
    manager: "SessionManager",
    *,
    reason: str = "",
    wait: bool = False,
) -> asyncio.Task[None]:
    return await reconciliation.request_profile_reevaluation(
        manager,
        reason=reason,
        wait=wait,
        reconcile=_reevaluate_profiles,
    )


async def reevaluate_profiles(
    manager: "SessionManager",
    *,
    reason: str = "",
) -> None:
    await request_profile_reevaluation(manager, reason=reason, wait=True)


async def _reevaluate_profiles(
    manager: "SessionManager",
    generation: int,
    reason: str,
) -> None:
    operations = resolution.ProfileResolutionOperations(
        apply_device=apply_resolved_device_profile,
        deactivate_device=application.deactivate_profile,
        update_combos=application.update_combos,
        clear_hardware_state=runtime_state.clear_hardware_runtime_state,
        refresh_device_status=runtime_status.refresh_device_runtime_status,
        build_active_payload=runtime_status.build_active_profiles_payload,
        play_lifecycle_macros=lifecycle.play_profile_lifecycle_macros,
    )
    await resolution.reconcile_resolved_profiles(
        manager,
        generation,
        reason,
        operations,
    )


async def apply_resolved_device_profile(
    manager: "SessionManager",
    hardware_id: str,
    resolved: ResolvedDeviceProfile,
    *,
    generation: int | None = None,
) -> None:
    operations = application.DeviceApplicationOperations(
        cancel_grab_retry=runtime_state.cancel_grab_retry,
        schedule_grab_retry=schedule_grab_retry,
        deactivate_profile=application.deactivate_profile,
        update_mapping=application.update_mapping,
        notify_activation=lifecycle.maybe_notify_profile_activation,
    )
    await application.apply_resolved_device_profile(
        manager,
        hardware_id,
        resolved,
        operations,
        generation=generation,
    )
