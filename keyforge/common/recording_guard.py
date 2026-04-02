import os
import time
from pathlib import Path

from keyforge.common.paths import RECORDING_UNLOCK_PERSISTENT_DIR, RECORDING_UNLOCK_RUNTIME_DIR

type UnlockStatus = dict[str, bool | int | str]


def runtime_unlock_path(uid: int) -> Path:
    return RECORDING_UNLOCK_RUNTIME_DIR / f"recording-unlock-{int(uid)}"


def persistent_unlock_path(uid: int) -> Path:
    return RECORDING_UNLOCK_PERSISTENT_DIR / f"recording-unlock-{int(uid)}"


def parse_unlock_expires_at(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception:
        return None

    if not text:
        return None

    try:
        value = int(text)
    except (TypeError, ValueError):
        return None

    return value


def is_unlock_value_active(expires_at: int, now: int | None = None) -> bool:
    if expires_at == 0:
        return True

    current = int(time.time()) if now is None else int(now)
    return expires_at >= current


def resolve_unlock_status(uid: int, now: int | None = None) -> UnlockStatus:
    runtime_path = runtime_unlock_path(uid)
    persistent_path = persistent_unlock_path(uid)

    runtime_expires = parse_unlock_expires_at(runtime_path)
    if runtime_expires is not None and is_unlock_value_active(runtime_expires, now=now):
        return {
            "unlocked": True,
            "source": "runtime",
            "expires_at": int(runtime_expires),
            "path": str(runtime_path),
        }

    persistent_expires = parse_unlock_expires_at(persistent_path)
    if persistent_expires is not None and is_unlock_value_active(persistent_expires, now=now):
        return {
            "unlocked": True,
            "source": "persistent",
            "expires_at": int(persistent_expires),
            "path": str(persistent_path),
        }

    return {
        "unlocked": False,
        "source": "none",
        "expires_at": 0,
        "path": "",
    }


def write_unlock_expires_at(
    path: Path,
    expires_at: int,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
    mode: int = 0o644,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp_path.write_text(f"{int(expires_at)}\n", encoding="utf-8")

    if owner_uid is not None and owner_gid is not None:
        try:
            os.chown(tmp_path, int(owner_uid), int(owner_gid))
        except OSError:
            pass

    os.chmod(tmp_path, int(mode))
    os.replace(tmp_path, path)
