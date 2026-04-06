import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import evdev
import pytest

from keyforge.keyforged.runtime import grabbed_device as gdm
from keyforge.keyforged.runtime.grabbed_device import GrabbedDevice


@pytest.mark.skipif(not os.access("/dev/uinput", os.W_OK), reason="No uinput access")
class TestGrabbedDevice:
    @pytest.fixture
    def event_callback(self):
        return AsyncMock()

    @pytest.fixture
    def mapping_getter(self):
        return lambda: {}

    @pytest.mark.asyncio
    async def test_grab_device(self, virtual_mouse, event_callback, mapping_getter):
        device_path = virtual_mouse.device.path

        grabbed = GrabbedDevice(
            path=device_path,
            hardware_id="test:device",
            button_map={"btn_left": "btn_left"},
            mapping_getter=mapping_getter,
            event_callback=event_callback,
        )

        await grabbed.grab()

        assert grabbed.device is not None
        assert grabbed.uinput is not None
        assert grabbed._running is True

        await grabbed.release()

    @pytest.mark.asyncio
    async def test_release_device(self, virtual_mouse, event_callback, mapping_getter):
        device_path = virtual_mouse.device.path

        grabbed = GrabbedDevice(
            path=device_path,
            hardware_id="test:device",
            button_map={},
            mapping_getter=mapping_getter,
            event_callback=event_callback,
        )

        await grabbed.grab()
        await grabbed.release()

        assert grabbed._running is False

    @pytest.mark.asyncio
    async def test_event_callback_called(self, virtual_mouse, event_callback, mapping_getter):
        device_path = virtual_mouse.device.path

        grabbed = GrabbedDevice(
            path=device_path,
            hardware_id="test:device",
            button_map={},
            mapping_getter=mapping_getter,
            event_callback=event_callback,
        )

        await grabbed.grab()

        virtual_mouse.write(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 1)
        virtual_mouse.syn()

        await asyncio.sleep(0.2)

        await grabbed.release()

        assert event_callback.call_count >= 1


class TestActionExecution:
    def test_resolve_code_btn_left(self):
        code = gdm.resolve_output_code("btn_left")
        assert code == evdev.ecodes.BTN_LEFT

    def test_resolve_code_key_a(self):
        code = gdm.resolve_output_code("key_a")
        assert code == evdev.ecodes.KEY_A

    def test_resolve_code_unknown(self):
        code = gdm.resolve_output_code("unknown_key_xyz")
        assert code is None

    @pytest.mark.asyncio
    async def test_tap_key(self):
        grabbed = GrabbedDevice(
            path="/dev/input/event0",
            hardware_id="test",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=lambda *args: None,
        )

        grabbed.uinput = MagicMock()
        grabbed.uinput.write = MagicMock()
        grabbed.uinput.syn = MagicMock()
        grabbed.keyboard_uinput = grabbed.uinput

        await gdm.tap_key(
            grabbed,
            evdev.ecodes.KEY_A,
            hold_ms=1,
            event_name="test_key",
            uinput_dev=grabbed.uinput,
            asyncio_mod=asyncio,
        )

        assert grabbed.uinput.write.call_count == 2
        assert grabbed.uinput.syn.call_count == 2


class TestPassthrough:
    def test_relative_mouse_movement_is_suppressed_when_filter_active(self):
        passthrough = MagicMock()

        grabbed = GrabbedDevice(
            path="/dev/input/event0",
            hardware_id="test",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=lambda *args: None,
            suppress_rel_getter=lambda: True,
        )
        grabbed.uinput = passthrough

        event = evdev.InputEvent(
            10,
            100,
            evdev.ecodes.EV_REL,
            evdev.ecodes.REL_X,
            10,
        )

        gdm.passthrough(grabbed, event, evdev_mod=evdev, uinput_writer=lambda device: device)
        passthrough.write.assert_not_called()

    def test_relative_mouse_movement_is_not_suppressed_without_filter(self):
        passthrough = MagicMock()

        grabbed = GrabbedDevice(
            path="/dev/input/event0",
            hardware_id="test",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=lambda *args: None,
            suppress_rel_getter=lambda: False,
        )
        grabbed.uinput = passthrough

        event = evdev.InputEvent(
            10,
            101,
            evdev.ecodes.EV_REL,
            evdev.ecodes.REL_X,
            12,
        )

        gdm.passthrough(grabbed, event, evdev_mod=evdev, uinput_writer=lambda device: device)
        passthrough.write.assert_called_once_with(evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 12)
