from __future__ import annotations

import io
import json
import lzma
import os
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, cast

from keymasq.common.coercion import require_json_object
from keymasq.common.config_files import write_config_atomically
from keymasq.common.model.actions import DEFAULT_MACRO_LOOP_STOP_BEHAVIOR
from keymasq.common.types import JsonObject

MACRO_FILE_SUFFIX = ".kmacro.xz"
MACRO_FILE_FORMAT = "keymasq-macro"
MACRO_FILE_VERSION = 1

type MacroEvent = dict[str, object]


class MacroFileSnapshot:
    """An open, immutable macro-file revision with repeatable event streams."""

    def __init__(self, path: Path) -> None:
        self._raw = path.open("rb")
        self._lock = threading.Lock()
        self._closed = False
        try:
            self.meta = self._read_meta()
        except BaseException:
            self._raw.close()
            self._closed = True
            raise

    def _open_text(self) -> io.TextIOWrapper:
        with self._lock:
            if self._closed:
                raise ValueError("Macro snapshot is closed")
            source_fd = self._raw.fileno()
            duplicated_fd = os.open(
                f"/proc/self/fd/{source_fd}",
                os.O_RDONLY | os.O_CLOEXEC,
            )
        try:
            duplicated = os.fdopen(duplicated_fd, "rb")
        except BaseException:
            os.close(duplicated_fd)
            raise
        compressed = lzma.LZMAFile(duplicated, "rb")
        return io.TextIOWrapper(compressed, encoding="utf-8", newline="\n")

    def _read_meta(self) -> MacroFileMeta:
        with self._open_text() as handle:
            first_line = handle.readline()
        return _macro_meta_from_line(first_line)

    def iter_events(self) -> Iterator[MacroEvent]:
        def generate() -> Iterator[MacroEvent]:
            with self._open_text() as handle:
                first_line = handle.readline()
                _macro_meta_from_line(first_line)
                for line in handle:
                    stripped = line.strip()
                    if stripped:
                        yield require_json_object(json.loads(stripped))

        return generate()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._raw.close()

    def __enter__(self) -> MacroFileSnapshot:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@dataclass(frozen=True)
class MacroFileMeta:
    name: str
    duration_us: int = 0
    device_types: list[str] = field(default_factory=list)
    event_count: int = 0
    created_at: str = ""
    revision: int = 1
    has_legacy_move_to_start: bool = False
    move_to_start: bool = False
    start_x: int = 0
    start_y: int = 0
    block_mouse_movement: bool = False
    loop_mode: str = "none"
    loop_count: int = 1
    loop_stop_behavior: str = DEFAULT_MACRO_LOOP_STOP_BEHAVIOR
    type_binding: bool = False
    type_text: str = ""
    type_down_ms: int = 0
    type_pause_ms: int = 0
    type_use_unicode_input: bool = False

    @classmethod
    def from_payload(cls, payload: JsonObject, *, name: str | None = None) -> MacroFileMeta:
        return cls(
            name=name or macro_payload_str(payload, "name"),
            duration_us=macro_payload_int(payload, "duration_us", 0),
            device_types=_payload_str_list(payload, "device_types"),
            event_count=macro_payload_int(payload, "event_count", _event_count(payload)),
            created_at=macro_payload_str(payload, "created_at", datetime.now().isoformat()),
            revision=macro_payload_int(payload, "revision", 1),
            has_legacy_move_to_start="move_to_start" in payload,
            move_to_start=bool(payload.get("move_to_start", False)),
            start_x=macro_payload_int(payload, "start_x", 0),
            start_y=macro_payload_int(payload, "start_y", 0),
            block_mouse_movement=bool(payload.get("block_mouse_movement", False)),
            loop_mode=macro_payload_str(payload, "loop_mode", "none") or "none",
            loop_count=macro_payload_int(payload, "loop_count", 1),
            loop_stop_behavior=macro_payload_str(
                payload,
                "loop_stop_behavior",
                DEFAULT_MACRO_LOOP_STOP_BEHAVIOR,
            )
            or DEFAULT_MACRO_LOOP_STOP_BEHAVIOR,
            type_binding=bool(payload.get("type_binding", False)),
            type_text=macro_payload_str(payload, "type_text"),
            type_down_ms=macro_payload_int(payload, "type_down_ms", 0),
            type_pause_ms=macro_payload_int(payload, "type_pause_ms", 0),
            type_use_unicode_input=bool(payload.get("type_use_unicode_input", False)),
        )

    def to_payload(self, *, include_type_text: bool = False) -> JsonObject:
        payload: JsonObject = {
            "name": self.name,
            "duration_us": int(self.duration_us),
            "device_types": list(self.device_types),
            "event_count": int(self.event_count),
            "created_at": self.created_at,
            "revision": int(self.revision),
            "block_mouse_movement": bool(self.block_mouse_movement),
            "loop_mode": self.loop_mode,
            "loop_count": int(self.loop_count),
            "loop_stop_behavior": self.loop_stop_behavior,
        }
        if self.has_legacy_move_to_start:
            payload["move_to_start"] = bool(self.move_to_start)
            payload["start_x"] = int(self.start_x)
            payload["start_y"] = int(self.start_y)
        if self.type_binding:
            payload["type_binding"] = True
            payload["type_down_ms"] = int(self.type_down_ms)
            payload["type_pause_ms"] = int(self.type_pause_ms)
            payload["type_use_unicode_input"] = bool(self.type_use_unicode_input)
            if include_type_text:
                payload["type_text"] = self.type_text
        return payload

    def to_record(self) -> JsonObject:
        return {
            "format": MACRO_FILE_FORMAT,
            "version": MACRO_FILE_VERSION,
            **self.to_payload(include_type_text=True),
        }


