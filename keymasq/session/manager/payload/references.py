"""Lifecycle for opaque command references sent to the daemon."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from keymasq.session.manager.state import ExecBinding

if TYPE_CHECKING:
    from keymasq.session.manager.core import SessionManager


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
        manager.exec_state.device_exec_refs.setdefault(hardware_id, set()).update(
            snapshot.bindings
        )
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
