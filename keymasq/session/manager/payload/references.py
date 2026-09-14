"""Lifecycle for opaque command references sent to the daemon."""

import asyncio
import logging
from dataclasses import dataclass
from itertools import islice
from typing import TYPE_CHECKING, Literal, cast

from keymasq.common.ipc import Command, CommandType
from keymasq.session.manager.state import ExecBinding

if TYPE_CHECKING:
    from keymasq.session.manager.core import SessionManager

log = logging.getLogger(__name__)


def resolve(manager: "SessionManager", reference: int) -> ExecBinding | None:
    return manager.exec_state.exec_refs.get(reference) or manager.exec_state.retired_exec_refs.get(
        reference
    )


def _retire(manager: "SessionManager", snapshot: "ReferenceSnapshot") -> None:
    manager.exec_state.retired_exec_refs.update(snapshot.bindings)
    task = manager.exec_state.retirement_task
    if manager.connected and snapshot.bindings and (task is None or task.done()):
        from keymasq.session.manager.events import create_event_task

        manager.exec_state.retirement_task = create_event_task(
            manager, _collect_retired_loop(manager), name="exec_reference_retirement"
        )


def retire_device(manager: "SessionManager", hardware_id: str) -> None:
    _retire(manager, take_device(manager, hardware_id))


def retire_combos(manager: "SessionManager") -> None:
    _retire(manager, take_combos(manager))


def clear_retired(manager: "SessionManager") -> None:
    task = manager.exec_state.retirement_task
    manager.exec_state.retirement_task = None
    if task is not None:
        task.cancel()
    manager.exec_state.retired_exec_refs.clear()


async def collect_retired(manager: "SessionManager") -> None:
    # Query only a bounded snapshot. References retired during the request wait
    # for the next acknowledgement, and live references have no expiry time.
    snapshot = dict(islice(manager.exec_state.retired_exec_refs.items(), 1024))
    if not snapshot:
        return
    result = await manager.client.send_command(
        Command(command=CommandType.UNUSED_EXEC_REFS, data={"exec_refs": list(snapshot)})
    )
    if result.status != "ok" or not isinstance(result.data, dict):
        return
    unused = cast(dict[str, object], result.data).get("unused_exec_refs")
    if not isinstance(unused, list) or any(
        type(ref) is not int for ref in cast(list[object], unused)
    ):
        return
    for ref in cast(list[int], unused):
        if ref in snapshot and manager.exec_state.retired_exec_refs.get(ref) is snapshot[ref]:
            manager.exec_state.retired_exec_refs.pop(ref, None)
    # Rotate live entries so a long-held batch cannot starve newer retirements.
    for ref, binding in snapshot.items():
        if manager.exec_state.retired_exec_refs.get(ref) is binding:
            manager.exec_state.retired_exec_refs.pop(ref)
            manager.exec_state.retired_exec_refs[ref] = binding


async def _collect_retired_loop(manager: "SessionManager") -> None:
    while manager.connected and manager.exec_state.retired_exec_refs:
        await asyncio.sleep(1)
        try:
            await collect_retired(manager)
        except (OSError, TimeoutError):
            log.debug("Could not collect retired command references", exc_info=True)


@dataclass(frozen=True)
class ReferenceSnapshot:
    bindings: dict[int, ExecBinding]
    registered: bool = True


def clear_device(manager: "SessionManager", hardware_id: str) -> None:
    refs = manager.exec_state.device_exec_refs.pop(hardware_id, set())
    for ref in refs:
        manager.exec_state.exec_refs.pop(ref, None)


def clear_combos(manager: "SessionManager") -> None:
    refs = list(manager.exec_state.combo_exec_refs)
    manager.exec_state.combo_exec_refs.clear()
    for ref in refs:
        manager.exec_state.exec_refs.pop(ref, None)


