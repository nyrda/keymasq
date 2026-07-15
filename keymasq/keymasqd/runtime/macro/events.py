from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from keymasq.keymasqd.runtime.macro.state import (
    IntValueFn,
    MacroEventSource,
    MacroRuntimeDeps,
)


def list_macro_event_source(
    macro_events: list[dict[str, object]],
    *,
    int_value_fn: IntValueFn,
) -> MacroEventSource:
    return MacroEventSource(
        event_count=len(macro_events),
        duration_us=max((int_value_fn(event.get("t_us"), 0) for event in macro_events), default=0),
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


async def iter_macro_source_events(
    source: MacroEventSource,
    *,
    deps: MacroRuntimeDeps,
) -> AsyncIterator[dict[str, object]]:
    reader = MacroBatchReader(source)
    while True:
        batch = await deps.asyncio_mod.to_thread(reader.next_batch)
        if not batch:
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
