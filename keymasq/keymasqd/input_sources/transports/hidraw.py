"""Read-only nonblocking hidraw transport using asyncio readiness."""

import asyncio
import errno
import os
from collections.abc import AsyncGenerator
from pathlib import Path


async def reports(path: str, *, hid_parent: str | None = None) -> AsyncGenerator[bytes]:
    opening = asyncio.create_task(asyncio.to_thread(_open, path, hid_parent))
    try:
        fd = await asyncio.shield(opening)
    except asyncio.CancelledError:
        opening.add_done_callback(_close_cancelled_open)
        raise
    loop = asyncio.get_running_loop()
    ready = asyncio.Event()
    try:
        loop.add_reader(fd, ready.set)
        while True:
            await ready.wait()
            ready.clear()
            # Bound each drain so a fast endpoint cannot monopolize the loop.
            for _ in range(64):
                try:
                    report = os.read(fd, 4096)
                except BlockingIOError:
                    break
                if not report:
                    raise OSError("hidraw endpoint closed")
                yield report
            else:
                await asyncio.sleep(0)
    finally:
        loop.remove_reader(fd)
        os.close(fd)


def _close_cancelled_open(task: asyncio.Task[int]) -> None:
    if not task.cancelled() and task.exception() is None:
        os.close(task.result())


def _open(path: str, expected_parent: str | None) -> int:
    def validate_connection() -> None:
        if expected_parent is None:
            return
        current = (Path("/sys/class/hidraw") / Path(path).name / "device").resolve()
        if str(current) != expected_parent:
            raise OSError(errno.ENODEV, "HID connection changed while opening", path)

    validate_connection()
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        validate_connection()
    except OSError:
        os.close(fd)
        raise
    return fd
