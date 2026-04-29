import logging
import os
import re
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import cast

from keymasq.common.models import DEFAULT_MACRO_LOOP_STOP_BEHAVIOR
from keymasq.keymasqd.macro_file import (
    MACRO_FILE_SUFFIX,
    MacroFileMeta,
    iter_macro_events,
    load_macro,
    macro_payload_from_events,
    read_macro_meta,
    write_macro,
)

INTERNAL_MACRO_PREFIX = "__"
log = logging.getLogger("keymasqd.macros")
type MacroEvent = dict[str, object]
type MacroPayload = dict[str, object]


def _payload_str(payload: MacroPayload, key: str, default: str = "") -> str:
    value = payload.get(key, default)
    return value if isinstance(value, str) else default


def _payload_int(payload: MacroPayload, key: str, default: int) -> int:
    value = payload.get(key, default)
    try:
        return int(cast(int | float | str, value))
    except (TypeError, ValueError):
        return default


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
        self._internal_macros[name] = macro_payload_from_events(
            {"name": name, "internal": True, **extra},
            events,
            name=name,
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
            except Exception as exc:
                log.warning("Skipping unreadable macro file %s: %s", path, exc)
                continue
        return macros

    def get(self, name: str) -> MacroPayload:
        if name in self._internal_macros:
            return dict(self._internal_macros[name])
        path = self._macro_path(name)
        if not path.exists():
            raise FileNotFoundError(f"Macro '{name}' not found")
        return load_macro(path)

    def get_meta(self, name: str) -> MacroPayload:
        if name in self._internal_macros:
            data = self._internal_macros[name]
            return {
                "name": _payload_str(data, "name", name),
                "duration_us": _payload_int(data, "duration_us", 0),
                "device_types": _payload_list(data, "device_types"),
                "created_at": _payload_str(data, "created_at"),
                "event_count": _payload_int(
                    data,
                    "event_count",
                    len(_payload_list(data, "events")),
                ),
                "revision": _payload_int(data, "revision", 1),
                "move_to_start": bool(data.get("move_to_start", False)),
                "start_x": _payload_int(data, "start_x", 0),
                "start_y": _payload_int(data, "start_y", 0),
                "block_mouse_movement": bool(data.get("block_mouse_movement", False)),
                "loop_mode": _payload_str(data, "loop_mode", "none") or "none",
                "loop_count": _payload_int(data, "loop_count", 1),
                "loop_stop_behavior": _payload_str(
                    data,
                    "loop_stop_behavior",
                    DEFAULT_MACRO_LOOP_STOP_BEHAVIOR,
                ),
            }
        path = self._macro_path(name)
        if not path.exists():
            raise FileNotFoundError(f"Macro '{name}' not found")
        return read_macro_meta(path).to_payload()

    def iter_events(self, name: str) -> Iterator[MacroEvent]:
        if name in self._internal_macros:
            events = _payload_events(self._internal_macros[name])
            return iter(events)
        path = self._macro_path(name)
        if not path.exists():
            raise FileNotFoundError(f"Macro '{name}' not found")
        return iter_macro_events(path)

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
        return self._create_from_events(payload, events, return_full=return_full)

    def update(
        self, name: str, payload: MacroPayload, expected_revision: int | None
    ) -> MacroPayload:
        if name in self._internal_macros:
            raise PermissionError(f"Cannot modify internal macro '{name}'")
        current = self.get(name)
        current_revision = _payload_int(current, "revision", 1)
        if expected_revision is not None and expected_revision != current_revision:
            raise ValueError(
                f"Revision conflict: expected {expected_revision}, current {current_revision}"
            )

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
        self._write_payload(self._macro_path(name), data)
        return self.get(name)

    def rename(self, old_name: str, new_name: str, expected_revision: int | None) -> MacroPayload:
        if old_name in self._internal_macros:
            raise PermissionError(f"Cannot rename internal macro '{old_name}'")
        if new_name.startswith(INTERNAL_MACRO_PREFIX):
            raise ValueError(f"Macro names starting with {INTERNAL_MACRO_PREFIX} are reserved")
        current = self.get(old_name)
        current_revision = _payload_int(current, "revision", 1)
        if expected_revision is not None and expected_revision != current_revision:
            raise ValueError(
                f"Revision conflict: expected {expected_revision}, current {current_revision}"
            )

        safe_new = self._sanitize_name(new_name)
        if not safe_new:
            raise ValueError("Invalid macro name")

        old_path = self._macro_path(old_name)
        new_path = self._macro_path(safe_new)
        if new_path.exists():
            raise FileExistsError(f"Macro '{safe_new}' already exists")

        current["name"] = safe_new
        current["revision"] = current_revision + 1
        current["event_count"] = len(_payload_events(current))
        self._write_payload(new_path, current)
        old_path.unlink(missing_ok=True)
        return self.get(safe_new)

    def delete(self, name: str, expected_revision: int | None) -> None:
        if name in self._internal_macros:
            raise PermissionError(f"Cannot delete internal macro '{name}'")
        current = self.get_meta(name)
        current_revision = _payload_int(current, "revision", 1)
        if expected_revision is not None and expected_revision != current_revision:
            raise ValueError(
                f"Revision conflict: expected {expected_revision}, current {current_revision}"
            )
        self._macro_path(name).unlink(missing_ok=True)

    def _create_from_events(
        self,
        payload: MacroPayload,
        events: Iterable[MacroEvent],
        *,
        return_full: bool,
    ) -> MacroPayload:
        self.ensure()
        raw_name = _payload_str(payload, "name")
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
        data["created_at"] = _payload_str(payload, "created_at", datetime.now().isoformat())
        data["revision"] = _payload_int(payload, "revision", 1)

        meta = MacroFileMeta.from_payload(data, name=name)
        write_macro(path, meta, events)
        return self.get(name) if return_full else meta.to_payload()

    def _macro_path(self, name: str) -> Path:
        safe = self._sanitize_name(name)
        if not safe:
            raise ValueError("Invalid macro name")
        return self.base_dir / f"{safe}{MACRO_FILE_SUFFIX}"

    def _sanitize_name(self, name: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_.-]", "_", name).strip("._")

    def _write_payload(self, path: Path, data: MacroPayload) -> None:
        events = _payload_events(data)
        payload = macro_payload_from_events(data, events)
        meta = MacroFileMeta.from_payload(payload)
        write_macro(path, meta, events)


def _payload_events(payload: MacroPayload) -> list[MacroEvent]:
    events = _payload_list(payload, "events")
    return [cast(MacroEvent, event) for event in events if isinstance(event, dict)]
