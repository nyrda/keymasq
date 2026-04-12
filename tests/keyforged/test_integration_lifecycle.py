# ruff: noqa: F403, F405, I001
from tests.keyforged.integration_support import *


@pytest.mark.skipif(not os.access("/dev/uinput", os.W_OK), reason="No uinput access")
@pytest.mark.asyncio
class TestIntegrationLifecycle(IntegrationTestBase):
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
