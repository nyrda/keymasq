import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, cast

import evdev

from keymasq.common.models import (
    ActionType,
    MappingAction,
    SuperkeyMode,
    clamp_rapidfire_hold_ms,
    clamp_rapidfire_wait_ms,
    superkey_action_shared_kwargs,
)
from keymasq.keymasqd.output_helpers import emit_mouse_move, get_trigger_axis, resolve_output_code
from keymasq.keymasqd.runtime.action_runner import (
    build_action_trigger_payload,
    build_macro_playback_request,
)
from keymasq.keymasqd.runtime.mouse_actions import (
    emit_relative_pulse,
    rapidfire_relative_pulses,
    resolve_mouse_output_target,
)

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
    profile_name: str | None = None
    compositor_id: str | None = None
    compositor_dispatcher: str | None = None
    compositor_args: str | None = None
    move_x: int = 0
    move_y: int = 0
    openrazer_setting: str | None = None
    openrazer_device: str = "serial"
    openrazer_serial: str | None = None
    openrazer_dpi_x: int = 0
    openrazer_dpi_y: int = 0
    openrazer_poll_rate: int = 0
    rapidfire_enabled: bool = False
    rapidfire_hold_ms: int = 20
    rapidfire_wait_ms: int = 20

    def __post_init__(self) -> None:
        self.rapidfire_hold_ms = clamp_rapidfire_hold_ms(self.rapidfire_hold_ms)
        self.rapidfire_wait_ms = clamp_rapidfire_wait_ms(self.rapidfire_wait_ms)


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


class _WritableUInput(Protocol):
    def write(self, event_type: int, code: int, value: int) -> None: ...

    def syn(self) -> None: ...


type CursorPositionSetter = Callable[[int, int], Awaitable[object]]


