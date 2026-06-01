import asyncio
from collections.abc import Callable

import pytest_asyncio

from keymasq.common import paths
from keymasq.common.ipc import Command, CommandType, decode_response, encode_command
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.socket_server import SocketServer


class IntegrationTestBase:
    @pytest_asyncio.fixture
    async def full_system(self, temp_socket_dir):
        manager = DeviceManager(release_grace_s=0.1, held_release_retry_s=0.1)

        async def handle_disconnect() -> None:
            await manager.release_all_devices()

        async def command_handler(cmd_type, data, _client):
            if cmd_type == CommandType.GRAB_DEVICE:
                return await manager.grab_device(
                    data["hardware_id"],
                    data["evdev_paths"],
                    data.get("button_map", {}),
                )
            if cmd_type == CommandType.RELEASE_DEVICE:
                return await manager.release_device(
                    data["hardware_id"],
                    immediate=bool(data.get("immediate", False)),
                    grace_s=data.get("grace_s"),
                )
            if cmd_type == CommandType.SET_MAPPING:
                return await manager.set_mapping(data["hardware_id"], data["mapping"])
            if cmd_type == CommandType.LIST_DEVICES:
                return await manager.list_devices()
            if cmd_type == CommandType.PING:
                return {"pong": True}
            return {}

        server = SocketServer(
            str(paths.SOCKET_PATH),
            command_handler,
            handle_disconnect,
        )

        await server.start()

        yield server, manager

        await manager.release_all_devices()
        await server.stop()

    async def _send_command(self, reader, writer, command: Command) -> dict:
        writer.write(encode_command(command))
        await writer.drain()
        response_data = b""
        while True:
            chunk = await reader.read(4096)
            assert chunk
            response_data += chunk
            response, remaining = decode_response(response_data)
            if response is None:
                response_data = remaining
                continue
            break
        assert response.status == "ok"
        return response.data or {}

    async def _wait_until(
        self,
        condition: Callable[[], bool],
        *,
        timeout_s: float = 1.0,
        interval_s: float = 0.01,
        reason: str = "condition",
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            if condition():
                return
            if loop.time() >= deadline:
                raise AssertionError(f"Timed out waiting for {reason}")
            await asyncio.sleep(interval_s)
