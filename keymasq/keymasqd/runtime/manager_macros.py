from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from functools import partial
from typing import Any, cast

from keymasq.common.coercion import coerce_int
from keymasq.common.ipc import CommandType
from keymasq.common.types import JsonObject
from keymasq.keymasqd.runtime.macro import cleanup, controls, playback
from keymasq.keymasqd.runtime.macro.options import (
    MacroPlaybackOptions,
    macro_playback_options_from_mapping,
)
from keymasq.keymasqd.runtime.macro.state import (
    MacroEventSource,
    MacroRuntimeDeps,
    MacroRuntimeState,
)

type MacroRuntimeDepsFactory = Callable[[], MacroRuntimeDeps]


class MacroManagerMixin:
    """Macro playback, storage lookup, and cancellation for ``DeviceManager``."""

    macro_store: Any | None
    macro_state: MacroRuntimeState

    def _initialize_macro_runtime(self, deps_factory: MacroRuntimeDepsFactory) -> None:
        self._macro_runtime_deps_factory = deps_factory

    def _broadcast_runtime_event(
        self,
        event_type: CommandType,
        data: JsonObject,
    ) -> None:
        raise NotImplementedError

    async def play_macro(
        self,
        playback_options: MacroPlaybackOptions | None = None,
        *,
        macro_event_source: MacroEventSource | None = None,
        **playback_kwargs: object,
    ) -> JsonObject:
        if playback_options is None:
            playback_options = macro_playback_options_from_mapping(
                playback_kwargs,
                strict=True,
            )
        elif playback_kwargs:
            raise TypeError("playback kwargs cannot be combined with playback_options")

        deps = self._macro_runtime_deps_factory()
        if (
            playback_options.load_stored_macro
            and macro_event_source is None
            and playback_options.macro_name
            and not playback_options.macro_events
        ):
            macro_event_source = await self._stored_macro_event_source(
                playback_options.macro_name,
                deps=deps,
            )
        return await playback.play_macro(
            self,
            playback_options,
            deps=deps,
            macro_event_source=macro_event_source,
        )

    async def _stored_macro_event_source(
        self,
        macro_name: str,
        *,
        deps: MacroRuntimeDeps,
    ) -> MacroEventSource | None:
        store = self.macro_store
        if store is None:
            return None
        started_ns = time.perf_counter_ns() if deps.diagnostics_recorder is not None else None
        revision = await asyncio.to_thread(store.probe_revision, macro_name)
        if revision is not None:
            cached = self.macro_state.replay_cache.get(revision)
            if cached is not None:
                initial_load_us = (
                    (time.perf_counter_ns() - started_ns) / 1000.0
                    if started_ns is not None
                    else None
                )
                return MacroEventSource(
                    event_count=cached.event_count,
                    duration_us=cached.duration_us,
                    iter_events=lambda: iter(cached.events),
                    diagnostic_initial_load_us=initial_load_us,
                    cached_events=cached.events,
                    verify_revision=revision.verify_unchanged,
                )

        snapshot = await asyncio.to_thread(store.open_snapshot, macro_name)
        initial_load_us = (
            (time.perf_counter_ns() - started_ns) / 1000.0 if started_ns is not None else None
        )
        meta = cast(JsonObject, snapshot.meta)
        event_count = coerce_int(meta.get("event_count"), 0)
        duration_us = coerce_int(meta.get("duration_us"), 0)
        snapshot_revision = snapshot.revision
        begin_cache_candidate = None
        if snapshot_revision is not None:
            begin_cache_candidate = partial(
                self.macro_state.replay_cache.begin_candidate,
                snapshot_revision,
                event_count=event_count,
                duration_us=duration_us,
            )

        return MacroEventSource(
            event_count=event_count,
            duration_us=duration_us,
            iter_events=lambda: cast(Iterator[JsonObject], snapshot.iter_events()),
            diagnostic_initial_load_us=initial_load_us,
            verify_revision=(
                snapshot_revision.verify_unchanged if snapshot_revision is not None else None
            ),
            begin_cache_candidate=begin_cache_candidate,
        )

    async def cancel_macro_playback(self) -> JsonObject:
        result = await cleanup.cancel_macro_playback(
            self,
            deps=self._macro_runtime_deps_factory(),
        )
        if bool(result.get("cancelled", False)):
            self._broadcast_runtime_event(
                CommandType.MACRO_PLAYBACK_CANCELLED,
                {"reason": "cancel_macro_playback", "cancelled": True},
            )
        return result

    def complete_macro_exec_wait(self, wait_id: str, returncode: int) -> JsonObject:
        return controls.complete_macro_exec_wait(self, wait_id, returncode)
