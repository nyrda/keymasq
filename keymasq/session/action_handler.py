import asyncio
import contextlib
import logging
import os
import signal

from keymasq.gui.session_client import JsonDict

log = logging.getLogger("keymasq-session.actions")
DEFAULT_COMMAND_TIMEOUT_S = 300.0
KILLED_COMMAND_CLEANUP_TIMEOUT_S = 1.0


async def _kill_and_drain_process(process: asyncio.subprocess.Process) -> None:
    pid = getattr(process, "pid", None)
    if isinstance(pid, int):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)
    else:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(
            process.communicate(),
            timeout=KILLED_COMMAND_CLEANUP_TIMEOUT_S,
        )


class ActionHandler:
    def __init__(self) -> None:
        self._background_tasks: set[asyncio.Task[int]] = set()

    async def handle_action(self, data: JsonDict) -> None:
        action_type = data.get("action_type")
        source_device = data.get("source_device")
        source_button = data.get("source_button")
        cmd = data.get("cmd")

        log.info(f"Handling action: {action_type} from {source_device}:{source_button}")

        if action_type == "exec" and isinstance(cmd, str) and cmd:
            await self.execute_command(cmd)

    async def execute_command(
        self,
        cmd: str,
        *,
        timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
    ) -> int:
        timeout_s = max(0.001, float(timeout_s))
        try:
            log.info(f"Executing: {cmd}")

            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except Exception as e:
            log.error(f"Failed to execute command: {e}")
            return -1

        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)

            if process.returncode != 0:
                log.warning(f"Command failed with code {process.returncode}: {stderr.decode()}")
            return int(process.returncode or 0)

        except TimeoutError:
            log.error(f"Command timed out after {timeout_s:g}s, killing: {cmd}")
            await _kill_and_drain_process(process)
            return -1
        except asyncio.CancelledError:
            log.debug("Command task cancelled, killing: %s", cmd)
            await _kill_and_drain_process(process)
            raise
        except Exception as e:
            log.error(f"Failed to execute command: {e}")
            return -1

    def execute_command_sync(self, cmd: str) -> None:
        task = asyncio.create_task(
            self.execute_command(cmd),
            name="keymasq-session:exec-command",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._handle_background_task_done)

    def _handle_background_task_done(self, task: asyncio.Task[int]) -> None:
        self._background_tasks.discard(task)
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            log.error(
                "Unhandled exception in async command task",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def cancel_background_tasks(self) -> None:
        tasks = list(self._background_tasks)
        if not tasks:
            return

        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.difference_update(tasks)
