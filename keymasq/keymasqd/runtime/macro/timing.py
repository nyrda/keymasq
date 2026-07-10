from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MacroPlaybackTimeline:
    """Anchored playback clock that prevents per-event sleep drift."""

    anchor_s: float
    speed_factor: float = 1.0
    blocking_offset_s: float = 0.0

    def __post_init__(self) -> None:
        self.speed_factor = max(0.01, float(self.speed_factor))

    def event_deadline(self, timestamp_us: int) -> float:
        return (
            self.anchor_s
            + self.blocking_offset_s
            + (int(timestamp_us) / self.speed_factor) / 1_000_000.0
        )

    def event_delay(self, timestamp_us: int, *, now_s: float) -> float:
        return max(0.0, self.event_deadline(timestamp_us) - now_s)

    def extend_for_blocking_action(self, elapsed_s: float) -> None:
        self.blocking_offset_s += max(0.0, float(elapsed_s))

    def nominal_end_deadline(self, duration_us: int) -> float:
        # Explicit control waits move later event deadlines but do not add a second
        # wait at the end.
        return self.anchor_s + (int(duration_us) / self.speed_factor) / 1_000_000.0

    def nominal_end_delay(self, duration_us: int, *, now_s: float) -> float:
        return max(0.0, self.nominal_end_deadline(duration_us) - now_s)
