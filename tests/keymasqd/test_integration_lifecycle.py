import asyncio
import os
from unittest.mock import Mock

import evdev
import pytest

from keymasq.common import paths
from keymasq.common.ipc import Command, CommandType
from tests.keymasqd.integration_support import IntegrationTestBase


@pytest.mark.skipif(not os.access("/dev/uinput", os.W_OK), reason="No uinput access")
@pytest.mark.asyncio
class TestIntegrationLifecycle(IntegrationTestBase):
    async def test_full_workflow(self, full_system, virtual_mouse):
        _server, _manager = full_system
        mouse_path = virtual_mouse.device.path

        reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))

        grab_data = await self._send_command(
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
        assert grab_data["grabbed"] is True

        mapping_data = await self._send_command(
            reader,
            writer,
            Command(
                command=CommandType.SET_MAPPING,
                data={
                    "hardware_id": "1234:5678",
                    "mapping": {
                        "btn_side": {"action": "keyboard", "target": "key_a"},
                    },
                },
            ),
        )
        assert mapping_data["updated"] is True

        await self._send_command(
            reader,
            writer,
            Command(
                command=CommandType.RELEASE_DEVICE,
                data={"hardware_id": "1234:5678"},
            ),
        )

        writer.close()
        await writer.wait_closed()

    async def test_event_passthrough(self, full_system, virtual_mouse):
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
                    "button_map": {"btn_left": "btn_left"},
                },
            ),
        )

        grabbed = manager.grabbed_devices["1234:5678"][0]
        original_passthrough = grabbed.uinput
        assert original_passthrough is not None
        passthrough = Mock()
        grabbed.uinput = passthrough  # type: ignore[assignment]
        original_passthrough.close()

        virtual_mouse.write(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 1)
        virtual_mouse.syn()
        await self._wait_until(
            lambda: any(
                call.args == (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 1)
                for call in passthrough.write.call_args_list
            ),
            reason="BTN_LEFT passthrough press",
        )
        assert evdev.ecodes.BTN_LEFT in grabbed.state.held_output_keys["passthrough"]

        virtual_mouse.write(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 0)
        virtual_mouse.syn()
        await self._wait_until(
            lambda: any(
                call.args == (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 0)
                for call in passthrough.write.call_args_list
            ),
            reason="BTN_LEFT passthrough release",
        )
        assert evdev.ecodes.BTN_LEFT not in grabbed.state.held_output_keys["passthrough"]

        await self._send_command(
            reader,
            writer,
            Command(command=CommandType.RELEASE_DEVICE, data={"hardware_id": "1234:5678"}),
        )

        writer.close()
        await writer.wait_closed()

    async def test_keyboard_with_rapidfire(self, full_system, virtual_mouse):
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

        grabbed = manager.grabbed_devices["1234:5678"][0]
        keyboard_output = Mock()
        grabbed.keyboard_uinput = keyboard_output  # type: ignore[assignment]

        await self._send_command(
            reader,
            writer,
            Command(
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
            ),
        )

        def target_writes(value: int) -> list:
            return [
                call
                for call in keyboard_output.write.call_args_list
                if call.args == (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, value)
            ]

        virtual_mouse.write(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 1)
        virtual_mouse.syn()
        await self._wait_until(
            lambda: grabbed.state.rapidfire_active.get("btn_side") is True,
            reason="rapidfire active",
        )
        await self._wait_until(
            lambda: len(target_writes(1)) >= 2,
            reason="repeated BTN_LEFT rapidfire presses",
        )
        assert target_writes(0)

        virtual_mouse.write(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 0)
        virtual_mouse.syn()
        await self._wait_until(
            lambda: (
                grabbed.state.rapidfire_active.get("btn_side", False) is False
                and "btn_side" not in grabbed.state.rapidfire_tasks
            ),
            reason="rapidfire stop",
        )
        await self._wait_until(
            lambda: evdev.ecodes.BTN_LEFT not in grabbed.state.held_output_keys["keyboard"],
            reason="rapidfire output release",
        )
        stopped_write_count = len(keyboard_output.write.call_args_list)
        await asyncio.sleep(0.08)
        assert len(keyboard_output.write.call_args_list) == stopped_write_count
        assert grabbed.state.rapidfire_outputs == {}

        await self._send_command(
            reader,
            writer,
            Command(command=CommandType.RELEASE_DEVICE, data={"hardware_id": "1234:5678"}),
        )

        writer.close()
        await writer.wait_closed()

    async def test_disconnect_releases_devices(self, full_system, virtual_mouse):
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

        assert "1234:5678" in manager.grabbed_devices

        writer.close()
        await writer.wait_closed()

        await self._wait_until(
            lambda: "1234:5678" not in manager.grabbed_devices,
            reason="disconnect releases devices",
        )
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

        await self._wait_until(
            lambda: "1234:5678" in manager.grab_state.pending_hardware_release,
            reason="hardware release grace scheduled",
        )

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

        await self._wait_until(
            lambda: (
                "1234:5678" not in manager.grab_state.pending_hardware_release
                and "1234:5678" in manager.grabbed_devices
                and any(d.path == mouse_path for d in manager.grabbed_devices["1234:5678"])
            ),
            reason="pending hardware release canceled by regrab",
        )
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

        await self._wait_until(
            lambda: (
                keyboard_path not in {d.path for d in manager.grabbed_devices.get(hardware_id, [])}
            ),
            reason="unused interface release after grace",
        )

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

        grabbed = manager.grabbed_devices[hardware_id][0]
        virtual_mouse.write(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 1)
        virtual_mouse.syn()
        await self._wait_until(
            lambda: "btn_side" in grabbed.state.held_source_actions,
            reason="held source button",
        )

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
        await self._wait_until(
            lambda: (
                grabbed.state.held_source_actions == {}
                and evdev.ecodes.KEY_A not in grabbed.state.held_output_keys["keyboard"]
                and evdev.ecodes.KEY_B not in grabbed.state.held_output_keys["keyboard"]
                and grabbed.state.rapidfire_active.get("btn_side", False) is False
            ),
            reason="held mapping release cleanup",
        )
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
            grabbed = manager.grabbed_devices[hardware_id][0]
            await self._wait_until(
                lambda: "btn_side" in grabbed.state.held_source_actions,
                reason="held source button",
            )

            release_data = await self._send_command(
                reader,
                writer,
                Command(
                    command=CommandType.RELEASE_DEVICE,
                    data={"hardware_id": hardware_id, "grace_s": 0.03},
                ),
            )
            assert release_data.get("scheduled") is True

            grace_elapsed_at = asyncio.get_running_loop().time() + 0.04
            await self._wait_until(
                lambda: (
                    asyncio.get_running_loop().time() >= grace_elapsed_at
                    and hardware_id in manager.grabbed_devices
                ),
                reason="device remains grabbed while button is held",
            )
            assert hardware_id in manager.grabbed_devices

            virtual_mouse.write(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 0)
            virtual_mouse.syn()

            await self._wait_until(
                lambda: hardware_id not in manager.grabbed_devices,
                reason="device release after held button release",
            )
            assert hardware_id not in manager.grabbed_devices
        finally:
            writer.close()
            await writer.wait_closed()
