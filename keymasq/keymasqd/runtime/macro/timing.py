from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class MacroPauseState:
    """One instance's pause request and cumulative wall-clock pause duration."""

    paused_at_s: float | None = None
    elapsed_s: float = 0.0
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    on_pause: Callable[[], None] | None = None
    on_resume: Callable[[], None] | None = None
    timeout_s: float = 0.0
    on_expire: Callable[[], None] | None = None
    timeout_handle: asyncio.TimerHandle | None = None

    @property
    def paused(self) -> bool:
        return self.paused_at_s is not None

    def total(self, now_s: float) -> float:
        return self.elapsed_s + (
            max(0.0, now_s - self.paused_at_s) if self.paused_at_s is not None else 0.0
        )

    def pause(self, now_s: float) -> None:
        if not self.paused:
            self.paused_at_s = now_s
            self.changed.set()
            if self.timeout_s > 0:
                self.timeout_handle = asyncio.get_running_loop().call_at(
                    now_s + self.timeout_s, self.expire
                )
            if self.on_pause:
                self.on_pause()

    def resume(self, now_s: float) -> None:
        self.clear_timeout()
        was_paused = self.paused
        self.elapsed_s = self.total(now_s)
        self.paused_at_s = None
        self.changed.set()
        if was_paused and self.on_resume:
            self.on_resume()

    def clear_timeout(self) -> None:
        if self.timeout_handle is not None:
            self.timeout_handle.cancel()
            self.timeout_handle = None

    def expired(self, now_s: float) -> bool:
        return (
            self.paused_at_s is not None
            and self.timeout_s > 0
            and now_s >= self.paused_at_s + self.timeout_s
        )

    def expire(self) -> None:
        self.clear_timeout()
        if self.paused and self.on_expire:
            self.on_expire()

    async def wait_running(self) -> None:
        while self.paused:
            self.changed.clear()
            await self.changed.wait()


@dataclass
class MacroPlaybackTimeline:
    """Anchored playback clock that prevents per-event sleep drift."""

    anchor_s: float
    speed_factor: float = 1.0
    blocking_offset_s: float = 0.0
    pause: MacroPauseState | None = None
    pause_baseline_s: float = 0.0

    def __post_init__(self) -> None:
        self.speed_factor = max(0.01, float(self.speed_factor))

    def event_deadline(self, timestamp_us: int) -> float:
        return (
            self.anchor_s
            + self.blocking_offset_s
            + (int(timestamp_us) / self.speed_factor) / 1_000_000.0
        )

    def event_delay(self, timestamp_us: int, *, now_s: float) -> float:
        return max(0.0, self.event_deadline(timestamp_us) + self.pause_offset(now_s) - now_s)

    def pause_offset(self, now_s: float) -> float:
        return self.pause.total(now_s) - self.pause_baseline_s if self.pause else 0.0

    def extend_for_blocking_action(self, elapsed_s: float) -> None:
        self.blocking_offset_s += max(0.0, float(elapsed_s))

    def nominal_end_deadline(self, duration_us: int) -> float:
        # Explicit control waits move later event deadlines but do not add a second
        # wait at the end.
        return self.anchor_s + (int(duration_us) / self.speed_factor) / 1_000_000.0

    def nominal_end_delay(self, duration_us: int, *, now_s: float) -> float:
        return max(0.0, self.nominal_end_deadline(duration_us) + self.pause_offset(now_s) - now_s)

    async def wait_until(self, timestamp_us: int, *, nominal_end: bool = False) -> None:
        """Wait for a deadline, recomputing it after each trigger transition."""
        loop = asyncio.get_running_loop()
        delay_fn = self.nominal_end_delay if nominal_end else self.event_delay
        while True:
            if self.pause:
                await self.pause.wait_running()
                self.pause.changed.clear()
            remaining = delay_fn(timestamp_us, now_s=loop.time())
            if remaining < 0.0005:
                return
            if self.pause is None:
                await asyncio.sleep(remaining)
                return
            try:
                await asyncio.wait_for(self.pause.changed.wait(), remaining)
            except TimeoutError:
                pass
