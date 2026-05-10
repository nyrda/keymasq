# pyright: reportUnusedImport=false, reportUnusedFunction=false, reportUnusedClass=false
# ruff: noqa: F401, I001
import asyncio
import os
from unittest.mock import Mock

import evdev
import pytest
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

        server = SocketServer(
            str(paths.SOCKET_PATH),
            lambda cmd, data, _client: manager._handle_command(cmd, data),
            handle_disconnect,
        )

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

        server.command_handler = command_handler

        await server.start()

        yield server, manager

        await manager.release_all_devices()
        await server.stop()

    async def _send_command(self, reader, writer, command: Command) -> dict:
        writer.write(encode_command(command))
        await writer.drain()
        response_data = await reader.read(4096)
        response, _ = decode_response(response_data)
        assert response is not None
        assert response.status == "ok"
        return response.data or {}


__all__ = [
    "asyncio",
    "os",
    "Mock",
    "evdev",
    "pytest",
    "pytest_asyncio",
    "paths",
    "Command",
    "CommandType",
    "decode_response",
    "encode_command",
    "DeviceManager",
    "SocketServer",
    "IntegrationTestBase",
]
