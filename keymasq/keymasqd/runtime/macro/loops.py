from __future__ import annotations

from dataclasses import dataclass

from keymasq.common.model.actions import (
    DEFAULT_MACRO_LOOP_STOP_BEHAVIOR,
    normalize_macro_loop_stop_behavior,
)
from keymasq.keymasqd.runtime.macro.state import MacroRuntimeState

LOOP_MODES = frozenset({"none", "count", "hold", "toggle"})


def normalize_loop_mode(value: object) -> str:
    mode = str(value or "none").lower()
    return mode if mode in LOOP_MODES else "none"


@dataclass
class MacroLoopStateMachine:
    """Iteration policy for one playback task, independent of daemon state."""

    mode: str
    count: int = 1
    iterations: int = 0
    active: bool = True

    def __post_init__(self) -> None:
        self.mode = normalize_loop_mode(self.mode)
        self.count = max(1, int(self.count))

    def begin_iteration(self) -> None:
        self.iterations += 1

    def request_stop(self) -> None:
        self.active = False

    def should_continue(self) -> bool:
        if self.mode == "count":
            return self.iterations < self.count
        if self.mode in {"hold", "toggle"}:
            return self.active
        return False


@dataclass(frozen=True)
class MacroLoopStopPlan:
    cancel_instance_ids: tuple[int, ...]
    finish_instance_ids: tuple[int, ...]


def running_macro_instance_ids(state: MacroRuntimeState) -> list[int]:
    return [instance_id for instance_id, task in state.tasks.items() if not task.done()]


def find_matching_macro_instances(
    state: MacroRuntimeState,
    *,
    loop_mode: str | None,
    source_key: tuple[str, str] | None,
) -> list[int]:
    ids: list[int] = []
    for instance_id, task in state.tasks.items():
        if task.done():
            continue
        meta = state.instance_meta.get(instance_id, {})
        if loop_mode is not None and meta.get("loop_mode") != loop_mode:
            continue
        if source_key is not None and (
            meta.get("source_device") != source_key[0] or meta.get("source_button") != source_key[1]
        ):
            continue
        ids.append(instance_id)
    return ids


def plan_loop_stop(
    state: MacroRuntimeState,
    instance_ids: list[int],
) -> MacroLoopStopPlan:
    cancel_ids: list[int] = []
    finish_ids: list[int] = []
    for instance_id in instance_ids:
        meta = state.instance_meta.get(instance_id, {})
        behavior = normalize_macro_loop_stop_behavior(
            meta.get("loop_stop_behavior", DEFAULT_MACRO_LOOP_STOP_BEHAVIOR)
        )
        if behavior == "cancel_run":
            cancel_ids.append(instance_id)
        else:
            finish_ids.append(instance_id)
    return MacroLoopStopPlan(tuple(cancel_ids), tuple(finish_ids))


def mark_loop_instances_stopping(
    state: MacroRuntimeState,
    instance_ids: list[int] | tuple[int, ...],
) -> None:
    for instance_id in instance_ids:
        meta = state.instance_meta.get(instance_id)
        if meta is not None:
            meta["loop_active"] = False


def is_loop_instance_active(state: MacroRuntimeState, instance_id: int) -> bool:
    meta = state.instance_meta.get(instance_id, {})
    return bool(meta.get("loop_active", True))
