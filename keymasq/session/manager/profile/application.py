"""Daemon-side grab, mapping, and combo application transactions."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol

from keymasq.common.coercion import coerce_int
from keymasq.common.ipc import Command, CommandType, Response
from keymasq.session.profile.types import (
    ResolvedCombo,
    ResolvedDeviceProfile,
)

from ..common import JsonObject, device_name_for_hardware, json_object
from ..constants import GRAB_DEVICE_TIMEOUT_S, GRAB_RETRY_DELAY_S
from ..payload import combo, mapping, references
from .grab_plan import (
    all_configured_interfaces,
    build_grab_device_payload,
    device_inspector_active,
    get_interfaces_to_grab,
    grab_device_payload_signature,
)
from .reconciliation import profile_apply_is_current, raise_if_stale_profile_apply
from .runtime_state import (
    cancel_grab_retry,
    clear_hardware_runtime_state,
)

if TYPE_CHECKING:
    from ..core import SessionManager

log = logging.getLogger("keymasq-session")
type ProfileActivationNotifier = Callable[[], None]
_CANCEL_SETTLE_TIMEOUT_S = 11.0
_CANCEL_CLEANUP_TIMEOUT_S = 1.0


def _observe_cleanup_task(task: asyncio.Future[object]) -> None:
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


async def _send_reference_command(
    manager: "SessionManager",
    command: Command,
) -> tuple[Response, bool]:
    """Settle a sent reference-bearing command before propagating cancellation."""

    task = asyncio.create_task(manager.client.send_command(command))
    try:
        return await asyncio.shield(task), False
    except asyncio.CancelledError as cancelled:
        outcome = await _settle_cancelled_request(manager, task, cancelled)
        if isinstance(outcome, BaseException):
            raise cancelled from outcome
        return outcome, True


async def _settle_cancelled_request(
    manager: "SessionManager",
    task: asyncio.Task[Response],
    cancelled: asyncio.CancelledError,
) -> Response | BaseException:
    settled = asyncio.gather(task, return_exceptions=True)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _CANCEL_SETTLE_TIMEOUT_S
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            return (
                await asyncio.wait_for(
                    asyncio.shield(settled),
                    timeout=remaining,
                )
            )[0]
        except asyncio.CancelledError:
            continue
        except TimeoutError:
            break

    task.cancel()
    await _await_cleanup(asyncio.gather(task, return_exceptions=True))
    await _await_cleanup(manager.client.disconnect())
    raise cancelled from TimeoutError("daemon command cancellation settlement timed out")


async def _await_cleanup(awaitable: Awaitable[object]) -> None:
    task = asyncio.ensure_future(awaitable)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _CANCEL_CLEANUP_TIMEOUT_S
    while not task.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            task.cancel()
            task.add_done_callback(_observe_cleanup_task)
            log.warning("Timed out waiting for cancellation cleanup")
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except asyncio.CancelledError:
            continue
        except TimeoutError:
            task.cancel()
            task.add_done_callback(_observe_cleanup_task)
            log.warning("Timed out waiting for cancellation cleanup")
            return
    await task


async def _disconnect_after_mapping_timeout(
    manager: "SessionManager",
    hardware_id: str,
) -> None:
    try:
        await _await_cleanup(manager.client.disconnect())
    except asyncio.CancelledError:
        log.warning("Daemon disconnect was cancelled after mapping timeout for %s", hardware_id)
        raise
    except Exception:
        log.exception("Failed to disconnect after mapping timeout for %s", hardware_id)


def _commit_device_references(
    manager: "SessionManager",
    hardware_id: str,
    staged_refs: references.ReferenceSnapshot,
    generation: int | None,
) -> None:
    if profile_apply_is_current(manager, generation):
        references.restore_device(manager, hardware_id, staged_refs)
    else:
        references.retain_device(manager, hardware_id, staged_refs)


def _commit_combo_references(
    manager: "SessionManager",
    staged_refs: references.ReferenceSnapshot,
    generation: int | None,
) -> None:
    if profile_apply_is_current(manager, generation):
        references.restore_combos(manager, staged_refs)
    else:
        references.retain_combos(manager, staged_refs)


class GrabRetryScheduler(Protocol):
    def __call__(
        self,
        manager: "SessionManager",
        hardware_id: str,
        delay_s: float,
    ) -> None: ...


class DeviceDeactivator(Protocol):
    async def __call__(
        self,
        manager: "SessionManager",
        hardware_id: str,
        immediate: bool = False,
        *,
        generation: int | None = None,
    ) -> bool: ...


class MappingUpdater(Protocol):
    async def __call__(
        self,
        manager: "SessionManager",
        hardware_id: str,
        resolved: ResolvedDeviceProfile,
        *,
        generation: int | None = None,
    ) -> bool: ...


class ActivationNotification(Protocol):
    def __call__(
        self,
        manager: "SessionManager",
        device_name: str,
        old_profile_names: list[str],
        resolved: ResolvedDeviceProfile,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class DeviceApplicationOperations:
    """Explicit collaborators used by the per-device application state machine."""

    cancel_grab_retry: Callable[["SessionManager", str], None]
    schedule_grab_retry: GrabRetryScheduler
    deactivate_profile: DeviceDeactivator
    update_mapping: MappingUpdater
    notify_activation: ActivationNotification


class _ReconcileOutcome(Enum):
    HANDLED = auto()
    REGRAB = auto()


class _GrabOutcome(Enum):
    PROCEED = auto()
    STOP = auto()


@dataclass(frozen=True, slots=True)
class _PreparedMapping:
    payload: JsonObject
    refs: references.ReferenceSnapshot


def _prepare_mapping(
    manager: "SessionManager",
    hardware_id: str,
    resolved: ResolvedDeviceProfile,
) -> _PreparedMapping | None:
    """Serialize a mapping without replacing the currently acknowledged refs."""

    previous_refs = references.take_device(manager, hardware_id)
    staged_refs: references.ReferenceSnapshot | None = None
    try:
        payload = mapping.serialize(manager, resolved, hardware_id)
        staged_refs = references.take_device(manager, hardware_id)
    except Exception:
        log.exception("Failed to prepare mapping for %s before device grab", hardware_id)
        return None
    finally:
        if staged_refs is None:
            references.discard(manager, references.take_device(manager, hardware_id))
        references.restore_device(manager, hardware_id, previous_refs)

    assert staged_refs is not None
    return _PreparedMapping(payload=payload, refs=staged_refs)


async def _reconcile_existing_grab(
    manager: "SessionManager",
    hardware_id: str,
    resolved: ResolvedDeviceProfile,
    grab_payload: JsonObject,
    grab_signature: str,
    old_profile_names: list[str],
    notify: ProfileActivationNotifier,
    operations: DeviceApplicationOperations,
    *,
    generation: int | None,
) -> _ReconcileOutcome:
    grab_update_needed = (
        manager.profile_state.last_sent_grab_signatures.get(hardware_id, "") != grab_signature
    )
    mapping_update_needed = mapping.update_needed(
        manager,
        hardware_id,
        resolved,
    )
    if not grab_update_needed and not mapping_update_needed:
        log.debug("Skipping unchanged mapping for %s", hardware_id)
        notify()
        return _ReconcileOutcome.HANDLED
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
            await operations.deactivate_profile(
                manager,
                hardware_id,
                generation=generation,
            )
        elif not mapping_update_needed:
            notify()
            return _ReconcileOutcome.HANDLED
    if hardware_id not in manager.profile_state.grabbed_devices:
        log.info(
            "Grab config refresh deactivated %s; reconfiguring in keymasqd",
            hardware_id,
        )
        return _ReconcileOutcome.REGRAB
    if mapping_update_needed:
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
        updated = await operations.update_mapping(
            manager,
            hardware_id,
            resolved,
            generation=generation,
        )
        raise_if_stale_profile_apply(manager, generation)
        if updated:
            notify()
            return _ReconcileOutcome.HANDLED
        log.warning(
            "Mapping update failed for %s with same interfaces; forcing re-grab",
            hardware_id,
        )
        await operations.deactivate_profile(
            manager,
            hardware_id,
            generation=generation,
        )
        manager.profile_state.last_sent_grab_signatures.pop(hardware_id, None)
        return _ReconcileOutcome.REGRAB
    notify()
    return _ReconcileOutcome.HANDLED


async def _send_grab_device_command(
    manager: "SessionManager",
    hardware_id: str,
    resolved: ResolvedDeviceProfile,
    grab_payload: JsonObject,
    grab_signature: str,
    new_interfaces: dict[str, str],
    operations: DeviceApplicationOperations,
    *,
    generation: int | None,
) -> _GrabOutcome:
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
            result_data = json_object(result.data)
            grabbed_count = (
                coerce_int(result_data.get("grabbed_count"), 0) if result_data is not None else 0
            )
            operations.cancel_grab_retry(manager, hardware_id)
            manager.profile_state.grab_waiting_devices.discard(hardware_id)
            log.info("keymasqd: Grabbed device %s: %s", hardware_id, result.data)
            if grabbed_count > 0:
                manager.profile_state.grabbed_devices.add(hardware_id)
                manager.profile_state.grabbed_interfaces[hardware_id] = new_interfaces
                manager.profile_state.last_sent_grab_signatures[hardware_id] = grab_signature
                manager.profile_state.grab_status.pop(hardware_id, None)
                return _GrabOutcome.PROCEED
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
            return _GrabOutcome.STOP

        log.error("keymasqd: Failed to grab device %s: %s", hardware_id, result.error)
        if "timed out waiting" in str(result.error or "").lower():
            manager.profile_state.grab_status[hardware_id] = {
                "state": "timed_out",
                "path": next(iter(new_interfaces.values()), ""),
            }
            operations.schedule_grab_retry(
                manager,
                hardware_id,
                delay_s=GRAB_RETRY_DELAY_S,
            )
        return _GrabOutcome.STOP
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
                f"{device_name_for_hardware(manager, hardware_id)}: grab timed out "
                "while waiting for keys to be released. Retrying automatically."
            ),
        )
        manager.profile_state.grab_status[hardware_id] = {
            "state": "timed_out",
            "path": next(iter(new_interfaces.values()), ""),
        }
        operations.schedule_grab_retry(
            manager,
            hardware_id,
            delay_s=GRAB_RETRY_DELAY_S,
        )
        return _GrabOutcome.STOP
    except OSError as exc:
        log.error(
            "keymasqd: Exception grabbing device %s: %s: %s",
            hardware_id,
            type(exc).__name__,
            exc,
        )
        return _GrabOutcome.STOP
    except Exception:
        log.exception("Unexpected failure grabbing device %s", hardware_id)
        return _GrabOutcome.STOP


async def _send_set_mapping_command(
    manager: "SessionManager",
    hardware_id: str,
    resolved: ResolvedDeviceProfile,
    notify: ProfileActivationNotifier,
    *,
    generation: int | None,
    prepared: _PreparedMapping | None = None,
) -> None:
    log.info(
        "Setting mapping for %s with %d buttons from profiles=%s",
        hardware_id,
        len(resolved.mappings),
        resolved.active_profile_names,
    )
    prepared = prepared or _prepare_mapping(manager, hardware_id, resolved)
    if prepared is None:
        return

    staged_refs = prepared.refs
    keep_staged_refs = False
    try:
        references.expose(manager, staged_refs)
        log.debug("Mapping data: %s", mapping.log_view(prepared.payload))
        raise_if_stale_profile_apply(manager, generation)

        result, cancelled = await _send_reference_command(
            manager,
            Command(
                command=CommandType.SET_MAPPING,
                data={
                    "hardware_id": hardware_id,
                    "mapping": prepared.payload,
                },
            )
        )
        if result.status == "ok":
            _commit_device_references(manager, hardware_id, staged_refs, generation)
            keep_staged_refs = True
            if cancelled:
                raise asyncio.CancelledError
            raise_if_stale_profile_apply(manager, generation)
            manager.profile_state.last_sent_mapping_signatures[hardware_id] = (
                mapping.signature(
                    manager,
                    resolved,
                    hardware_id,
                )
            )
            log.info(
                "Activated resolved profiles %s for %s",
                resolved.active_profile_names,
                hardware_id,
            )
            notify()
        else:
            if cancelled:
                raise asyncio.CancelledError
            raise_if_stale_profile_apply(manager, generation)
            log.error("Failed to set mapping: %s", result.error)

    except TimeoutError as exc:
        references.retain_device(manager, hardware_id, staged_refs)
        keep_staged_refs = True
        await _disconnect_after_mapping_timeout(manager, hardware_id)
        log.error("Timed out setting mapping for %s: %s", hardware_id, exc)
    except OSError as exc:
        log.error("Exception setting mapping: %s: %s", type(exc).__name__, exc)
    except Exception:
        log.exception("Unexpected failure setting mapping for %s", hardware_id)
    finally:
        if not keep_staged_refs:
            references.discard(manager, staged_refs)


async def apply_resolved_device_profile(
    manager: "SessionManager",
    hardware_id: str,
    resolved: ResolvedDeviceProfile,
    operations: DeviceApplicationOperations,
    *,
    generation: int | None = None,
) -> None:
    """Drive one resolved device through release, grab, and mapping states."""
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
    inspector_active = device_inspector_active(manager, hardware_id)

    def _notify() -> None:
        operations.notify_activation(
            manager,
            hardware_config.name,
            old_profile_names,
            resolved,
        )

    if not resolved.has_effective_mapping and not inspector_active:
        operations.cancel_grab_retry(manager, hardware_id)
        manager.profile_state.grab_waiting_devices.discard(hardware_id)
        manager.profile_state.grab_status.pop(hardware_id, None)
        manager.profile_state.last_sent_grab_signatures.pop(hardware_id, None)
        if hardware_id in manager.profile_state.grabbed_devices:
            await operations.deactivate_profile(
                manager,
                hardware_id,
                immediate=True,
                generation=generation,
            )
        return

    new_interfaces = (
        all_configured_interfaces(hardware_config)
        if inspector_active
        else get_interfaces_to_grab(hardware_config, resolved, manager=manager)
    )
    current_interfaces = manager.profile_state.grabbed_interfaces.get(
        hardware_id,
        {},
    )
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
        _notify()
        return

    if hardware_id in manager.profile_state.grabbed_devices:
        if set(current_interfaces.keys()) == set(new_interfaces.keys()):
            outcome = await _reconcile_existing_grab(
                manager,
                hardware_id,
                resolved,
                grab_payload,
                grab_signature,
                old_profile_names,
                _notify,
                operations,
                generation=generation,
            )
            if outcome is _ReconcileOutcome.HANDLED:
                return

        log.info(
            "Interfaces changed for %s, reconfiguring in keymasqd (old: %s -> new: %s)",
            hardware_id,
            list(current_interfaces.keys()),
            list(new_interfaces.keys()),
        )

    prepared_mapping = _prepare_mapping(manager, hardware_id, resolved)
    if prepared_mapping is None:
        return
    log.info(
        "Grabbing device %s (interfaces: %s)",
        hardware_id,
        list(new_interfaces.keys()),
    )
    grab_outcome = await _send_grab_device_command(
        manager,
        hardware_id,
        resolved,
        grab_payload,
        grab_signature,
        new_interfaces,
        operations,
        generation=generation,
    )
    if grab_outcome is _GrabOutcome.STOP:
        return
    await _send_set_mapping_command(
        manager,
        hardware_id,
        resolved,
        _notify,
        generation=generation,
        prepared=prepared_mapping,
    )


async def update_grab_device_payload(
    manager: "SessionManager",
    hardware_id: str,
    payload: JsonObject,
    signature: str,
    *,
    generation: int | None = None,
) -> bool:
    """Refresh metadata for an unchanged set of grabbed interfaces."""
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
            log.error(
                "Failed to update grab config for %s: %s",
                hardware_id,
                result.error,
            )
            return False
        result_data = json_object(result.data)
        grabbed_count = (
            coerce_int(result_data.get("grabbed_count"), 0) if result_data is not None else 0
        )
        if grabbed_count <= 0:
            log.error(
                "Grab config update for %s returned zero grabbed interfaces",
                hardware_id,
            )
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
    """Serialize and apply combos only when their resolved signature changes."""
    raise_if_stale_profile_apply(manager, generation)
    signature = combo.signature(manager, combos)
    if signature == manager.profile_state.last_sent_combo_signature:
        log.debug("Skipping unchanged combo payload")
        return
    previous_refs = references.take_combos(manager)
    staged_refs: references.ReferenceSnapshot | None = None
    keep_staged_refs = False
    try:
        try:
            payload: list[JsonObject] = []
            active_combos: list[ResolvedCombo] = []
            for resolved_combo in combos:
                combo_payload = combo.serialize(manager, resolved_combo)
                if combo_payload is None:
                    continue
                payload.append(combo_payload)
                active_combos.append(resolved_combo)
            staged_refs = references.take_combos(manager)
        finally:
            if staged_refs is None:
                references.take_combos(manager)
            references.restore_combos(manager, previous_refs)
        assert staged_refs is not None
        references.expose(manager, staged_refs)
        raise_if_stale_profile_apply(manager, generation)
        result, cancelled = await _send_reference_command(
            manager,
            Command(
                command=CommandType.SET_COMBOS,
                data={"combos": payload},
            )
        )
        if result.status != "ok":
            if cancelled:
                raise asyncio.CancelledError
            raise_if_stale_profile_apply(manager, generation)
            log.error("Failed to update combos: %s", result.error)
            return
        _commit_combo_references(manager, staged_refs, generation)
        keep_staged_refs = True
        if cancelled:
            raise asyncio.CancelledError
        raise_if_stale_profile_apply(manager, generation)
        manager.profile_state.last_sent_combo_signature = signature
        manager.profile_state.resolved_combos = list(active_combos)
    except OSError as exc:
        log.error("Exception updating combos: %s: %s", type(exc).__name__, exc)
    except Exception:
        log.exception("Unexpected failure updating combos")
    finally:
        if staged_refs is not None and not keep_staged_refs:
            references.discard(manager, staged_refs)


async def update_mapping(
    manager: "SessionManager",
    hardware_id: str,
    resolved: ResolvedDeviceProfile,
    *,
    generation: int | None = None,
) -> bool:
    """Apply an updated mapping to a device whose grab remains active."""
    raise_if_stale_profile_apply(manager, generation)
    if hardware_id not in manager.profile_state.grabbed_devices:
        return False

    signature = mapping.signature(
        manager,
        resolved,
        hardware_id,
    )
    log.info(
        "Updating mapping for %s with %d buttons",
        hardware_id,
        len(resolved.mappings),
    )
    previous_refs = references.take_device(manager, hardware_id)
    staged_refs: references.ReferenceSnapshot | None = None
    keep_staged_refs = False
    try:
        try:
            serialized_mapping = mapping.serialize(manager, resolved, hardware_id)
            staged_refs = references.take_device(manager, hardware_id)
        finally:
            if staged_refs is None:
                references.take_device(manager, hardware_id)
            references.restore_device(manager, hardware_id, previous_refs)
        assert staged_refs is not None
        references.expose(manager, staged_refs)
        raise_if_stale_profile_apply(manager, generation)
        result, cancelled = await _send_reference_command(
            manager,
            Command(
                command=CommandType.SET_MAPPING,
                data={
                    "hardware_id": hardware_id,
                    "mapping": serialized_mapping,
                },
            )
        )
        if result.status == "ok":
            _commit_device_references(manager, hardware_id, staged_refs, generation)
            keep_staged_refs = True
            if cancelled:
                raise asyncio.CancelledError
            raise_if_stale_profile_apply(manager, generation)
            log.info("Updated mapping for %s", hardware_id)
            manager.profile_state.last_sent_mapping_signatures[hardware_id] = signature
            return True
        if cancelled:
            raise asyncio.CancelledError
        raise_if_stale_profile_apply(manager, generation)
        log.error("Failed to update mapping: %s", result.error)
        return False
    except TimeoutError as exc:
        if staged_refs is not None:
            references.retain_device(manager, hardware_id, staged_refs)
            keep_staged_refs = True
        await _disconnect_after_mapping_timeout(manager, hardware_id)
        log.error("Timed out updating mapping for %s: %s", hardware_id, exc)
        return False
    except OSError as exc:
        log.error("Exception updating mapping: %s: %s", type(exc).__name__, exc)
        return False
    except Exception:
        log.exception("Unexpected failure updating mapping for %s", hardware_id)
        return False
    finally:
        if staged_refs is not None and not keep_staged_refs:
            references.discard(manager, staged_refs)


async def deactivate_profile(
    manager: "SessionManager",
    hardware_id: str,
    immediate: bool = False,
    *,
    generation: int | None = None,
) -> bool:
    """Release one device transactionally, retaining state after a failed release."""
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

    references.clear_device(manager, hardware_id)
    log.info("Deactivated grabbed mapping for %s", hardware_id)
    return True
