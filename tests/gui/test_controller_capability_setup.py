"""Capability-to-hardware regression tests for controller setup and targeting."""

import evdev
import pytest

from keymasq.common.controller_capabilities import hardware_controller_template
from keymasq.common.model.core import DeviceType
from keymasq.common.model.hardware import EvdevDevice, HardwareConfig
from keymasq.common.types import JsonObject
from keymasq.common.virtual_device_templates import LOGITECH_EXTREME_3D_TEMPLATE, XBOX_360_TEMPLATE
from keymasq.gui.wizards.hardware_setup.flow import merge_inventory_abs_info
from keymasq.gui.wizards.hardware_setup.templates import (
    build_gamepad_analog_inputs,
    build_gamepad_buttons,
)


def controller_interface(template):
    return {
        "id": "controller",
        "device_types": ["gamepad"],
        "capabilities": [button.evdev for button in template.buttons],
        "raw_capabilities": {
            evdev.ecodes.EV_KEY: [getattr(evdev.ecodes, b.evdev.upper()) for b in template.buttons],
            evdev.ecodes.EV_ABS: [
                (
                    getattr(evdev.ecodes, a.evdev.upper()),
                    evdev.AbsInfo(a.maximum, a.minimum, a.maximum, 0, 0, 0),
                )
                for a in template.axes
            ],
        },
    }


def hardware_from_interfaces(interfaces):
    return HardwareConfig(
        vendor_id="046d",
        product_id="c215",
        name="Test controller",
        evdev_devices=[EvdevDevice("/dev/test", DeviceType.GAMEPAD)],
        buttons=build_gamepad_buttons(interfaces),
        analog_inputs=build_gamepad_analog_inputs(interfaces),
    )


def output_inventory(config, *, source="controller") -> JsonObject:
    from dataclasses import asdict

    return {
        "source_hardware_id": config.hardware_id,
        "source_interface_id": source,
        "capabilities": [b.evdev for b in config.buttons]
        + [axis.evdev for analog in config.analog_inputs for axis in analog.axes],
        "gamepad_output": {
            "analog_inputs": {analog.id: asdict(analog) for analog in config.analog_inputs}
        },
    }


def test_routed_picker_excludes_secondary_interface_buttons_and_duplicates():
    from keymasq.common.controller_capabilities import routed_controller_template

    config = hardware_from_interfaces(
        [
            {"id": "one", "capabilities": ["btn_trigger"]},
            {"id": "two", "capabilities": ["btn_trigger", "btn_trigger_happy1"]},
        ]
    )
    inventory = output_inventory(config, source="one")
    inventory["capabilities"] = [f"EV_KEY_{evdev.ecodes.BTN_TRIGGER}"]
    template, _ = routed_controller_template(config, inventory)
    assert [(b.id, b.evdev) for b in template.buttons] == [
        (config.buttons[0].id, "btn_trigger"),
    ]


@pytest.mark.parametrize("rest", [None, 255])
def test_routed_picker_uses_resolved_rest_or_disables_percentages(rest):
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.controller_capabilities import routed_controller_template
    from keymasq.gui.widgets.virtual_device_picker import VirtualDevicePicker

    config = hardware_from_interfaces([controller_interface(LOGITECH_EXTREME_3D_TEMPLATE)])
    inventory = output_inventory(config)
    inventory["gamepad_output"]["analog_inputs"]["abs_throttle"]["axes"][0]["rest"] = rest
    template, unknown = routed_controller_template(config, inventory)
    events = []
    picker = VirtualDevicePicker(
        template,
        lambda *_: None,
        lambda _b, code, value: events.append((code, value)),
        unknown_rest_axes=unknown,
    )
    picker.axis_choice.set_selected(picker.axis_names.index("abs_throttle"))
    assert picker.percent_spin.get_sensitive() == (rest is not None)
    if rest is None:
        shortcut = picker._axis_shortcut("Idle", "abs_throttle", 0, "Idle")
        assert not shortcut.get_visible()
        assert "unavailable" in picker.range_label.get_text()
        picker.value_spin.set_value(42)
        picker._map_axis(Gtk.Button())
        assert events == [("abs_throttle", 42)]
    else:
        picker.percent_spin.set_value(0)
        assert picker.value_spin.get_value_as_int() == 255
        picker.percent_spin.set_value(100)
        assert picker.value_spin.get_value_as_int() == 0


