import logging
import os
import pwd
import stat
import tempfile
import time
from pathlib import Path

from keymasq.common.paths import RECORDING_UNLOCK_PERSISTENT_DIR, RECORDING_UNLOCK_RUNTIME_DIR

log = logging.getLogger(__name__)

type UnlockStatus = dict[str, bool | int | str]


def runtime_unlock_path(uid: int) -> Path:
    return RECORDING_UNLOCK_RUNTIME_DIR / f"recording-unlock-{int(uid)}"


def persistent_unlock_path(uid: int) -> Path:
    return RECORDING_UNLOCK_PERSISTENT_DIR / f"recording-unlock-{int(uid)}"


def runtime_macro_recording_path(uid: int) -> Path:
    return RECORDING_UNLOCK_RUNTIME_DIR / f"macro-recording-enabled-{int(uid)}"


def persistent_macro_recording_path(uid: int) -> Path:
    return RECORDING_UNLOCK_PERSISTENT_DIR / f"macro-recording-enabled-{int(uid)}"


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

    return _resolve_status_from_paths(runtime_path, persistent_path, now=now)


def resolve_macro_recording_status(uid: int, now: int | None = None) -> UnlockStatus:
    runtime_path = runtime_macro_recording_path(uid)
    persistent_path = persistent_macro_recording_path(uid)

    return _resolve_status_from_paths(runtime_path, persistent_path, now=now)


def _resolve_status_from_paths(
    runtime_path: Path,
    persistent_path: Path,
    *,
    now: int | None = None,
) -> UnlockStatus:
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


def _validate_unlock_parent(path: Path) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)

    try:
        parent_stat = parent.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise PermissionError(f"Recording unlock directory is unavailable: {parent}") from exc

    if not stat.S_ISDIR(parent_stat.st_mode):
        raise PermissionError(f"Recording unlock parent is not a directory: {parent}")

    trusted_owner_uids = _trusted_unlock_parent_owner_uids()
    if parent_stat.st_uid not in trusted_owner_uids:
        raise PermissionError(f"Recording unlock directory has untrusted owner: {parent}")

    if parent_stat.st_mode & stat.S_IWOTH:
        raise PermissionError(f"Recording unlock directory is world-writable: {parent}")

    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return

    if stat.S_ISLNK(path_stat.st_mode):
        raise PermissionError(f"Recording unlock path is a symlink: {path}")


def _trusted_unlock_parent_owner_uids() -> set[int]:
    trusted = {0, os.geteuid()}
    try:
        trusted.add(int(pwd.getpwnam("keymasq").pw_uid))
    except KeyError:
        pass
    return trusted


def write_unlock_expires_at(
    path: Path,
    expires_at: int,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
    mode: int = 0o644,
) -> None:
    _validate_unlock_parent(path)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    tmp_path = Path(tmp_name)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{int(expires_at)}\n")

            if owner_uid is not None and owner_gid is not None:
                try:
                    os.fchown(handle.fileno(), int(owner_uid), int(owner_gid))
                except OSError as exc:
                    log.warning(
                        "Failed to set recording unlock file owner on %s to %s:%s: %s",
                        tmp_path,
                        owner_uid,
                        owner_gid,
                        exc,
                    )

            os.fchmod(handle.fileno(), int(mode))
            handle.flush()
            os.fsync(handle.fileno())

        _validate_unlock_parent(path)
        os.replace(tmp_path, path)
        dir_fd: int | None = None
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
        finally:
            if dir_fd is not None:
                os.close(dir_fd)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise
