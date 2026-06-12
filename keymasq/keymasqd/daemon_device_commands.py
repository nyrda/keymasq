from collections.abc import Sequence
from typing import Protocol, cast

from keymasq.common.coercion import coerce_float, coerce_str
from keymasq.common.ipc import CommandType
from keymasq.common.types import JsonObject, JsonObjectList
from keymasq.keymasqd import daemon_macro_commands


class _DeviceCommandManager(Protocol):
    async def grab_device(
        self,
        hardware_id: str,
        evdev_paths: list[str],
        button_map: dict[str, str],
        button_codes: dict[str, int] | None = None,
        button_values: dict[str, int] | None = None,
        analog_inputs: JsonObject | None = None,
        force_grab_unmapped: bool = False,
        evdev_interfaces: list[JsonObject] | None = None,
    ) -> JsonObject: ...

    async def release_device(
        self, hardware_id: str, immediate: bool = False, grace_s: float | None = None
    ) -> JsonObject: ...

    async def set_mapping(self, hardware_id: str, mapping: JsonObject) -> JsonObject: ...

    async def set_combos(self, combos: Sequence[object]) -> JsonObject: ...

    async def set_cursor_position(self, x: int, y: int) -> JsonObject: ...

    async def list_devices(self) -> JsonObject: ...

    async def set_diagnostics(
        self,
        enabled: bool,
        interval: float,
        categories: Sequence[object] | None = None,
    ) -> JsonObject: ...

    async def set_virtual_gamepads(self, count: object) -> JsonObject: ...

    async def track_profile_activation(
        self,
        profile_name: str,
        activation_id: str,
        trigger_id: str,
        deactivation: object,
    ) -> JsonObject: ...

    async def cancel_profile_activation(
        self,
        profile_name: str = "",
        activation_id: str = "",
    ) -> JsonObject: ...

    async def start_device_inspector(self, hardware_id: str) -> JsonObject: ...

    async def stop_device_inspector(self, hardware_id: str) -> JsonObject: ...

    async def enable_device_inspector_suppression(self, hardware_id: str) -> JsonObject: ...

    async def disable_device_inspector_suppression(
        self,
        hardware_id: str,
        reason: str = "manual",
    ) -> JsonObject: ...


class _DeviceCommandMacroStore(Protocol):
    def get(self, name: str) -> JsonObject: ...

    def get_meta(self, name: str) -> JsonObject: ...


class _DeviceCommandDaemon(Protocol):
    device_manager: _DeviceCommandManager
    macro_store: _DeviceCommandMacroStore


DeviceCommandDaemon = _DeviceCommandDaemon


async def handle_device_command(
    daemon: _DeviceCommandDaemon,
    command_type: CommandType,
    data: JsonObject,
) -> JsonObject | None:
    if command_type == CommandType.GRAB_DEVICE:
        return await daemon.device_manager.grab_device(
            hardware_id=coerce_str(data["hardware_id"]),
            evdev_paths=cast(list[str], data["evdev_paths"]),
            button_map=cast(dict[str, str], data.get("button_map", {})),
            button_codes=cast(dict[str, int], data.get("button_codes", {})),
            button_values=cast(dict[str, int], data.get("button_values", {})),
            analog_inputs=cast(JsonObject, data.get("analog_inputs", {})),
            force_grab_unmapped=bool(data.get("force_grab_unmapped", False)),
            evdev_interfaces=cast(JsonObjectList, data.get("evdev_interfaces", [])),
        )

    if command_type == CommandType.RELEASE_DEVICE:
        grace_s = data.get("grace_s")
        return await daemon.device_manager.release_device(
            hardware_id=coerce_str(data["hardware_id"]),
            immediate=bool(data.get("immediate", False)),
            grace_s=coerce_float(grace_s, 0.0) if grace_s is not None else None,
        )

    if command_type == CommandType.SET_MAPPING:
        mapping = await daemon_macro_commands.resolve_mapping_macros(
            daemon.macro_store,
            cast(JsonObject, data["mapping"]),
        )
        return await daemon.device_manager.set_mapping(
            hardware_id=coerce_str(data["hardware_id"]),
            mapping=mapping,
        )

    if command_type == CommandType.SET_COMBOS:
        combos = await daemon_macro_commands.resolve_combo_macros(
            daemon.macro_store,
            cast(JsonObjectList, data.get("combos", [])),
        )
        return await daemon.device_manager.set_combos(combos)

    if command_type == CommandType.LIST_DEVICES:
        return await daemon.device_manager.list_devices()

    if command_type == CommandType.SET_DIAGNOSTICS:
        enabled = bool(data.get("enabled", False))
        interval = coerce_float(data.get("interval", 5.0), 5.0)
        raw_categories = data.get("categories")
        categories = (
            [coerce_str(category) for category in cast(list[object], raw_categories)]
            if isinstance(raw_categories, list)
            else None
        )
        return await daemon.device_manager.set_diagnostics(enabled, interval, categories)

    if command_type == CommandType.SET_VIRTUAL_GAMEPADS:
        return await daemon.device_manager.set_virtual_gamepads(data.get("count"))

    if command_type == CommandType.TRACK_PROFILE_ACTIVATION:
        return await daemon.device_manager.track_profile_activation(
            profile_name=coerce_str(data.get("profile_name", "")),
            activation_id=coerce_str(data.get("activation_id", "")),
            trigger_id=coerce_str(data.get("trigger_id", "")),
            deactivation=data.get("deactivation", {}),
        )

    if command_type == CommandType.CANCEL_PROFILE_ACTIVATION:
        return await daemon.device_manager.cancel_profile_activation(
            profile_name=coerce_str(data.get("profile_name", "")),
            activation_id=coerce_str(data.get("activation_id", "")),
        )

    if command_type == CommandType.DEVICE_INSPECTOR_START:
        return await daemon.device_manager.start_device_inspector(
            hardware_id=coerce_str(data["hardware_id"]),
        )

    if command_type == CommandType.DEVICE_INSPECTOR_STOP:
        return await daemon.device_manager.stop_device_inspector(
            hardware_id=coerce_str(data["hardware_id"]),
        )

    if command_type == CommandType.DEVICE_INSPECTOR_ENABLE_SUPPRESSION:
        return await daemon.device_manager.enable_device_inspector_suppression(
            hardware_id=coerce_str(data["hardware_id"]),
        )

    if command_type == CommandType.DEVICE_INSPECTOR_DISABLE_SUPPRESSION:
        return await daemon.device_manager.disable_device_inspector_suppression(
            hardware_id=coerce_str(data["hardware_id"]),
            reason=coerce_str(data.get("reason", "manual"), "manual"),
        )

    return None
