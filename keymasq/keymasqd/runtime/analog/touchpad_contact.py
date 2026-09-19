"""Contact filtering for touchpad-style area mouse input.

A finger's contact patch grows while it lands and shrinks while it lifts, which
moves the reported centroid although the finger did not travel. Measured on a
Steam Deck pad at 250 Hz, a lift from rest ends with a jump of 1-4 % of the
pad's half-width inside the last 4-16 ms before the zero release. A landing
jumps by up to 5 % in its first report and keeps settling for 50-60 ms, at times
overshooting and coming back. Pad pressure does not help: light touches never
report any.
"""

import math
from collections import deque
from dataclasses import dataclass, field

# Positions are normalized to -1..1, so speeds are in pad half-widths per second.
SETTLE_S = 0.060
SETTLE_MIN_S = 0.020
SWIPE_WINDOW_S = 0.012
LIFT_HOLD_S = 0.024
SPEED_WINDOW_S = 0.040
SLOW_SPEED = 1.0
FAST_SPEED = 3.0


@dataclass
class TouchpadContact:
    """Turn one touch's positions into motion that is safe to emit.

    Motion is held for up to LIFT_HOLD_S and dropped if the touch ends first.
    The hold depends on how fast the finger travelled before the step, not on
    the step itself: a lift spike out of rest is held in full, while a moving
    finger has already earned zero latency. Motion while a touch settles only
    moves the reference; settling takes SETTLE_S, or ends after SETTLE_MIN_S once
    the finger is evidently swiping, so a flick keeps its travel.
    """

    # Deadline the owner's flush timer currently sleeps for, if any.
    flush_armed_for: float | None = None
    _started: float | None = None
    _settled: bool = False
    _position: tuple[float, float] = (0.0, 0.0)
    _recent: deque[tuple[float, float]] = field(default_factory=deque)
    _held: deque[tuple[float, float, float]] = field(default_factory=deque)

    def move(self, now: float, x: float, y: float) -> tuple[float, float]:
        """Record a position and return the motion that is due."""
        if self._started is None:
            self._started = now
            self._position = (x, y)
            return 0.0, 0.0
        dx, dy = x - self._position[0], y - self._position[1]
        self._position = (x, y)

        while self._recent and now - self._recent[0][0] > SPEED_WINDOW_S:
            self._recent.popleft()
        speed = sum(distance for _, distance in self._recent) / SPEED_WINDOW_S
        self._recent.append((now, math.hypot(dx, dy)))
        if not self._settled:
            elapsed = now - self._started
            swiped = sum(d for at, d in self._recent if now - at <= SWIPE_WINDOW_S)
            if elapsed < SETTLE_S and (
                elapsed < SETTLE_MIN_S or swiped / SWIPE_WINDOW_S < FAST_SPEED
            ):
                return 0.0, 0.0
            self._settled = True

        slowness = (FAST_SPEED - speed) / (FAST_SPEED - SLOW_SPEED)
        due = now + LIFT_HOLD_S * max(0.0, min(1.0, slowness))
        # Motion stays ordered: a step due sooner releases everything before it.
        self._held = deque((min(held_due, due), hx, hy) for held_due, hx, hy in self._held)
        self._held.append((due, dx, dy))
        return self.drain(now)

    def drain(self, now: float) -> tuple[float, float]:
        """Return held motion whose hold has expired."""
        dx = dy = 0.0
        while self._held and self._held[0][0] <= now:
            _, hx, hy = self._held.popleft()
            dx += hx
            dy += hy
        return dx, dy

    def next_due(self) -> float | None:
        return self._held[0][0] if self._held else None
