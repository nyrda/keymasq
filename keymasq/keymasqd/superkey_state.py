import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

import evdev

from keymasq.common.gamepad_axes import clamp_gamepad_axis_value, normalize_gamepad_axis_target
from keymasq.common.ipc import CommandType
from keymasq.common.models import (
    DEFAULT_NATURAL_MOUSE_MOVE_CURVE,
    DEFAULT_NATURAL_MOUSE_MOVE_SPEED,
    ActionType,
    MappingAction,
    ProfileDeactivationPolicy,
    SuperkeyMode,
    clamp_rapidfire_hold_ms,
    clamp_rapidfire_wait_ms,
    normalize_mpris_command,
    normalize_profile_deactivation_policy,
    superkey_action_shared_kwargs,
)
from keymasq.common.types import SyntheticInputEvent as _SyntheticInputEvent
from keymasq.keymasqd.runtime.adapters import WritableUInput, identity_uinput_writer
from keymasq.keymasqd.runtime.mouse_actions import (
    emit_relative_pulse,
    rapidfire_relative_pulses,
    resolve_mouse_output_target,
)
from keymasq.keymasqd.runtime.repeat import (
    SUPERKEY_SLOT_DOUBLE_TAP,
    SUPERKEY_SLOT_HOLD,
    SUPERKEY_SLOT_TAP,
    SUPERKEY_SLOT_TAP_HOLD,
)
from keymasq.keymasqd.task_helpers import fire_and_observe as _fire_and_observe

if TYPE_CHECKING:
    from keymasq.keymasqd.runtime.grabbed_device.types import ActionExecutionDeps

log = logging.getLogger("keymasqd.superkey")


class SuperkeyState(Enum):
    IDLE = "idle"
    DOWN_WAIT = "down_wait"
    UP_WAIT = "up_wait"
    HOLDING = "holding"
    DOWN_WAIT_2 = "down_wait_2"
    TAP_HOLDING = "tap_holding"


@dataclass
class SuperkeyActionData:
    action_type: str
    target: str | None = None
    output_id: str | None = None
    cmd: str | None = None
    exec_ref: int | None = None
    macro_name: str | None = None
    macro_replay_mouse_movement: bool = True
    macro_replay_mouse_clicks: bool = True
    macro_speed: float = 1.0
    macro_loop_mode: str = "none"
    macro_loop_count: int = 1
    macro_loop_stop_behavior: str = "finish_run"
    macro_move_to_start: bool = False
    macro_start_x: int = 0
    macro_start_y: int = 0
    macro_block_mouse_movement: bool = False
    macro_recording_slot: int = 0
    profile_name: str | None = None
    compositor_id: str | None = None
    compositor_dispatcher: str | None = None
    compositor_args: str | None = None
    mpris_command: str | None = None
    move_x: int = 0
    move_y: int = 0
    axis_value: int = 0
    move_speed: float = DEFAULT_NATURAL_MOUSE_MOVE_SPEED
    move_jitter: float = 0.3
    move_curve: str = DEFAULT_NATURAL_MOUSE_MOVE_CURVE
    move_tolerance: int = 2
    move_max_duration_ms: int = 3000
    move_stop_on_failure: bool = False
    rapidfire_enabled: bool = False
    rapidfire_hold_ms: int = 20
    rapidfire_wait_ms: int = 20
    profile_deactivation: ProfileDeactivationPolicy | None = None

    def __post_init__(self) -> None:
        self.output_id = (
            str(self.output_id).strip()
            if self.action_type in (ActionType.GAMEPAD.value, ActionType.GAMEPAD_AXIS.value)
            and self.output_id is not None
            else None
        ) or None
        if self.action_type == ActionType.GAMEPAD_AXIS.value:
            self.target = normalize_gamepad_axis_target(self.target)
            self.axis_value = clamp_gamepad_axis_value(self.target, self.axis_value)
        self.rapidfire_hold_ms = clamp_rapidfire_hold_ms(self.rapidfire_hold_ms)
        self.rapidfire_wait_ms = clamp_rapidfire_wait_ms(self.rapidfire_wait_ms)
        self.profile_deactivation = normalize_profile_deactivation_policy(
            ActionType(self.action_type),
            self.profile_deactivation,
        )
        if self.action_type == ActionType.MPRIS.value:
            self.mpris_command = normalize_mpris_command(self.mpris_command)
        else:
            self.mpris_command = None
        if self.action_type not in {
            ActionType.START_MACRO_RECORDING.value,
            ActionType.STOP_MACRO_RECORDING.value,
            ActionType.PLAY_MACRO_SLOT.value,
        }:
            self.macro_recording_slot = 0


