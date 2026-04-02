import asyncio
import logging

log = logging.getLogger("keyforge-session.actions")


class ActionHandler:
    def __init__(self) -> None:
        pass

    async def handle_action(self, data: dict) -> None:
        action_type = data.get("action_type")
        source_device = data.get("source_device")
        source_button = data.get("source_button")
        cmd = data.get("cmd")

        log.info(f"Handling action: {action_type} from {source_device}:{source_button}")

        if action_type == "exec" and cmd:
            await self.execute_command(cmd)

    async def execute_command(self, cmd: str) -> int:
        try:
            log.info(f"Executing: {cmd}")

            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            log.error(f"Failed to execute command: {e}")
            return -1

        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=300.0)

            if process.returncode != 0:
                log.warning(f"Command failed with code {process.returncode}: {stderr.decode()}")
            return int(process.returncode or 0)

        except TimeoutError:
            log.error(f"Command timed out after 300s, killing: {cmd}")
            process.kill()
            return -1
        except Exception as e:
            log.error(f"Failed to execute command: {e}")
            return -1

    def execute_command_sync(self, cmd: str) -> None:
        async def _runner() -> None:
            await self.execute_command(cmd)

        asyncio.create_task(_runner())
