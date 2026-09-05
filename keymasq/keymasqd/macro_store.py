import copy
import fcntl
import json
import logging
import lzma
import os
import re
import threading
from collections.abc import Callable, Generator, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from keymasq.keymasqd.macro_file import (
    MACRO_FILE_SUFFIX,
    MacroFileMeta,
    MacroFileRevision,
    MacroFileSnapshot,
    iter_macro_events,
    load_macro,
    macro_payload_from_events,
    macro_payload_int,
    macro_payload_str,
    read_macro_meta,
    write_macro,
)

INTERNAL_MACRO_PREFIX = "__"
_MUTATION_LOCK_NAME = ".macro-store.lock"
_PROCESS_MUTATION_LOCK = threading.Lock()
log = logging.getLogger("keymasqd.macros")
type MacroEvent = dict[str, object]
type MacroPayload = dict[str, object]


@dataclass(frozen=True)
class MacroStoreSnapshot:
    meta: MacroPayload
    iter_events: Callable[[], Iterator[MacroEvent]]
    revision: MacroFileRevision | None = None


def _payload_list(payload: MacroPayload, key: str) -> list[object]:
    value = payload.get(key, [])
    return cast(list[object], value) if isinstance(value, list) else []


class MacroStore:
    """Central store for macro data, persisted as compressed JSONL files.

    Persistent macros are stored as ``*.kmacro.xz`` files under ``base_dir``.
    The first JSONL record contains macro metadata; each following record is
    one macro event. Stored playback can iterate event records without loading
    the full macro into memory. GUI/editor operations may still call ``get()``
    to load a complete canonical macro payload.

    Internal macros (names prefixed with ``__``) are registered in memory and
    are not written to disk.
    """

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self._internal_macros: dict[str, MacroPayload] = {}

    def register_internal(self, name: str, events: list[MacroEvent], **extra: object) -> None:
        if not name.startswith(INTERNAL_MACRO_PREFIX):
            raise ValueError(f"Internal macro names must start with {INTERNAL_MACRO_PREFIX}")
        self._internal_macros[name] = copy.deepcopy(
            macro_payload_from_events(
                {"name": name, "internal": True, **extra},
                events,
                name=name,
            )
        )

    def is_internal(self, name: str) -> bool:
        return name in self._internal_macros

    def ensure(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.base_dir, 0o700)

    def list_meta(self) -> list[MacroPayload]:
        self.ensure()
        macros: list[MacroPayload] = []
        for path in sorted(self.base_dir.glob(f"*{MACRO_FILE_SUFFIX}")):
            try:
                meta = read_macro_meta(path)
                if meta.name.startswith(INTERNAL_MACRO_PREFIX):
                    continue
                macros.append(meta.to_payload())
            except FileNotFoundError as exc:
                log.debug("Macro file disappeared while listing %s: %s", path, exc)
            except PermissionError as exc:
                log.warning(
                    "Skipping unreadable macro file %s: %s. Check macro file ownership and "
                    "permissions for the keymasqd user.",
                    path,
                    exc,
                )
            except OSError as exc:
                log.warning("Skipping unreadable macro file %s: %s", path, exc)
            except lzma.LZMAError as exc:
                log.warning("Skipping corrupt compressed macro file %s: %s", path, exc)
            except json.JSONDecodeError as exc:
                log.warning("Skipping malformed macro file %s: invalid JSON: %s", path, exc)
            except UnicodeDecodeError as exc:
                log.warning("Skipping malformed macro file %s: invalid UTF-8: %s", path, exc)
            except ValueError as exc:
                log.warning("Skipping malformed macro file %s: %s", path, exc)
            except Exception:
                log.exception("Unexpected failure reading macro file %s", path)
        return macros

    def get(self, name: str) -> MacroPayload:
        if name in self._internal_macros:
            return copy.deepcopy(self._internal_macros[name])
        path = self._macro_path(name)
        if not path.exists():
            raise FileNotFoundError(f"Macro '{name}' not found")
        return load_macro(path)

    def get_meta(self, name: str) -> MacroPayload:
        if name in self._internal_macros:
            data = self._internal_macros[name]
            return MacroFileMeta.from_payload(data, name=name).to_payload()
        path = self._macro_path(name)
        if not path.exists():
            raise FileNotFoundError(f"Macro '{name}' not found")
        return read_macro_meta(path).to_payload()

    def iter_events(self, name: str) -> Iterator[MacroEvent]:
        if name in self._internal_macros:
            events = _payload_events(self._internal_macros[name])
            return iter(copy.deepcopy(events))
        path = self._macro_path(name)
        if not path.exists():
            raise FileNotFoundError(f"Macro '{name}' not found")
        return iter_macro_events(path)

    def open_snapshot(self, name: str) -> MacroStoreSnapshot:
        """Open repeatable event reads that stop if the stored revision changes."""

        if name in self._internal_macros:
            payload = copy.deepcopy(self._internal_macros[name])
            events = _payload_events(payload)
            meta = MacroFileMeta.from_payload(payload, name=name).to_payload()
            return MacroStoreSnapshot(meta, lambda: iter(copy.deepcopy(events)))

        path = self._macro_path(name)
        if not path.exists():
            raise FileNotFoundError(f"Macro '{name}' not found")
        snapshot = MacroFileSnapshot(path)
        return MacroStoreSnapshot(
            snapshot.meta.to_payload(),
            snapshot.iter_events,
            snapshot.revision,
        )

    def probe_revision(self, name: str) -> MacroFileRevision | None:
        """Return a cheap stored-file identity for playback-cache lookup."""

        if name in self._internal_macros:
            return None
        path = self._macro_path(name)
        try:
            return MacroFileRevision.from_path(path)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"Macro '{name}' not found") from exc

    def create(self, payload: MacroPayload) -> MacroPayload:
        events = _payload_events(payload)
        return self._create_from_events(
            macro_payload_from_events(payload, events),
            events,
            return_full=True,
        )

    def create_from_events(
        self,
        payload: MacroPayload,
        events: Iterable[MacroEvent],
        *,
        return_full: bool = False,
    ) -> MacroPayload:
        missing_meta = [
            key for key in ("event_count", "duration_us", "device_types") if key not in payload
        ]
        if missing_meta:
            raise ValueError(
                "create_from_events requires streamed macro metadata: " + ", ".join(missing_meta)
            )
        return self._create_from_events(payload, events, return_full=return_full)

    def update(
        self, name: str, payload: MacroPayload, expected_revision: int | None
    ) -> MacroPayload:
        if name in self._internal_macros:
            raise PermissionError(f"Cannot modify internal macro '{name}'")
        with self._mutation_guard():
            current = self.get(name)
            current_revision = macro_payload_int(current, "revision", 1)
            _raise_revision_conflict(expected_revision, current_revision)

            data: MacroPayload = dict(current)
            events_changed = "events" in payload
            for key, value in payload.items():
                if key in {"name", "created_at", "revision"}:
                    continue
                data[key] = value

            data["revision"] = current_revision + 1
            data["event_count"] = len(_payload_events(data))
            if events_changed:
                if "duration_us" not in payload:
                    data.pop("duration_us", None)
                if "device_types" not in payload:
                    data.pop("device_types", None)
                if not (payload.get("type_binding") is True and "type_text" in payload):
                    for key in (
                        "type_text",
                        "type_down_ms",
                        "type_pause_ms",
                        "type_use_unicode_input",
                    ):
                        data.pop(key, None)
                    data["type_binding"] = False
            self._write_payload(self._macro_path(name), data)
            return self.get(name)

    def rename(self, old_name: str, new_name: str, expected_revision: int | None) -> MacroPayload:
        if old_name in self._internal_macros:
            raise PermissionError(f"Cannot rename internal macro '{old_name}'")
        if new_name.startswith(INTERNAL_MACRO_PREFIX):
            raise ValueError(f"Macro names starting with {INTERNAL_MACRO_PREFIX} are reserved")
        safe_new = self._sanitize_name(new_name)
        if not safe_new:
            raise ValueError("Invalid macro name")

        old_path = self._macro_path(old_name)
        new_path = self._macro_path(safe_new)
        with self._mutation_guard():
            current = self.get(old_name)
            current_revision = macro_payload_int(current, "revision", 1)
            _raise_revision_conflict(expected_revision, current_revision)
            if new_path.exists():
                raise FileExistsError(f"Macro '{safe_new}' already exists")

            current["name"] = safe_new
            current["revision"] = current_revision + 1
            current["event_count"] = len(_payload_events(current))
            try:
                self._write_payload(new_path, current, overwrite=False)
                renamed = self.get(safe_new)
                if renamed != current:
                    raise ValueError(f"Renamed macro '{safe_new}' failed validation")
            except FileExistsError:
                raise
            except Exception:
                new_path.unlink(missing_ok=True)
                raise
            old_path.unlink(missing_ok=True)
            return renamed

    def delete(self, name: str, expected_revision: int | None) -> None:
        if name in self._internal_macros:
            raise PermissionError(f"Cannot delete internal macro '{name}'")
        with self._mutation_guard():
            current = self.get_meta(name)
            current_revision = macro_payload_int(current, "revision", 1)
            _raise_revision_conflict(expected_revision, current_revision)
            self._macro_path(name).unlink(missing_ok=True)

    def _create_from_events(
        self,
        payload: MacroPayload,
        events: Iterable[MacroEvent],
        *,
        return_full: bool,
    ) -> MacroPayload:
        self.ensure()
        raw_name = macro_payload_str(payload, "name")
        if raw_name.startswith(INTERNAL_MACRO_PREFIX):
            raise ValueError(f"Macro names starting with {INTERNAL_MACRO_PREFIX} are reserved")
        name = self._sanitize_name(raw_name)
        if not name:
            raise ValueError("Invalid macro name")

        path = self._macro_path(name)
        if path.exists():
            raise FileExistsError(f"Macro '{name}' already exists")

        data = dict(payload)
        data["name"] = name
        data["created_at"] = macro_payload_str(payload, "created_at", datetime.now().isoformat())
        data["revision"] = macro_payload_int(payload, "revision", 1)

        meta = MacroFileMeta.from_payload(data, name=name)
        write_macro(path, meta, events, overwrite=False)
        return self.get(name) if return_full else meta.to_payload()

    def _macro_path(self, name: str) -> Path:
        safe = self._sanitize_name(name)
        if not safe:
            raise ValueError("Invalid macro name")
        return self.base_dir / f"{safe}{MACRO_FILE_SUFFIX}"

    def _sanitize_name(self, name: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_.-]", "_", name).strip("._")

    def _write_payload(self, path: Path, data: MacroPayload, *, overwrite: bool = True) -> None:
        events = _payload_events(data)
        payload = macro_payload_from_events(data, events)
        meta = MacroFileMeta.from_payload(payload)
        write_macro(path, meta, events, overwrite=overwrite)

    @contextmanager
    def _mutation_guard(self) -> Generator[None]:
        self.ensure()
        with _PROCESS_MUTATION_LOCK:
            fd = self._open_mutation_lock()
            try:
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _open_mutation_lock(self) -> int:
        flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        return os.open(self.base_dir / _MUTATION_LOCK_NAME, flags, 0o600)


def _payload_events(payload: MacroPayload) -> list[MacroEvent]:
    events = _payload_list(payload, "events")
    return [cast(MacroEvent, event) for event in events if isinstance(event, dict)]


def _raise_revision_conflict(expected_revision: int | None, current_revision: int) -> None:
    if expected_revision is not None and expected_revision != current_revision:
        raise ValueError(
            f"Revision conflict: expected {expected_revision}, current {current_revision}"
        )