def read_macro_meta(path: Path) -> MacroFileMeta:
    with _open_text(path, "rb") as f:
        first_line = f.readline()
    return _macro_meta_from_line(first_line)


def iter_macro_events(path: Path) -> Iterator[MacroEvent]:
    with _open_text(path, "rb") as f:
        first_line = f.readline()
        if not first_line:
            raise ValueError("Empty macro file")
        _validate_meta_record(require_json_object(json.loads(first_line)))
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            event = require_json_object(json.loads(stripped))
            yield event


def load_macro(path: Path) -> JsonObject:
    with MacroFileSnapshot(path) as snapshot:
        payload = snapshot.meta.to_payload(include_type_text=True)
        payload["events"] = list(snapshot.iter_events())
        return payload


def write_macro(
    path: Path,
    meta: MacroFileMeta,
    events: Iterable[MacroEvent],
    *,
    overwrite: bool = True,
) -> None:
    def write(fileobj: BinaryIO) -> None:
        with lzma.LZMAFile(fileobj, "wb") as raw:
            with io.TextIOWrapper(raw, encoding="utf-8", newline="\n") as f:
                f.write(_json_line(meta.to_record()))
                for event in events:
                    f.write(_json_line(event))

    write_config_atomically(path, write, overwrite=overwrite, temp_suffix=".tmp")
    path.chmod(0o600)


def macro_payload_from_events(
    payload: JsonObject,
    events: list[MacroEvent],
    *,
    name: str | None = None,
    revision: int | None = None,
    created_at: str | None = None,
) -> JsonObject:
    data = dict(payload)
    data["events"] = events
    if name is not None:
        data["name"] = name
    if revision is not None:
        data["revision"] = int(revision)
    if created_at is not None:
        data["created_at"] = created_at
    data["event_count"] = len(events)
    if "duration_us" not in data:
        data["duration_us"] = _duration_us(events)
    if "device_types" not in data:
        data["device_types"] = _device_types(events)
    return data


def _open_text(path: Path, mode: str) -> io.TextIOWrapper:
    raw = lzma.LZMAFile(path, mode)
    return io.TextIOWrapper(raw, encoding="utf-8", newline="\n")


def _json_line(value: JsonObject) -> str:
    return json.dumps(value, separators=(",", ":")) + "\n"


def _validate_meta_record(record: JsonObject) -> None:
    if record.get("format") != MACRO_FILE_FORMAT:
        raise ValueError("Unsupported macro file format")
    if record.get("version") != MACRO_FILE_VERSION:
        raise ValueError("Unsupported macro file version")


def _macro_meta_from_line(first_line: str) -> MacroFileMeta:
    if not first_line:
        raise ValueError("Empty macro file")
    record = require_json_object(json.loads(first_line))
    _validate_meta_record(record)
    return MacroFileMeta.from_payload(record)


def macro_payload_str(payload: JsonObject, key: str, default: str = "") -> str:
    value = payload.get(key, default)
    return value if isinstance(value, str) else default


def macro_payload_int(payload: JsonObject, key: str, default: int) -> int:
    value = payload.get(key, default)
    try:
        return int(cast(int | float | str, value))
    except (TypeError, ValueError):
        return default


def _payload_str_list(payload: JsonObject, key: str) -> list[str]:
    value = payload.get(key, [])
    if not isinstance(value, list):
        return []
    items = cast(list[object], value)
    return [str(item) for item in items if str(item)]


def _event_count(payload: JsonObject) -> int:
    events = payload.get("events")
    return len(cast(list[object], events)) if isinstance(events, list) else 0


def _event_t_us(event: MacroEvent) -> int:
    value = event.get("t_us", 0)
    return value if isinstance(value, int) else 0


def _duration_us(events: list[MacroEvent]) -> int:
    return max((_event_t_us(event) for event in events), default=0)


def _device_types(events: list[MacroEvent]) -> list[str]:
    device_types: set[str] = set()
    for event in events:
        if str(event.get("macro_action", "") or "") in {
            "mouse_move_abs",
            "mouse_move_rel",
            "mouse_move_natural_abs",
        }:
            device_types.add("mouse")
            continue
        device_type = str(event.get("device_type", "") or "")
        if device_type and device_type != "macro":
            device_types.add(device_type)
    return sorted(device_types)
