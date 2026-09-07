import logging
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import evdev
import pytest

from keymasq.common.virtual_device_templates import (
    LOGITECH_EXTREME_3D_TEMPLATE,
    VirtualDeviceConfig,
    VirtualDeviceInstance,
    resolve_virtual_devices,
)
from keymasq.keymasqd.runtime import outputs


def test_failed_replacement_preserves_all_existing_outputs(monkeypatch):
    original_config = VirtualDeviceConfig(
        devices=(
            VirtualDeviceInstance("flight", LOGITECH_EXTREME_3D_TEMPLATE.id),
            VirtualDeviceInstance("removed", LOGITECH_EXTREME_3D_TEMPLATE.id),
        )
    )
    specs = {item.output_id: item for item in resolve_virtual_devices(0, original_config)}
    old, removed, prepared = Mock(), Mock(), Mock()
    state = outputs.OutputRuntimeState(
        virtual_gamepad_count=0,
        virtual_gamepad_uinputs={"flight": old, "removed": removed},
        virtual_device_config=original_config,
        virtual_device_specs=dict(specs),
    )
    changed = VirtualDeviceConfig(
        devices=(
            replace(original_config.devices[0], name="Changed"),
            VirtualDeviceInstance("new", LOGITECH_EXTREME_3D_TEMPLATE.id),
        )
    )
    create = Mock(side_effect=[prepared, OSError("device creation failed")])
    monkeypatch.setattr(outputs, "create_virtual_device", create)
    manager: Any = SimpleNamespace(output_state=state)
    with pytest.raises(OSError, match="device creation failed"):
        outputs.configure_virtual_gamepads(
            manager,
            0,
            config=changed,
            evdev_mod=Mock(),
            log=logging.getLogger(__name__),
            uinput_writer=lambda device: device,
        )
    old.close.assert_not_called()
    removed.close.assert_not_called()
    prepared.close.assert_called_once_with()
    assert state.virtual_gamepad_uinputs == {"flight": old, "removed": removed}
    assert state.virtual_device_specs == specs
    assert state.virtual_device_config == original_config

    replacement, added = Mock(), Mock()
    create.side_effect = [replacement, added]
    outputs.configure_virtual_gamepads(
        manager,
        0,
        config=changed,
        evdev_mod=Mock(),
        log=logging.getLogger(__name__),
        uinput_writer=lambda device: device,
    )
    old.close.assert_called_once_with()
    removed.close.assert_called_once_with()
    assert state.virtual_gamepad_uinputs == {"flight": replacement, "new": added}
    assert state.virtual_device_config == changed


def test_failed_axis_initialization_closes_new_device(monkeypatch):
    device = Mock()
    device.write.side_effect = OSError("initial write failed")
    monkeypatch.setattr(outputs, "_create_synthetic_uinput", Mock(return_value=device))
    spec = resolve_virtual_devices(1, VirtualDeviceConfig())[0]
    evdev_mod: Any = SimpleNamespace(ecodes=evdev.ecodes, AbsInfo=evdev.AbsInfo)
    with pytest.raises(OSError, match="initial write failed"):
        outputs.create_virtual_device(spec, evdev_mod, lambda output: output)
    device.close.assert_called_once_with()
