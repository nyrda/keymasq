"""Tracked daemon output state, grab retries, and runtime-state cleanup."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ..payload.references import clear_all, clear_device

if TYPE_CHECKING:
    from ..core import SessionManager

type ReevaluateProfiles = Callable[[str], Awaitable[None]]


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
    *,
    reevaluate: ReevaluateProfiles,
) -> None:
    """Schedule at most one delayed reconciliation for a waiting device."""
    if not hardware_id:
        return
    existing = manager.profile_state.grab_retry_tasks.get(hardware_id)
    if existing is not None and not existing.done():
        return

    async def _retry() -> None:
        try:
            await asyncio.sleep(delay_s)
            await reevaluate(f"grab retry for {hardware_id}")
        except asyncio.CancelledError:
            pass
        finally:
            task = manager.profile_state.grab_retry_tasks.get(hardware_id)
            if task is asyncio.current_task():
                manager.profile_state.grab_retry_tasks.pop(hardware_id, None)

    manager.profile_state.grab_retry_tasks[hardware_id] = asyncio.create_task(_retry())


def invalidate_grabbed_state(manager: "SessionManager") -> None:
    """Reset every daemon-owned profile runtime cache after disconnection."""
    clear_all(manager)
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


def clear_hardware_runtime_state(
    manager: "SessionManager",
    hardware_id: str,
) -> None:
    """Forget cached runtime state for one hardware configuration."""
    cancel_grab_retry(manager, hardware_id)
    manager.profile_state.grabbed_devices.discard(hardware_id)
    manager.profile_state.grabbed_interfaces.pop(hardware_id, None)
    manager.profile_state.grab_waiting_devices.discard(hardware_id)
    manager.profile_state.grab_status.pop(hardware_id, None)
    manager.profile_state.last_sent_grab_signatures.pop(hardware_id, None)
    manager.profile_state.last_sent_mapping_signatures.pop(hardware_id, None)
    clear_device(manager, hardware_id)


def invalidate_runtime_payload_signatures(manager: "SessionManager") -> None:
    manager.profile_state.last_sent_mapping_signatures.clear()
    manager.profile_state.last_sent_combo_signature = ""


def device_name_for_hardware(manager: "SessionManager", hardware_id: str) -> str:
    hardware = manager.hardware.get_hardware(hardware_id)
    if hardware is None:
        return hardware_id
    return str(getattr(hardware, "name", "") or hardware_id)
