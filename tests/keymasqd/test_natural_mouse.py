import asyncio

import evdev
import pytest

from keymasq.keymasqd.runtime.natural_mouse import (
    NaturalMouseMoveConfig,
    move_cursor_naturally,
)


class FastAsyncio:
    def get_running_loop(self):
        return asyncio.get_running_loop()

    async def sleep(self, delay: float) -> None:
        _ = delay
        await asyncio.sleep(0)


class TrackingMouse:
    def __init__(self) -> None:
        self.position = [0, 0]
        self.pending = [0, 0]
        self.writes: list[tuple[int, int, int]] = []

    def write(self, event_type: int, code: int, value: int) -> None:
        self.writes.append((int(event_type), int(code), int(value)))
        if int(event_type) == evdev.ecodes.EV_REL and int(code) == evdev.ecodes.REL_X:
            self.pending[0] += int(value)
        if int(event_type) == evdev.ecodes.EV_REL and int(code) == evdev.ecodes.REL_Y:
            self.pending[1] += int(value)

    def syn(self) -> None:
        self.position[0] += self.pending[0]
        self.position[1] += self.pending[1]
        self.pending = [0, 0]


class OffsetDisplayMouse(TrackingMouse):
    def syn(self) -> None:
        next_x = self.position[0] + self.pending[0]
        next_y = self.position[1] + self.pending[1]
        if self.position[1] < 6:
            next_x = min(next_x, 5)
        self.position = [next_x, next_y]
        self.pending = [0, 0]


class AcceleratedMouse(TrackingMouse):
    def syn(self) -> None:
        factor_x = 3 if abs(self.pending[0]) >= 2 else 1
        factor_y = 3 if abs(self.pending[1]) >= 2 else 1
        self.position[0] += self.pending[0] * factor_x
        self.position[1] += self.pending[1] * factor_y
        self.pending = [0, 0]


@pytest.mark.asyncio
async def test_natural_mouse_move_reaches_target_with_relative_events() -> None:
    mouse = TrackingMouse()

    async def get_position(**_kwargs: object) -> tuple[int, int]:
        return mouse.position[0], mouse.position[1]

    result = await move_cursor_naturally(
        uinput=mouse,
        target_x=20,
        target_y=12,
        get_cursor_position=get_position,
        config=NaturalMouseMoveConfig(
            speed_px_s=8000,
            jitter_px=0,
            curve="linear",
            tolerance_px=0,
            max_duration_ms=500,
        ),
        asyncio_mod=FastAsyncio(),
    )

    assert result["status"] == "ok"
    assert result["reached"] is True
    assert mouse.position == [20, 12]
    assert any(write[1] == evdev.ecodes.REL_X for write in mouse.writes)
    assert any(write[1] == evdev.ecodes.REL_Y for write in mouse.writes)


@pytest.mark.asyncio
async def test_natural_mouse_move_follows_edge_when_axis_is_clipped() -> None:
    mouse = OffsetDisplayMouse()

    async def get_position(**_kwargs: object) -> tuple[int, int]:
        return mouse.position[0], mouse.position[1]

    result = await move_cursor_naturally(
        uinput=mouse,
        target_x=10,
        target_y=10,
        get_cursor_position=get_position,
        config=NaturalMouseMoveConfig(
            speed_px_s=5000,
            jitter_px=0,
            curve="linear",
            tolerance_px=0,
            max_duration_ms=500,
        ),
        asyncio_mod=FastAsyncio(),
    )

    assert result["status"] == "ok"
    assert result["reached"] is True
    assert mouse.position == [10, 10]


@pytest.mark.asyncio
async def test_natural_mouse_move_brakes_when_relative_motion_is_accelerated() -> None:
    mouse = AcceleratedMouse()

    async def get_position(**_kwargs: object) -> tuple[int, int]:
        return mouse.position[0], mouse.position[1]

    result = await move_cursor_naturally(
        uinput=mouse,
        target_x=80,
        target_y=0,
        get_cursor_position=get_position,
        config=NaturalMouseMoveConfig(
            speed_px_s=12000,
            jitter_px=0,
            curve="linear",
            tolerance_px=1,
            max_duration_ms=1000,
        ),
        asyncio_mod=FastAsyncio(),
    )

    assert result["status"] == "ok"
    assert result["reached"] is True
    assert abs(mouse.position[0] - 80) <= 1
    assert mouse.position[1] == 0


@pytest.mark.asyncio
async def test_natural_mouse_move_reports_missing_feedback() -> None:
    async def get_position(**_kwargs: object) -> tuple[int, int] | None:
        return None

    result = await move_cursor_naturally(
        uinput=TrackingMouse(),
        target_x=10,
        target_y=10,
        get_cursor_position=get_position,
        config=NaturalMouseMoveConfig(
            speed_px_s=5000,
            jitter_px=0,
            curve="linear",
            tolerance_px=0,
            max_duration_ms=500,
        ),
        asyncio_mod=FastAsyncio(),
    )

    assert result["status"] == "error"
    assert "Realtime cursor position" in str(result["message"])


@pytest.mark.asyncio
async def test_natural_mouse_move_passes_tracking_hint_to_cursor_feedback() -> None:
    mouse = TrackingMouse()
    hints: list[int | None] = []

    async def get_position(*, tracking_hint_ms: int | None = None) -> tuple[int, int]:
        hints.append(tracking_hint_ms)
        return mouse.position[0], mouse.position[1]

    result = await move_cursor_naturally(
        uinput=mouse,
        target_x=16,
        target_y=0,
        get_cursor_position=get_position,
        config=NaturalMouseMoveConfig(
            speed_px_s=8000,
            jitter_px=0,
            curve="linear",
            tolerance_px=0,
            max_duration_ms=500,
        ),
        asyncio_mod=FastAsyncio(),
    )

    assert result["status"] == "ok"
    assert hints
    assert hints[0] == 500
    assert all(hint is not None and 1 <= hint <= 500 for hint in hints)
