import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import evdev
import pytest
from evdev.uinput import UInputError

from keymasq.common.model.core import DeviceType
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.output_helpers import resolve_output_code
from keymasq.keymasqd.permission_hints import UINPUT_PERMISSION_HINT
from keymasq.keymasqd.runtime import adapters
from keymasq.keymasqd.runtime.grabbed_device import device as grabbed_device
from keymasq.keymasqd.runtime.grabbed_device import outputs, repeat
from keymasq.keymasqd.runtime.grabbed_device.device import GrabbedDevice


async def _wait_for_uinput_events(
    uinput: evdev.UInput,
    expected: set[tuple[int, int, int]],
    *,
    timeout_s: float = 1.0,
) -> set[tuple[int, int, int]]:
    seen: set[tuple[int, int, int]] = set()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        try:
            seen.update(
                (int(event.type), int(event.code), int(event.value)) for event in uinput.read()
            )
        except BlockingIOError:
            pass
        if expected <= seen:
            return seen
        await asyncio.sleep(0.01)
    raise AssertionError(f"Timed out waiting for uinput events {sorted(expected - seen)}")


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
        try:
            assert grabbed.device is not None
            assert grabbed.uinput is not None
            assert grabbed.running is True
        finally:
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

        assert grabbed.running is False

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
        try:
            virtual_mouse.write(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 1)
            virtual_mouse.syn()

            await asyncio.sleep(0.2)
        finally:
            await grabbed.release()

        assert event_callback.call_count >= 1

    @pytest.mark.asyncio
    async def test_passthrough_led_and_sound_feedback_reaches_source(
        self,
        virtual_feedback_keyboard,
        event_callback,
        mapping_getter,
    ) -> None:
        grabbed = GrabbedDevice(
            path=virtual_feedback_keyboard.device.path,
            hardware_id="test:feedback-keyboard",
            button_map={},
            mapping_getter=mapping_getter,
            event_callback=event_callback,
        )

        await grabbed.grab()
        try:
            assert grabbed.uinput is not None
            assert grabbed.output_feedback_proxy is not None
            passthrough_caps = grabbed.uinput.capabilities()
            assert evdev.ecodes.LED_CAPSL in passthrough_caps[evdev.ecodes.EV_LED]
            assert evdev.ecodes.SND_BELL in passthrough_caps[evdev.ecodes.EV_SND]

            grabbed.uinput.device.write(
                evdev.ecodes.EV_LED,
                evdev.ecodes.LED_CAPSL,
                1,
            )
            grabbed.uinput.device.write(
                evdev.ecodes.EV_SND,
                evdev.ecodes.SND_BELL,
                1,
            )
            await _wait_for_uinput_events(
                virtual_feedback_keyboard,
                {
                    (evdev.ecodes.EV_LED, evdev.ecodes.LED_CAPSL, 1),
                    (evdev.ecodes.EV_SND, evdev.ecodes.SND_BELL, 1),
                },
            )
        finally:
            await grabbed.release()

    @pytest.mark.asyncio
    async def test_synthetic_outputs_do_not_advertise_output_feedback(
        self,
        virtual_feedback_keyboard,
        event_callback,
        mapping_getter,
    ) -> None:
        grabbed = GrabbedDevice(
            path=virtual_feedback_keyboard.device.path,
            hardware_id="test:global-feedback-keyboard",
            button_map={},
            mapping_getter=mapping_getter,
            event_callback=event_callback,
        )
        manager = DeviceManager()

        await grabbed.grab()
        manager.grabbed_devices[grabbed.hardware_id] = [grabbed]
        manager.initialize_output_devices()
        try:
            keyboard = manager.output_state.keyboard_uinput
            mouse = manager.output_state.mouse_uinput
            gamepad = manager.output_state.gamepad_uinput
            assert keyboard is not None
            assert mouse is not None
            assert gamepad is not None

            for output in (keyboard, mouse, gamepad):
                assert evdev.ecodes.EV_FF not in output.capabilities()
            assert evdev.ecodes.EV_LED not in keyboard.capabilities()
            assert evdev.ecodes.EV_SND not in keyboard.capabilities()
        finally:
            manager.shutdown_output_devices()
            manager.grabbed_devices.clear()
            await grabbed.release()

    @pytest.mark.asyncio
    async def test_passthrough_force_feedback_capabilities_and_gain_reach_source(
        self,
        virtual_force_feedback_device,
        event_callback,
        mapping_getter,
    ) -> None:
        grabbed = GrabbedDevice(
            path=virtual_force_feedback_device.device.path,
            hardware_id="test:force-feedback",
            button_map={},
            mapping_getter=mapping_getter,
            event_callback=event_callback,
            device_type=DeviceType.OTHER,
            device_types=[DeviceType.OTHER.value],
        )

        await grabbed.grab()
        try:
            assert grabbed.uinput is not None
            assert grabbed.output_feedback_proxy is not None
            source_caps = virtual_force_feedback_device.capabilities()
            passthrough_caps = grabbed.uinput.capabilities()
            assert set(passthrough_caps[evdev.ecodes.EV_FF]) == set(source_caps[evdev.ecodes.EV_FF])
            assert grabbed.uinput.device.ff_effects_count == (
                virtual_force_feedback_device.device.ff_effects_count
            )

            grabbed.uinput.device.write(
                evdev.ecodes.EV_FF,
                evdev.ecodes.FF_GAIN,
                37,
            )
            await _wait_for_uinput_events(
                virtual_force_feedback_device,
                {(evdev.ecodes.EV_FF, evdev.ecodes.FF_GAIN, 37)},
            )
        finally:
            await grabbed.release()


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

    monkeypatch.setattr(grabbed_device, "_device_input", lambda path: fake_device)
    monkeypatch.setattr(grabbed_device.evdev, "UInput", lambda **kwargs: passthrough_uinput)
    monkeypatch.setattr(grabbed_device.grab, "wait_for_active_keys_to_clear", fail_wait)

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

    monkeypatch.setattr(grabbed_device, "_device_input", lambda path: fake_device)
    monkeypatch.setattr(grabbed_device.evdev, "UInput", fail_uinput_creation)
    monkeypatch.setattr(
        grabbed_device.grab,
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


@pytest.mark.asyncio
async def test_grab_uinput_error_mentions_uinput_when_passthrough_creation_fails(
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

    def fail_uinput_creation(**kwargs):
        raise UInputError('"/dev/uinput" cannot be opened for writing')

    monkeypatch.setattr(grabbed_device, "_device_input", lambda path: fake_device)
    monkeypatch.setattr(grabbed_device.evdev, "UInput", fail_uinput_creation)

    grabbed = GrabbedDevice(
        path="/dev/input/event-test",
        hardware_id="test:device",
        button_map={},
        mapping_getter=lambda: {},
        event_callback=AsyncMock(),
    )

    with pytest.raises(PermissionError) as excinfo:
        await grabbed.grab()

    message = str(excinfo.value)
    assert "passthrough uinput device" in message
    assert UINPUT_PERMISSION_HINT in message
    fake_device.close.assert_called_once_with()
    fake_device.grab.assert_not_called()
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

        await repeat.tap_key(
            grabbed,
            evdev.ecodes.KEY_A,
            hold_ms=1,
            event_name="test_key",
            uinput_dev=grabbed.uinput,
            asyncio_mod=adapters.ASYNCIO_RUNTIME,
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
            await repeat.tap_key(
                grabbed,
                evdev.ecodes.KEY_A,
                hold_ms=1,
                event_name="test_key",
                uinput_dev=grabbed.uinput,
                asyncio_mod=adapters.ASYNCIO_RUNTIME,
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
        grabbed.running = True
        event_name = "test_key"
        grabbed.state.rapidfire_active[event_name] = True
        grabbed.state.rapidfire_tasks[event_name] = asyncio.current_task()
        grabbed.state.rapidfire_outputs[event_name] = repeat.RapidfireOutputState(
            kind="key",
            code=evdev.ecodes.KEY_A,
            uinput=grabbed.uinput,
        )
        started = asyncio.Event()

        with pytest.raises(RuntimeError, match="write failed"):
            await repeat.rapidfire_key(
                grabbed,
                evdev.ecodes.KEY_A,
                hold_ms=1,
                wait_ms=1,
                event_name=event_name,
                uinput_dev=grabbed.uinput,
                asyncio_mod=adapters.ASYNCIO_RUNTIME,
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

    outputs.release_all_keys(
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

        outputs.passthrough(grabbed, event, evdev_mod=evdev, uinput_writer=lambda device: device)
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

        outputs.passthrough(grabbed, event, evdev_mod=evdev, uinput_writer=lambda device: device)
        passthrough.write.assert_called_once_with(evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 12)
