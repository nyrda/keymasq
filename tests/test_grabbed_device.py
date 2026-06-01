import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import evdev
import pytest

from keymasq.keymasqd.output_helpers import resolve_output_code
from keymasq.keymasqd.runtime import grabbed_device as gdm
from keymasq.keymasqd.runtime import grabbed_device_outputs as gdo
from keymasq.keymasqd.runtime import grabbed_device_repeat as gdr
from keymasq.keymasqd.runtime.grabbed_device import GrabbedDevice


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


@pytest.mark.asyncio
async def test_grab_failure_closes_opened_input_device(monkeypatch):
    fake_device = SimpleNamespace(
        name="fake input",
        info=SimpleNamespace(vendor=None, product=None, version=None, bustype=None),
        capabilities=MagicMock(
            return_value={
                evdev.ecodes.EV_SYN: [],
                evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_LEFT],
            }
        ),
        close=MagicMock(),
        grab=MagicMock(),
    )
    passthrough_uinput = MagicMock()

    async def fail_wait(*args, **kwargs):
        raise RuntimeError("grab preflight failed")

    monkeypatch.setattr(gdm, "_device_input", lambda path: fake_device)
    monkeypatch.setattr(gdm.evdev, "UInput", lambda **kwargs: passthrough_uinput)
    monkeypatch.setattr(gdm.runtime_grab, "wait_for_active_keys_to_clear", fail_wait)

    grabbed = GrabbedDevice(
        path="/dev/input/event-test",
        hardware_id="test:device",
        button_map={},
        mapping_getter=lambda: {},
        event_callback=AsyncMock(),
    )

    with pytest.raises(RuntimeError, match="grab preflight failed"):
        await grabbed.grab()

    fake_device.close.assert_called_once_with()
    passthrough_uinput.close.assert_called_once_with()
    assert grabbed.device is None
    assert grabbed.uinput is None


@pytest.mark.asyncio
async def test_grab_failure_closes_input_device_when_passthrough_creation_fails(
    monkeypatch,
):
    fake_device = SimpleNamespace(
        name="fake input",
        info=SimpleNamespace(vendor=None, product=None, version=None, bustype=None),
        capabilities=MagicMock(
            return_value={
                evdev.ecodes.EV_SYN: [],
                evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_LEFT],
            }
        ),
        close=MagicMock(),
        grab=MagicMock(),
    )
    wait_for_active_keys_to_clear = AsyncMock()

    def fail_uinput_creation(**kwargs):
        raise RuntimeError("passthrough creation failed")

    monkeypatch.setattr(gdm, "_device_input", lambda path: fake_device)
    monkeypatch.setattr(gdm.evdev, "UInput", fail_uinput_creation)
    monkeypatch.setattr(
        gdm.runtime_grab,
        "wait_for_active_keys_to_clear",
        wait_for_active_keys_to_clear,
    )

    grabbed = GrabbedDevice(
        path="/dev/input/event-test",
        hardware_id="test:device",
        button_map={},
        mapping_getter=lambda: {},
        event_callback=AsyncMock(),
    )

    with pytest.raises(RuntimeError, match="passthrough creation failed"):
        await grabbed.grab()

    fake_device.close.assert_called_once_with()
    fake_device.grab.assert_not_called()
    wait_for_active_keys_to_clear.assert_not_awaited()
    assert grabbed.device is None
    assert grabbed.uinput is None


