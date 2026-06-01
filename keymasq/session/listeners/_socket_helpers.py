import asyncio
import os
from pathlib import Path


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
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False
