from __future__ import annotations

import heapq
import time
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

from keymasq.common.coercion import coerce_int
from keymasq.common.macro_rapidfire import macro_event_end_us, macro_rapidfire_events
from keymasq.keymasqd.runtime.macro.state import (
    MacroEventSource,
    MacroRuntimeDeps,
)


def list_macro_event_source(
    macro_events: list[dict[str, object]],
) -> MacroEventSource:
    return MacroEventSource(
        event_count=len(macro_events),
        duration_us=max((macro_event_end_us(event) for event in macro_events), default=0),
        iter_events=lambda: iter(macro_events),
    )


class MacroBatchReader:
    """Synchronous batch reader used behind ``asyncio.to_thread``."""

    def __init__(self, source: MacroEventSource, batch_size: int = 128) -> None:
        self._iterator = source.iter_events()
        self._batch_size = max(1, int(batch_size))

    def next_batch(self) -> list[dict[str, object]]:
        batch: list[dict[str, object]] = []
        for _ in range(self._batch_size):
            try:
                batch.append(next(self._iterator))
            except StopIteration:
                break
        return batch


async def expand_macro_rapidfire(
    source: AsyncIterator[dict[str, object]],
    *,
    observe: Callable[[dict[str, object]], None] | None = None,
) -> AsyncIterator[dict[str, object]]:
    """Merge active pulse streams with source events, preserving equal-time source order.

    Keep only the next edge of each active block. Cache the original source,
    never its generated pulses, so cache size stays proportional to stored data.
    """
    pending: list[tuple[int, int, dict[str, object], Iterator[dict[str, object]]]] = []

    def advance(order: int, pulses: Iterator[dict[str, object]]) -> None:
        event = next(pulses, None)
        if event is not None:
            heapq.heappush(pending, (coerce_int(event.get("t_us"), 0), order, event, pulses))

    order = 0
    async for event in source:
        if observe is not None:
            observe(event)
        timestamp = coerce_int(event.get("t_us"), 0)
        while pending and pending[0][:2] <= (timestamp, order):
            _, pulse_order, pulse, pulses = heapq.heappop(pending)
            yield pulse
            advance(pulse_order, pulses)
        if event.get("macro_action") == "macro_rapidfire":
            advance(order, macro_rapidfire_events(event))
        else:
            yield event
        order += 1
    while pending:
        _, pulse_order, pulse, pulses = heapq.heappop(pending)
        yield pulse
        advance(pulse_order, pulses)


async def iter_macro_source_events(
    source: MacroEventSource,
    *,
    deps: MacroRuntimeDeps,
    diagnostic_initial_load_us: float | None = None,
    cached_events: tuple[dict[str, object], ...] | None = None,
    verify_cached_revision: bool = False,
) -> AsyncIterator[dict[str, object]]:
    recorder = deps.diagnostics_recorder
    measure_load = recorder is not None and diagnostic_initial_load_us is not None
    load_us = float(diagnostic_initial_load_us or 0.0)

    if cached_events is not None:
        if verify_cached_revision and source.verify_revision is not None:
            started_ns = time.perf_counter_ns() if measure_load else None
            await deps.asyncio_mod.to_thread(source.verify_revision)
            if started_ns is not None:
                load_us += (time.perf_counter_ns() - started_ns) / 1000.0
        if measure_load and recorder is not None:
            recorder("macro_load", load_us)
        for event in cached_events:
            yield event
        return

    reader = MacroBatchReader(source)
    while True:
        started_ns = time.perf_counter_ns() if measure_load else None
        batch = await deps.asyncio_mod.to_thread(reader.next_batch)
        if started_ns is not None:
            load_us += (time.perf_counter_ns() - started_ns) / 1000.0
        if not batch:
            if measure_load and recorder is not None:
                recorder("macro_load", load_us)
            break
        for event in batch:
            yield event


def is_wheel_event(event_type: int, event_code: int, *, evdev_mod: Any) -> bool:
    ecodes = evdev_mod.ecodes
    if int(event_type) != int(ecodes.EV_REL):
        return False
    return int(event_code) in {
        int(ecodes.REL_WHEEL),
        int(ecodes.REL_HWHEEL),
        *(
            int(code)
            for code in (
                getattr(ecodes, "REL_WHEEL_HI_RES", None),
                getattr(ecodes, "REL_HWHEEL_HI_RES", None),
            )
            if code is not None
        ),
    }