def test_flight_stick_preserves_every_control_and_range():
    config = hardware_from_interfaces([controller_interface(LOGITECH_EXTREME_3D_TEMPLATE)])
    assert {b.evdev for b in config.buttons} == {
        b.evdev for b in LOGITECH_EXTREME_3D_TEMPLATE.buttons
    }
    analogs = {a.id: a for a in config.analog_inputs}
    assert set(analogs) == {"stick", "hat_0", "abs_rz", "abs_throttle"}
    assert analogs["abs_rz"].label == "Twist"
    assert analogs["abs_rz"].axes[0].rest == 127
    assert analogs["abs_throttle"].axes[0].rest is None
    assert analogs["stick"].axes[0].center == 511
    output = hardware_controller_template(config)
    assert output.layout == "flight-stick"
    assert {a.evdev: (a.minimum, a.maximum) for a in output.axes} == {
        a.evdev: (a.minimum, a.maximum) for a in LOGITECH_EXTREME_3D_TEMPLATE.axes
    }


def test_gamepad_keeps_digital_triggers_and_hats():
    config = hardware_from_interfaces([controller_interface(XBOX_360_TEMPLATE)])
    assert len(config.buttons) == len(XBOX_360_TEMPLATE.buttons)
    assert {a.id for a in config.analog_inputs} == {
        "left_stick",
        "right_stick",
        "left_trigger",
        "right_trigger",
        "hat_0",
    }
    assert hardware_controller_template(config).layout == "gamepad"


def test_inventory_fallback_preserves_buttons_and_axis_metadata():
    iface = {
        "id": "controller",
        "device_types": ["gamepad"],
        "capabilities": ["btn_trigger", "btn_trigger_happy1", "abs_throttle"],
        "raw_capabilities": merge_inventory_abs_info(
            {},
            {
                str(evdev.ecodes.ABS_THROTTLE): {"minimum": 10, "maximum": 900, "value": 500},
            },
        ),
    }
    config = hardware_from_interfaces([iface])
    assert {b.evdev for b in config.buttons} == {"btn_trigger", "btn_trigger_happy1"}
    axis = config.analog_inputs[0].axes[0]
    assert (axis.minimum, axis.maximum, axis.rest) == (10, 900, None)


def test_pairing_does_not_cross_interfaces_or_drop_duplicate_controls():
    interfaces = [
        {"id": "one", "capabilities": ["btn_trigger", "abs_x", "abs_rx", "abs_ry"]},
        {"id": "two", "capabilities": ["btn_trigger", "abs_y", "abs_rx"]},
    ]
    config = hardware_from_interfaces(interfaces)
    assert len(config.buttons) == 2
    assert len({b.id for b in config.buttons}) == 2
    assert len(config.analog_inputs) == 5
    assert all(a.type == "axis" for a in config.analog_inputs)
    assert len({a.id for a in config.analog_inputs}) == 5


def test_motion_interfaces_are_excluded():
    iface = controller_interface(LOGITECH_EXTREME_3D_TEMPLATE)
    iface["device_types"] = ["motion"]
    config = hardware_from_interfaces([iface])
    assert not config.buttons
    assert not config.analog_inputs


