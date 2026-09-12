import asyncio
import logging
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import evdev
import pytest

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.analog import AnalogControlConfig, AnalogMouseMotionConfig
from keymasq.common.model.core import ActionType, DeviceType
from keymasq.common.output_axes import STANDARD_OUTPUT_AXES
from keymasq.common.virtual_device_templates import (
    VirtualDeviceConfig,
    VirtualDeviceInstance,
    resolve_virtual_devices,
)
from keymasq.keymasqd.runtime.default_controller_route import DefaultControllerRoute
from keymasq.keymasqd.runtime.grabbed_device import device as device_module
from keymasq.keymasqd.runtime.grabbed_device.event import pipeline
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
        self.packets = []
        self._flushed = 0

    def syn(self):
        self.frames += 1
        self.packets.append(self.writes[self._flushed:])
        self._flushed = len(self.writes)

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
    assert replacement.writes == [(E.EV_ABS, E.ABS_X, 32767), (E.EV_ABS, E.ABS_X, -32768)]


async def test_missing_target_drops_output_without_clone_fallback(routed):
    device, writer, _, state = routed
    state.virtual_gamepad_uinputs.clear()
    device.uinput = Output()
    await send(device, E.EV_KEY, E.BTN_SOUTH, 1)
    await send(device, E.EV_ABS, E.ABS_X, 255)
    assert writer.writes == []
    assert device.uinput.writes == []


@pytest.mark.parametrize("missing_specs_attribute", [False, True])
async def test_output_without_spec_routes_axes_and_releases_to_standard_neutral(
    routed, missing_specs_attribute
):
    device, writer, _, state = routed
    if missing_specs_attribute:
        del state.virtual_device_specs
    else:
        state.virtual_device_specs.pop("virtual-gamepad-1")
    target = device.resolve_gamepad_output("virtual-gamepad-1", "test")
    assert target.axis_ranges == {
        axis.code: (axis.minimum, axis.maximum) for axis in STANDARD_OUTPUT_AXES
    }
    assert target.axis_rest_values == {axis.code: axis.neutral for axis in STANDARD_OUTPUT_AXES}
    await send(device, E.EV_ABS, E.ABS_X, 255)
    await send(device, E.EV_SYN, E.SYN_REPORT, 0)
    assert writer.packets == [[(E.EV_ABS, E.ABS_X, 32767)]]
    device.default_route.release_axes(device)
    assert writer.packets[-1] == [(E.EV_ABS, E.ABS_X, 0)]


@pytest.mark.parametrize("failed_operation", ["write", "syn"])
async def test_closed_axis_output_does_not_interrupt_source_release(routed, failed_operation):
    device, writer, _, _ = routed
    await send(device, E.EV_ABS, E.ABS_X, 255)
    physical = Mock()
    device.device = physical
    setattr(writer, failed_operation, Mock(side_effect=OSError("output closed")))
    await device.release()
    physical.ungrab.assert_called_once()
    physical.close.assert_called_once()
    assert device.device is None
    assert not device.default_route.axes
    assert not any(device.state.held_output_abs.values())


def test_virtual_target_cache_tracks_output_and_template_replacement(routed):
    device, writer, _, state = routed
    def resolve():
        return device.resolve_gamepad_output("virtual-gamepad-1", "test")

    target = resolve()
    assert resolve() is target
    state.virtual_gamepad_uinputs["virtual-gamepad-1"] = Output()
    replacement = resolve()
    assert replacement is not target
    assert replacement.uinput is not writer
    assert replacement.stick_output is not target.stick_output
    state.virtual_device_specs["virtual-gamepad-1"] = state.virtual_device_specs["flight"]
    changed_template = resolve()
    assert changed_template is not replacement
    assert changed_template.stick_output is replacement.stick_output
    assert changed_template.axis_ranges[E.ABS_X] == (0, 1023)
    state.virtual_gamepad_uinputs.clear()
    assert resolve() is None
    state.virtual_gamepad_uinputs["virtual-gamepad-1"] = replacement.uinput
    assert resolve() is not changed_template


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


async def test_deferred_routed_release_is_flushed_in_its_source_report(routed):
    device, writer, mapping, _ = routed
    device.device = SimpleNamespace(
        absinfo=lambda code: evdev.AbsInfo(128, 0, 255, 0, 0, 0),
        read_one=lambda: None,
    )
    device.update_analog_inputs({
        "stick": {"type": "stick", "axes": [
            {"role": "x", "evdev": "abs_x"},
            {"role": "y", "evdev": "abs_y"},
        ]},
    })
    mapping["stick"] = MappingAction(
        action_type=ActionType.ANALOG_CONTROL,
        analog_control_config=AnalogControlConfig(
            name="Area", mouse_motion=AnalogMouseMotionConfig(enabled=True, mode="area")
        ),
    )
    await send(device, E.EV_KEY, E.BTN_SOUTH, 1)
    await send(device, E.EV_SYN, E.SYN_REPORT, 0)
    await send(device, E.EV_ABS, E.ABS_X, 128)
    await send(device, E.EV_KEY, E.BTN_SOUTH, 0)
    assert device.state.analog_deferred_keys
    assert (E.EV_KEY, E.BTN_SOUTH, 0) not in writer.writes
    await send(device, E.EV_SYN, E.SYN_REPORT, 0)
    assert any((E.EV_KEY, E.BTN_SOUTH, 0) in packet for packet in writer.packets)
    assert device.state.passthrough_frame_output is None


