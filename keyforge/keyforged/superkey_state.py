import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

import evdev

from keyforge.keyforged.output_helpers import get_trigger_axis, resolve_output_code

log = logging.getLogger("keyforged.superkey")


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
    rapidfire_enabled: bool = False
    rapidfire_hold_ms: int = 20
    rapidfire_wait_ms: int = 20


@dataclass
class SuperkeyConfig:
    name: str
    tap_timeout_ms: int = 200
    double_tap_window_ms: int = 300
    hold_threshold_ms: int = 300
    tap_action: SuperkeyActionData | None = None
    double_tap_action: SuperkeyActionData | None = None
    hold_action: SuperkeyActionData | None = None
    tap_hold_action: SuperkeyActionData | None = None


class SuperkeyMachine:
    def __init__(
        self,
        config: SuperkeyConfig,
        event_name: str,
        keyboard_uinput: evdev.UInput,
        mouse_uinput: evdev.UInput,
        gamepad_uinput: evdev.UInput,
        broadcast_callback: Callable[[dict], Awaitable[None]] | None = None,
        key_event_tracker: Callable[[str, int, int], bool] | None = None,
    ):
        self.config = config
        self.event_name = event_name
        self.keyboard_uinput = keyboard_uinput
        self.mouse_uinput = mouse_uinput
        self.gamepad_uinput = gamepad_uinput
        self.broadcast_callback = broadcast_callback
        self.key_event_tracker = key_event_tracker

        self.state = SuperkeyState.IDLE
        self._hold_task: asyncio.Task | None = None
        self._double_tap_task: asyncio.Task | None = None
        self._rapidfire_task: asyncio.Task | None = None
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

        rapidfire_stopped = await self._stop_rapidfire_task()

        if self.state == SuperkeyState.HOLDING:
            if not rapidfire_stopped:
                await self._emit_hold_up()
        elif self.state == SuperkeyState.TAP_HOLDING:
            if not rapidfire_stopped:
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

        if self.config.hold_action:
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

        if self.config.double_tap_action:
            self.state = SuperkeyState.UP_WAIT
            self._double_tap_task = asyncio.create_task(self._double_tap_timeout())
        elif self.config.tap_action:
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
                if self.config.tap_action:
                    await self._emit_tap()
                self.state = SuperkeyState.IDLE

        except asyncio.CancelledError:
            pass

    async def _on_second_down(self) -> None:
        if self._double_tap_task:
            self._double_tap_task.cancel()
            self._double_tap_task = None

        self.state = SuperkeyState.DOWN_WAIT_2

        if self.config.tap_hold_action:
            self._hold_task = asyncio.create_task(self._hold_timeout())

    async def _on_second_up(self) -> None:
        if self._hold_task:
            self._hold_task.cancel()
            self._hold_task = None

        if self.config.double_tap_action:
            await self._emit_double_tap()
        elif self.config.tap_action:
            await self._emit_tap()

        self.state = SuperkeyState.IDLE

    async def _on_hold_release(self) -> None:
        self._rapidfire_active = False
        rapidfire_stopped = await self._stop_rapidfire_task()
        if not rapidfire_stopped:
            await self._emit_hold_up()
        self.state = SuperkeyState.IDLE

    async def _on_tap_hold_release(self) -> None:
        self._rapidfire_active = False
        rapidfire_stopped = await self._stop_rapidfire_task()
        if not rapidfire_stopped:
            await self._emit_tap_hold_up()
        self.state = SuperkeyState.IDLE

    async def _emit_tap(self) -> None:
        if self.config.tap_action:
            await self._execute_action(self.config.tap_action, tap=True)

    async def _emit_double_tap(self) -> None:
        if self.config.double_tap_action:
            await self._execute_action(self.config.double_tap_action, tap=True)

    async def _emit_hold_down(self) -> None:
        if self.config.hold_action:
            if self.config.hold_action.rapidfire_enabled:
                self._rapidfire_active = True
                self._rapidfire_task = asyncio.create_task(
                    self._rapidfire_loop(self.config.hold_action)
                )
            else:
                await self._execute_action_down(self.config.hold_action)

    async def _emit_hold_up(self) -> None:
        if self.config.hold_action:
            await self._execute_action_up(self.config.hold_action)

    async def _emit_tap_hold_down(self) -> None:
        if self.config.tap_hold_action:
            if self.config.tap_hold_action.rapidfire_enabled:
                self._rapidfire_active = True
                self._rapidfire_task = asyncio.create_task(
                    self._rapidfire_loop(self.config.tap_hold_action)
                )
            else:
                await self._execute_action_down(self.config.tap_hold_action)

    async def _emit_tap_hold_up(self) -> None:
        if self.config.tap_hold_action:
            await self._execute_action_up(self.config.tap_hold_action)

    async def _rapidfire_loop(self, action: SuperkeyActionData) -> None:
        hold = action.rapidfire_hold_ms / 1000.0
        wait = action.rapidfire_wait_ms / 1000.0

        try:
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
            self._rapidfire_task = None
            await self._execute_action_up(action)

    async def _stop_rapidfire_task(self) -> bool:
        if self._rapidfire_task is None:
            return False

        self._rapidfire_task.cancel()
        task = self._rapidfire_task
        self._rapidfire_task = None
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return True

    async def _execute_action(self, action: SuperkeyActionData, tap: bool = False) -> None:
        await self._execute_action_down(action)
        if tap:
            await asyncio.sleep(0.01)
        await self._execute_action_up(action)

    async def _execute_action_down(self, action: SuperkeyActionData) -> None:
        if action.action_type == "exec":
            if action.exec_ref is not None and self.broadcast_callback:
                await self.broadcast_callback(
                    {
                        "action_type": "exec",
                        "exec_ref": action.exec_ref,
                    }
                )
            return

        if action.action_type == "macro":
            if action.macro_name and self.broadcast_callback:
                await self.broadcast_callback(
                    {
                        "action_type": "macro",
                        "macro_name": action.macro_name,
                    }
                )
            return

        if action.action_type in ("keyboard", "mouse", "gamepad"):
            code = self._resolve_code(action.target)
            if code is None:
                return

            uinput = self._get_uinput(action.action_type)
            if uinput is None:
                return

            is_trigger, axis_code = self._get_trigger_axis(action.target)
            if is_trigger:
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
        if action.action_type in ("exec", "macro"):
            return

        if action.action_type in ("keyboard", "mouse", "gamepad"):
            code = self._resolve_code(action.target)
            if code is None:
                return

            uinput = self._get_uinput(action.action_type)
            if uinput is None:
                return

            is_trigger, axis_code = self._get_trigger_axis(action.target)
            if is_trigger:
                uinput.write(evdev.ecodes.EV_ABS, axis_code, 0)
                uinput.syn()
            else:
                should_emit = True
                if self.key_event_tracker:
                    should_emit = self.key_event_tracker(action.action_type, int(code), 0)
                if should_emit:
                    uinput.write(evdev.ecodes.EV_KEY, code, 0)
                    uinput.syn()

    def _get_uinput(self, action_type: str) -> evdev.UInput | None:
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
