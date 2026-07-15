from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, TypedDict

from keymasq.common.ipc import CommandType
from keymasq.common.model.actions import MappingAction
from keymasq.common.types import JsonObject
from keymasq.keymasqd.output_helpers import resolve_output_code
from keymasq.keymasqd.runtime.adapters import AsyncioEvent
from keymasq.keymasqd.runtime.grabbed_device.types import (
    ActionExecutionDeps,
    ActionRuntime,
    CursorPositionSetter,
    EmergencyResetter,
    GrabbedDeviceState,
    InputEventLike,
    MacroPlayer,
    NaturalMouseMover,
)
from keymasq.keymasqd.runtime.repeat import RepeatHistoryEntry, RepeatRuntimeState

type BroadcastCallback = Callable[[CommandType, JsonObject], Awaitable[None]]
type OutputTracker = Callable[[str, int, int], bool]
type ResolveCodeFn = Callable[[str], int | None]
type CancelMacroPlayback = Callable[[], Awaitable[JsonObject]]


class SuperkeyExecutor(Protocol):
    async def __call__(
        self,
        device_runtime: ActionRuntime,
        action: MappingAction,
        event: InputEventLike,
        event_name: str,
        *,
        deps: ActionExecutionDeps,
        shared_output_tracker: OutputTracker | None = None,
        shared_abs_output_tracker: OutputTracker | None = None,
        execution_handle: ActionExecutionHandle | None = None,
        cancel_macro_playback: CancelMacroPlayback | None = None,
        resolve_code_fn: ResolveCodeFn = resolve_output_code,
    ) -> None: ...


class RepeatSuperkeyExecutor(Protocol):
    async def __call__(
        self,
        device_runtime: ActionRuntime,
        repeated_entry: RepeatHistoryEntry,
        event_name: str,
        *,
        deps: ActionExecutionDeps,
        execution_handle: ActionExecutionHandle | None = None,
        cancel_macro_playback: CancelMacroPlayback | None = None,
        resolve_code_fn: ResolveCodeFn = resolve_output_code,
    ) -> None: ...


class MacroPlaybackRequest(TypedDict):
    macro_events: list[JsonObject]
    macro_name: str
    replay_mouse_movement: bool
    replay_mouse_clicks: bool
    speed: float
    loop_mode: str
    loop_count: int
    loop_stop_behavior: str
    move_to_start: bool
    start_x: int
    start_y: int
    block_mouse_movement: bool
    source_device: str
    source_button: str
    trigger_value: int


@dataclass
class ActionOutputTarget:
    output_id: str
    uinput: object | None
    bucket: str


@dataclass
class ActionRuntimeContext:
    """Minimal action runtime useful for isolated action-state tests."""

    path: str
    hardware_id: str
    state: GrabbedDeviceState = field(default_factory=GrabbedDeviceState)
    uinput: object | None = None
    keyboard_uinput: object | None = None
    mouse_uinput: object | None = None
    gamepad_uinput: object | None = None
    broadcast_callback: BroadcastCallback | None = None
    cursor_position_setter: CursorPositionSetter | None = None
    natural_mouse_mover: NaturalMouseMover | None = None
    macro_player: MacroPlayer | None = None
    emergency_resetter: EmergencyResetter | None = None
    repeat_state: RepeatRuntimeState | None = None
    suppress_rel_getter: Callable[[], bool] | None = None
    gamepad_output_resolver: Callable[[str | None, str], object | None] | None = None
    running: bool = True

    def stop(self) -> None:
        self.running = False

    def resolve_gamepad_output(self, output_id: str | None, context: str) -> object | None:
        if self.gamepad_output_resolver is not None:
            return self.gamepad_output_resolver(output_id, context)
        return ActionOutputTarget(
            output_id=output_id or "virtual-gamepad-1",
            uinput=self.gamepad_uinput,
            bucket="gamepad",
        )


@dataclass
class ActionExecutionHandle:
    started: AsyncioEvent | None = None
    tasks: list[asyncio.Task[object]] = field(default_factory=list)


def mark_action_started(handle: ActionExecutionHandle | None) -> None:
    if handle is not None and handle.started is not None:
        handle.started.set()


def register_action_task(
    handle: ActionExecutionHandle | None,
    task: asyncio.Task[object],
) -> None:
    if handle is not None:
        handle.tasks.append(task)


async def drain_action_tasks(handle: ActionExecutionHandle | None) -> None:
    if handle is None or not handle.tasks:
        return
    tasks = list(dict.fromkeys(handle.tasks))
    handle.tasks.clear()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def cancel_action_tasks(handle: ActionExecutionHandle | None) -> None:
    if handle is None or not handle.tasks:
        return
    tasks = list(dict.fromkeys(handle.tasks))
    handle.tasks.clear()
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