@dataclass
class SuperkeyConfig:
    name: str
    mode: SuperkeyMode = SuperkeyMode.PATTERN
    tap_timeout_ms: int = 200
    double_tap_window_ms: int = 300
    hold_threshold_ms: int = 300
    tap_actions: list[SuperkeyActionData] = field(default_factory=list)
    double_tap_actions: list[SuperkeyActionData] = field(default_factory=list)
    hold_actions: list[SuperkeyActionData] = field(default_factory=list)
    tap_hold_actions: list[SuperkeyActionData] = field(default_factory=list)
    overload_actions: list[MappingAction] = field(default_factory=list)
    overload_down_actions: list[MappingAction] = field(default_factory=list)
    overload_up_actions: list[MappingAction] = field(default_factory=list)

    def __post_init__(self) -> None:
        for actions in (
            self.tap_actions,
            self.double_tap_actions,
            self.hold_actions,
            self.tap_hold_actions,
        ):
            for action in actions:
                if action.action_type == ActionType.SUPERKEY.value:
                    raise ValueError("nested superkeys are not allowed inside superkeys")
        for action in (
            *self.overload_actions,
            *self.overload_down_actions,
            *self.overload_up_actions,
        ):
            if action.action_type == ActionType.SUPERKEY:
                raise ValueError("nested superkeys are not allowed inside superkeys")


type CursorPositionSetter = Callable[[int, int], Awaitable[dict[str, object]]]
type NaturalMouseMover = Callable[
    [int, int, float, float, str, int, int],
    Awaitable[dict[str, object]],
]
type CancelMacroPlayback = Callable[[], Awaitable[dict[str, object]]]
type MacroPlayer = Callable[..., Awaitable[dict[str, object]]]
type EmergencyResetter = Callable[[], Awaitable[dict[str, object]]]


def _default_action_deps() -> "ActionExecutionDeps":
    from keymasq.keymasqd.runtime.grabbed_device.types import (
        ActionExecutionDeps,
    )

    return ActionExecutionDeps(
        asyncio_mod=cast(Any, asyncio),
        fire_and_observe_fn=_fire_and_observe,
        evdev_mod=cast(Any, evdev),
        uinput_writer=identity_uinput_writer,
    )


