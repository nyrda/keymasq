from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from typing import Any

from keymasq.common.types import JsonObject

DIAGNOSTICS_CATEGORIES = frozenset({"mainline", "combo", "internal"})
DEFAULT_DIAGNOSTICS_CATEGORIES = frozenset({"mainline"})

type DiagnosticsSummary = dict[str, JsonObject]
type LoopFactory = Callable[[], Coroutine[Any, Any, None]]
type Sleep = Callable[[float], Awaitable[None]]
type Summarize = Callable[[dict[str, list[float]]], DiagnosticsSummary]
type SummaryConsumer = Callable[[DiagnosticsSummary], object]
type ToThread = Callable[..., Awaitable[Any]]


@dataclass
class DiagnosticsState:
    enabled: bool = False
    interval: float = 5.0
    categories: set[str] = field(default_factory=lambda: {"mainline"})
    task: asyncio.Task[None] | None = None
    samples: dict[str, deque[float]] = field(default_factory=dict)


def normalize_categories(categories: Sequence[object] | None) -> set[str]:
    if not categories:
        return set(DEFAULT_DIAGNOSTICS_CATEGORIES)

    normalized = {
        str(category or "").strip().lower()
        for category in categories
        if str(category or "").strip()
    }
    if "all" in normalized:
        return set(DIAGNOSTICS_CATEGORIES)
    selected = normalized & DIAGNOSTICS_CATEGORIES
    return selected or set(DEFAULT_DIAGNOSTICS_CATEGORIES)


def label_enabled(label: str, categories: set[str]) -> bool:
    normalized = str(label or "").lower()
    if "internal" in categories and _label_is_internal(normalized):
        return True
    if "combo" in categories and _label_is_combo(normalized):
        return True
    return "mainline" in categories and _label_is_mainline(normalized)


def _label_is_mainline(label: str) -> bool:
    return (
        label.startswith("action_")
        or label
        in {
            "passthrough_fast",
            "passthrough_mapped",
            "passthrough_other",
            "passthrough_syn",
        }
        or label == "wheel_passthrough"
    )


def _label_is_combo(label: str) -> bool:
    return label.startswith("combo_") and not label.startswith("combo_recalled_")


def _label_is_internal(label: str) -> bool:
    return label == "syn" or label in {
        "combo_recalled_repeat_suppressed",
        "combo_recalled_release_suppressed",
        "wheel_high_res_suppressed",
    }


def summarize(snapshot: dict[str, list[float]]) -> DiagnosticsSummary:
    def percentile(values: list[float], proportion: float) -> float:
        if not values:
            return 0.0
        index = int((len(values) - 1) * proportion)
        return values[max(0, min(index, len(values) - 1))]

    summary: DiagnosticsSummary = {}
    for label, samples in snapshot.items():
        if not samples:
            continue
        values = sorted(samples)
        summary[label] = {
            "n": len(values),
            "p50": percentile(values, 0.50),
            "p95": percentile(values, 0.95),
            "p99": percentile(values, 0.99),
            "max": values[-1],
        }
    return summary


def _int_value(value: object) -> int:
    if isinstance(value, (int, float, str, bytes)):
        return int(value)
    return 0


def _float_value(value: object) -> float:
    if isinstance(value, (int, float, str, bytes)):
        return float(value)
    return 0.0


def log_summary(logger: logging.Logger, summary: DiagnosticsSummary) -> None:
    for label, stats in summary.items():
        logger.info(
            "diagnostics[%s]: n=%d p50=%.2fus p95=%.2fus p99=%.2fus max=%.2fus",
            label,
            _int_value(stats.get("n", 0)),
            _float_value(stats.get("p50", 0.0)),
            _float_value(stats.get("p95", 0.0)),
            _float_value(stats.get("p99", 0.0)),
            _float_value(stats.get("max", 0.0)),
        )


class DiagnosticsRuntime:
    """Owns diagnostics configuration, sample collection, and loop state."""

    def __init__(self, logger: logging.Logger) -> None:
        self.state = DiagnosticsState()
        self._logger = logger

    async def configure(
        self,
        enabled: bool,
        interval: float,
        categories: Sequence[object] | None,
        *,
        loop_factory: LoopFactory,
    ) -> JsonObject:
        state = self.state
        state.enabled = bool(enabled)
        state.interval = max(0.5, float(interval or 5.0))
        state.categories = normalize_categories(categories)
        state.samples.clear()

        if not state.enabled:
            if state.task:
                state.task.cancel()
                try:
                    await state.task
                except asyncio.CancelledError:
                    pass
                state.task = None
            self._logger.info("Diagnostics disabled")
        elif state.task is None or state.task.done():
            state.task = asyncio.create_task(loop_factory())

        if state.enabled:
            self._logger.info(
                "Diagnostics enabled (interval %.2fs, categories=%s)",
                state.interval,
                ",".join(sorted(state.categories)),
            )
        return {
            "enabled": state.enabled,
            "interval": state.interval,
            "categories": sorted(state.categories),
        }

    def record(self, label: str, duration_us: float) -> None:
        state = self.state
        if not state.enabled or not label_enabled(label, state.categories):
            return
        state.samples.setdefault(label, deque(maxlen=20000)).append(float(duration_us))

    async def run(
        self,
        *,
        sleep: Sleep,
        to_thread: ToThread,
        summarize_snapshot: Summarize,
        publish_summary: SummaryConsumer,
        write_summary: SummaryConsumer,
    ) -> None:
        try:
            while self.state.enabled:
                await sleep(self.state.interval)
                snapshot = {
                    label: list(samples) for label, samples in self.state.samples.items() if samples
                }
                if not snapshot:
                    continue
                summary = await to_thread(summarize_snapshot, snapshot)
                publish_summary(summary)
                await to_thread(write_summary, summary)
        except asyncio.CancelledError:
            raise
