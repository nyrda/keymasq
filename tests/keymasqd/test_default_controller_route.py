import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import evdev
import pytest

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType, DeviceType
from keymasq.common.virtual_device_templates import (
    VirtualDeviceConfig,
    VirtualDeviceInstance,
    resolve_virtual_devices,
)
from keymasq.keymasqd.runtime.default_controller_route import DefaultControllerRoute
from keymasq.keymasqd.runtime.grabbed_device import device as device_module
from keymasq.keymasqd.runtime.grabbed_device.event.pipeline import process_event
from keymasq.keymasqd.runtime.virtual_gamepads import (
    GamepadOutputRouter,
    reconfigure_virtual_gamepads,
)
from tests.keymasqd.device_manager_support import (
    FakeUInput,
    grabbed_event_processing_deps,
    make_grabbed_device,
)

E = evdev.ecodes


class Output(FakeUInput):
    def __init__(self):
        super().__init__()
        self.frames = 0
        self.closed = False

    def syn(self):
        self.frames += 1

    def close(self):
        self.closed = True


@pytest.fixture
def routed(monkeypatch):
    writer = Output()
    config = VirtualDeviceConfig(
        devices=(VirtualDeviceInstance("flight", "logitech-extreme-3d-pro"),)
    )
    state = SimpleNamespace(
        virtual_gamepad_uinputs={"virtual-gamepad-1": writer, "flight": Output()},
        virtual_device_specs={spec.output_id: spec for spec in resolve_virtual_devices(1, config)},
    )
    router = GamepadOutputRouter(logging.getLogger(__name__))
    mapping = {}
    device = make_grabbed_device(
        monkeypatch,
        device_type=DeviceType.GAMEPAD,
        default_output="virtual-gamepad-1",
        button_map={"south": "btn_south"},
        mapping_getter=lambda: mapping,
        gamepad_output_resolver=lambda output_id, context: router.resolve(
            state, {}, output_id, context=context
        ),
        analog_inputs={
            "stick": {"type": "stick", "axes": [{"role": "x", "evdev": "abs_x"}]},
        },
    )
    device.default_route = DefaultControllerRoute(
        "virtual-gamepad-1", {E.ABS_X: (0, 255), E.ABS_RZ: (0, 1023)}
    )
    return device, writer, mapping, state


async def send(device, event_type, code, value):
    await process_event(
        device,
        evdev.InputEvent(0, 0, event_type, code, value),
        deps=grabbed_event_processing_deps(),
    )


async def test_same_codes_only_and_source_frame_boundaries(routed):
    device, writer, _, _ = routed
    for code in (E.BTN_SOUTH, E.BTN_TL2, E.BTN_Z):
        await send(device, E.EV_KEY, code, 1)
    await send(device, E.EV_ABS, E.ABS_X, 255)
    await send(device, E.EV_ABS, E.ABS_THROTTLE, 200)
    await send(device, E.EV_REL, E.REL_X, 8)
    assert writer.writes == [
        (E.EV_KEY, E.BTN_SOUTH, 1),
        (E.EV_KEY, E.BTN_TL2, 1),
        (E.EV_ABS, E.ABS_X, 32767),
    ]
    assert writer.frames == 0
    await send(device, E.EV_SYN, E.SYN_REPORT, 0)
    assert writer.frames == 1
    assert device.uinput is None


async def test_mapping_override_and_held_release(routed):
    device, writer, mapping, _ = routed
    mapping["south"] = MappingAction(action_type=ActionType.KEYBOARD, target="key_enter")
    await send(device, E.EV_KEY, E.BTN_SOUTH, 1)
    previous = dict(mapping)
    mapping.clear()
    await device.reset_mapping_runtime_state(previous)
    await send(device, E.EV_KEY, E.BTN_SOUTH, 0)
    assert writer.writes == []
    assert device.keyboard_uinput.writes == [
        (E.EV_KEY, E.KEY_ENTER, 1),
        (E.EV_KEY, E.KEY_ENTER, 0),
    ]
    await send(device, E.EV_KEY, E.BTN_SOUTH, 1)
    await send(device, E.EV_KEY, E.BTN_SOUTH, 0)
    assert writer.writes == [(E.EV_KEY, E.BTN_SOUTH, 1), (E.EV_KEY, E.BTN_SOUTH, 0)]


async def test_axis_override_neutralizes_previous_default_output(routed):
    device, writer, mapping, _ = routed
    await send(device, E.EV_ABS, E.ABS_X, 255)
    mapping["stick"] = MappingAction(action_type=ActionType.SUPPRESS)
    await device.reset_mapping_runtime_state({})
    await send(device, E.EV_ABS, E.ABS_X, 0)
    assert writer.writes == [(E.EV_ABS, E.ABS_X, 32767), (E.EV_ABS, E.ABS_X, 0)]