def clear_all(manager: "SessionManager") -> None:
    clear_retired(manager)
    for hardware_id in list(manager.exec_state.device_exec_refs):
        clear_device(manager, hardware_id)
    clear_combos(manager)


def take_device(manager: "SessionManager", hardware_id: str) -> ReferenceSnapshot:
    """Detach one device's refs so a replacement can be staged off to the side."""

    registered = hardware_id in manager.exec_state.device_exec_refs
    refs = manager.exec_state.device_exec_refs.pop(hardware_id, set())
    bindings = {
        ref: binding
        for ref in refs
        if (binding := manager.exec_state.exec_refs.pop(ref, None)) is not None
    }
    return ReferenceSnapshot(bindings=bindings, registered=registered)


def restore_device(
    manager: "SessionManager",
    hardware_id: str,
    snapshot: ReferenceSnapshot,
) -> None:
    clear_device(manager, hardware_id)
    if snapshot.registered:
        manager.exec_state.device_exec_refs[hardware_id] = set(snapshot.bindings)
    manager.exec_state.exec_refs.update(snapshot.bindings)


def retain_device(
    manager: "SessionManager",
    hardware_id: str,
    snapshot: ReferenceSnapshot,
) -> None:
    """Keep refs from an accepted stale command without replacing newer refs."""

    if snapshot.registered:
        manager.exec_state.device_exec_refs.setdefault(hardware_id, set()).update(snapshot.bindings)
    manager.exec_state.exec_refs.update(snapshot.bindings)


def expose(manager: "SessionManager", snapshot: ReferenceSnapshot) -> None:
    """Make staged refs resolvable without changing acknowledged ownership."""

    manager.exec_state.exec_refs.update(snapshot.bindings)


def discard(manager: "SessionManager", snapshot: ReferenceSnapshot) -> None:
    """Remove refs from a rejected or failed staged command."""

    for ref in snapshot.bindings:
        manager.exec_state.exec_refs.pop(ref, None)


def take_combos(manager: "SessionManager") -> ReferenceSnapshot:
    """Detach combo refs so a replacement can be staged until daemon acknowledgement."""

    refs = set(manager.exec_state.combo_exec_refs)
    manager.exec_state.combo_exec_refs.clear()
    bindings = {
        ref: binding
        for ref in refs
        if (binding := manager.exec_state.exec_refs.pop(ref, None)) is not None
    }
    return ReferenceSnapshot(bindings=bindings)


def restore_combos(manager: "SessionManager", snapshot: ReferenceSnapshot) -> None:
    clear_combos(manager)
    manager.exec_state.combo_exec_refs.update(snapshot.bindings)
    manager.exec_state.exec_refs.update(snapshot.bindings)


def retain_combos(manager: "SessionManager", snapshot: ReferenceSnapshot) -> None:
    """Keep refs from an accepted stale command without replacing newer refs."""

    manager.exec_state.combo_exec_refs.update(snapshot.bindings)
    manager.exec_state.exec_refs.update(snapshot.bindings)


def allocate(
    manager: "SessionManager",
    cmd: str,
    *,
    owner: Literal["device", "combo"],
    hardware_id: str | None = None,
) -> int:
    """Allocate a daemon-visible reference while retaining the command locally."""
    exec_ref = manager.exec_state.next_exec_ref
    manager.exec_state.next_exec_ref += 1
    if owner == "device":
        if not hardware_id:
            raise ValueError("device exec refs require a hardware_id")
        manager.exec_state.device_exec_refs.setdefault(hardware_id, set()).add(exec_ref)
        manager.exec_state.exec_refs[exec_ref] = ExecBinding(
            cmd=cmd,
            owner="device",
            hardware_id=hardware_id,
        )
    elif owner == "combo":
        manager.exec_state.combo_exec_refs.add(exec_ref)
        manager.exec_state.exec_refs[exec_ref] = ExecBinding(cmd=cmd, owner="combo")
    else:
        raise ValueError(f"unknown exec ref owner: {owner}")
    return exec_ref
