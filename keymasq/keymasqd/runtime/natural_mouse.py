from __future__ import annotations

import math
import random
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Protocol

from keymasq.common.models import normalize_natural_mouse_move_curve
from keymasq.keymasqd.output_helpers import emit_mouse_move
from keymasq.keymasqd.runtime.adapters import WritableUInput

_DEFAULT_TICK_HZ = 120.0
_EDGE_FOLLOW_RETRY_S = 0.09
_STUCK_FRAME_LIMIT = 8


class CursorPositionGetter(Protocol):
    def __call__(
        self,
        *,
        tracking_hint_ms: int | None = None,
    ) -> Awaitable[tuple[int, int] | None]: ...


class _AsyncioModule(Protocol):
    async def sleep(self, delay: float, /) -> None: ...

    def get_running_loop(self) -> Any: ...


@dataclass(frozen=True)
class NaturalMouseMoveConfig:
    speed_px_s: float
    jitter_px: float
    curve: str
    tolerance_px: int
    max_duration_ms: int
    tick_hz: float = _DEFAULT_TICK_HZ

    def normalized(self) -> NaturalMouseMoveConfig:
        return NaturalMouseMoveConfig(
            speed_px_s=max(1.0, float(self.speed_px_s)),
            jitter_px=max(0.0, float(self.jitter_px)),
            curve=normalize_natural_mouse_move_curve(self.curve),
            tolerance_px=max(0, int(self.tolerance_px)),
            max_duration_ms=max(1, int(self.max_duration_ms)),
            tick_hz=max(30.0, min(240.0, float(self.tick_hz))),
        )


def distance_to_target(position: tuple[int, int], target: tuple[int, int]) -> float:
    return math.hypot(float(target[0] - position[0]), float(target[1] - position[1]))


async def move_cursor_naturally(
    *,
    uinput: WritableUInput | None,
    target_x: int,
    target_y: int,
    get_cursor_position: CursorPositionGetter,
    config: NaturalMouseMoveConfig,
    asyncio_mod: _AsyncioModule,
) -> dict[str, object]:
    if uinput is None:
        return {"status": "error", "message": "No mouse uinput device available"}

    config = config.normalized()
    target = (int(target_x), int(target_y))
    tracking_hint_ms = config.max_duration_ms
    position = await get_cursor_position(tracking_hint_ms=tracking_hint_ms)
    if position is None:
        return {
            "status": "error",
            "message": "Realtime cursor position is unavailable",
            "target_x": target[0],
            "target_y": target[1],
        }

    loop = asyncio_mod.get_running_loop()
    started_at = loop.time()
    deadline = started_at + config.max_duration_ms / 1000.0
    start_distance = max(1.0, distance_to_target(position, target))
    nominal_duration_s = max(
        1.0 / config.tick_hz,
        min(config.max_duration_ms / 1000.0, start_distance / config.speed_px_s),
    )
    tick_s = 1.0 / config.tick_hz
    residual_x = 0.0
    residual_y = 0.0
    mode = "direct"
    mode_retry_at = started_at
    stuck_frames = 0
    emitted_frames = 0

    while loop.time() <= deadline:
        distance = distance_to_target(position, target)
        if distance <= config.tolerance_px:
            return _success_result(position, target, config.tolerance_px, emitted_frames)

        now = loop.time()
        error_x = target[0] - position[0]
        error_y = target[1] - position[1]
        if mode != "direct" and (
            now >= mode_retry_at or _axis_error_done(mode, error_x, error_y, config)
        ):
            mode = "direct"

        direction_x, direction_y = _direction_for_mode(mode, error_x, error_y)
        if direction_x == 0.0 and direction_y == 0.0:
            mode = "direct"
            direction_x, direction_y = _direction_for_mode(mode, error_x, error_y)
            if direction_x == 0.0 and direction_y == 0.0:
                return _success_result(position, target, config.tolerance_px, emitted_frames)

        progress = min(1.0, max(0.0, (now - started_at) / nominal_duration_s))
        step_px = max(
            1.0,
            config.speed_px_s * tick_s * _curve_velocity_scale(config.curve, progress),
        )
        move_x_f = direction_x * step_px
        move_y_f = direction_y * step_px
        move_x_f, move_y_f = _apply_perpendicular_jitter(
            move_x_f,
            move_y_f,
            config.jitter_px,
        )
        move_x, move_y, residual_x, residual_y = _quantize_move(
            move_x_f,
            move_y_f,
            residual_x,
            residual_y,
            error_x,
            error_y,
        )
        if move_x == 0 and move_y == 0:
            move_x, move_y = _minimum_move(error_x, error_y, mode)

        emit_mouse_move(uinput, move_x, move_y)
        emitted_frames += 1
        await asyncio_mod.sleep(tick_s)

        remaining_ms = max(1, int((deadline - loop.time()) * 1000.0))
        next_position = await get_cursor_position(tracking_hint_ms=remaining_ms)
        if next_position is None:
            return {
                "status": "error",
                "message": "Realtime cursor position became unavailable",
                "target_x": target[0],
                "target_y": target[1],
                "last_x": position[0],
                "last_y": position[1],
                "reached": False,
            }

        actual_dx = next_position[0] - position[0]
        actual_dy = next_position[1] - position[1]
        if actual_dx == 0 and actual_dy == 0:
            stuck_frames += 1
        else:
            stuck_frames = 0

        mode, mode_retry_at = _next_route_mode(
            mode=mode,
            retry_at=mode_retry_at,
            now=loop.time(),
            requested_dx=move_x,
            requested_dy=move_y,
            actual_dx=actual_dx,
            actual_dy=actual_dy,
            error_x=error_x,
            error_y=error_y,
            tolerance=config.tolerance_px,
        )
        position = next_position

        if stuck_frames >= _STUCK_FRAME_LIMIT:
            break

    return {
        "status": "error",
        "message": "Cursor did not reach target",
        "target_x": target[0],
        "target_y": target[1],
        "last_x": int(position[0]),
        "last_y": int(position[1]),
        "distance": distance_to_target(position, target),
        "tolerance": int(config.tolerance_px),
        "reached": False,
        "frames": emitted_frames,
    }


