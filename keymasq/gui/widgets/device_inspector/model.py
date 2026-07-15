from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

Payload = dict[str, Any]

EVENT_ROW_LIMIT = 100
EVENT_FILTERS: tuple[tuple[str, str, bool], ...] = (
    ("button", "Keys", True),
    ("axis", "Axes", False),
    ("mousemove", "Move", False),
    ("syn", "Syn", False),
    ("other", "Other", False),
)

DEFAULT_STICK_MIN = -32768
DEFAULT_STICK_MAX = 32767
DEFAULT_AXIS_MIN = 0
DEFAULT_AXIS_MAX = 255


@dataclass
class EventHistory:
    """Bounded, category-aware inspector event history."""

    limit: int = EVENT_ROW_LIMIT
    by_category: dict[str, list[Payload]] = field(
        default_factory=lambda: {filter_id: [] for filter_id, _label, _active in EVENT_FILTERS}
    )
    order: int = 0

    def add(self, event: Payload) -> str:
        category = event_category(event)
        self.order += 1
        stored = dict(event)
        stored["_inspector_order"] = self.order
        history = self.by_category.setdefault(category, [])
        history.insert(0, stored)
        del history[self.limit :]
        return category

    def visible(self, active_categories: set[str]) -> list[Payload]:
        events = [
            event
            for category, history in self.by_category.items()
            if category in active_categories
            for event in history
        ]
        events.sort(
            key=lambda event: int_or_none(event.get("_inspector_order")) or 0,
            reverse=True,
        )
        return events[: self.limit]

    def export(self, active_categories: set[str]) -> str:
        return "\n".join(event_export_line(event) for event in self.visible(active_categories))


def text(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def int_or_none(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(cast(int | float | str | bytes, value))
    except (TypeError, ValueError):
        return None


def list_of_dicts(value: object) -> list[Payload]:
    if not isinstance(value, list):
        return []
    return [cast(Payload, item) for item in value if isinstance(item, dict)]


def ellipsize_middle(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    if max_chars <= 3:
        return value[:max_chars]
    head = max(1, (max_chars - 3) // 2)
    tail = max(1, max_chars - 3 - head)
    return f"{value[:head]}...{value[-tail:]}"


def event_category(event: Payload) -> str:
    event_type = text(event.get("type_name"), text(event.get("type"))).lower()
    code_name = text(event.get("code_name"), text(event.get("code"))).lower()
    if event_type in {"ev_key", "1"}:
        return "button"
    if event_type in {"ev_syn", "0"} or (
        event_type in {"ev_msc", "4"} and code_name in {"msc_scan", "4"}
    ):
        return "syn"
    if event_type in {"ev_abs", "3"}:
        return "axis"
    if event_type in {"ev_rel", "2"}:
        if code_name in {"rel_x", "rel_y"}:
            return "mousemove"
        return "axis"
    return "other"


def event_export_line(event: Payload) -> str:
    sequence = int(event.get("sequence", 0) or 0)
    code_name = text(event.get("code_name"), text(event.get("code"), "unknown"))
    event_type = text(event.get("type_name"), text(event.get("type"), "unknown"))
    value = text(event.get("value"), "0")
    source = text(event.get("source"))
    parts = [f"#{sequence}" if sequence else "#-", code_name, event_type, f"value={value}"]
    if source:
        parts.append(f"source={source}")
    return " ".join(parts)


def axis_min_max(axis: Payload, analog_type: str) -> tuple[int, int]:
    minimum = int_or_none(axis.get("minimum"))
    maximum = int_or_none(axis.get("maximum"))
    if minimum is None or maximum is None or maximum <= minimum:
        if analog_type == "axis":
            return DEFAULT_AXIS_MIN, DEFAULT_AXIS_MAX
        return DEFAULT_STICK_MIN, DEFAULT_STICK_MAX
    return minimum, maximum


def normalize_axis(axis: Payload, value: int, analog_type: str) -> float:
    minimum, maximum = axis_min_max(axis, analog_type)
    if analog_type == "axis":
        rest = int_or_none(axis.get("rest"))
        if rest is None:
            rest = minimum if minimum >= 0 else 0
        positive_span = float(maximum) - float(rest)
        negative_span = float(minimum) - float(rest)
        active_span = positive_span if abs(positive_span) >= abs(negative_span) else negative_span
        if abs(active_span) < 1.0:
            active_span = float(maximum) - float(minimum)
        if abs(active_span) < 1.0:
            return 0.0
        normalized = (float(value) - float(rest)) / active_span
        return max(0.0, min(1.0, normalized))

    center = int_or_none(axis.get("center"))
    midpoint = float(center) if center is not None else (float(minimum) + float(maximum)) / 2.0
    raw = float(value)
    if raw < midpoint:
        span = max(1.0, midpoint - float(minimum))
        normalized = (raw - midpoint) / span
    else:
        span = max(1.0, float(maximum) - midpoint)
        normalized = (raw - midpoint) / span
    if bool(axis.get("invert", False)):
        normalized = -normalized
    return max(-1.0, min(1.0, normalized))


def level_bar_value(analog_type: str, normalized: float) -> float:
    if analog_type == "axis":
        return max(0.0, min(1.0, normalized))
    return max(0.0, min(1.0, (normalized + 1.0) / 2.0))


def axis_value_label(role: str, raw_value: int, normalized: float) -> str:
    return f"{role}: raw {raw_value:6d} | norm {normalized:+.3f}"
