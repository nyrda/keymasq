"""Pure digital-threshold state machine for normalized analog values."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from keymasq.common.model.analog import AnalogActionThreshold


def threshold_key(source_id: str, index: int) -> str:
    return f"{source_id}:{index}"


def threshold_index(key: str) -> int | None:
    try:
        return int(key.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


def threshold_source_key(key: str) -> str:
    return key.rsplit(":", 1)[0]


@dataclass(frozen=True, slots=True)
class ThresholdTransition:
    """One edge produced by a threshold evaluation pass."""

    kind: Literal["activate", "release"]
    index: int
    threshold: AnalogActionThreshold


@dataclass(slots=True)
class DigitalThresholdStateMachine:
    """Evaluate hysteresis ranges while keeping active state explicit."""

    source_id: str
    active_keys: set[str]

    def evaluate(
        self,
        thresholds: list[AnalogActionThreshold],
        axis_values: dict[str, float],
        *,
        input_type: str,
    ) -> Iterator[ThresholdTransition]:
        for index, threshold in enumerate(thresholds):
            value_key = f"{threshold.axis}_signed" if input_type == "axis" else threshold.axis
            value = float(axis_values.get(value_key, 0.0))
            key = threshold_key(self.source_id, index)
            is_active = key in self.active_keys
            if not is_active and _inside(
                value,
                threshold.trigger_min,
                threshold.trigger_max,
            ):
                self.active_keys.add(key)
                yield ThresholdTransition("activate", index, threshold)
            elif is_active and not _inside(
                value,
                threshold.release_min,
                threshold.release_max,
            ):
                self.active_keys.discard(key)
                yield ThresholdTransition("release", index, threshold)


def _inside(value: float, minimum: float, maximum: float) -> bool:
    return minimum <= value <= maximum
