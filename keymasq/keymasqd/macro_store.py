import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import cast

INTERNAL_MACRO_PREFIX = "__"
type MacroEvent = dict[str, object]
type MacroPayload = dict[str, object]


def _as_macro_payload(value: object) -> MacroPayload:
    if not isinstance(value, dict):
        raise ValueError("Invalid macro payload")
    return cast(MacroPayload, value)


def _payload_str(payload: MacroPayload, key: str, default: str = "") -> str:
    value = payload.get(key, default)
    return value if isinstance(value, str) else default


def _payload_int(payload: MacroPayload, key: str, default: int) -> int:
    value = payload.get(key, default)
    return value if isinstance(value, int) else default


def _payload_list(payload: MacroPayload, key: str) -> list[object]:
    value = payload.get(key, [])
    return cast(list[object], value) if isinstance(value, list) else []


class MacroStore:
    """Central store for macro data, persisted as JSON files.

    Each macro is a JSON file under ``base_dir`` containing:

    - ``name``                  – unique identifier used by profiles and the CLI.
    - ``events``                – recorded input events (evdev-format list).
    - ``duration_ms``           – total duration in milliseconds.
    - ``device_types``          – which device types are involved (keyboard, mouse, etc.).
    - ``move_to_start``         – whether to reposition the cursor before playback.
    - ``start_x`` / ``start_y`` – cursor position at the start of recording.
    - ``block_mouse_movement``  – whether to suppress mouse movement during playback.
    - ``loop_stop_behavior`` – whether Hold/Toggle stop input finishes or
      cancels the current run.
    - ``revision``              – version counter for optimistic concurrency.
    - ``created_at``            – ISO timestamp of initial creation.

    Editor metadata (such as gap notes) may also be stored to support
    non-destructive editing in the GUI.

    Internal macros (names prefixed with ``__``) are registered in memory
    and are not written to disk.
    """

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self._internal_macros: dict[str, MacroPayload] = {}

    def register_internal(self, name: str, events: list[MacroEvent], **extra: object) -> None:
        if not name.startswith(INTERNAL_MACRO_PREFIX):
            raise ValueError(f"Internal macro names must start with {INTERNAL_MACRO_PREFIX}")
        self._internal_macros[name] = {
            "name": name,
            "events": events,
            "internal": True,
            **extra,
        }

    def is_internal(self, name: str) -> bool:
        return name in self._internal_macros

    def ensure(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.base_dir, 0o700)

    def list_meta(self) -> list[MacroPayload]:
        self.ensure()
        macros: list[MacroPayload] = []
        for path in sorted(self.base_dir.glob("*.json")):
            try:
                data = _as_macro_payload(json.loads(path.read_text()))
                name = _payload_str(data, "name", path.stem)
                if name.startswith(INTERNAL_MACRO_PREFIX):
                    continue
                macros.append(
                    {
                        "name": name,
                        "duration_ms": _payload_int(data, "duration_ms", 0),
                        "device_types": _payload_list(data, "device_types"),
                        "created_at": _payload_str(data, "created_at"),
                        "event_count": len(_payload_list(data, "events")),
                        "revision": _payload_int(data, "revision", 1),
                    }
                )
            except Exception:
                continue
        return macros

    def get(self, name: str) -> MacroPayload:
        if name in self._internal_macros:
            return dict(self._internal_macros[name])
        path = self._macro_path(name)
        if not path.exists():
            raise FileNotFoundError(f"Macro '{name}' not found")
        data = _as_macro_payload(json.loads(path.read_text()))
        data["revision"] = _payload_int(data, "revision", 1)
        return data

    def create(self, payload: MacroPayload) -> MacroPayload:
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

        now = datetime.now().isoformat()
        data: MacroPayload = dict(payload)
        data["name"] = name
        data["created_at"] = _payload_str(payload, "created_at", now) or now
        data["revision"] = _payload_int(payload, "revision", 1)

        self._write_atomic(path, data)
        return self.get(name)

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
        for key, value in payload.items():
            if key in {"name", "created_at", "revision"}:
                continue
            data[key] = value

        data["revision"] = current_revision + 1
        self._write_atomic(self._macro_path(name), data)
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
        self._write_atomic(new_path, current)
        old_path.unlink(missing_ok=True)
        return self.get(safe_new)

    def delete(self, name: str, expected_revision: int | None) -> None:
        if name in self._internal_macros:
            raise PermissionError(f"Cannot delete internal macro '{name}'")
        current = self.get(name)
        current_revision = _payload_int(current, "revision", 1)
        if expected_revision is not None and expected_revision != current_revision:
            raise ValueError(
                f"Revision conflict: expected {expected_revision}, current {current_revision}"
            )
        self._macro_path(name).unlink(missing_ok=True)

    def _macro_path(self, name: str) -> Path:
        safe = self._sanitize_name(name)
        if not safe:
            raise ValueError("Invalid macro name")
        return self.base_dir / f"{safe}.json"

    def _sanitize_name(self, name: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_.-]", "_", name).strip("._")

    def _write_atomic(self, path: Path, data: MacroPayload) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data))
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