class SuperkeyMachine:
    def __init__(
        self,
        config: SuperkeyConfig,
        event_name: str,
        keyboard_uinput: WritableUInput,
        mouse_uinput: WritableUInput,
        gamepad_uinput: WritableUInput,
        source_device: str = "",
        broadcast_callback: Callable[[dict[str, object]], Awaitable[None]] | None = None,
        cursor_position_setter: CursorPositionSetter | None = None,
        natural_mouse_mover: NaturalMouseMover | None = None,
        key_event_tracker: Callable[[str, int, int], bool] | None = None,
        axis_event_tracker: Callable[[str, int, int], bool] | None = None,
        gamepad_output_resolver: Callable[[str | None, str], object | None] | None = None,
        macro_player: MacroPlayer | None = None,
        emergency_resetter: EmergencyResetter | None = None,
        cancel_macro_playback: CancelMacroPlayback | None = None,
        action_deps: "ActionExecutionDeps | None" = None,
        await_action_tasks: bool = True,
        repeat_path_recorder: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.event_name = event_name
        self.keyboard_uinput = keyboard_uinput
        self.mouse_uinput = mouse_uinput
        self.gamepad_uinput = gamepad_uinput
        self.source_device = source_device
        self.broadcast_callback = broadcast_callback
        self.cursor_position_setter = cursor_position_setter
        self.natural_mouse_mover = natural_mouse_mover
        self.key_event_tracker = key_event_tracker
        self.axis_event_tracker = axis_event_tracker
        self.gamepad_output_resolver = gamepad_output_resolver
        self.macro_player = macro_player
        self.emergency_resetter = emergency_resetter
        self.cancel_macro_playback = cancel_macro_playback
        self.action_deps = action_deps or _default_action_deps()
        self.await_action_tasks = await_action_tasks
        self.repeat_path_recorder = repeat_path_recorder

        self.state = SuperkeyState.IDLE
        self._hold_task: asyncio.Task[None] | None = None
        self._double_tap_task: asyncio.Task[None] | None = None
        self._rapidfire_tasks: list[asyncio.Task[None]] = []
        self._rapidfire_active = False
        self._running = True
        from keymasq.keymasqd.runtime.action_runner import ActionRuntimeContext

        self._action_runtime = ActionRuntimeContext(
            path=f"superkey:{event_name}",
            hardware_id=source_device,
            keyboard_uinput=keyboard_uinput,
            mouse_uinput=mouse_uinput,
            gamepad_uinput=gamepad_uinput,
            broadcast_callback=self._broadcast_action_trigger,
            cursor_position_setter=cursor_position_setter,
            natural_mouse_mover=natural_mouse_mover,
            macro_player=macro_player,
            emergency_resetter=emergency_resetter,
            gamepad_output_resolver=gamepad_output_resolver,
        )

    async def stop(self) -> None:
        self._running = False
        self._action_runtime.stop()
        self._rapidfire_active = False
        if self._hold_task:
            self._hold_task.cancel()
            self._hold_task = None
        if self._double_tap_task:
            self._double_tap_task.cancel()
            self._double_tap_task = None

        await self._stop_rapidfire_tasks()

        if self.state == SuperkeyState.HOLDING:
            await self._emit_hold_up()
        elif self.state == SuperkeyState.TAP_HOLDING:
            await self._emit_tap_hold_up()
        self.state = SuperkeyState.IDLE

    async def on_down(self) -> None:
        if self.state == SuperkeyState.IDLE:
            await self._transition_to_down_wait()
        elif self.state == SuperkeyState.UP_WAIT:
            await self._on_second_down()

    async def on_up(self) -> None:
        if self.state == SuperkeyState.DOWN_WAIT:
            await self._on_first_up()
        elif self.state == SuperkeyState.HOLDING:
            await self._on_hold_release()
        elif self.state == SuperkeyState.DOWN_WAIT_2:
            await self._on_second_up()
        elif self.state == SuperkeyState.TAP_HOLDING:
            await self._on_tap_hold_release()

    async def _transition_to_down_wait(self) -> None:
        self.state = SuperkeyState.DOWN_WAIT

        if self.config.hold_actions:
            self._hold_task = asyncio.create_task(self._hold_timeout())

    async def _hold_timeout(self) -> None:
        try:
            await asyncio.sleep(self.config.hold_threshold_ms / 1000.0)

            if not self._running:
                return

            if self.state == SuperkeyState.DOWN_WAIT:
                await self._start_holding()
            elif self.state == SuperkeyState.DOWN_WAIT_2:
                await self._start_tap_holding()

        except asyncio.CancelledError:
            pass

    async def _start_holding(self) -> None:
        self.state = SuperkeyState.HOLDING
        await self._emit_hold_down()

    async def _start_tap_holding(self) -> None:
        if not self.config.tap_hold_actions:
            await self._start_holding()
            return

        self.state = SuperkeyState.TAP_HOLDING
        await self._emit_tap_hold_down()

    async def _on_first_up(self) -> None:
        if self._hold_task:
            self._hold_task.cancel()
            self._hold_task = None

        # Tap+hold uses the same second-press window as double tap. Without
        # this branch, a quick tap followed by a held second press falls back
        # to a fresh first press and incorrectly triggers the plain hold slot.
        if self.config.double_tap_actions or self.config.tap_hold_actions:
            self.state = SuperkeyState.UP_WAIT
            self._double_tap_task = asyncio.create_task(self._double_tap_timeout())
        elif self.config.tap_actions:
            await self._emit_tap()
            self.state = SuperkeyState.IDLE
        else:
            self.state = SuperkeyState.IDLE

    async def _double_tap_timeout(self) -> None:
        try:
            await asyncio.sleep(self.config.double_tap_window_ms / 1000.0)

            if not self._running:
                return

            if self.state == SuperkeyState.UP_WAIT:
                if self.config.tap_actions:
                    await self._emit_tap()
                self.state = SuperkeyState.IDLE

        except asyncio.CancelledError:
            pass

    async def _on_second_down(self) -> None:
        if self._double_tap_task:
            self._double_tap_task.cancel()
            self._double_tap_task = None

        self.state = SuperkeyState.DOWN_WAIT_2

        if self.config.tap_hold_actions or self.config.hold_actions:
            self._hold_task = asyncio.create_task(self._hold_timeout())

    async def _on_second_up(self) -> None:
        if self._hold_task:
            self._hold_task.cancel()
            self._hold_task = None

        if self.config.double_tap_actions:
            await self._emit_double_tap()
        elif self.config.tap_actions:
            await self._emit_tap()

        self.state = SuperkeyState.IDLE

    async def _on_hold_release(self) -> None:
        self._rapidfire_active = False
        await self._stop_rapidfire_tasks()
        await self._emit_hold_up()
        self.state = SuperkeyState.IDLE

    async def _on_tap_hold_release(self) -> None:
        self._rapidfire_active = False
        await self._stop_rapidfire_tasks()
        await self._emit_tap_hold_up()
        self.state = SuperkeyState.IDLE

    async def _emit_tap(self) -> None:
        if self.config.tap_actions:
            self._record_repeat_path(SUPERKEY_SLOT_TAP)
            await self._execute_actions_tap(self.config.tap_actions)

    async def _emit_double_tap(self) -> None:
        if self.config.double_tap_actions:
            self._record_repeat_path(SUPERKEY_SLOT_DOUBLE_TAP)
            await self._execute_actions_tap(self.config.double_tap_actions)

    async def _emit_hold_down(self) -> None:
        if self.config.hold_actions:
            self._record_repeat_path(SUPERKEY_SLOT_HOLD)
            await self._execute_actions_down(self.config.hold_actions)

    async def _emit_hold_up(self) -> None:
        if self.config.hold_actions:
            await self._execute_actions_up(self.config.hold_actions)

    async def _emit_tap_hold_down(self) -> None:
        if self.config.tap_hold_actions:
            self._record_repeat_path(SUPERKEY_SLOT_TAP_HOLD)
            await self._execute_actions_down(self.config.tap_hold_actions)

    async def _emit_tap_hold_up(self) -> None:
        if self.config.tap_hold_actions:
            await self._execute_actions_up(self.config.tap_hold_actions)

    async def execute_repeat_slot(self, slot: str) -> None:
        if slot == SUPERKEY_SLOT_TAP:
            await self._emit_tap()
            return
        if slot == SUPERKEY_SLOT_DOUBLE_TAP:
            await self._emit_double_tap()
            return
        if slot == SUPERKEY_SLOT_HOLD:
            if self.config.hold_actions:
                self._record_repeat_path(SUPERKEY_SLOT_HOLD)
                await self._execute_actions_tap(self.config.hold_actions)
            return
        if slot == SUPERKEY_SLOT_TAP_HOLD:
            if self.config.tap_hold_actions:
                self._record_repeat_path(SUPERKEY_SLOT_TAP_HOLD)
                await self._execute_actions_tap(self.config.tap_hold_actions)

    def _record_repeat_path(self, slot: str) -> None:
        if self.repeat_path_recorder is not None:
            self.repeat_path_recorder(slot)

    async def _rapidfire_loop(self, action: SuperkeyActionData) -> None:
        hold = clamp_rapidfire_hold_ms(action.rapidfire_hold_ms) / 1000.0
        wait = clamp_rapidfire_wait_ms(action.rapidfire_wait_ms) / 1000.0
        mouse_target = (
            self._resolve_mouse_target(action.target) if action.action_type == "mouse" else None
        )
        is_relative_mouse = bool(mouse_target and mouse_target.is_relative)

        try:
            if is_relative_mouse and mouse_target is not None:
                await rapidfire_relative_pulses(
                    emit_pulse=lambda: emit_relative_pulse(
                        self.mouse_uinput,
                        mouse_target.code,
                        mouse_target.relative_value,
                        ev_rel_code=evdev.ecodes.EV_REL,
                    ),
                    is_active=lambda: self._rapidfire_active and self._running,
                    hold_s=hold,
                    wait_s=wait,
                    asyncio_mod=asyncio,
                )
            else:
                while self._rapidfire_active and self._running:
                    await self._execute_action_down(action)
                    await asyncio.sleep(hold)

                    if not self._rapidfire_active:
                        break

                    await self._execute_action_up(action)
                    await asyncio.sleep(wait)
        except OSError:
            log.debug("Rapidfire loop stopped after error", exc_info=True)
        except Exception:
            log.exception("Rapidfire loop stopped after unexpected error")
        finally:
            current_task = asyncio.current_task()
            if current_task is not None:
                with contextlib.suppress(ValueError):
                    self._rapidfire_tasks.remove(cast(asyncio.Task[None], current_task))
            if not is_relative_mouse:
                try:
                    await self._execute_action_up(action)
                except OSError:
                    log.debug("Failed to release rapidfire action after loop stop", exc_info=True)
                except Exception:
                    log.exception("Unexpected failure releasing rapidfire action after loop stop")

    async def _stop_rapidfire_tasks(self) -> None:
        if not self._rapidfire_tasks:
            return

        tasks = list(self._rapidfire_tasks)
        self._rapidfire_tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _execute_actions_tap(self, actions: list[SuperkeyActionData]) -> None:
        indexed_actions = list(enumerate(actions))
        for index, action in indexed_actions:
            await self._execute_action_down(
                action,
                action_event_name=self._child_event_name(action, index),
            )
        await asyncio.sleep(0.01)
        for index, action in reversed(indexed_actions):
            await self._execute_action_up(
                action,
                action_event_name=self._child_event_name(action, index),
            )

    async def _execute_actions_down(self, actions: list[SuperkeyActionData]) -> None:
        for index, action in enumerate(actions):
            await self._execute_action_down(
                action,
                action_event_name=self._child_event_name(action, index),
            )

    async def _execute_actions_up(self, actions: list[SuperkeyActionData]) -> None:
        indexed_actions = list(enumerate(actions))
        for index, action in reversed(indexed_actions):
            await self._execute_action_up(
                action,
                action_event_name=self._child_event_name(action, index),
            )

    async def _execute_action_down(
        self,
        action: SuperkeyActionData,
        *,
        action_event_name: str | None = None,
    ) -> None:
        await self._execute_mapping_action(action, 1, action_event_name=action_event_name)

    async def _execute_action_up(
        self,
        action: SuperkeyActionData,
        *,
        action_event_name: str | None = None,
    ) -> None:
        await self._execute_mapping_action(action, 0, action_event_name=action_event_name)

    async def _execute_mapping_action(
        self,
        action: SuperkeyActionData,
        value: int,
        *,
        action_event_name: str | None = None,
    ) -> None:
        from keymasq.keymasqd.runtime import action_runner

        event_name = action_event_name or self.event_name
        started = asyncio.Event()
        handle = action_runner.ActionExecutionHandle(started=started)
        await action_runner.execute_action(
            self._action_runtime,
            self._mapping_action(action),
            _SyntheticInputEvent(evdev.ecodes.EV_KEY, 0, value),
            event_name,
            deps=self.action_deps,
            shared_output_tracker=self.key_event_tracker,
            shared_abs_output_tracker=self.axis_event_tracker,
            execution_handle=handle,
            cancel_macro_playback=self.cancel_macro_playback,
        )
        await started.wait()
        if self.await_action_tasks:
            await action_runner.drain_action_tasks(handle)

    def _child_event_name(self, action: SuperkeyActionData, index: int) -> str:
        if not action.rapidfire_enabled:
            return self.event_name
        return f"{self.event_name}#{index}"

    async def _broadcast_action_trigger(
        self,
        event_type: CommandType,
        data: dict[str, object],
    ) -> None:
        if event_type != CommandType.ACTION_TRIGGER or self.broadcast_callback is None:
            return
        await self.broadcast_callback(data)

    def _resolve_mouse_target(self, target: str | None):
        return resolve_mouse_output_target(target)

    def _mapping_action(self, action: SuperkeyActionData) -> MappingAction:
        action_kwargs = superkey_action_shared_kwargs(action)
        action_kwargs["rapidfire_enabled"] = action.rapidfire_enabled
        action_kwargs["rapidfire_hold_ms"] = action.rapidfire_hold_ms
        action_kwargs["rapidfire_wait_ms"] = action.rapidfire_wait_ms
        return MappingAction(
            action_type=ActionType(action.action_type),
            **action_kwargs,
        )