def _success_result(
    position: tuple[int, int],
    target: tuple[int, int],
    tolerance: int,
    frames: int,
) -> dict[str, object]:
    return {
        "status": "ok",
        "x": int(position[0]),
        "y": int(position[1]),
        "target_x": int(target[0]),
        "target_y": int(target[1]),
        "distance": distance_to_target(position, target),
        "tolerance": int(tolerance),
        "reached": True,
        "frames": int(frames),
    }


def _curve_velocity_scale(curve: str, progress: float) -> float:
    progress = min(1.0, max(0.0, progress))
    normalized = normalize_natural_mouse_move_curve(curve)
    if normalized == "linear":
        return 1.0
    if normalized == "natural":
        # Natural uses the minimum-jerk position curve's velocity profile:
        # derivative of 10t^3 - 15t^4 + 6t^5, normalized near a 1.0 peak.
        return max(0.18, (30.0 * progress * progress * (1.0 - progress) ** 2) / 1.875)
    return 1.0


def _direction_for_mode(mode: str, error_x: int, error_y: int) -> tuple[float, float]:
    if mode == "x":
        return float(_sign(error_x)), 0.0
    if mode == "y":
        return 0.0, float(_sign(error_y))
    distance = math.hypot(float(error_x), float(error_y))
    if distance <= 0.0:
        return 0.0, 0.0
    return float(error_x) / distance, float(error_y) / distance


def _axis_error_done(
    mode: str,
    error_x: int,
    error_y: int,
    config: NaturalMouseMoveConfig,
) -> bool:
    if mode == "x":
        return abs(error_x) <= config.tolerance_px
    if mode == "y":
        return abs(error_y) <= config.tolerance_px
    return True


def _apply_perpendicular_jitter(
    move_x: float,
    move_y: float,
    jitter_px: float,
) -> tuple[float, float]:
    if jitter_px <= 0.0:
        return move_x, move_y
    length = math.hypot(move_x, move_y)
    if length <= 0.0:
        return move_x, move_y
    jitter = random.uniform(-jitter_px, jitter_px)
    return move_x + (-move_y / length) * jitter, move_y + (move_x / length) * jitter


def _quantize_move(
    move_x: float,
    move_y: float,
    residual_x: float,
    residual_y: float,
    error_x: int,
    error_y: int,
) -> tuple[int, int, float, float]:
    next_x = residual_x + move_x
    next_y = residual_y + move_y
    emit_x = int(round(next_x))
    emit_y = int(round(next_y))
    residual_x = next_x - emit_x
    residual_y = next_y - emit_y
    if error_x > 0:
        emit_x = max(0, min(emit_x, error_x))
    elif error_x < 0:
        emit_x = min(0, max(emit_x, error_x))
    else:
        emit_x = 0
    if error_y > 0:
        emit_y = max(0, min(emit_y, error_y))
    elif error_y < 0:
        emit_y = min(0, max(emit_y, error_y))
    else:
        emit_y = 0
    return emit_x, emit_y, residual_x, residual_y


def _minimum_move(error_x: int, error_y: int, mode: str) -> tuple[int, int]:
    if mode == "x":
        return _sign(error_x), 0
    if mode == "y":
        return 0, _sign(error_y)
    if abs(error_x) >= abs(error_y):
        return _sign(error_x), 0
    return 0, _sign(error_y)


def _next_route_mode(
    *,
    mode: str,
    retry_at: float,
    now: float,
    requested_dx: int,
    requested_dy: int,
    actual_dx: int,
    actual_dy: int,
    error_x: int,
    error_y: int,
    tolerance: int,
) -> tuple[str, float]:
    if mode != "direct":
        return mode, retry_at

    x_blocked = requested_dx != 0 and actual_dx == 0 and abs(error_x) > tolerance
    y_blocked = requested_dy != 0 and actual_dy == 0 and abs(error_y) > tolerance
    if x_blocked and abs(error_y) > tolerance:
        return "y", now + _EDGE_FOLLOW_RETRY_S
    if y_blocked and abs(error_x) > tolerance:
        return "x", now + _EDGE_FOLLOW_RETRY_S
    return mode, retry_at


def _sign(value: int) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0