async def test_default_press_releases_when_a_mapping_is_added_while_held(routed):
    device, writer, mapping, _ = routed
    await send(device, E.EV_KEY, E.BTN_SOUTH, 1)
    mapping["south"] = MappingAction(action_type=ActionType.SUPPRESS)
    await device.reset_mapping_runtime_state({})
    await send(device, E.EV_KEY, E.BTN_SOUTH, 0)
    await send(device, E.EV_KEY, E.BTN_SOUTH, 1)
    assert writer.writes == [(E.EV_KEY, E.BTN_SOUTH, 1), (E.EV_KEY, E.BTN_SOUTH, 0)]


async def test_combo_recall_and_restore_use_routed_output(routed):
    device, writer, _, _ = routed
    await send(device, E.EV_KEY, E.BTN_SOUTH, 1)
    assert device.combo_passthrough_binding_active("btn_south")
    device.emit_combo_release("btn_south")
    assert not device.combo_passthrough_binding_active("btn_south")
    device.emit_combo_press("btn_south")
    await send(device, E.EV_KEY, E.BTN_SOUTH, 0)
    assert writer.writes == [(E.EV_KEY, E.BTN_SOUTH, value) for value in (1, 0, 1, 0)]


async def test_output_replacement_releases_old_state_and_uses_new_instance(routed):
    device, writer, _, state = routed
    await send(device, E.EV_KEY, E.BTN_SOUTH, 1)
    await send(device, E.EV_ABS, E.ABS_X, 255)
    replacement = Output()
    await reconfigure_virtual_gamepads(
        count=1,
        current_count=1,
        output_devices_active=True,
        grabbed_devices={device.hardware_id: [device]},
        clear_combo_runtime=AsyncMock(),
        configure_outputs=lambda _: state.virtual_gamepad_uinputs.update(
            {"virtual-gamepad-1": replacement}
        ),
        set_inactive_count=Mock(),
        logger=logging.getLogger(__name__),
        configuration_changed=True,
    )
    assert (E.EV_KEY, E.BTN_SOUTH, 0) in writer.writes
    assert (E.EV_ABS, E.ABS_X, 0) in writer.writes
    await send(device, E.EV_ABS, E.ABS_X, 0)
    assert replacement.writes == [(E.EV_ABS, E.ABS_X, -32768)]


async def test_missing_target_drops_output_without_clone_fallback(routed):
    device, writer, _, state = routed
    state.virtual_gamepad_uinputs.clear()
    device.uinput = Output()
    await send(device, E.EV_KEY, E.BTN_SOUTH, 1)
    await send(device, E.EV_ABS, E.ABS_X, 255)
    assert writer.writes == []
    assert device.uinput.writes == []


async def test_flight_axis_keeps_code_and_uses_target_range_and_release(routed):
    device, _, _, state = routed
    device.default_output = "flight"
    device.default_route.output_id = "flight"
    writer = state.virtual_gamepad_uinputs["flight"]
    await send(device, E.EV_ABS, E.ABS_RZ, 1023)
    assert writer.writes == [(E.EV_ABS, E.ABS_RZ, 255)]
    await device.release()
    assert (E.EV_ABS, E.ABS_RZ, 127) in writer.writes
    assert not writer.closed


async def test_grab_skips_clone_and_route_change_releases_old_output(monkeypatch, routed):
    device, writer, _, state = routed

    async def input_events():
        await asyncio.Event().wait()
        yield evdev.InputEvent(0, 0, E.EV_SYN, E.SYN_REPORT, 0)

    physical = Mock()
    physical.capabilities.return_value = {
        E.EV_KEY: [E.BTN_SOUTH],
        E.EV_ABS: [(E.ABS_X, evdev.AbsInfo(0, 0, 255, 0, 0, 0))],
    }
    physical.absinfo.return_value = evdev.AbsInfo(0, 0, 255, 0, 0, 0)
    physical.async_read_loop = input_events
    monkeypatch.setattr(device_module, "_device_input", lambda _: physical)
    clone = Mock(side_effect=AssertionError("A default route must not create a clone"))
    monkeypatch.setattr(device_module, "_copy_passthrough_capabilities", clone)
    monkeypatch.setattr(device_module.grab, "wait_for_active_keys_to_clear", AsyncMock())
    monkeypatch.setattr(device_module.source_hiding, "hide_source", AsyncMock(return_value=[]))
    await device.grab()
    try:
        await send(device, E.EV_KEY, E.BTN_SOUTH, 1)
        await send(device, E.EV_ABS, E.ABS_X, 255)
        await device.update_default_output("flight")
        assert (E.EV_KEY, E.BTN_SOUTH, 0) in writer.writes
        assert (E.EV_ABS, E.ABS_X, 0) in writer.writes
        await send(device, E.EV_ABS, E.ABS_X, 255)
        assert state.virtual_gamepad_uinputs["flight"].writes[-1] == (E.EV_ABS, E.ABS_X, 1023)
        assert device.uinput is None
        clone.assert_not_called()
    finally:
        await device.release()
    assert not writer.closed