@pytest.mark.parametrize("action_type", [None, ActionType.PASSTHROUGH, ActionType.SUPPRESS])
async def test_startup_and_replacement_publish_unchanged_axes(monkeypatch, routed, action_type):
    device, writer, mapping, state = routed
    device.default_route = DefaultControllerRoute("virtual-gamepad-1", {E.ABS_X: (0, 255)})
    device.device = SimpleNamespace(
        absinfo=lambda code: evdev.AbsInfo(255, 0, 255, 0, 0, 0),
        read_one=lambda: None,
    )
    if action_type is None:
        # Raw axes need initialization even without saved analog definitions.
        device.update_analog_inputs({})
    else:
        mapping["stick"] = MappingAction(action_type=action_type)

    async def no_new_events(runtime):
        while runtime.state.input_event_buffer:
            yield runtime.state.input_event_buffer.popleft()

    monkeypatch.setattr(pipeline, "read_events", no_new_events)
    device.running = True
    await pipeline.event_loop(device, asyncio_mod=asyncio, log=logging.getLogger(__name__))
    expected = [] if action_type == ActionType.SUPPRESS else [(E.EV_ABS, E.ABS_X, 32767)]
    assert writer.writes == expected
    assert writer.packets == ([expected] if expected else [])
    replacement = Output()
    await reconfigure_virtual_gamepads(
        count=1, current_count=1, output_devices_active=True,
        grabbed_devices={device.hardware_id: [device]},
        clear_combo_runtime=AsyncMock(),
        configure_outputs=lambda _: state.virtual_gamepad_uinputs.update(
            {"virtual-gamepad-1": replacement}
        ),
        set_inactive_count=Mock(), logger=logging.getLogger(__name__),
        configuration_changed=True,
    )
    assert replacement.writes == expected
    assert replacement.packets == ([expected] if expected else [])


async def test_initial_snapshot_does_not_replay_older_queued_axes(monkeypatch, routed):
    device, writer, _, _ = routed
    device.default_route = DefaultControllerRoute("virtual-gamepad-1", {E.ABS_X: (0, 255)})
    queued = deque(evdev.InputEvent(0, 0, kind, code, value) for kind, code, value in [
        (E.EV_ABS, E.ABS_X, 32),
        (E.EV_KEY, E.BTN_SOUTH, 1),
        (E.EV_SYN, E.SYN_REPORT, 0),
        (E.EV_ABS, E.ABS_X, 64),
        (E.EV_KEY, E.BTN_SOUTH, 0),
        (E.EV_SYN, E.SYN_REPORT, 0),
    ])
    device.device = SimpleNamespace(
        absinfo=lambda code: evdev.AbsInfo(255, 0, 255, 0, 0, 0),
        read_one=lambda: queued.popleft() if queued else None,
    )

    async def buffered_events(runtime):
        while runtime.state.input_event_buffer:
            yield runtime.state.input_event_buffer.popleft()

    monkeypatch.setattr(pipeline, "read_events", buffered_events)
    device.running = True
    await pipeline.event_loop(device, asyncio_mod=asyncio, log=logging.getLogger(__name__))
    assert writer.writes == [
        (E.EV_KEY, E.BTN_SOUTH, 1), (E.EV_KEY, E.BTN_SOUTH, 0),
        (E.EV_ABS, E.ABS_X, 32767),
    ]
    assert writer.packets[-1] == [(E.EV_ABS, E.ABS_X, 32767)]
    assert device.default_route.source_values[E.ABS_X] == 255
    await send(device, E.EV_ABS, E.ABS_X, 0)
    assert writer.writes[-1] == (E.EV_ABS, E.ABS_X, -32768)


@pytest.mark.parametrize("replacement_code", [None, "abs_rz"])
async def test_removing_or_rebinding_saved_axis_restores_device_bounds(routed, replacement_code):
    device, writer, _, _ = routed
    route = device.default_route
    device.device = SimpleNamespace(
        absinfo=lambda code: evdev.AbsInfo(0, 0, 255 if code == E.ABS_X else 1023, 0, 0, 0)
    )

    def definition(code):
        return {"stick": {"type": "axis", "axes": [
            {"role": "x", "evdev": code, "minimum": 20, "maximum": 235},
        ]}}

    device.update_analog_inputs(definition("abs_x"))
    assert route.axis_ranges[E.ABS_X] == (20, 235)
    device.update_analog_inputs(definition(replacement_code) if replacement_code else {})
    await device.update_default_output("virtual-gamepad-1")
    assert device.default_route is route
    assert route.axis_ranges[E.ABS_X] == (0, 255)
    if replacement_code:
        assert route.axis_ranges[E.ABS_RZ] == (20, 235)
    await send(device, E.EV_ABS, E.ABS_X, 128)
    assert writer.writes == [(E.EV_ABS, E.ABS_X, 128)]
