from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from typing import Any, cast

from keymasq.common.coercion import coerce_int
from keymasq.common.ipc import CommandType
from keymasq.common.types import JsonObject
from keymasq.keymasqd.runtime.macro import cleanup, controls, playback
from keymasq.keymasqd.runtime.macro.options import (
    MacroPlaybackOptions,
    macro_playback_options_from_mapping,
)
from keymasq.keymasqd.runtime.macro.state import MacroEventSource, MacroRuntimeDeps

type MacroRuntimeDepsFactory = Callable[[], MacroRuntimeDeps]


class MacroManagerMixin:
    """Macro playback, storage lookup, and cancellation for ``DeviceManager``."""

    macro_store: Any | None

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

        if (
            playback_options.load_stored_macro
            and macro_event_source is None
            and playback_options.macro_name
            and not playback_options.macro_events
        ):
            macro_event_source = await self._stored_macro_event_source(playback_options.macro_name)
        return await playback.play_macro(
            self,
            playback_options,
            deps=self._macro_runtime_deps_factory(),
            macro_event_source=macro_event_source,
        )

    async def _stored_macro_event_source(
        self,
        macro_name: str,
    ) -> MacroEventSource | None:
        store = self.macro_store
        if store is None:
            return None
        open_snapshot = getattr(store, "open_snapshot", None)
        if callable(open_snapshot):
            snapshot = await asyncio.to_thread(open_snapshot, macro_name)
            meta_raw = getattr(snapshot, "meta", None)
            iter_snapshot_events = getattr(snapshot, "iter_events", None)
            close_snapshot = getattr(snapshot, "close", None)
            if not isinstance(meta_raw, dict) or not callable(iter_snapshot_events):
                if callable(close_snapshot):
                    close_snapshot()
                return None
            meta = cast(JsonObject, meta_raw)
            close_callback = (
                cast(Callable[[], None], close_snapshot) if callable(close_snapshot) else None
            )
            return MacroEventSource(
                event_count=coerce_int(meta.get("event_count"), 0),
                duration_us=coerce_int(meta.get("duration_us"), 0),
                iter_events=lambda: cast(Iterator[JsonObject], iter_snapshot_events()),
                close=close_callback,
            )

        get_meta = getattr(store, "get_meta", None)
        iter_events = getattr(store, "iter_events", None)
        if not callable(get_meta) or not callable(iter_events):
            return None

        meta_raw = await asyncio.to_thread(get_meta, macro_name)
        if not isinstance(meta_raw, dict):
            return None
        meta = cast(JsonObject, meta_raw)

        def iter_stored_events() -> Iterator[JsonObject]:
            return cast(Iterator[JsonObject], iter_events(macro_name))

        return MacroEventSource(
            event_count=coerce_int(meta.get("event_count"), 0),
            duration_us=coerce_int(meta.get("duration_us"), 0),
            iter_events=iter_stored_events,
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
