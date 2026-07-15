"""Button capture and analog-learning orchestration."""

import logging
from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.core import DeviceType
from keymasq.common.model.hardware import EvdevDevice
from keymasq.gui.widgets.device_tab.add_inputs_flow import AddInputsFlow, AddInputsResult
from keymasq.gui.widgets.device_tab.input_helpers import label_from_evdev
from keymasq.gui.widgets.device_tab.learn_analog_flow import LearnAnalogFlow, LearnAnalogResult

log = logging.getLogger(__name__)


class CaptureMixin:
    def _on_add_keys_clicked(self: Any, _button: Gtk.Button | None) -> None:
        AddInputsFlow(
            self.main_window or self.get_root(),
            self._request_session_async,
            self.device,
            self._on_add_inputs_complete,
        ).present()

    def _on_add_inputs_complete(self: Any, result: AddInputsResult) -> None:
        if self.hardware_manager is None:
            log.warning(
                "Cannot save added inputs for device %s without a hardware manager",
                self.device.hardware_id,
            )
            return
        self.device.buttons.extend(result.buttons)
        self.device.evdev_devices.extend(result.evdev_devices)
        self.hardware_manager.save_hardware(self.device)
        self._request_session_async({"command": "reload"}, self._ignore_session_response)
        self._reload_ui()

    def _on_learn_analog_clicked(self: Any, _button: Gtk.Button | None) -> None:
        LearnAnalogFlow(
            self.main_window or self.get_root(),
            self._request_session_async,
            self.device,
            self._on_learn_analog_complete,
        ).present()

    def _on_learn_analog_complete(self: Any, result: LearnAnalogResult) -> None:
        if self.hardware_manager is None:
            log.warning(
                "Cannot save learned analog input for device %s without a hardware manager",
                self.device.hardware_id,
            )
            return
        self.device.analog_inputs.append(result.analog)
        self._ensure_analog_evdev_interface(result.source, result.stable_path)
        self.hardware_manager.save_hardware(self.device)
        self._request_session_async({"command": "reload"}, self._ignore_session_response)
        self._reload_ui()

    def _ensure_analog_evdev_interface(
        self: Any,
        source: str | None,
        stable_path: str | None,
    ) -> None:
        if not source or not stable_path:
            return
        for device in self.device.evdev_devices:
            if device.id == source:
                return
            if device.path == stable_path:
                if not device.id:
                    device.id = source
                return
        self.device.evdev_devices.append(
            EvdevDevice(path=stable_path, device_type=DeviceType.GAMEPAD, id=source)
        )

    def _label_from_evdev(self: Any, evdev_name: str) -> str:
        return label_from_evdev(evdev_name)
