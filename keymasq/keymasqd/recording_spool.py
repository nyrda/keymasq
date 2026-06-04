import asyncio
import json
import os
import tempfile
import threading
import uuid
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

type RecordingEvent = dict[str, object]

DEFAULT_MEMORY_EVENT_LIMIT = 8192
DEFAULT_MEMORY_BYTE_LIMIT = 2 * 1024 * 1024
DEFAULT_MAX_PENDING_FLUSH_CHUNKS = 8


@dataclass(frozen=True)
class RecordingSnapshot:
    recording_id: str
    duration_ms: int
    device_types: list[str]
    event_count: int
    spool_path: Path | None
    memory_events: tuple[RecordingEvent, ...]
    recording_slot: int = 0
    cleanup_paths: tuple[Path, ...] = ()

    def iter_events(self) -> Iterator[RecordingEvent]:
        if self.spool_path is not None:
            with self.spool_path.open("r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    value = json.loads(stripped)
                    if isinstance(value, dict):
                        yield cast(RecordingEvent, value)
        yield from self.memory_events

    def cleanup(self) -> None:
        cleanup_paths: list[Path] = []
        if self.spool_path is not None:
            cleanup_paths.append(self.spool_path)
        cleanup_paths.extend(self.cleanup_paths)
        seen: set[Path] = set()
        for path in cleanup_paths:
            if path in seen:
                continue
            seen.add(path)
            path.unlink(missing_ok=True)


class RecordingSpool:
    """Bounded recording event buffer with asynchronous uncompressed spill files.

    RecordingSpool is owned by one asyncio event loop. The lock only serializes
    defensive concurrent append calls; async lifecycle methods stay loop-owned.
    """

    def __init__(
        self,
        spool_dir: Path,
        *,
        memory_event_limit: int = DEFAULT_MEMORY_EVENT_LIMIT,
        memory_byte_limit: int = DEFAULT_MEMORY_BYTE_LIMIT,
        max_pending_flush_chunks: int = DEFAULT_MAX_PENDING_FLUSH_CHUNKS,
    ) -> None:
        self.spool_dir = spool_dir
        self.memory_event_limit = max(1, int(memory_event_limit))
        self.memory_byte_limit = max(1, int(memory_byte_limit))
        self.max_pending_flush_chunks = max(1, int(max_pending_flush_chunks))
        self._memory_events: list[RecordingEvent] = []
        self._memory_bytes = 0
        self._append_lock = threading.Lock()
        self._pending_flush_chunks: deque[list[RecordingEvent]] = deque()
        self._flush_task: asyncio.Task[None] | None = None
        self._spool_path: Path | None = None
        self._fatal_error: BaseException | None = None
        self.event_count = 0
        self.duration_ms = 0
        self.device_types: set[str] = set()

    @property
    def failed(self) -> BaseException | None:
        return self._fatal_error

    def append(self, event: RecordingEvent) -> None:
        with self._append_lock:
            if self._fatal_error is not None:
                return

            self._memory_events.append(event)
            self._memory_bytes += _estimate_event_bytes(event)
            self.event_count += 1
            self.duration_ms = max(self.duration_ms, int(_event_t_us(event) / 1000))
            self.device_types.add(str(event.get("device_type", "other")))

            if (
                len(self._memory_events) >= self.memory_event_limit
                or self._memory_bytes >= self.memory_byte_limit
            ):
                self._queue_memory_chunk()

    async def finish(self) -> RecordingSnapshot:
        try:
            while True:
                if self._fatal_error is not None:
                    raise RuntimeError(
                        f"Recording spool failed: {self._fatal_error}"
                    ) from self._fatal_error

                task = self._flush_task
                if task is not None:
                    await task

                if self._fatal_error is not None:
                    raise RuntimeError(
                        f"Recording spool failed: {self._fatal_error}"
                    ) from self._fatal_error

                if self._spool_path is not None and self._memory_events:
                    self._queue_memory_chunk()
                    continue
                break
        except Exception:
            await self.discard()
            raise

        snapshot = RecordingSnapshot(
            recording_id=uuid.uuid4().hex,
            duration_ms=int(self.duration_ms),
            device_types=sorted(self.device_types),
            event_count=int(self.event_count),
            spool_path=self._spool_path,
            memory_events=tuple(self._memory_events),
        )
        self._memory_events = []
        self._memory_bytes = 0
        self._spool_path = None
        return snapshot

    async def discard(self) -> None:
        self._memory_events = []
        self._memory_bytes = 0
        self._pending_flush_chunks.clear()
        task = self._flush_task
        if task is not None:
            await task
        self._flush_task = None
        if self._spool_path is not None:
            self._spool_path.unlink(missing_ok=True)
            self._spool_path = None

    def _queue_memory_chunk(self) -> None:
        if not self._memory_events:
            return
        if len(self._pending_flush_chunks) >= self.max_pending_flush_chunks:
            self._fatal_error = MemoryError("Recording spool writer fell behind")
            return
        self._pending_flush_chunks.append(self._memory_events)
        self._memory_events = []
        self._memory_bytes = 0
        self._ensure_flush_task()

    def _ensure_flush_task(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            self._fatal_error = exc
            return
        self._flush_task = loop.create_task(self._flush_pending_chunks())

    async def _flush_pending_chunks(self) -> None:
        try:
            while self._pending_flush_chunks:
                chunk = self._pending_flush_chunks.popleft()
                await asyncio.to_thread(self._write_chunk, chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - command handling logs the recorded fatal error.
            self._fatal_error = exc
        finally:
            if self._flush_task is asyncio.current_task():
                self._flush_task = None
            if self._pending_flush_chunks and self._fatal_error is None:
                self._ensure_flush_task()

    def _write_chunk(self, chunk: list[RecordingEvent]) -> None:
        path = self._ensure_spool_path()
        with path.open("a", encoding="utf-8") as f:
            for event in chunk:
                f.write(json.dumps(event, separators=(",", ":")))
                f.write("\n")

    def _ensure_spool_path(self) -> Path:
        if self._spool_path is not None:
            return self._spool_path

        self.spool_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.spool_dir, 0o700)
        fd, raw_path = tempfile.mkstemp(
            prefix="recording-",
            suffix=".jsonl",
            dir=self.spool_dir,
            text=True,
        )
        os.close(fd)
        path = Path(raw_path)
        path.chmod(0o600)
        self._spool_path = path
        return path


def _event_t_us(event: RecordingEvent) -> int:
    value = event.get("t_us", 0)
    return value if isinstance(value, int) else 0


def _estimate_event_bytes(event: RecordingEvent) -> int:
    size = 64
    for key, value in event.items():
        size += len(str(key)) + len(str(value)) + 8
    return size
