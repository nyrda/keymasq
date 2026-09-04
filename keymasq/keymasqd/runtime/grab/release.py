"""Graceful release transactions for grabbed hardware and interfaces."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

from keymasq.keymasqd.runtime import adapters, outputs
from keymasq.keymasqd.runtime.combo import lifecycle
from keymasq.keymasqd.runtime.grab.source_hiding import (
    desired_grab_requests_gamepad_source_hiding,
    disable_hardware_hotplug_hiding_if_unused_best_effort,
    enable_hardware_hotplug_hiding_best_effort,
)
from keymasq.keymasqd.runtime.grab.state import (
    FireAndObserve,
    GrabManager,
    ManagedGrabbedDevice,
)
from keymasq.keymasqd.runtime.grab.support import (
    combo_runtime_deps,
    stop_device_event_loops,
)

log = logging.getLogger("keymasqd.devices")


@dataclass(frozen=True)
class HardwareReleaseDecision:
    """One transition of the delayed hardware-release state machine."""

    action: Literal["cancel", "defer", "release"]
    next_delay: float | None = None


def hardware_release_decision(
    manager: GrabManager,
    hardware_id: str,
) -> HardwareReleaseDecision:
    """Evaluate release policy independently from sleeping and lock ownership."""

    if manager.grab_state.desired_paths.get(hardware_id):
        return HardwareReleaseDecision("cancel")
    if hardware_has_held_inputs(manager, hardware_id):
        return HardwareReleaseDecision(
            "defer",
            float(manager.grab_state.held_release_retry_s),
        )
    return HardwareReleaseDecision("release")


async def release_device_unlocked(
    manager: GrabManager,
    hardware_id: str,
    *,
    log: logging.Logger,
) -> dict[str, object]:
    cancel_pending_hardware_release(manager, hardware_id)
    cancel_pending_interface_releases_for_hardware(manager, hardware_id)
    await stop_device_event_loops(manager.grabbed_devices.get(hardware_id, []))
    await lifecycle.clear_combo_runtime_for_binding_scope(
        manager,
        hardware_id,
        None,
        deps=combo_runtime_deps(),
    )
    desired_config = manager.grab_state.desired_grabs.pop(hardware_id, None)
    if desired_grab_requests_gamepad_source_hiding(desired_config):
        await disable_hardware_hotplug_hiding_if_unused_best_effort(
            manager,
            hardware_id,
        )
    devices = manager.grabbed_devices.pop(hardware_id, [])

    for device in devices:
        await device.release()

    if devices:
        outputs.destroy_global_uinputs(manager, log=log)
    manager.active_mappings.pop(hardware_id, None)
    manager.grab_state.desired_paths.pop(hardware_id, None)
    log.info("Released device %s", hardware_id)
    return {"released": True, "hardware_id": hardware_id}


async def schedule_hardware_release_unlocked(
    manager: GrabManager,
    hardware_id: str,
    grace_s: float | None,
    *,
    asyncio_mod: adapters.AsyncioRuntimeAdapter,
    log: logging.Logger,
) -> dict[str, object]:
    devices = manager.grabbed_devices.get(hardware_id, [])
    desired_config = manager.grab_state.desired_grabs.get(hardware_id)
    if not devices:
        manager.grab_state.desired_grabs.pop(hardware_id, None)
        manager.active_mappings.pop(hardware_id, None)
        manager.grab_state.desired_paths.pop(hardware_id, None)
        if desired_grab_requests_gamepad_source_hiding(desired_config):
            await disable_hardware_hotplug_hiding_if_unused_best_effort(
                manager,
                hardware_id,
            )
        return {"released": True, "hardware_id": hardware_id}

    manager.active_mappings[hardware_id] = {}
    manager.grab_state.desired_paths[hardware_id] = set()
    if desired_grab_requests_gamepad_source_hiding(desired_config):
        await disable_hardware_hotplug_hiding_if_unused_best_effort(
            manager,
            hardware_id,
        )

    delay = max(
        0.01,
        float(manager.grab_state.release_grace_s if grace_s is None else grace_s),
    )
    cancel_pending_hardware_release(manager, hardware_id)
    manager.grab_state.pending_hardware_release[hardware_id] = asyncio_mod.create_task(
        delayed_hardware_release(
            manager,
            hardware_id,
            delay,
            asyncio_mod=asyncio_mod,
            log=log,
        )
    )
    log.info("Scheduled hardware release for %s in %.1fs", hardware_id, delay)
    return {
        "released": False,
        "scheduled": True,
        "hardware_id": hardware_id,
        "grace_s": delay,
    }


async def delayed_hardware_release(
    manager: GrabManager,
    hardware_id: str,
    delay: float,
    *,
    asyncio_mod: adapters.AsyncioRuntimeAdapter,
    log: logging.Logger,
) -> None:
    next_delay = float(delay)
    try:
        while True:
            await asyncio_mod.sleep(next_delay)
            async with manager._op_lock:
                task = manager.grab_state.pending_hardware_release.get(hardware_id)
                if task is not asyncio_mod.current_task():
                    return
                decision = hardware_release_decision(manager, hardware_id)
                if decision.action == "cancel":
                    return
                if decision.action == "defer":
                    next_delay = float(decision.next_delay or 0.0)
                    log.info(
                        "Deferred release for %s: source button still held, retrying in %.1fs",
                        hardware_id,
                        next_delay,
                    )
                    continue
                await release_device_unlocked(manager, hardware_id, log=log)
                return
    except asyncio.CancelledError:
        pass
    finally:
        task = manager.grab_state.pending_hardware_release.get(hardware_id)
        if task is asyncio_mod.current_task():
            manager.grab_state.pending_hardware_release.pop(hardware_id, None)


def hardware_has_held_inputs(manager: GrabManager, hardware_id: str) -> bool:
    return any(
        device.has_held_source_inputs() for device in manager.grabbed_devices.get(hardware_id, [])
    )


def cancel_pending_hardware_release(manager: GrabManager, hardware_id: str) -> None:
    task = manager.grab_state.pending_hardware_release.pop(hardware_id, None)
    if task and not task.done():
        task.cancel()


def cancel_pending_interface_release(
    manager: GrabManager,
    hardware_id: str,
    path: str,
) -> None:
    key = (hardware_id, path)
    task = manager.grab_state.pending_interface_release.pop(key, None)
    if task and not task.done():
        task.cancel()


def cancel_pending_interface_releases_for_hardware(
    manager: GrabManager,
    hardware_id: str,
) -> None:
    for key in list(manager.grab_state.pending_interface_release.keys()):
        if key[0] != hardware_id:
            continue
        task = manager.grab_state.pending_interface_release.pop(key)
        if not task.done():
            task.cancel()


def schedule_interface_release(
    manager: GrabManager,
    hardware_id: str,
    path: str,
    *,
    asyncio_mod: adapters.AsyncioRuntimeAdapter,
    log: logging.Logger,
) -> None:
    cancel_pending_interface_release(manager, hardware_id, path)
    delay = manager.grab_state.release_grace_s
    manager.grab_state.pending_interface_release[(hardware_id, path)] = asyncio_mod.create_task(
        delayed_interface_release(
            manager,
            hardware_id,
            path,
            delay,
            asyncio_mod=asyncio_mod,
        )
    )
    log.info(
        "Scheduled interface release for %s (%s) in %.1fs",
        hardware_id,
        path,
        delay,
    )


async def delayed_interface_release(
    manager: GrabManager,
    hardware_id: str,
    path: str,
    delay: float,
    *,
    asyncio_mod: adapters.AsyncioRuntimeAdapter,
) -> None:
    key = (hardware_id, path)
    try:
        await asyncio_mod.sleep(delay)
        async with manager._op_lock:
            task = manager.grab_state.pending_interface_release.get(key)
            if task is not asyncio_mod.current_task():
                return
            if path in manager.grab_state.desired_paths.get(hardware_id, set()):
                return
            await release_interface_unlocked(manager, hardware_id, path)
    except asyncio.CancelledError:
        pass
    finally:
        task = manager.grab_state.pending_interface_release.get(key)
        if task is asyncio_mod.current_task():
            manager.grab_state.pending_interface_release.pop(key, None)


async def release_interface_unlocked(
    manager: GrabManager,
    hardware_id: str,
    path: str,
) -> None:
    """Release one grabbed interface. Caller must hold ``manager._op_lock``."""

    devices = manager.grabbed_devices.get(hardware_id, [])
    keep: list[ManagedGrabbedDevice] = []
    removed: ManagedGrabbedDevice | None = None
    for device in devices:
        if removed is None and device.path == path:
            removed = device
            continue
        keep.append(device)

    if removed is None:
        return

    await removed.stop_event_loop()
    await lifecycle.clear_combo_runtime_for_binding_scope(
        manager,
        hardware_id,
        str(getattr(removed, "interface_id", "") or "").lower(),
        deps=combo_runtime_deps(),
    )
    removed.release_tracked_outputs()
    await removed.release()

    if keep:
        manager.grabbed_devices[hardware_id] = keep
    else:
        manager.grabbed_devices.pop(hardware_id, None)
        desired_config = manager.grab_state.desired_grabs.get(hardware_id)
        if manager.grab_state.desired_paths.get(hardware_id):
            if desired_grab_requests_gamepad_source_hiding(desired_config):
                await enable_hardware_hotplug_hiding_best_effort(
                    manager,
                    hardware_id,
                )
        else:
            manager.active_mappings.pop(hardware_id, None)
            manager.grab_state.desired_paths.pop(hardware_id, None)
            manager.grab_state.desired_grabs.pop(hardware_id, None)
        outputs.destroy_global_uinputs(manager, log=log)


async def release_interface(
    manager: GrabManager,
    hardware_id: str,
    path: str,
) -> None:
    async with manager._op_lock:
        await release_interface_unlocked(manager, hardware_id, path)


async def release_all_devices(
    manager: GrabManager,
    *,
    fire_and_observe_fn: FireAndObserve,
) -> None:
    async with manager._op_lock:
        await manager.cancel_macro_playback()
        for devices in list(manager.grabbed_devices.values()):
            await stop_device_event_loops(devices)
        await lifecycle.clear_combo_runtime(
            manager,
            deps=combo_runtime_deps(fire_and_observe_fn=fire_and_observe_fn),
        )
        hardware_ids = set(manager.grabbed_devices) | set(manager.grab_state.desired_grabs)
        for hardware_id in list(hardware_ids):
            await release_device_unlocked(manager, hardware_id, log=log)
