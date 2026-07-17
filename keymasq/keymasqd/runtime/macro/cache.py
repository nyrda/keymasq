from __future__ import annotations

import sys
from collections import OrderedDict
from dataclasses import dataclass
from typing import cast

from keymasq.keymasqd.macro_file import MacroFileRevision

DEFAULT_MACRO_CACHE_BYTES = 10 * 1024 * 1024
MAX_CACHEABLE_MACRO_RUNTIME_US = 2_000_000.0
MAX_INELIGIBLE_REVISIONS = 256
_EVENT_SLOT_BYTES = sys.getsizeof((None,)) - sys.getsizeof(())

type MacroEvent = dict[str, object]


@dataclass(frozen=True)
class MacroCacheEntry:
    revision: MacroFileRevision
    event_count: int
    duration_us: int
    events: tuple[MacroEvent, ...]
    weight: int


class MacroCacheCandidate:
    """One streamed iteration being considered for cache admission."""

    def __init__(
        self,
        cache: MacroReplayCache,
        revision: MacroFileRevision,
        *,
        event_count: int,
        duration_us: int,
    ) -> None:
        self._cache = cache
        self.revision = revision
        self.event_count = max(0, int(event_count))
        self.duration_us = max(0, int(duration_us))
        self._events: list[MacroEvent] = []
        self._weight = 0
        self._active = True

    @property
    def active(self) -> bool:
        return self._active

    def observe(self, event: MacroEvent) -> None:
        if not self._active:
            return
        weight = _estimate_event_heap_bytes(event)
        if not self._cache.reserve_candidate_bytes(weight):
            self._finish(ineligible=True)
            return
        self._events.append(event)
        self._weight += weight

    def commit(self) -> MacroCacheEntry | None:
        if not self._active:
            return None
        if len(self._events) != self.event_count:
            self._finish(ineligible=True)
            return None

        events = tuple(self._events)
        entry = MacroCacheEntry(
            revision=self.revision,
            event_count=self.event_count,
            duration_us=self.duration_us,
            events=events,
            weight=self._weight,
        )
        self._cache.commit_candidate(entry, candidate_bytes=self._weight)
        self._events.clear()
        self._weight = 0
        self._active = False
        return entry

    def reject(self) -> None:
        self._finish(ineligible=True)

    def discard(self) -> None:
        self._finish(ineligible=False)

    def _finish(self, *, ineligible: bool) -> None:
        if not self._active:
            return
        self._cache.release_candidate(
            self.revision,
            self._weight,
            ineligible=ineligible,
        )
        self._events.clear()
        self._weight = 0
        self._active = False


class MacroReplayCache:
    """Daemon-wide LRU of parsed stored-macro revisions."""

    def __init__(self, max_bytes: int = DEFAULT_MACRO_CACHE_BYTES) -> None:
        self.max_bytes = max(0, int(max_bytes))
        self._entries: OrderedDict[MacroFileRevision, MacroCacheEntry] = OrderedDict()
        self._ineligible: OrderedDict[MacroFileRevision, None] = OrderedDict()
        self._resident_bytes = 0
        self._candidate_bytes = 0

    @property
    def total_bytes(self) -> int:
        return self._resident_bytes + self._candidate_bytes

    def get(self, revision: MacroFileRevision) -> MacroCacheEntry | None:
        self._discard_other_revisions(revision)
        entry = self._entries.pop(revision, None)
        if entry is None:
            return None
        self._entries[revision] = entry
        return entry

    def begin_candidate(
        self,
        revision: MacroFileRevision,
        *,
        event_count: int,
        duration_us: int,
    ) -> MacroCacheCandidate | None:
        self._discard_other_revisions(revision)
        if (
            self.max_bytes <= 0
            or int(event_count) <= 0
            or revision in self._entries
            or revision in self._ineligible
        ):
            return None
        return MacroCacheCandidate(
            self,
            revision,
            event_count=event_count,
            duration_us=duration_us,
        )

    def reserve_candidate_bytes(self, weight: int) -> bool:
        required = max(0, int(weight))
        while self.total_bytes + required > self.max_bytes and self._entries:
            _, evicted = self._entries.popitem(last=False)
            self._resident_bytes -= evicted.weight
        if self.total_bytes + required > self.max_bytes:
            return False
        self._candidate_bytes += required
        return True

    def commit_candidate(self, entry: MacroCacheEntry, *, candidate_bytes: int) -> None:
        self._candidate_bytes = max(0, self._candidate_bytes - max(0, candidate_bytes))
        previous = self._entries.pop(entry.revision, None)
        if previous is not None:
            self._resident_bytes -= previous.weight
        self._entries[entry.revision] = entry
        self._resident_bytes += entry.weight
        self._ineligible.pop(entry.revision, None)

    def release_candidate(
        self,
        revision: MacroFileRevision,
        weight: int,
        *,
        ineligible: bool,
    ) -> None:
        self._candidate_bytes = max(0, self._candidate_bytes - max(0, int(weight)))
        if not ineligible:
            return
        self._ineligible.pop(revision, None)
        self._ineligible[revision] = None
        while len(self._ineligible) > MAX_INELIGIBLE_REVISIONS:
            self._ineligible.popitem(last=False)

    def _discard_other_revisions(self, current: MacroFileRevision) -> None:
        for revision in tuple(self._entries):
            if revision.path == current.path and revision != current:
                entry = self._entries.pop(revision)
                self._resident_bytes -= entry.weight
        for revision in tuple(self._ineligible):
            if revision.path == current.path and revision != current:
                self._ineligible.pop(revision, None)


def _estimate_event_heap_bytes(event: MacroEvent) -> int:
    """Conservatively account for one independently parsed JSON event."""

    def estimate(value: object) -> int:
        size = sys.getsizeof(value)
        if isinstance(value, dict):
            mapping = cast(dict[object, object], value)
            size += sum(estimate(key) + estimate(item) for key, item in mapping.items())
        elif isinstance(value, (list, tuple)):
            sequence = cast(list[object] | tuple[object, ...], value)
            size += sum(estimate(item) for item in sequence)
        return size

    return _EVENT_SLOT_BYTES + estimate(event)