def test_physical_selector_uses_saved_flight_controls_and_ranges(monkeypatch):
    from types import SimpleNamespace

    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    import keymasq.gui.widgets.key_selector.tabs as tabs
    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.core import ActionType
    from keymasq.common.virtual_device_templates import VirtualDeviceConfig
    from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog

    iface = controller_interface(LOGITECH_EXTREME_3D_TEMPLATE)
    iface["capabilities"].append("btn_trigger_happy1")
    config = hardware_from_interfaces([iface])
    monkeypatch.setattr(tabs, "virtual_gamepad_count", lambda: 1)
    monkeypatch.setattr(tabs, "virtual_device_config", VirtualDeviceConfig)
    monkeypatch.setattr(
        tabs, "HardwareManager", lambda: SimpleNamespace(list_hardware=lambda: [config])
    )
    monkeypatch.setattr(
        tabs,
        "session_request_async",
        lambda _request, callback: callback({"devices": [output_inventory(config)]}),
    )
    dialog = KeySelectorDialog(
        Gtk.Box(),
        "Source",
        MappingAction(
            action_type=ActionType.GAMEPAD,
            target="btn_trigger",
            output_id=config.hardware_id,
        ),
    )
    assert not dialog._standard_gamepad_picker.get_visible()
    picker = dialog._template_gamepad_picker.get_first_child()
    assert picker._template.layout == "flight-stick"
    assert picker._axes["abs_x"].maximum == 1023
    assert len(picker._template.buttons) == 13
    assert "Flight stick" in dict(dialog._gamepad_output_choices())[config.hardware_id]
    actions = []
    dialog._emit_selected_action = actions.append
    dialog._close_after_selection_emit = lambda: None
    picker._on_axis(Gtk.Button(), "abs_x", 1023)
    assert actions[0].output_id == config.hardware_id
    assert actions[0].axis_value == 1023
    assert actions[0].target == "abs_x"
    assert all(axis.evdev != "abs_z" for axis in picker._template.axes)

    # Execute the action emitted by the picker through the physical router.
    import asyncio
    import logging
    from dataclasses import asdict
    from unittest.mock import AsyncMock

    from keymasq.keymasqd.runtime.action.outputs import execute_gamepad_axis_action
    from keymasq.keymasqd.runtime.adapters import identity_uinput_writer
    from keymasq.keymasqd.runtime.grabbed_device.device import GrabbedDevice
    from keymasq.keymasqd.runtime.grabbed_device.types import ActionExecutionDeps
    from keymasq.keymasqd.runtime.virtual_gamepads import GamepadOutputRouter

    runtime = GrabbedDevice(
        path="/dev/test",
        hardware_id=config.hardware_id,
        button_map={},
        mapping_getter=lambda: {},
        event_callback=AsyncMock(),
        device_type=DeviceType.GAMEPAD,
    )
    runtime.interface_id = "controller"
    runtime.update_analog_inputs({analog.id: asdict(analog) for analog in config.analog_inputs})
    emitted = []
    runtime.uinput = SimpleNamespace(write=lambda *event: emitted.append(event), syn=lambda: None)
    router = GamepadOutputRouter(logging.getLogger(__name__))
    runtime._gamepad_output_resolver = lambda output_id, _context: router.resolve(
        SimpleNamespace(),
        {config.hardware_id: [runtime]},
        output_id,
    )
    deps = ActionExecutionDeps(
        asyncio_mod=asyncio,
        evdev_mod=evdev,
        uinput_writer=identity_uinput_writer,
        fire_and_observe_fn=lambda coro, _label: asyncio.create_task(coro),
    )

    async def press_release():
        for value in (1, 0):
            await execute_gamepad_axis_action(
                runtime, actions[0], SimpleNamespace(value=value), "key_a", deps=deps
            )

    asyncio.run(press_release())
    assert emitted == [
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 1023),
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 511),
    ]


def test_button_only_physical_picker_does_not_invent_axes():
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.virtual_device_picker import VirtualDevicePicker

    config = hardware_from_interfaces([{"id": "buttons", "capabilities": ["btn_trigger"]}])
    selected = []
    picker = VirtualDevicePicker(
        hardware_controller_template(config),
        lambda _, code: selected.append(code),
        lambda *args: None,
    )
    assert picker.axis_names == []
    assert not picker.range_label.get_visible()
    picker._on_button(Gtk.Button(), "btn_trigger")
    assert selected == ["btn_trigger"]


def test_legacy_controller_keeps_axis_targets_without_mutating_calibration():
    from copy import deepcopy

    config = hardware_from_interfaces([controller_interface(XBOX_360_TEMPLATE)])
    for analog in config.analog_inputs:
        for axis in analog.axes:
            if analog.id != "hat_0":
                axis.minimum = axis.maximum = axis.center = axis.rest = None
    original = deepcopy(config)
    output = hardware_controller_template(config)
    axes = {axis.evdev: axis for axis in output.axes}
    assert len(axes) == 8
    assert (axes["abs_x"].minimum, axes["abs_x"].maximum, axes["abs_x"].rest) == (
        -32768,
        32767,
        0,
    )
    assert (axes["abs_z"].minimum, axes["abs_z"].maximum) == (0, 255)
    assert config == original


def test_picker_prefers_saved_ranges_over_legacy_defaults():
    config = hardware_from_interfaces([controller_interface(XBOX_360_TEMPLATE)])
    stick = next(a for a in config.analog_inputs if a.id == "left_stick")
    stick.axes[0].minimum = 10
    stick.axes[0].maximum = 1000
    stick.axes[0].center = 400
    stick.axes[1].minimum = stick.axes[1].maximum = stick.axes[1].center = None
    axes = {a.evdev: a for a in hardware_controller_template(config).axes}
    assert (axes["abs_x"].minimum, axes["abs_x"].maximum, axes["abs_x"].rest) == (10, 1000, 400)
    assert (axes["abs_y"].minimum, axes["abs_y"].maximum) == (-32768, 32767)
