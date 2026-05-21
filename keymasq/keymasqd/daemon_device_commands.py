from collections.abc import Sequence
from typing import Protocol, cast

from keymasq.common.ipc import CommandType
from keymasq.keymasqd import daemon_macro_commands
from keymasq.keymasqd.daemon_helpers import (
    JsonObject,
    float_like,
    int_dict,
    json_object,
    json_object_list,
    str_dict,
    str_list,
    str_value,
)


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
            hardware_id=str_value(data["hardware_id"]),
            evdev_paths=str_list(data["evdev_paths"]),
            button_map=str_dict(data.get("button_map", {})),
            button_codes=int_dict(data.get("button_codes", {})),
            button_values=int_dict(data.get("button_values", {})),
            analog_inputs=json_object(data.get("analog_inputs", {})),
            force_grab_unmapped=bool(data.get("force_grab_unmapped", False)),
        )

    if command_type == CommandType.RELEASE_DEVICE:
        grace_s = data.get("grace_s")
        return await daemon.device_manager.release_device(
            hardware_id=str_value(data["hardware_id"]),
            immediate=bool(data.get("immediate", False)),
            grace_s=float_like(grace_s, 0.0) if grace_s is not None else None,
        )

    if command_type == CommandType.SET_MAPPING:
        mapping = await daemon_macro_commands.resolve_mapping_macros(
            daemon.macro_store,
            json_object(data["mapping"]),
        )
        return await daemon.device_manager.set_mapping(
            hardware_id=str_value(data["hardware_id"]),
            mapping=mapping,
        )

    if command_type == CommandType.SET_COMBOS:
        combos = await daemon_macro_commands.resolve_combo_macros(
            daemon.macro_store,
            json_object_list(data.get("combos", [])),
        )
        return await daemon.device_manager.set_combos(combos)

    if command_type == CommandType.LIST_DEVICES:
        return await daemon.device_manager.list_devices()

    if command_type == CommandType.SET_DIAGNOSTICS:
        enabled = bool(data.get("enabled", False))
        interval = float_like(data.get("interval", 5.0), 5.0)
        raw_categories = data.get("categories")
        categories = (
            [str_value(category) for category in cast(list[object], raw_categories)]
            if isinstance(raw_categories, list)
            else None
        )
        return await daemon.device_manager.set_diagnostics(enabled, interval, categories)

    if command_type == CommandType.SET_VIRTUAL_GAMEPADS:
        return await daemon.device_manager.set_virtual_gamepads(data.get("count"))

    if command_type == CommandType.DEVICE_INSPECTOR_START:
        return await daemon.device_manager.start_device_inspector(
            hardware_id=str_value(data["hardware_id"]),
        )

    if command_type == CommandType.DEVICE_INSPECTOR_STOP:
        return await daemon.device_manager.stop_device_inspector(
            hardware_id=str_value(data["hardware_id"]),
        )

    if command_type == CommandType.DEVICE_INSPECTOR_ENABLE_SUPPRESSION:
        return await daemon.device_manager.enable_device_inspector_suppression(
            hardware_id=str_value(data["hardware_id"]),
        )

    if command_type == CommandType.DEVICE_INSPECTOR_DISABLE_SUPPRESSION:
        return await daemon.device_manager.disable_device_inspector_suppression(
            hardware_id=str_value(data["hardware_id"]),
            reason=str_value(data.get("reason", "manual"), "manual"),
        )

    return None
