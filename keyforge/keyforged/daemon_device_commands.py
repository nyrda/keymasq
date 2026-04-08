from collections.abc import Sequence
from typing import Protocol

from keyforge.common.ipc import CommandType
from keyforge.keyforged import daemon_macro_commands
from keyforge.keyforged.daemon_helpers import (
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
        force_grab_unmapped: bool = False,
    ) -> JsonObject: ...

    async def release_device(
        self, hardware_id: str, immediate: bool = False, grace_s: float | None = None
    ) -> JsonObject: ...

    async def set_mapping(self, hardware_id: str, mapping: JsonObject) -> JsonObject: ...

    async def set_combos(self, combos: Sequence[object]) -> JsonObject: ...

    async def list_devices(self) -> JsonObject: ...

    async def set_diagnostics(self, enabled: bool, interval: float) -> JsonObject: ...


class _DeviceCommandMacroStore(Protocol):
    def get(self, name: str) -> JsonObject: ...


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
        return await daemon.device_manager.set_diagnostics(enabled, interval)

    return None