class TestActionExecution:
    def test_resolve_code_btn_left(self):
        code = resolve_output_code("btn_left")
        assert code == evdev.ecodes.BTN_LEFT

    def test_resolve_code_key_a(self):
        code = resolve_output_code("key_a")
        assert code == evdev.ecodes.KEY_A

    def test_resolve_code_unknown(self):
        code = resolve_output_code("unknown_key_xyz")
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

        await gdr.tap_key(
            grabbed,
            evdev.ecodes.KEY_A,
            hold_ms=1,
            event_name="test_key",
            uinput_dev=grabbed.uinput,
            asyncio_mod=gdm.ASYNCIO_RUNTIME,
        )

        assert grabbed.uinput.write.call_count == 2
        assert grabbed.uinput.syn.call_count == 2

    @pytest.mark.asyncio
    async def test_tap_key_output_failure_propagates_and_cleans_state(self):
        grabbed = GrabbedDevice(
            path="/dev/input/event0",
            hardware_id="test",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=lambda *args: None,
        )
        grabbed.uinput = MagicMock()
        grabbed.uinput.write.side_effect = RuntimeError("write failed")
        grabbed.keyboard_uinput = grabbed.uinput
        grabbed.state.tap_active["test_key"] = True
        started = asyncio.Event()

        with pytest.raises(RuntimeError, match="write failed"):
            await gdr.tap_key(
                grabbed,
                evdev.ecodes.KEY_A,
                hold_ms=1,
                event_name="test_key",
                uinput_dev=grabbed.uinput,
                asyncio_mod=gdm.ASYNCIO_RUNTIME,
                started=started,
            )

        assert "test_key" not in grabbed.state.tap_active
        assert started.is_set()

    @pytest.mark.asyncio
    async def test_rapidfire_key_output_failure_propagates_and_cleans_state(self):
        grabbed = GrabbedDevice(
            path="/dev/input/event0",
            hardware_id="test",
            button_map={},
            mapping_getter=lambda: {},
            event_callback=lambda *args: None,
        )
        grabbed.uinput = MagicMock()
        grabbed.uinput.write.side_effect = RuntimeError("write failed")
        grabbed.keyboard_uinput = grabbed.uinput
        grabbed._running = True
        event_name = "test_key"
        grabbed.state.rapidfire_active[event_name] = True
        grabbed.state.rapidfire_tasks[event_name] = asyncio.current_task()
        grabbed.state.rapidfire_outputs[event_name] = gdr.RapidfireOutputState(
            kind="key",
            code=evdev.ecodes.KEY_A,
            uinput=grabbed.uinput,
        )
        started = asyncio.Event()

        with pytest.raises(RuntimeError, match="write failed"):
            await gdr.rapidfire_key(
                grabbed,
                evdev.ecodes.KEY_A,
                hold_ms=1,
                wait_ms=1,
                event_name=event_name,
                uinput_dev=grabbed.uinput,
                asyncio_mod=gdm.ASYNCIO_RUNTIME,
                started=started,
            )

        assert event_name not in grabbed.state.rapidfire_active
        assert event_name not in grabbed.state.rapidfire_tasks
        assert event_name not in grabbed.state.rapidfire_outputs
        assert started.is_set()


def test_release_all_keys_resets_abs_only_dynamic_gamepad_output():
    pad2_uinput = MagicMock()
    resolver = MagicMock(return_value=SimpleNamespace(uinput=pad2_uinput))

    grabbed = GrabbedDevice(
        path="/dev/input/event0",
        hardware_id="test",
        button_map={},
        mapping_getter=lambda: {},
        event_callback=lambda *args: None,
        gamepad_output_resolver=resolver,
    )
    grabbed.state.held_output_abs["gamepad:pad2"] = {evdev.ecodes.ABS_X}

    gdo.release_all_keys(
        grabbed,
        evdev_mod=evdev,
        uinput_writer=lambda device: device,
    )

    resolver.assert_called_once_with("pad2", "release tracked gamepad:pad2")
    pad2_uinput.write.assert_called_once_with(
        evdev.ecodes.EV_ABS,
        evdev.ecodes.ABS_X,
        0,
    )
    pad2_uinput.syn.assert_called_once_with()
    assert grabbed.state.held_output_abs["gamepad:pad2"] == set()


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

        gdo.passthrough(grabbed, event, evdev_mod=evdev, uinput_writer=lambda device: device)
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

        gdo.passthrough(grabbed, event, evdev_mod=evdev, uinput_writer=lambda device: device)
        passthrough.write.assert_called_once_with(evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 12)
