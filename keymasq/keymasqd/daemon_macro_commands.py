import asyncio
from typing import Protocol, cast

from keymasq.common.ipc import CommandType
from keymasq.keymasqd.daemon_helpers import (
    JsonObject,
    JsonObjectList,
    float_like,
    int_like,
    json_object_list,
    str_value,
)


class _MacroCommandDeviceManager(Protocol):
    async def play_macro(
        self,
        macro_events: JsonObjectList,
        macro_name: str = "",
        replay_mouse_movement: bool = True,
        replay_mouse_clicks: bool = True,
        speed: float = 1.0,
        loop_mode: str = "none",
        loop_count: int = 1,
        move_to_start: bool = False,
        start_x: int = 0,
        start_y: int = 0,
        block_mouse_movement: bool = False,
        source_device: str = "",
        source_button: str = "",
        trigger_value: int = 1,
    ) -> JsonObject: ...

    async def cancel_macro_playback(self) -> JsonObject: ...

    def complete_macro_exec_wait(self, wait_id: str, returncode: int) -> JsonObject: ...


class _MacroDefinitionStore(Protocol):
    def get(self, name: str) -> JsonObject: ...


class _MacroCommandStore(_MacroDefinitionStore, Protocol):

    def list_meta(self) -> JsonObjectList: ...

    def create(self, payload: JsonObject) -> JsonObject: ...

    def update(
        self, name: str, payload: JsonObject, expected_revision: int | None
    ) -> JsonObject: ...

    def rename(self, old_name: str, new_name: str, expected_revision: int | None) -> JsonObject: ...

    def delete(self, name: str, expected_revision: int | None) -> None: ...


class _MacroCommandDaemon(Protocol):
    device_manager: _MacroCommandDeviceManager
    macro_store: _MacroCommandStore


MacroCommandDaemon = _MacroCommandDaemon


async def handle_macro_command(
    daemon: _MacroCommandDaemon,
    command_type: CommandType,
    data: JsonObject,
) -> JsonObject | None:
    if command_type == CommandType.PLAY_MACRO:
        return await play_macro_from_payload(daemon, data)

    if command_type == CommandType.MACRO_LIST_META:
        macros = await asyncio.to_thread(daemon.macro_store.list_meta)
        return {"macros": macros}

    if command_type == CommandType.MACRO_GET:
        name = str_value(data.get("name", ""))
        macro = await asyncio.to_thread(daemon.macro_store.get, name)
        return {"macro": macro}

    if command_type == CommandType.MACRO_CREATE:
        raw_payload = data.get("macro", {})
        if not isinstance(raw_payload, dict):
            raise ValueError("macro payload must be an object")
        macro = await asyncio.to_thread(
            daemon.macro_store.create,
            cast(JsonObject, raw_payload),
        )
        return {"macro": macro}

    if command_type == CommandType.MACRO_UPDATE:
        name = str_value(data.get("name", ""))
        raw_payload = data.get("macro", {})
        if not isinstance(raw_payload, dict):
            raise ValueError("macro payload must be an object")
        expected_revision = data.get("expected_revision")
        revision = int_like(expected_revision, 0) if expected_revision is not None else None
        macro = await asyncio.to_thread(
            daemon.macro_store.update,
            name,
            cast(JsonObject, raw_payload),
            revision,
        )
        return {"macro": macro}

    if command_type == CommandType.MACRO_RENAME:
        old_name = str_value(data.get("old_name", ""))
        new_name = str_value(data.get("new_name", ""))
        expected_revision = data.get("expected_revision")
        revision = int_like(expected_revision, 0) if expected_revision is not None else None
        macro = await asyncio.to_thread(daemon.macro_store.rename, old_name, new_name, revision)
        return {"macro": macro}

    if command_type == CommandType.MACRO_DELETE:
        name = str_value(data.get("name", ""))
        expected_revision = data.get("expected_revision")
        revision = int_like(expected_revision, 0) if expected_revision is not None else None
        await asyncio.to_thread(daemon.macro_store.delete, name, revision)
        return {"status": "ok"}

    if command_type == CommandType.MACRO_PLAY_BY_NAME:
        return await play_macro_by_name(daemon, data)

    if command_type == CommandType.CANCEL_MACRO_PLAYBACK:
        return await daemon.device_manager.cancel_macro_playback()

    if command_type == CommandType.MACRO_EXEC_COMPLETE:
        wait_id = str_value(data.get("wait_id", ""))
        returncode = int_like(data.get("returncode", 0), 0)
        return daemon.device_manager.complete_macro_exec_wait(wait_id, returncode)

    return None


