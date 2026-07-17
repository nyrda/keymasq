from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

type IntValueFn = Callable[[object, int], int]
type StrValueFn = Callable[[object, str], str]
type NaturalMacroMover = Callable[
    [int, int, float, float, str, int, int],
    Awaitable[dict[str, object]],
]
type MacroEventIteratorFactory = Callable[[], Iterator[dict[str, object]]]
type MacroEventSourceCloser = Callable[[], None]


@dataclass(frozen=True)
class MacroRuntimeDeps:
    """Runtime-specific services used by the portable macro helpers."""

    asyncio_mod: Any
    evdev_mod: Any
    uinput_writer: Any
    log: logging.Logger
    int_value_fn: IntValueFn
    str_value_fn: StrValueFn


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

    def allocate_instance(
        self,
        *,
        loop_mode: str,
        source_key: tuple[str, str],
        macro_name: str,
        loop_stop_behavior: str,
    ) -> int:
        """Allocate and initialize one playback instance before its task starts."""

        self.instance_seq += 1
        instance_id = self.instance_seq
        self.instance_held[instance_id] = set()
        self.instance_held_abs[instance_id] = set()
        self.cancel_instance_ids.discard(instance_id)
        self.instance_meta[instance_id] = {
            "loop_mode": loop_mode,
            "source_device": source_key[0],
            "source_button": source_key[1],
            "macro_name": macro_name,
            "loop_active": loop_mode in {"hold", "toggle"},
            "loop_stop_behavior": loop_stop_behavior,
        }
        return instance_id

    def forget_instance(self, instance_id: int) -> None:
        """Remove scheduler metadata after cleanup has released held outputs."""

        self.cancel_instance_ids.discard(instance_id)
        self.tasks.pop(instance_id, None)
        self.instance_meta.pop(instance_id, None)


@dataclass(frozen=True)
class MacroEventSource:
    """Re-openable macro event stream with scheduling metadata."""

    event_count: int
    duration_us: int
    iter_events: MacroEventIteratorFactory
    close: MacroEventSourceCloser | None = None
