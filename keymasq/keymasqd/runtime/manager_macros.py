from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterator
from functools import partial
from typing import Any, cast

from keymasq.common.coercion import coerce_bool, coerce_float, coerce_int, coerce_str
from keymasq.common.ipc import CommandType
from keymasq.common.model.actions import DEFAULT_MACRO_LOOP_STOP_BEHAVIOR
from keymasq.common.types import JsonObject
from keymasq.keymasqd.runtime.macro import cleanup, controls, playback
from keymasq.keymasqd.runtime.macro.exceptions import MacroCallError
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

log = logging.getLogger("keymasqd.devices")


class MacroManagerMixin:
    """Macro playback, storage lookup, and cancellation for ``DeviceManager``."""

    macro_store: Any | None
    macro_state: MacroRuntimeState
    grabbed_devices: dict[str, list[Any]]

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

    async def start_macro_child(
        self,
        parent_instance_id: int,
        event: dict[str, object],
        *,
        deps: MacroRuntimeDeps,
    ) -> asyncio.Task[None] | None:
        """Resolve and start one dynamic child-macro timeline event."""

        macro_name = coerce_str(event.get("macro_name", "")).strip()
        if not macro_name:
            raise MacroCallError("macro call event has no macro name")
        store = self.macro_store
        if store is None:
            raise MacroCallError("macro storage is unavailable")

        try:
            meta = cast(JsonObject, await asyncio.to_thread(store.get_meta, macro_name))
            event_source = await self._stored_macro_event_source(macro_name, deps=deps)
        except FileNotFoundError as exc:
            raise MacroCallError(f"Macro '{macro_name}' not found") from exc
        if event_source is None:
            raise MacroCallError(f"Macro '{macro_name}' not found")
        if event_source.event_count <= 0:
            return None

        options = MacroPlaybackOptions(
            macro_name=macro_name,
            replay_mouse_movement=coerce_bool(event.get("replay_mouse_movement"), True),
            replay_mouse_clicks=coerce_bool(event.get("replay_mouse_clicks"), True),
            speed=max(0.01, coerce_float(event.get("speed"), 1.0)),
            loop_mode=coerce_str(event.get("loop_mode"), "none") or "none",
            loop_count=max(1, coerce_int(event.get("loop_count"), 1)),
            loop_stop_behavior=(
                coerce_str(
                    event.get("loop_stop_behavior"),
                    DEFAULT_MACRO_LOOP_STOP_BEHAVIOR,
                )
                or DEFAULT_MACRO_LOOP_STOP_BEHAVIOR
            ),
            move_to_start=coerce_bool(meta.get("move_to_start"), False),
            start_x=coerce_int(meta.get("start_x"), 0),
            start_y=coerce_int(meta.get("start_y"), 0),
            block_mouse_movement=coerce_bool(meta.get("block_mouse_movement"), False),
        )
        return playback.start_child_macro(
            self,
            options,
            parent_instance_id=parent_instance_id,
            macro_event_source=event_source,
            deps=deps,
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

    async def cancel_macro_playback_and_release_outputs(self) -> JsonObject:
        """Cancel macros, then neutralize outputs tracked by grabbed devices."""

        try:
            return await self.cancel_macro_playback()
        finally:
            for devices in self.grabbed_devices.values():
                for device in devices:
                    try:
                        device.release_tracked_outputs()
                    except OSError:
                        log.debug(
                            "Failed to release tracked outputs during emergency macro cancel",
                            exc_info=True,
                        )
                    except Exception:
                        log.exception(
                            "Unexpected failure releasing tracked outputs during "
                            "emergency macro cancel"
                        )

    def complete_macro_exec_wait(self, wait_id: str, returncode: int) -> JsonObject:
        return controls.complete_macro_exec_wait(self, wait_id, returncode)
