import asyncio
import logging
import os
from pathlib import Path

log = logging.getLogger("keymasq-session.listeners.socket_helpers")


def runtime_dir() -> Path:
    env_dir = os.environ.get("XDG_RUNTIME_DIR")
    if env_dir:
        return Path(env_dir)
    return Path(f"/run/user/{os.getuid()}")


def _candidate_wayland_sockets_sync() -> list[Path]:
    base_dir = runtime_dir()
    if not base_dir.exists():
        return []
    sockets = [path for path in base_dir.glob("wayland-*") if path.is_socket()]
    sockets.sort(key=lambda path: path.name)
    return sockets


async def candidate_wayland_sockets() -> list[Path]:
    return await asyncio.to_thread(_candidate_wayland_sockets_sync)


async def unix_socket_connectable(socket_path: Path, timeout_s: float = 0.2) -> bool:
    try:
        connect_coro = asyncio.open_unix_connection(path=str(socket_path))
        _reader, writer = await asyncio.wait_for(connect_coro, timeout=timeout_s)
    except TimeoutError as exc:
        log.debug("Timed out probing Unix socket %s: %s", socket_path, exc)
        return False
    except OSError as exc:
        log.debug("Could not connect to Unix socket %s: %s", socket_path, exc)
        return False
    except Exception:
        log.exception("Unexpected error probing Unix socket %s", socket_path)
        return False

    writer.close()
    try:
        await writer.wait_closed()
    except OSError as exc:
        log.debug("Failed to close Unix socket probe connection %s: %s", socket_path, exc)
    except Exception:
        log.exception("Unexpected error closing Unix socket probe connection %s", socket_path)
    return True
