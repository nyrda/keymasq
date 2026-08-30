from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import cast

from keymasq.common.coercion import coerce_int, coerce_str
from keymasq.common.ipc import CommandType
from keymasq.common.types import JsonObject
from keymasq.keymasqd.output_helpers import emit_mouse_move
from keymasq.keymasqd.runtime import adapters, natural_mouse, outputs

type BroadcastCallback = Callable[[CommandType, JsonObject], Awaitable[None]]


class CursorManagerMixin:
    """Daemon-facing cursor feedback and natural-motion coordination."""

    output_state: outputs.OutputRuntimeState
    broadcast_callback: BroadcastCallback | None

    def _initialize_cursor_runtime(self) -> None:
        self._cursor_move_lock = asyncio.Lock()
        self._cursor_move_cancel: asyncio.Event | None = None
        self._cursor_move_cancel_generation = 0
        self._cursor_request_seq = 0
        self._cursor_position_waiters: dict[str, asyncio.Future[JsonObject]] = {}

    def _broadcast_runtime_event(
        self,
        event_type: CommandType,
        data: JsonObject,
    ) -> None:
        raise NotImplementedError

    async def set_cursor_position(self, x: int, y: int) -> JsonObject:
        if self.output_state.mouse_uinput is None:
            return {"status": "error", "message": "No mouse uinput device available"}

        emit_mouse_move(
            cast(adapters.WritableUInput, self.output_state.mouse_uinput),
            int(x),
            int(y),
            absolute=True,
        )
        return {"status": "ok", "x": int(x), "y": int(y)}

    async def get_cursor_position(
        self,
        timeout_s: float = 0.75,
        *,
        tracking_hint_ms: int | None = None,
    ) -> tuple[int, int] | None:
        if self.broadcast_callback is None:
            return None

        self._cursor_request_seq += 1
        request_id = str(self._cursor_request_seq)
        future: asyncio.Future[JsonObject] = asyncio.get_running_loop().create_future()
        self._cursor_position_waiters[request_id] = future
        payload: JsonObject = {"request_id": request_id}
        if tracking_hint_ms is not None:
            payload["tracking_hint_ms"] = max(1, int(tracking_hint_ms))

        self._broadcast_runtime_event(CommandType.CURSOR_POSITION_REQUEST, payload)
        request_timeout_s = max(0.05, float(timeout_s))
        if tracking_hint_ms is not None:
            remaining_timeout_s = max(0.001, int(tracking_hint_ms) / 1000.0)
            request_timeout_s = min(request_timeout_s, remaining_timeout_s)

        try:
            result = await asyncio.wait_for(future, timeout=request_timeout_s)
        except TimeoutError:
            return None
        finally:
            self._cursor_position_waiters.pop(request_id, None)

        if result.get("status") != "ok":
            return None
        return coerce_int(result.get("x"), 0), coerce_int(result.get("y"), 0)

    def handle_cursor_position_response(self, data: JsonObject) -> JsonObject:
        request_id = coerce_str(data.get("request_id"), "")
        if not request_id:
            return {"status": "error", "message": "request_id required"}
        future = self._cursor_position_waiters.get(request_id)
        if future is None or future.done():
            return {"status": "ok", "matched": False}
        future.set_result(data)
        return {"status": "ok", "matched": True}

    async def stop_cursor_position_tracking(self) -> None:
        if self.broadcast_callback is None:
            return
        try:
            await self.broadcast_callback(CommandType.CURSOR_POSITION_TRACKING_STOP, {})
        except (ConnectionError, OSError, RuntimeError, TimeoutError, TypeError):
            logging.getLogger("keymasqd.devices").debug(
                "Failed to broadcast cursor position tracking stop",
                exc_info=True,
            )

    async def move_cursor_natural(
        self,
        x: int,
        y: int,
        speed: float,
        jitter: float,
        curve: str,
        tolerance: int,
        max_duration_ms: int,
    ) -> JsonObject:
        cancel_generation = self._cursor_move_cancel_generation
        async with self._cursor_move_lock:
            if cancel_generation != self._cursor_move_cancel_generation:
                return {
                    "status": "error",
                    "message": "Cursor move cancelled",
                    "target_x": int(x),
                    "target_y": int(y),
                    "reached": False,
                }
            cancel_event = asyncio.Event()
            self._cursor_move_cancel = cancel_event
            try:
                return await natural_mouse.move_cursor_naturally(
                    uinput=cast(
                        adapters.WritableUInput | None,
                        self.output_state.mouse_uinput,
                    ),
                    target_x=int(x),
                    target_y=int(y),
                    get_cursor_position=self.get_cursor_position,
                    config=natural_mouse.NaturalMouseMoveConfig(
                        speed_px_s=float(speed),
                        jitter_px=float(jitter),
                        curve=str(curve),
                        tolerance_px=int(tolerance),
                        max_duration_ms=int(max_duration_ms),
                    ),
                    asyncio_mod=adapters.ASYNCIO_RUNTIME,
                    should_cancel=cancel_event.is_set,
                )
            finally:
                if self._cursor_move_cancel is cancel_event:
                    self._cursor_move_cancel = None
                await self.stop_cursor_position_tracking()

    async def cancel_cursor_move(self) -> None:
        self._cursor_move_cancel_generation += 1
        cancel_event = self._cursor_move_cancel
        if cancel_event is not None:
            cancel_event.set()
        async with self._cursor_move_lock:
            pass
