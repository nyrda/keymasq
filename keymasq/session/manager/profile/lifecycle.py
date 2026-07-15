"""Temporary activation tracking, lifecycle macros, and activation notifications."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from keymasq.common.ipc import Command, CommandType
from keymasq.common.model.profiles import ProfileConfig
from keymasq.session.profile.types import ResolvedDeviceProfile

from ..common import JsonObject

if TYPE_CHECKING:
    from ..core import SessionManager

log = logging.getLogger("keymasq-session")
type ReevaluateProfiles = Callable[[str], Awaitable[None]]


async def set_profile_enabled(
    manager: "SessionManager",
    profile_name: str,
    enabled: bool | None,
    *,
    reevaluate: ReevaluateProfiles,
) -> JsonObject:
    """Persist profile enablement and reconcile its runtime activation state."""
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
            reevaluate=None,
        )

    await reevaluate(f"profile {profile_name} enabled={profile.enabled}")
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
    reevaluate: ReevaluateProfiles | None,
) -> bool:
    """Remove one temporary activation and cancel its daemon-side lifetime."""
    activation = manager.profile_state.runtime_profile_activations.pop(
        profile_name,
        None,
    )
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
    if reevaluate is not None:
        await reevaluate(f"runtime profile activation cancelled {profile_name}")
    return True


def runtime_profile_names(manager: "SessionManager") -> list[str]:
    """Return temporary profile names in deterministic activation order."""
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
    reevaluate: ReevaluateProfiles,
) -> None:
    """Clear all temporary activations and request one reconciliation."""
    if not manager.profile_state.runtime_profile_activations:
        return
    manager.profile_state.runtime_profile_activations.clear()
    await reevaluate(reason)


async def play_profile_lifecycle_macros(
    manager: "SessionManager",
    old_active_profile_names: list[str],
    new_active_profiles: list[ProfileConfig],
) -> None:
    """Dispatch exactly one macro for each profile activation transition."""
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
    """Play one transition macro while containing daemon communication failures."""
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


def maybe_notify_profile_activation(
    manager: "SessionManager",
    device_name: str,
    old_profile_names: list[str],
    resolved: ResolvedDeviceProfile,
) -> None:
    """Notify only when a requesting profile changes the active profile set."""
    if old_profile_names == resolved.active_profile_names:
        return
    if not resolved.notify_profiles:
        return
    profile_list = ", ".join(resolved.active_profile_names) or "passthrough"
    manager.send_notification("Profile Activated", f"{device_name}: {profile_list}")