async def play_macro_from_payload(daemon: _MacroCommandDaemon, data: JsonObject) -> JsonObject:
    macro_events = json_object_list(data.get("macro_events", []))
    macro_name = str_value(data.get("macro_name", ""))
    loop_mode = str_value(data.get("loop_mode", "none"), "none") or "none"
    loop_count = int_like(data.get("loop_count", 1), 1)
    move_to_start = bool(data.get("move_to_start", False))
    start_x = int_like(data.get("start_x", 0), 0)
    start_y = int_like(data.get("start_y", 0), 0)
    block_mouse_movement = bool(data.get("block_mouse_movement", False))

    if macro_name and not macro_events:
        macro_data = await asyncio.to_thread(daemon.macro_store.get, macro_name)
        macro_events = json_object_list(macro_data.get("events", []))
        loop_mode = str_value(macro_data.get("loop_mode", loop_mode), loop_mode) or loop_mode
        loop_count = int_like(macro_data.get("loop_count", loop_count), loop_count)
        move_to_start = bool(macro_data.get("move_to_start", move_to_start))
        start_x = int_like(macro_data.get("start_x", start_x), start_x)
        start_y = int_like(macro_data.get("start_y", start_y), start_y)
        block_mouse_movement = bool(
            macro_data.get("block_mouse_movement", block_mouse_movement)
        )

    return await daemon.device_manager.play_macro(
        macro_events=macro_events,
        macro_name=macro_name,
        replay_mouse_movement=bool(data.get("replay_mouse_movement", True)),
        replay_mouse_clicks=bool(data.get("replay_mouse_clicks", True)),
        speed=float_like(data.get("speed", 1.0), 1.0),
        loop_mode=loop_mode,
        loop_count=loop_count,
        move_to_start=move_to_start,
        start_x=start_x,
        start_y=start_y,
        block_mouse_movement=block_mouse_movement,
        source_device=str_value(data.get("source_device", "")),
        source_button=str_value(data.get("source_button", "")),
        trigger_value=int_like(data.get("trigger_value", 1), 1),
    )


async def play_macro_by_name(daemon: _MacroCommandDaemon, data: JsonObject) -> JsonObject:
    name = str_value(data.get("name", ""))
    macro_data = await asyncio.to_thread(daemon.macro_store.get, name)
    return await daemon.device_manager.play_macro(
        macro_events=json_object_list(macro_data.get("events", [])),
        macro_name=name,
        replay_mouse_movement=bool(data.get("replay_mouse_movement", True)),
        replay_mouse_clicks=bool(data.get("replay_mouse_clicks", True)),
        speed=float_like(data.get("speed", 1.0), 1.0),
        loop_mode=str_value(macro_data.get("loop_mode", "none"), "none") or "none",
        loop_count=int_like(macro_data.get("loop_count", 1), 1),
        move_to_start=bool(macro_data.get("move_to_start", False)),
        start_x=int_like(macro_data.get("start_x", 0), 0),
        start_y=int_like(macro_data.get("start_y", 0), 0),
        block_mouse_movement=bool(macro_data.get("block_mouse_movement", False)),
    )


async def load_macro_definitions(
    macro_store: _MacroDefinitionStore,
    macro_names: set[str],
) -> dict[str, JsonObject]:
    if not macro_names:
        return {}

    async def load_macro(name: str) -> tuple[str, JsonObject | None]:
        try:
            macro = await asyncio.to_thread(macro_store.get, name)
        except Exception:
            return name, None
        return name, macro

    loaded = await asyncio.gather(*(load_macro(name) for name in sorted(macro_names)))
    return {name: macro for name, macro in loaded if isinstance(macro, dict)}


