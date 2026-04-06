import asyncio
import os
from unittest.mock import Mock

import evdev
import pytest
import pytest_asyncio

from keyforge.common import paths
from keyforge.common.ipc import Command, CommandType, decode_response, encode_command
from keyforge.keyforged.device_manager import DeviceManager
from keyforge.keyforged.socket_server import SocketServer


@pytest.mark.skipif(not os.access("/dev/uinput", os.W_OK), reason="No uinput access")
@pytest.mark.asyncio
class TestIntegration:
    @pytest_asyncio.fixture
    async def full_system(self, temp_socket_dir):
        manager = DeviceManager(release_grace_s=0.1, held_release_retry_s=0.1)

        async def handle_disconnect() -> None:
            await manager.release_all_devices()

        server = SocketServer(
            str(paths.SOCKET_PATH),
            lambda cmd, data: (
                manager._handle_command(cmd, data)
                if hasattr(manager, "_handle_command")
                else self._default_handler(cmd, data)
            ),
            handle_disconnect,
        )

        async def command_handler(cmd_type, data):
            if cmd_type == CommandType.GRAB_DEVICE:
                return await manager.grab_device(
                    data["hardware_id"],
                    data["evdev_paths"],
                    data.get("button_map", {}),
                )
            elif cmd_type == CommandType.RELEASE_DEVICE:
                return await manager.release_device(
                    data["hardware_id"],
                    immediate=bool(data.get("immediate", False)),
                    grace_s=data.get("grace_s"),
                )
            elif cmd_type == CommandType.SET_MAPPING:
                return await manager.set_mapping(data["hardware_id"], data["mapping"])
            elif cmd_type == CommandType.LIST_DEVICES:
                return await manager.list_devices()
            elif cmd_type == CommandType.PING:
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

    async def test_full_workflow(self, full_system, virtual_mouse):
        _server, _manager = full_system
        mouse_path = virtual_mouse.device.path

        reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))

        cmd = Command(
            command=CommandType.GRAB_DEVICE,
            data={
                "hardware_id": "1234:5678",
                "evdev_paths": [mouse_path],
                "button_map": {"btn_side": "btn_side"},
            },
        )
        writer.write(encode_command(cmd))
        await writer.drain()

        response_data = await reader.read(4096)
        response, _ = decode_response(response_data)

        assert response.status == "ok"
        assert response.data["grabbed"] is True

        cmd = Command(
            command=CommandType.SET_MAPPING,
            data={
                "hardware_id": "1234:5678",
                "mapping": {
                    "btn_side": {"action": "keyboard", "target": "key_a"},
                },
            },
        )
        writer.write(encode_command(cmd))
        await writer.drain()

        response_data = await reader.read(4096)
        response, _ = decode_response(response_data)

        assert response.status == "ok"
        assert response.data["updated"] is True

        cmd = Command(
            command=CommandType.RELEASE_DEVICE,
            data={"hardware_id": "1234:5678"},
        )
        writer.write(encode_command(cmd))
        await writer.drain()

        response_data = await reader.read(4096)
        response, _ = decode_response(response_data)

        assert response.status == "ok"

        writer.close()
        await writer.wait_closed()

    async def test_event_passthrough(self, full_system, virtual_mouse):
        _server, _manager = full_system
        mouse_path = virtual_mouse.device.path

        reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))

        cmd = Command(
            command=CommandType.GRAB_DEVICE,
            data={
                "hardware_id": "1234:5678",
                "evdev_paths": [mouse_path],
                "button_map": {},
            },
        )
        writer.write(encode_command(cmd))
        await writer.drain()

        await reader.read(4096)

        virtual_mouse.write(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 1)
        virtual_mouse.syn()
        virtual_mouse.write(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 0)
        virtual_mouse.syn()

        await asyncio.sleep(0.1)

        cmd = Command(command=CommandType.RELEASE_DEVICE, data={"hardware_id": "1234:5678"})
        writer.write(encode_command(cmd))
        await writer.drain()
        await reader.read(4096)

        writer.close()
        await writer.wait_closed()

    async def test_keyboard_with_rapidfire(self, full_system, virtual_mouse):
        _server, _manager = full_system
        mouse_path = virtual_mouse.device.path

        reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))

        cmd = Command(
            command=CommandType.GRAB_DEVICE,
            data={
                "hardware_id": "1234:5678",
                "evdev_paths": [mouse_path],
                "button_map": {"btn_side": "btn_side"},
            },
        )
        writer.write(encode_command(cmd))
        await writer.drain()
        await reader.read(4096)

        cmd = Command(
            command=CommandType.SET_MAPPING,
            data={
                "hardware_id": "1234:5678",
                "mapping": {
                    "btn_side": {
                        "action": "keyboard",
                        "target": "btn_left",
                        "rapidfire_enabled": True,
                        "rapidfire_hold_ms": 20,
                        "rapidfire_wait_ms": 20,
                    },
                },
            },
        )
        writer.write(encode_command(cmd))
        await writer.drain()
        await reader.read(4096)

        await asyncio.sleep(0.3)

        cmd = Command(command=CommandType.RELEASE_DEVICE, data={"hardware_id": "1234:5678"})
        writer.write(encode_command(cmd))
        await writer.drain()
        await reader.read(4096)

        writer.close()
        await writer.wait_closed()

    async def test_disconnect_releases_devices(self, full_system, virtual_mouse):
        _server, manager = full_system
        mouse_path = virtual_mouse.device.path

        reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))

        cmd = Command(
            command=CommandType.GRAB_DEVICE,
            data={
                "hardware_id": "1234:5678",
                "evdev_paths": [mouse_path],
                "button_map": {"btn_side": "btn_side"},
            },
        )
        writer.write(encode_command(cmd))
        await writer.drain()
        await reader.read(4096)

        assert "1234:5678" in manager.grabbed_devices

        writer.close()
        await writer.wait_closed()

        await asyncio.sleep(0.3)
        assert "1234:5678" not in manager.grabbed_devices

    async def test_release_with_grace_can_be_canceled_by_regrab(self, full_system, virtual_mouse):
        _server, manager = full_system
        mouse_path = virtual_mouse.device.path

        reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))

        await self._send_command(
            reader,
            writer,
            Command(
                command=CommandType.GRAB_DEVICE,
                data={
                    "hardware_id": "1234:5678",
                    "evdev_paths": [mouse_path],
                    "button_map": {"btn_side": "btn_side"},
                },
            ),
        )

        release_data = await self._send_command(
            reader,
            writer,
            Command(
                command=CommandType.RELEASE_DEVICE,
                data={"hardware_id": "1234:5678", "grace_s": 0.12},
            ),
        )

        assert release_data.get("scheduled") is True
        assert "1234:5678" in manager.grabbed_devices

        await asyncio.sleep(0.04)

        await self._send_command(
            reader,
            writer,
            Command(
                command=CommandType.GRAB_DEVICE,
                data={
                    "hardware_id": "1234:5678",
                    "evdev_paths": [mouse_path],
                    "button_map": {"btn_side": "btn_side"},
                },
            ),
        )

        await asyncio.sleep(0.14)
        assert "1234:5678" in manager.grabbed_devices
        assert any(d.path == mouse_path for d in manager.grabbed_devices["1234:5678"])

        await self._send_command(
            reader,
            writer,
            Command(
                command=CommandType.RELEASE_DEVICE,
                data={"hardware_id": "1234:5678", "immediate": True},
            ),
        )

        writer.close()
        await writer.wait_closed()

    async def test_reconfigure_releases_unused_interface_after_grace(
        self,
        full_system,
        virtual_mouse,
        virtual_keyboard,
    ):
        _server, manager = full_system
        mouse_path = virtual_mouse.device.path
        keyboard_path = virtual_keyboard.device.path

        reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))

        hardware_id = "1234:5678"
        button_map = {
            "btn_side": "btn_side",
            "key_a": "key_a",
        }

        await self._send_command(
            reader,
            writer,
            Command(
                command=CommandType.GRAB_DEVICE,
                data={
                    "hardware_id": hardware_id,
                    "evdev_paths": [mouse_path, keyboard_path],
                    "button_map": button_map,
                },
            ),
        )

        paths_before = {d.path for d in manager.grabbed_devices.get(hardware_id, [])}
        assert mouse_path in paths_before
        assert keyboard_path in paths_before

        await self._send_command(
            reader,
            writer,
            Command(
                command=CommandType.GRAB_DEVICE,
                data={
                    "hardware_id": hardware_id,
                    "evdev_paths": [mouse_path],
                    "button_map": button_map,
                },
            ),
        )

        paths_immediate = {d.path for d in manager.grabbed_devices.get(hardware_id, [])}
        assert mouse_path in paths_immediate
        assert keyboard_path in paths_immediate

        await asyncio.sleep(0.14)

        paths_after = {d.path for d in manager.grabbed_devices.get(hardware_id, [])}
        assert mouse_path in paths_after
        assert keyboard_path not in paths_after

        await self._send_command(
            reader,
            writer,
            Command(
                command=CommandType.RELEASE_DEVICE,
                data={"hardware_id": hardware_id, "immediate": True},
            ),
        )

        writer.close()
        await writer.wait_closed()

    async def test_profile_mapping_switch_while_held_defers_until_release(
        self,
        full_system,
        virtual_mouse,
    ):
        _server, manager = full_system
        mouse_path = virtual_mouse.device.path
        hardware_id = "1234:5678"

        reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))

        await self._send_command(
            reader,
            writer,
            Command(
                command=CommandType.GRAB_DEVICE,
                data={
                    "hardware_id": hardware_id,
                    "evdev_paths": [mouse_path],
                    "button_map": {"btn_side": "btn_side"},
                },
            ),
        )

        await self._send_command(
            reader,
            writer,
            Command(
                command=CommandType.SET_MAPPING,
                data={
                    "hardware_id": hardware_id,
                    "mapping": {
                        "btn_side": {"action": "keyboard", "target": "key_a"},
                    },
                },
            ),
        )

        virtual_mouse.write(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 1)
        virtual_mouse.syn()
        await asyncio.sleep(0.05)

        await self._send_command(
            reader,
            writer,
            Command(
                command=CommandType.SET_MAPPING,
                data={
                    "hardware_id": hardware_id,
                    "mapping": {
                        "btn_side": {"action": "keyboard", "target": "key_b"},
                    },
                },
            ),
        )

        virtual_mouse.write(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 0)
        virtual_mouse.syn()
        await asyncio.sleep(0.08)

        grabbed = manager.grabbed_devices[hardware_id][0]
        assert grabbed.state.held_source_actions == {}
        assert evdev.ecodes.KEY_A not in grabbed.state.held_output_keys["keyboard"]
        assert evdev.ecodes.KEY_B not in grabbed.state.held_output_keys["keyboard"]
        assert grabbed.state.rapidfire_active.get("btn_side", False) is False

        await self._send_command(
            reader,
            writer,
            Command(
                command=CommandType.RELEASE_DEVICE,
                data={"hardware_id": hardware_id, "immediate": True},
            ),
        )

        writer.close()
        await writer.wait_closed()

    async def test_release_deferred_while_button_held(self, full_system, virtual_mouse):
        _server, manager = full_system
        mouse_path = virtual_mouse.device.path
        hardware_id = "1234:5678"

        reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))
        try:
            await self._send_command(
                reader,
                writer,
                Command(
                    command=CommandType.GRAB_DEVICE,
                    data={
                        "hardware_id": hardware_id,
                        "evdev_paths": [mouse_path],
                        "button_map": {"btn_side": "btn_side"},
                    },
                ),
            )

            await self._send_command(
                reader,
                writer,
                Command(
                    command=CommandType.SET_MAPPING,
                    data={
                        "hardware_id": hardware_id,
                        "mapping": {
                            "btn_side": {"action": "keyboard", "target": "key_a"},
                        },
                    },
                ),
            )

            virtual_mouse.write(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 1)
            virtual_mouse.syn()
            await asyncio.sleep(0.04)

            release_data = await self._send_command(
                reader,
                writer,
                Command(
                    command=CommandType.RELEASE_DEVICE,
                    data={"hardware_id": hardware_id, "grace_s": 0.03},
                ),
            )
            assert release_data.get("scheduled") is True

            await asyncio.sleep(0.06)
            assert hardware_id in manager.grabbed_devices

            virtual_mouse.write(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 0)
            virtual_mouse.syn()

            await asyncio.sleep(0.12)
            assert hardware_id not in manager.grabbed_devices
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_combo_single_step_is_transparent_until_match_then_recalls(
        self,
        full_system,
        virtual_keyboard,
    ):
        _server, manager = full_system
        keyboard_path = virtual_keyboard.device.path
        hardware_id = "abcd:ef01"

        result = await manager.grab_device(
            hardware_id=hardware_id,
            evdev_paths=[keyboard_path],
            button_map={
                "key_leftctrl": "key_leftctrl",
                "key_a": "key_a",
            },
        )
        assert result["grabbed"] is True

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Quick Action",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "evdev": "key_leftctrl",
                                },
                                {
                                    "hardware_id": hardware_id,
                                    "evdev": "key_a",
                                },
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                }
            ]
        )

        manager.output_state.keyboard_uinput = Mock()
        grabbed = manager.grabbed_devices[hardware_id][0]

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        assert evdev.ecodes.KEY_LEFTCTRL in grabbed.state.held_output_keys["passthrough"]
        assert manager.output_state.keyboard_uinput.write.call_count == 0

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        assert evdev.ecodes.KEY_LEFTCTRL in grabbed.state.held_output_keys["passthrough"]
        assert evdev.ecodes.KEY_A not in grabbed.state.held_output_keys["passthrough"]
        assert manager.output_state.keyboard_uinput.write.call_args_list[0].args == (
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            1,
        )

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        assert manager.output_state.keyboard_uinput.write.call_args_list[1].args == (
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            0,
        )

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        assert grabbed.state.held_output_keys["passthrough"] == set()

    async def test_combo_multi_step_timeout_starts_after_release_phase(
        self,
        full_system,
        virtual_keyboard,
    ):
        _server, manager = full_system
        keyboard_path = virtual_keyboard.device.path
        hardware_id = "abcd:ef01"

        result = await manager.grab_device(
            hardware_id=hardware_id,
            evdev_paths=[keyboard_path],
            button_map={
                "key_leftctrl": "key_leftctrl",
                "key_a": "key_a",
                "key_1": "key_1",
            },
        )
        assert result["grabbed"] is True

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Quick Action",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "evdev": "key_leftctrl",
                                },
                                {
                                    "hardware_id": hardware_id,
                                    "evdev": "key_a",
                                },
                            ]
                        },
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "evdev": "key_1",
                                }
                            ],
                            "timeout_ms": 80,
                        },
                    ],
                    "action": {"action": "keyboard", "target": "key_f14"},
                }
            ]
        )

        manager.output_state.keyboard_uinput = Mock()
        grabbed = manager.grabbed_devices[hardware_id][0]

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.05)
        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.05)

        assert manager.combo_state.engine.next_deadline() is None

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.05)
        assert manager.combo_state.engine.next_deadline() is None

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.05)
        assert manager.combo_state.engine.next_deadline() is not None

        await asyncio.sleep(0.12)
        assert manager.combo_state.engine.next_deadline() is None

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        assert evdev.ecodes.KEY_1 in grabbed.state.held_output_keys["passthrough"]
        assert manager.output_state.keyboard_uinput.write.call_count == 0

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        assert grabbed.state.held_output_keys["passthrough"] == set()

    async def test_combo_single_step_rearms_when_modifier_stays_held(
        self,
        full_system,
        virtual_keyboard,
    ):
        _server, manager = full_system
        keyboard_path = virtual_keyboard.device.path
        hardware_id = "abcd:ef01"

        result = await manager.grab_device(
            hardware_id=hardware_id,
            evdev_paths=[keyboard_path],
            button_map={
                "key_leftctrl": "key_leftctrl",
                "key_1": "key_1",
            },
        )
        assert result["grabbed"] is True

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Repeatable Combo",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "evdev": "key_leftctrl",
                                },
                                {
                                    "hardware_id": hardware_id,
                                    "evdev": "key_1",
                                },
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                }
            ]
        )

        manager.output_state.keyboard_uinput = Mock()
        grabbed = manager.grabbed_devices[hardware_id][0]

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        press_release_values = [
            call.args[2]
            for call in manager.output_state.keyboard_uinput.write.call_args_list
            if call.args[:2] == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13)
        ]
        assert press_release_values == [1, 0, 1, 0]
        assert evdev.ecodes.KEY_LEFTCTRL in grabbed.state.held_output_keys["passthrough"]

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        assert grabbed.state.held_output_keys["passthrough"] == set()

    async def test_combo_single_step_releasing_non_completing_key_stops_action(
        self,
        full_system,
        virtual_keyboard,
    ):
        _server, manager = full_system
        keyboard_path = virtual_keyboard.device.path
        hardware_id = "abcd:ef01"

        result = await manager.grab_device(
            hardware_id=hardware_id,
            evdev_paths=[keyboard_path],
            button_map={
                "key_leftalt": "key_leftalt",
                "key_2": "key_2",
            },
        )
        assert result["grabbed"] is True

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Alt Two",
                    "steps": [
                        {
                            "events": [
                                {"hardware_id": hardware_id, "evdev": "key_leftalt"},
                                {"hardware_id": hardware_id, "evdev": "key_2"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                }
            ]
        )

        manager.output_state.keyboard_uinput = Mock()

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTALT, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_2, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTALT, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        press_release_values = [
            call.args[2]
            for call in manager.output_state.keyboard_uinput.write.call_args_list
            if call.args[:2] == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13)
        ]
        assert press_release_values == [1, 0]

    async def test_combo_single_step_rearm_keeps_other_modifier_combos_available(
        self,
        full_system,
        virtual_keyboard,
    ):
        _server, manager = full_system
        keyboard_path = virtual_keyboard.device.path
        hardware_id = "abcd:ef01"

        result = await manager.grab_device(
            hardware_id=hardware_id,
            evdev_paths=[keyboard_path],
            button_map={
                "key_leftctrl": "key_leftctrl",
                "key_1": "key_1",
                "key_2": "key_2",
            },
        )
        assert result["grabbed"] is True

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "First Combo",
                    "steps": [
                        {
                            "events": [
                                {"hardware_id": hardware_id, "evdev": "key_leftctrl"},
                                {"hardware_id": hardware_id, "evdev": "key_1"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                },
                {
                    "id": "combo-2",
                    "name": "Second Combo",
                    "steps": [
                        {
                            "events": [
                                {"hardware_id": hardware_id, "evdev": "key_leftctrl"},
                                {"hardware_id": hardware_id, "evdev": "key_2"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f14"},
                },
            ]
        )

        manager.output_state.keyboard_uinput = Mock()

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)
        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_2, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)
        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_2, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        output_codes = [
            call.args[1]
            for call in manager.output_state.keyboard_uinput.write.call_args_list
            if call.args[0] == evdev.ecodes.EV_KEY and call.args[2] == 1
        ]
        assert evdev.ecodes.KEY_F13 in output_codes
        assert evdev.ecodes.KEY_F14 in output_codes

    async def test_combo_single_step_held_completing_key_allows_sibling_combo(
        self,
        full_system,
        virtual_keyboard,
    ):
        _server, manager = full_system
        keyboard_path = virtual_keyboard.device.path
        hardware_id = "abcd:ef01"

        result = await manager.grab_device(
            hardware_id=hardware_id,
            evdev_paths=[keyboard_path],
            button_map={
                "key_leftalt": "key_leftalt",
                "key_1": "key_1",
                "key_2": "key_2",
            },
        )
        assert result["grabbed"] is True

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "First Combo",
                    "steps": [
                        {
                            "events": [
                                {"hardware_id": hardware_id, "evdev": "key_leftalt"},
                                {"hardware_id": hardware_id, "evdev": "key_1"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                },
                {
                    "id": "combo-2",
                    "name": "Second Combo",
                    "steps": [
                        {
                            "events": [
                                {"hardware_id": hardware_id, "evdev": "key_leftalt"},
                                {"hardware_id": hardware_id, "evdev": "key_2"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f14"},
                },
            ]
        )

        manager.output_state.keyboard_uinput = Mock()

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTALT, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_2, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        output_presses = [
            call.args[1]
            for call in manager.output_state.keyboard_uinput.write.call_args_list
            if call.args[0] == evdev.ecodes.EV_KEY and call.args[2] == 1
        ]
        assert output_presses == [evdev.ecodes.KEY_F13, evdev.ecodes.KEY_F14]

    async def test_combo_single_step_releasing_one_active_sibling_stops_only_that_action(
        self,
        full_system,
        virtual_keyboard,
    ):
        _server, manager = full_system
        keyboard_path = virtual_keyboard.device.path
        hardware_id = "abcd:ef01"

        result = await manager.grab_device(
            hardware_id=hardware_id,
            evdev_paths=[keyboard_path],
            button_map={
                "key_leftalt": "key_leftalt",
                "key_1": "key_1",
                "key_2": "key_2",
            },
        )
        assert result["grabbed"] is True

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "First Combo",
                    "steps": [
                        {
                            "events": [
                                {"hardware_id": hardware_id, "evdev": "key_leftalt"},
                                {"hardware_id": hardware_id, "evdev": "key_1"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                },
                {
                    "id": "combo-2",
                    "name": "Second Combo",
                    "steps": [
                        {
                            "events": [
                                {"hardware_id": hardware_id, "evdev": "key_leftalt"},
                                {"hardware_id": hardware_id, "evdev": "key_2"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f14"},
                },
            ]
        )

        manager.output_state.keyboard_uinput = Mock()

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTALT, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_2, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_2, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        writes = [
            call.args
            for call in manager.output_state.keyboard_uinput.write.call_args_list
            if call.args[0] == evdev.ecodes.EV_KEY
            and call.args[1] in {evdev.ecodes.KEY_F13, evdev.ecodes.KEY_F14}
        ]
        assert writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 0),
        ]
