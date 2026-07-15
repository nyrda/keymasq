"""Lifecycle for opaque command references sent to the daemon."""

from typing import TYPE_CHECKING, Literal

from keymasq.session.manager.state import ExecBinding

if TYPE_CHECKING:
    from keymasq.session.manager.core import SessionManager


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
