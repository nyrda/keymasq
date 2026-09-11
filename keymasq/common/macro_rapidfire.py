"""Fixed-duration rapidfire blocks shared by macro playback and the editor."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from fractions import Fraction

from keymasq.common.coercion import coerce_int
from keymasq.common.model.actions import (
    DEFAULT_RAPIDFIRE_HOLD_MS,
    DEFAULT_RAPIDFIRE_WAIT_MS,
    MIN_RAPIDFIRE_WAIT_MS,
    clamp_rapidfire_hold_ms,
    clamp_rapidfire_wait_ms,
)


@dataclass(frozen=True)
class MacroRapidfirePlan:
    duration_us: int
    hold_us: int
    count: int

    @property
    def wait_us(self) -> float:
        if self.count == 1:
            return 0.0
        return (self.duration_us - self.count * self.hold_us) / (self.count - 1)

    def pulse(self, index: int) -> tuple[int, int]:
        """Round absolute offsets, avoiding accumulated interval rounding error."""
        if self.count == 1:
            return 0, self.duration_us
        gaps = self.count - 1
        press = ((self.duration_us - self.hold_us) * index + gaps // 2) // gaps
        return press, press + self.hold_us


def plan_macro_rapidfire(duration_us: int, hold_ms: int, wait_ms: int) -> MacroRapidfirePlan:
    duration = max(0, int(duration_us))
    hold = clamp_rapidfire_hold_ms(hold_ms) * 1000
    wait = clamp_rapidfire_wait_ms(wait_ms) * 1000
    min_wait = MIN_RAPIDFIRE_WAIT_MS * 1000
    max_count = (duration + min_wait) // (hold + min_wait)
    if max_count < 2:
        return MacroRapidfirePlan(duration, duration, 1)

    # D = N*H + (N-1)*W. The closest adjusted wait is on either side
    # of this ideal count; ties prefer fewer presses.
    below = (duration + wait) // (hold + wait)
    candidates = {max(2, min(max_count, n)) for n in (below, below + 1)}
    count = min(
        candidates,
        key=lambda n: (abs(Fraction(duration - n * hold, n - 1) - wait), n),
    )
    return MacroRapidfirePlan(duration, hold, count)


def macro_event_end_us(event: Mapping[str, object]) -> int:
    start = coerce_int(event.get("t_us"), 0)
    if event.get("macro_action") == "macro_rapidfire":
        return start + max(0, coerce_int(event.get("duration_us"), 0))
    return start


def macro_rapidfire_events(event: Mapping[str, object]) -> Iterator[dict[str, object]]:
    """Expand one stored block lazily; the caller merges it into the timeline."""
    if event.get("device_type") not in {"keyboard", "mouse", "gamepad"}:
        raise ValueError("Macro rapidfire requires a keyboard, mouse, or gamepad button")
    if coerce_int(event.get("type"), 0) != 1:
        raise ValueError("Macro rapidfire requires an EV_KEY target")
    plan = plan_macro_rapidfire(
        coerce_int(event.get("duration_us"), 0),
        coerce_int(event.get("rapidfire_hold_ms"), DEFAULT_RAPIDFIRE_HOLD_MS),
        coerce_int(event.get("rapidfire_wait_ms"), DEFAULT_RAPIDFIRE_WAIT_MS),
    )
    start = coerce_int(event.get("t_us"), 0)
    target = {key: event[key] for key in ("device_type", "type", "code")}
    if event.get("output_id"):
        target["output_id"] = event["output_id"]
    for index in range(plan.count):
        press, release = plan.pulse(index)
        yield {**target, "value": 1, "t_us": start + press}
        yield {**target, "value": 0, "t_us": start + release}
