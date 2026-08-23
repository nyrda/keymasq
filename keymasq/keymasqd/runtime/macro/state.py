from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from keymasq.keymasqd.runtime.macro.cache import MacroCacheCandidate, MacroReplayCache

type IntValueFn = Callable[[object, int], int]
type StrValueFn = Callable[[object, str], str]
type DiagnosticRecorder = Callable[[str, float], None]
type NaturalMacroMover = Callable[
    [int, int, float, float, str, int, int],
    Awaitable[dict[str, object]],
]
type MacroEventIteratorFactory = Callable[[], Iterator[dict[str, object]]]
type MacroCacheCandidateFactory = Callable[[], MacroCacheCandidate | None]
type MacroRevisionVerifier = Callable[[], None]


@dataclass(frozen=True)
class MacroRuntimeDeps:
    """Runtime-specific services used by the portable macro helpers."""

    asyncio_mod: Any
    evdev_mod: Any
    uinput_writer: Any
    log: logging.Logger
    int_value_fn: IntValueFn
    str_value_fn: StrValueFn
    diagnostics_recorder: DiagnosticRecorder | None = None


@dataclass
class MacroRuntimeState:
    """All mutable state owned by the macro playback subsystem."""

    tasks: dict[int, asyncio.Task[None]] = field(default_factory=dict)
    instance_meta: dict[int, dict[str, object]] = field(default_factory=dict)
    instance_seq: int = 0
    instance_held: dict[int, set[tuple[str, int]]] = field(default_factory=dict)
    held_refcount: dict[tuple[str, int], int] = field(default_factory=dict)
    instance_held_abs: dict[int, set[tuple[str, int]]] = field(default_factory=dict)
    held_abs_refcount: dict[tuple[str, int], int] = field(default_factory=dict)
    cancel_instance_ids: set[int] = field(default_factory=set)
    mouse_inhibit_count: int = 0
    exec_waiters: dict[str, asyncio.Future[int]] = field(default_factory=dict)
    mouse_rel_suppressed: bool = False
    mouse_rel_suppression_watchdog_task: asyncio.Task[None] | None = None
    replay_cache: MacroReplayCache = field(default_factory=MacroReplayCache)
    instance_children: dict[int, set[int]] = field(default_factory=dict)

    def allocate_instance(
        self,
        *,
        loop_mode: str,
        source_key: tuple[str, str],
        macro_name: str,
        loop_stop_behavior: str,
        parent_instance_id: int | None = None,
    ) -> int:
        """Allocate and initialize one playback instance before its task starts."""

        self.instance_seq += 1
        instance_id = self.instance_seq
        self.instance_held[instance_id] = set()
        self.instance_held_abs[instance_id] = set()
        self.instance_children[instance_id] = set()
        self.cancel_instance_ids.discard(instance_id)
        parent_meta = self.instance_meta.get(parent_instance_id or -1, {})
        raw_root_instance_id = parent_meta.get("root_instance_id")
        root_instance_id = (
            raw_root_instance_id if isinstance(raw_root_instance_id, int) else instance_id
        )
        self.instance_meta[instance_id] = {
            "loop_mode": loop_mode,
            "source_device": source_key[0],
            "source_button": source_key[1],
            "macro_name": macro_name,
            "loop_active": loop_mode in {"hold", "toggle"},
            "loop_stop_behavior": loop_stop_behavior,
            "parent_instance_id": parent_instance_id,
            "root_instance_id": root_instance_id,
        }
        if parent_instance_id is None:
            self.instance_meta[instance_id]["source_lifecycle_available"] = bool(
                source_key[0] or source_key[1]
            )
            self.instance_meta[instance_id]["source_lifecycle_active"] = True
        else:
            self.instance_children.setdefault(parent_instance_id, set()).add(instance_id)
        return instance_id

    def mark_source_released(self, source_key: tuple[str, str]) -> None:
        """End the trigger lifecycle for every active root from one source."""

        for meta in self.instance_meta.values():
            if meta.get("parent_instance_id") is not None:
                continue
            if (
                meta.get("source_device") == source_key[0]
                and meta.get("source_button") == source_key[1]
            ):
                meta["source_lifecycle_active"] = False

    def source_lifecycle(self, instance_id: int) -> tuple[bool, bool]:
        """Return whether a root has a trigger source and whether it remains held."""

        meta = self.instance_meta.get(instance_id, {})
        raw_root_id = meta.get("root_instance_id")
        root_id = raw_root_id if isinstance(raw_root_id, int) else instance_id
        root_meta = self.instance_meta.get(root_id, {})
        return (
            bool(root_meta.get("source_lifecycle_available", False)),
            bool(root_meta.get("source_lifecycle_active", False)),
        )

    def descendant_instance_ids(self, instance_ids: list[int]) -> list[int]:
        """Expand instance IDs to their complete, currently-known subtrees."""

        expanded: list[int] = []
        pending = list(dict.fromkeys(int(value) for value in instance_ids))
        seen: set[int] = set()
        while pending:
            instance_id = pending.pop()
            if instance_id in seen:
                continue
            seen.add(instance_id)
            expanded.append(instance_id)
            pending.extend(self.instance_children.get(instance_id, ()))
        return expanded

    def call_chain(self, instance_id: int, child_name: str | None = None) -> str:
        """Format the active parent chain for diagnostics."""

        names: list[str] = []
        current: int | None = instance_id
        while current is not None:
            meta = self.instance_meta.get(current, {})
            names.append(str(meta.get("macro_name") or "<unnamed>"))
            raw_parent = meta.get("parent_instance_id")
            current = int(raw_parent) if isinstance(raw_parent, int) else None
        names.reverse()
        if child_name:
            names.append(child_name)
        return " -> ".join(names)

    def call_chain_contains(self, instance_id: int, macro_name: str) -> bool:
        """Return whether a macro name already appears among active ancestors."""

        current: int | None = instance_id
        while current is not None:
            meta = self.instance_meta.get(current, {})
            if str(meta.get("macro_name") or "") == macro_name:
                return True
            raw_parent = meta.get("parent_instance_id")
            current = raw_parent if isinstance(raw_parent, int) else None
        return False

    def forget_instance(self, instance_id: int) -> None:
        """Remove scheduler metadata after cleanup has released held outputs."""

        self.cancel_instance_ids.discard(instance_id)
        self.tasks.pop(instance_id, None)
        meta = self.instance_meta.pop(instance_id, None) or {}
        raw_parent = meta.get("parent_instance_id")
        if isinstance(raw_parent, int):
            self.instance_children.get(raw_parent, set()).discard(instance_id)
        self.instance_children.pop(instance_id, None)


@dataclass(frozen=True)
class MacroEventSource:
    """Re-openable macro event stream with scheduling metadata."""

    event_count: int
    duration_us: int
    iter_events: MacroEventIteratorFactory
    diagnostic_initial_load_us: float | None = None
    cached_events: tuple[dict[str, object], ...] | None = None
    verify_revision: MacroRevisionVerifier | None = None
    begin_cache_candidate: MacroCacheCandidateFactory | None = None