def apply_macro_definition(action_data: JsonObject, macro: JsonObject) -> JsonObject:
    updated: JsonObject = dict(action_data)
    updated["macro_events"] = json_object_list(macro.get("events", []))
    updated["macro_loop_mode"] = str_value(macro.get("loop_mode", "none"), "none") or "none"
    updated["macro_loop_count"] = int_like(macro.get("loop_count", 1), 1)
    updated["macro_move_to_start"] = bool(macro.get("move_to_start", False))
    updated["macro_start_x"] = int_like(macro.get("start_x", 0), 0)
    updated["macro_start_y"] = int_like(macro.get("start_y", 0), 0)
    updated["macro_block_mouse_movement"] = bool(macro.get("block_mouse_movement", False))
    return updated


def _collect_macro_names_from_action(action_data: JsonObject, macro_names: set[str]) -> None:
    action_type = str(action_data.get("action", "") or "")
    macro_name = str(action_data.get("macro_name", "") or "")
    if action_type == "macro" and macro_name and not action_data.get("macro_events"):
        macro_names.add(macro_name)
        return

    if action_type != "superkey":
        return

    superkey = action_data.get("superkey")
    if isinstance(superkey, dict):
        _collect_macro_names_from_superkey(cast(JsonObject, superkey), macro_names)


def _collect_macro_names_from_superkey(superkey: JsonObject, macro_names: set[str]) -> None:
    for key in (
        "tap_actions",
        "double_tap_actions",
        "hold_actions",
        "tap_hold_actions",
        "overload_actions",
    ):
        bundle = superkey.get(key)
        if not isinstance(bundle, list):
            continue
        for item in cast(list[object], bundle):
            if isinstance(item, dict):
                _collect_macro_names_from_action(cast(JsonObject, item), macro_names)


def _resolve_action_macros(action_data: JsonObject, macros: dict[str, JsonObject]) -> JsonObject:
    updated: JsonObject = dict(action_data)
    action_type = str(updated.get("action", "") or "")
    macro_name = str(updated.get("macro_name", "") or "")

    if (
        action_type == "macro"
        and macro_name
        and not updated.get("macro_events")
        and macro_name in macros
    ):
        try:
            return apply_macro_definition(updated, macros[macro_name])
        except (TypeError, ValueError):
            return updated

    if action_type != "superkey":
        return updated

    superkey = updated.get("superkey")
    if not isinstance(superkey, dict):
        return updated

    updated["superkey"] = _resolve_superkey_macros(cast(JsonObject, superkey), macros)
    return updated


def _resolve_superkey_macros(superkey: JsonObject, macros: dict[str, JsonObject]) -> JsonObject:
    updated: JsonObject = dict(superkey)
    for key in (
        "tap_actions",
        "double_tap_actions",
        "hold_actions",
        "tap_hold_actions",
        "overload_actions",
    ):
        bundle = updated.get(key)
        if not isinstance(bundle, list):
            continue
        updated[key] = [
            (
                _resolve_action_macros(cast(JsonObject, item), macros)
                if isinstance(item, dict)
                else item
            )
            for item in cast(list[object], bundle)
        ]
    return updated


async def resolve_mapping_macros(
    macro_store: _MacroDefinitionStore,
    mapping: JsonObject,
) -> JsonObject:
    macro_names: set[str] = set()
    for action_raw in mapping.values():
        if isinstance(action_raw, dict):
            _collect_macro_names_from_action(cast(JsonObject, action_raw), macro_names)
    macros = await load_macro_definitions(macro_store, macro_names)

    resolved: JsonObject = {}
    for button_id, action_data in mapping.items():
        if not isinstance(action_data, dict):
            resolved[button_id] = action_data
            continue

        resolved[button_id] = _resolve_action_macros(cast(JsonObject, action_data), macros)

    return resolved


async def resolve_combo_macros(
    macro_store: _MacroDefinitionStore,
    combos: JsonObjectList,
) -> JsonObjectList:
    macro_names: set[str] = set()
    for combo in combos:
        action_raw = combo.get("action")
        if isinstance(action_raw, dict):
            _collect_macro_names_from_action(cast(JsonObject, action_raw), macro_names)
    macros = await load_macro_definitions(macro_store, macro_names)

    resolved: JsonObjectList = []
    for combo in combos:
        updated: JsonObject = dict(combo)
        action_data = updated.get("action")
        if not isinstance(action_data, dict):
            resolved.append(updated)
            continue

        updated["action"] = _resolve_action_macros(cast(JsonObject, action_data), macros)
        resolved.append(updated)

    return resolved