class SuperkeyMachine:
    def __init__(
        self,
        config: SuperkeyConfig,
        event_name: str,
        keyboard_uinput: _WritableUInput,
        mouse_uinput: _WritableUInput,
        gamepad_uinput: _WritableUInput,
        source_device: str = "",
        broadcast_callback: Callable[[dict[str, object]], Awaitable[None]] | None = None,
        cursor_position_setter: CursorPositionSetter | None = None,
        key_event_tracker: Callable[[str, int, int], bool] | None = None,
    ) -> None:
        self.config = config
        self.event_name = event_name
        self.keyboard_uinput = keyboard_uinput
        self.mouse_uinput = mouse_uinput
        self.gamepad_uinput = gamepad_uinput
        self.source_device = source_device
        self.broadcast_callback = broadcast_callback
        self.cursor_position_setter = cursor_position_setter
        self.key_event_tracker = key_event_tracker

        self.state = SuperkeyState.IDLE
        self._hold_task: asyncio.Task[None] | None = None
        self._double_tap_task: asyncio.Task[None] | None = None
        self._rapidfire_tasks: list[asyncio.Task[None]] = []
        self._rapidfire_active = False
        self._running = True

    async def stop(self) -> None:
        self._running = False
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

        if self.config.tap_hold_actions:
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
            await self._execute_actions_tap(self.config.tap_actions)

    async def _emit_double_tap(self) -> None:
        if self.config.double_tap_actions:
            await self._execute_actions_tap(self.config.double_tap_actions)

    async def _emit_hold_down(self) -> None:
        if self.config.hold_actions:
            await self._execute_actions_down(self.config.hold_actions)

    async def _emit_hold_up(self) -> None:
        if self.config.hold_actions:
            await self._execute_actions_up(self.config.hold_actions)

    async def _emit_tap_hold_down(self) -> None:
        if self.config.tap_hold_actions:
            await self._execute_actions_down(self.config.tap_hold_actions)

    async def _emit_tap_hold_up(self) -> None:
        if self.config.tap_hold_actions:
            await self._execute_actions_up(self.config.tap_hold_actions)

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
        except Exception:
            pass
        finally:
            current_task = asyncio.current_task()
            if current_task is not None:
                with contextlib.suppress(ValueError):
                    self._rapidfire_tasks.remove(cast(asyncio.Task[None], current_task))
            if not is_relative_mouse:
                await self._execute_action_up(action)

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
        for action in actions:
            await self._execute_action_down(action)
        await asyncio.sleep(0.01)
        for action in reversed(actions):
            await self._execute_action_up(action)

    async def _execute_actions_down(self, actions: list[SuperkeyActionData]) -> None:
        self._rapidfire_active = any(action.rapidfire_enabled for action in actions)

        for action in actions:
            if action.rapidfire_enabled:
                task = asyncio.create_task(self._rapidfire_loop(action))
                self._rapidfire_tasks.append(task)
            else:
                await self._execute_action_down(action)

    async def _execute_actions_up(self, actions: list[SuperkeyActionData]) -> None:
        for action in reversed(actions):
            if action.rapidfire_enabled:
                continue
            await self._execute_action_up(action)

    async def _execute_action_down(self, action: SuperkeyActionData) -> None:
        if action.action_type == "exec":
            trigger_payload = build_action_trigger_payload(
                self._mapping_action(action),
                source_device=self.source_device,
                source_button=self.event_name,
            )
            if trigger_payload is not None and self.broadcast_callback:
                await self.broadcast_callback(trigger_payload)
            return

        if action.action_type == "macro":
            trigger_payload = self._macro_trigger_payload(action, trigger_value=1)
            if trigger_payload is not None and self.broadcast_callback:
                await self.broadcast_callback(trigger_payload)
            return

        if action.action_type == "mouse_move_abs":
            if self.cursor_position_setter is not None:
                await self.cursor_position_setter(int(action.move_x), int(action.move_y))
                return
            emit_mouse_move(
                self.mouse_uinput,
                int(action.move_x),
                int(action.move_y),
                absolute=True,
            )
            return

        if action.action_type == "mouse_move_rel":
            emit_mouse_move(
                self.mouse_uinput,
                int(action.move_x),
                int(action.move_y),
                absolute=False,
            )
            return

        trigger_action = self._broadcast_action(action)
        if trigger_action is not None and self.broadcast_callback:
            await self.broadcast_callback(trigger_action)
            return

        if action.action_type in ("keyboard", "mouse", "gamepad"):
            code = self._resolve_code(action.target)
            if code is None:
                return

            uinput = self._get_uinput(action.action_type)
            if uinput is None:
                return

            mouse_target = (
                self._resolve_mouse_target(action.target) if action.action_type == "mouse" else None
            )
            if mouse_target is not None and mouse_target.is_relative:
                emit_relative_pulse(
                    uinput,
                    mouse_target.code,
                    mouse_target.relative_value,
                    ev_rel_code=evdev.ecodes.EV_REL,
                )
                return

            is_trigger, axis_code = self._get_trigger_axis(action.target)
            if is_trigger:
                if axis_code is None:
                    return
                uinput.write(evdev.ecodes.EV_ABS, axis_code, 255)
                uinput.syn()
            else:
                should_emit = True
                if self.key_event_tracker:
                    should_emit = self.key_event_tracker(action.action_type, int(code), 1)
                if should_emit:
                    uinput.write(evdev.ecodes.EV_KEY, code, 1)
                    uinput.syn()

    async def _execute_action_up(self, action: SuperkeyActionData) -> None:
        if action.action_type in (
            "exec",
            "mouse_move_rel",
            "mouse_move_abs",
            "compositor_dispatch",
            "start_macro_recording",
            "stop_macro_recording",
            "cancel_macro_playback",
            "emergency_reset",
            "profile_enable",
            "profile_disable",
            "profile_toggle",
            "openrazer",
        ):
            return

        if action.action_type == "macro":
            trigger_payload = self._macro_trigger_payload(action, trigger_value=0)
            if trigger_payload is not None and self.broadcast_callback:
                await self.broadcast_callback(trigger_payload)
            return

        if action.action_type in ("keyboard", "mouse", "gamepad"):
            code = self._resolve_code(action.target)
            if code is None:
                return

            uinput = self._get_uinput(action.action_type)
            if uinput is None:
                return

            mouse_target = (
                self._resolve_mouse_target(action.target) if action.action_type == "mouse" else None
            )
            if mouse_target is not None and mouse_target.is_relative:
                return

            is_trigger, axis_code = self._get_trigger_axis(action.target)
            if is_trigger:
                if axis_code is None:
                    return
                uinput.write(evdev.ecodes.EV_ABS, axis_code, 0)
                uinput.syn()
            else:
                should_emit = True
                if self.key_event_tracker:
                    should_emit = self.key_event_tracker(action.action_type, int(code), 0)
                if should_emit:
                    uinput.write(evdev.ecodes.EV_KEY, code, 0)
                    uinput.syn()

    def _get_uinput(self, action_type: str) -> _WritableUInput | None:
        if action_type == "keyboard":
            return self.keyboard_uinput
        elif action_type == "mouse":
            return self.mouse_uinput
        elif action_type == "gamepad":
            return self.gamepad_uinput
        return None

    def _resolve_code(self, target: str | None) -> int | None:
        return resolve_output_code(target)

    def _get_trigger_axis(self, target: str | None) -> tuple[bool, int | None]:
        return get_trigger_axis(target)

    def _resolve_mouse_target(self, target: str | None):
        return resolve_mouse_output_target(target)

    def _mapping_action(self, action: SuperkeyActionData) -> MappingAction:
        return MappingAction(
            action_type=ActionType(action.action_type),
            **superkey_action_shared_kwargs(action),
        )

    def _broadcast_action(self, action: SuperkeyActionData) -> dict[str, object] | None:
        action_type = action.action_type
        if action_type not in {
            "compositor_dispatch",
            "start_macro_recording",
            "stop_macro_recording",
            "cancel_macro_playback",
            "emergency_reset",
            "profile_enable",
            "profile_disable",
            "profile_toggle",
            "openrazer",
        }:
            return None
        return build_action_trigger_payload(
            self._mapping_action(action),
            source_device=self.source_device,
            source_button=self.event_name,
        )

    def _macro_trigger_payload(
        self,
        action: SuperkeyActionData,
        *,
        trigger_value: int,
    ) -> dict[str, object] | None:
        payload = build_macro_playback_request(
            self._mapping_action(action),
            source_device=self.source_device,
            source_button=self.event_name,
            trigger_value=trigger_value,
            include_macro_events=False,
        )
        if payload is None:
            return None
        return {
            "action_type": "macro",
            **payload,
        }
