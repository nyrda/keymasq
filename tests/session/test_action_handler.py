import asyncio
import contextlib
import os
import shlex
import signal
import sys
from pathlib import Path

import pytest

from keymasq.session.action_handler import ActionHandler


@pytest.mark.asyncio
async def test_execute_command_returns_nonzero_code_with_non_utf8_stderr() -> None:
    script = "import sys\nsys.stderr.buffer.write(b'\\xff')\nsys.exit(7)\n"
    cmd = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    result = await ActionHandler().execute_command(cmd)

    assert result == 7


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _read_pid(path: Path) -> int:
    for _ in range(100):
        try:
            content = path.read_text(encoding="utf-8")
            if content.endswith("\n"):
                return int(content)
        except (FileNotFoundError, ValueError):
            pass
        await asyncio.sleep(0.01)
    raise AssertionError("child process pid was not written")


async def _wait_process_gone(pid: int) -> bool:
    for _ in range(100):
        if not _process_exists(pid):
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.mark.asyncio
async def test_execute_command_timeout_kills_spawned_child(tmp_path: Path) -> None:
    pid_path = tmp_path / "child.pid"
    script = (
        "import pathlib, subprocess\n"
        f"pid_path = pathlib.Path({str(pid_path)!r})\n"
        "process = subprocess.Popen(['sleep', '30'])\n"
        "pid_path.write_text(str(process.pid) + '\\n', encoding='utf-8')\n"
        "process.wait()\n"
    )
    cmd = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    task = asyncio.create_task(ActionHandler().execute_command(cmd, timeout_s=0.5))
    child_pid: int | None = None

    try:
        child_pid = await _read_pid(pid_path)
        result = await asyncio.wait_for(task, timeout=3.0)

        assert result == -1
        assert await _wait_process_gone(child_pid)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if child_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)
