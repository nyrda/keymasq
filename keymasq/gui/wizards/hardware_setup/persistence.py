from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from keymasq.common.model.hardware import (
    AnalogInputDefinition,
    ButtonDefinition,
    EvdevDevice,
    HardwareConfig,
)
from keymasq.common.model.motion import MotionSensorDefinition

from . import templates


class PersistenceMixin:
    def _persist_config(self: Any, config: HardwareConfig) -> None:
        self.hardware_manager.save_hardware(config)
        self.emit("device-created", config)
        self.close()

    def _save_custom_config(self: Any) -> None:
        selected_device = self._discovery_state.selected_device
        if selected_device is None:
            return
        vendor_id, product_id, name = self._selected_device_fields(selected_device)
        self._persist_config(
            HardwareConfig(
                vendor_id=vendor_id,
                product_id=product_id,
                name=name,
                evdev_devices=self._build_evdev_devices(
                    list(self._discovery_state.discovered_interfaces.values())
                ),
                buttons=[],
                id=self._selected_config_id(selected_device),
            )
        )

    def _save_keyboard_config(self: Any) -> None:
        selected_device = self._discovery_state.selected_device
        if selected_device is None:
            return
        vendor_id, product_id, name = self._selected_device_fields(selected_device)
        keyboard_interfaces = self._interfaces_for_roles({"keyboard"})
        interfaces = self._merge_interface_lists(
            keyboard_interfaces,
            list(self._discovery_state.discovered_interfaces.values()),
        )
        source = str(keyboard_interfaces[0].get("id", "") or "") if keyboard_interfaces else ""
        self._persist_config(
            HardwareConfig(
                vendor_id=vendor_id,
                product_id=product_id,
                name=name,
                evdev_devices=self._build_evdev_devices(interfaces),
                buttons=self._build_standard_keyboard_buttons(source),
                id=self._selected_config_id(selected_device),
            )
        )

    def _save_mouse_config(self: Any) -> None:
        selected_device = self._discovery_state.selected_device
        if selected_device is None:
            return
        vendor_id, product_id, name = self._selected_device_fields(selected_device)
        mouse_interfaces = self._interfaces_for_roles({"mouse", "pointstick"})
        interfaces = self._merge_interface_lists(
            mouse_interfaces,
            list(self._discovery_state.discovered_interfaces.values()),
        )
        source = str(mouse_interfaces[0].get("id", "") or "") if mouse_interfaces else ""
        self._persist_config(
            HardwareConfig(
                vendor_id=vendor_id,
                product_id=product_id,
                name=name,
                evdev_devices=self._build_evdev_devices(interfaces),
                buttons=self._build_standard_mouse_buttons(
                    source,
                    include_horizontal=self._interfaces_have_capability(
                        mouse_interfaces,
                        "rel_hwheel",
                    ),
                ),
                id=self._selected_config_id(selected_device),
            )
        )

    def _save_mouse_keyboard_config(self: Any) -> None:
        selected_device = self._discovery_state.selected_device
        if selected_device is None:
            return
        vendor_id, product_id, name = self._selected_device_fields(selected_device)
        keyboard_interfaces = self._interfaces_for_roles({"keyboard"})
        mouse_interfaces = self._interfaces_for_roles({"mouse", "pointstick"})
        keyboard_source = (
            str(keyboard_interfaces[0].get("id", "") or "") if keyboard_interfaces else ""
        )
        mouse_source = str(mouse_interfaces[0].get("id", "") or "") if mouse_interfaces else ""
        interfaces = self._merge_interface_lists(
            mouse_interfaces,
            keyboard_interfaces,
            list(self._discovery_state.discovered_interfaces.values()),
        )
        buttons = self._build_standard_mouse_buttons(
            mouse_source,
            include_horizontal=self._interfaces_have_capability(
                mouse_interfaces,
                "rel_hwheel",
            ),
        )
        buttons.extend(self._build_standard_keyboard_buttons(keyboard_source))
        self._persist_config(
            HardwareConfig(
                vendor_id=vendor_id,
                product_id=product_id,
                name=name,
                evdev_devices=self._build_evdev_devices(interfaces),
                buttons=buttons,
                id=self._selected_config_id(selected_device),
            )
        )

    def _save_gamepad_config(self: Any) -> None:
        selected_device = self._discovery_state.selected_device
        if selected_device is None:
            return
        vendor_id, product_id, name = self._selected_device_fields(selected_device)
        gamepad_interfaces = self._gamepad_interfaces()
        motion_interfaces = self._interfaces_for_roles({"motion"})
        interfaces = self._merge_interface_lists(
            gamepad_interfaces,
            motion_interfaces,
            list(self._discovery_state.discovered_interfaces.values()),
        )
        self._persist_config(
            HardwareConfig(
                vendor_id=vendor_id,
                product_id=product_id,
                name=name,
                evdev_devices=self._build_evdev_devices(interfaces),
                buttons=self._build_gamepad_buttons(gamepad_interfaces),
                analog_inputs=self._build_gamepad_analog_inputs(gamepad_interfaces),
                motion_sensors=self._build_motion_sensors(motion_interfaces),
                id=self._selected_config_id(selected_device),
            )
        )

    def _gamepad_interfaces(self: Any) -> list[Mapping[str, Any]]:
        return self._interfaces_for_roles({"gamepad"})

    def _interfaces_for_roles(
        self: Any,
        roles: set[str],
    ) -> list[Mapping[str, Any]]:
        return templates.interfaces_for_roles(
            list(self._discovery_state.discovered_interfaces.values()),
            roles,
        )

    def _merge_interface_lists(
        self: Any,
        *interface_lists: Sequence[Mapping[str, Any]],
    ) -> list[Mapping[str, Any]]:
        return templates.merge_interface_lists(*interface_lists)

    def _interfaces_have_capability(
        self: Any,
        interfaces: Sequence[Mapping[str, Any]],
        capability: str,
    ) -> bool:
        return templates.interfaces_have_capability(interfaces, capability)

    def _build_evdev_devices(
        self: Any,
        interfaces: Sequence[Mapping[str, Any]],
    ) -> list[EvdevDevice]:
        return templates.build_evdev_devices(interfaces)

    def _build_standard_mouse_buttons(
        self: Any,
        source_id: str,
        *,
        include_horizontal: bool = False,
    ) -> list[ButtonDefinition]:
        return templates.build_standard_mouse_buttons(
            source_id,
            include_horizontal=include_horizontal,
        )

    def _build_gamepad_buttons(
        self: Any,
        interfaces: Sequence[Mapping[str, Any]],
    ) -> list[ButtonDefinition]:
        return templates.build_gamepad_buttons(interfaces)

    def _build_gamepad_analog_inputs(
        self: Any,
        interfaces: Sequence[Mapping[str, Any]],
    ) -> list[AnalogInputDefinition]:
        return templates.build_gamepad_analog_inputs(interfaces)

    def _build_motion_sensors(
        self: Any,
        interfaces: Sequence[Mapping[str, Any]],
    ) -> list[MotionSensorDefinition]:
        return templates.build_motion_sensors(interfaces)

    def _build_standard_keyboard_buttons(
        self: Any,
        source_id: str,
    ) -> list[ButtonDefinition]:
        return templates.build_standard_keyboard_buttons(source_id)

    def _keyboard_label_from_evdev(self: Any, key_name: str) -> str:
        return templates.keyboard_label_from_evdev(key_name)
