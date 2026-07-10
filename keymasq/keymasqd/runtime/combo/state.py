"""Explicit state and dependency boundaries for the combo runtime."""

import asyncio
import queue
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from keymasq.common.model.actions import MappingAction
from keymasq.keymasqd.combo_engine import RuntimeCombo, RuntimeComboBinding
from keymasq.keymasqd.runtime import adapters
from keymasq.keymasqd.runtime.action.state import (
    ActionExecutionHandle,
    ActionRuntimeContext,
)
from keymasq.keymasqd.runtime.combo.progression import ComboProgressionMachine
from keymasq.keymasqd.superkey_state import SuperkeyMachine

type IntValueFn = Callable[..., int]
type StrValueFn = Callable[..., str]
type ResolveStablePathFn = Callable[[str], str]
type GetInterfaceIdFn = Callable[[str], str | None]
type ResolveCodeFn = Callable[[str], int | None]
type FireAndObserve = Callable[[Awaitable[object], str], asyncio.Task[object]]

type ComboCaptureQueueState = tuple[
    queue.SimpleQueue[dict[str, object]], set[str], asyncio.Event | None
]

type GrabbedComboDevice = Any
type ComboManager = Any


@dataclass
class ComboActionState:
    """One active combo action and the resources released with it."""

    kind: str
    started: adapters.AsyncioEvent | None = None
    action: MappingAction | None = None
    source_device: str | None = None
    source_button: str | None = None
    trigger_binding: RuntimeComboBinding | None = None
    trigger_bindings: list[RuntimeComboBinding] = field(default_factory=list)
    child_combo_ids: list[str] = field(default_factory=list)
    machine: SuperkeyMachine | None = None
    recalled_bindings: list[RuntimeComboBinding] = field(default_factory=list)
    restore_bindings: list[RuntimeComboBinding] = field(default_factory=list)
    action_runtime: ActionRuntimeContext | None = None
    execution_handle: ActionExecutionHandle | None = None


@dataclass
class ComboRuntimeState:
    """All mutable state owned by combo matching and action execution."""

    capture_queues: dict[str, ComboCaptureQueueState] = field(default_factory=dict)
    active_combos: list[RuntimeCombo] = field(default_factory=list)
    progression: ComboProgressionMachine = field(default_factory=ComboProgressionMachine)
    runtime_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    timeout_task: asyncio.Task[None] | None = None
    active_actions: dict[str, ComboActionState] = field(default_factory=dict)
    superkey_machines: dict[str, SuperkeyMachine] = field(default_factory=dict)
    superkey_machine_bindings: dict[str, tuple[RuntimeComboBinding, ...]] = field(
        default_factory=dict
    )
    held_output_keys: dict[str, set[int]] = field(
        default_factory=lambda: {
            "keyboard": set(),
            "mouse": set(),
            "gamepad": set(),
        }
    )
    superkey_output_refcounts: dict[str, dict[int, int]] = field(
        default_factory=lambda: {
            "keyboard": {},
            "mouse": {},
            "gamepad": {},
        }
    )


@dataclass(frozen=True)
class ComboRuntimeDeps:
    """Runtime services supplied by the daemon composition root."""

    asyncio_mod: adapters.AsyncioRuntimeAdapter
    evdev_mod: adapters.ComboEvdevAdapter
    uinput_writer: adapters.UInputWriter
    resolve_code_fn: ResolveCodeFn
    fire_and_observe_fn: FireAndObserve
