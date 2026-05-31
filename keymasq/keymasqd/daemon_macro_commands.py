import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, cast

from keymasq.common.ipc import CommandType
from keymasq.common.models import (
    DEFAULT_MACRO_LOOP_STOP_BEHAVIOR,
    normalize_macro_loop_stop_behavior,
    normalize_macro_recording_slot,
)
from keymasq.keymasqd.daemon_helpers import (
    JsonObject,
    JsonObjectList,
    float_like,
    int_like,
    str_value,
)

type MacroEvent = dict[str, object]
type ActionTransform = Callable[[JsonObject], JsonObject]

SUPERKEY_ACTION_KEYS = (
    "tap_actions",
    "double_tap_actions",
    "hold_actions",
    "tap_hold_actions",
    "overload_actions",
    "overload_down_actions",
    "overload_up_actions",
)


@dataclass(frozen=True)
class MacroRuntimeOptions:
    loop_mode: str = "none"
    loop_count: int = 1
    loop_stop_behavior: str = DEFAULT_MACRO_LOOP_STOP_BEHAVIOR
    move_to_start: bool = False
    start_x: int = 0
    start_y: int = 0
    block_mouse_movement: bool = False


@dataclass(frozen=True)
class MacroPlaybackOptions:
    macro_events: JsonObjectList
    macro_name: str = ""
    replay_mouse_movement: bool = True
    replay_mouse_clicks: bool = True
    speed: float = 1.0
    runtime_options: MacroRuntimeOptions = field(default_factory=MacroRuntimeOptions)
    source_device: str = ""
    source_button: str = ""
    trigger_value: int = 1
    load_stored_macro: bool = True


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
        loop_stop_behavior: str = DEFAULT_MACRO_LOOP_STOP_BEHAVIOR,
        move_to_start: bool = False,
        start_x: int = 0,
        start_y: int = 0,
        block_mouse_movement: bool = False,
        source_device: str = "",
        source_button: str = "",
        trigger_value: int = 1,
        load_stored_macro: bool = True,
    ) -> JsonObject: ...

    async def cancel_macro_playback(self) -> JsonObject: ...

    async def emergency_reset(self) -> JsonObject: ...

    def complete_macro_exec_wait(self, wait_id: str, returncode: int) -> JsonObject: ...


class _MacroDefinitionStore(Protocol):
    def get(self, name: str) -> JsonObject: ...

    def get_meta(self, name: str) -> JsonObject: ...


class _MacroCommandStore(_MacroDefinitionStore, Protocol):
    def list_meta(self) -> JsonObjectList: ...

    def create(self, payload: JsonObject) -> JsonObject: ...

    def update(
        self, name: str, payload: JsonObject, expected_revision: int | None
    ) -> JsonObject: ...

    def rename(self, old_name: str, new_name: str, expected_revision: int | None) -> JsonObject: ...

    def delete(self, name: str, expected_revision: int | None) -> None: ...

    def create_from_events(
        self,
        payload: JsonObject,
        events: Iterable[MacroEvent],
        *,
        return_full: bool = False,
    ) -> JsonObject: ...


class _MacroCommandRecordingManager(Protocol):
    async def list_pending_recordings(self) -> JsonObjectList: ...

    async def claim_pending_recording(self, recording_id: str) -> object: ...

    async def release_pending_recording_claim(
        self,
        recording_id: str,
        *,
        saved: bool,
    ) -> None: ...

    async def discard_pending_recording(self, recording_id: str) -> None: ...


class _PendingRecording(Protocol):
    recording_id: str
    duration_ms: int
    device_types: list[str]
    event_count: int

    def iter_events(self) -> Iterable[MacroEvent]: ...


class _MacroCommandDaemon(Protocol):
    device_manager: _MacroCommandDeviceManager
    macro_store: _MacroCommandStore
    recording_manager: _MacroCommandRecordingManager


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

    if command_type == CommandType.MACRO_SAVE_RECORDING:
        return await save_pending_recording(daemon, data)

    if command_type == CommandType.MACRO_LIST_RECORDINGS:
        recordings = await daemon.recording_manager.list_pending_recordings()
        return {"recordings": recordings}

    if command_type == CommandType.MACRO_DELETE_RECORDING:
        recording_id = str_value(data.get("pending_recording_id", ""))
        await daemon.recording_manager.discard_pending_recording(recording_id)
        return {"status": "ok"}

    if command_type == CommandType.MACRO_PLAY_RECORDING:
        return await play_pending_recording(daemon, data)

    if command_type == CommandType.MACRO_PLAY_BY_NAME:
        return await play_macro_by_name(daemon, data)

    if command_type == CommandType.CANCEL_MACRO_PLAYBACK:
        return await daemon.device_manager.cancel_macro_playback()

    if command_type == CommandType.EMERGENCY_RESET:
        return await daemon.device_manager.emergency_reset()

    if command_type == CommandType.MACRO_EXEC_COMPLETE:
        wait_id = str_value(data.get("wait_id", ""))
        returncode = int_like(data.get("returncode", 0), 0)
        return daemon.device_manager.complete_macro_exec_wait(wait_id, returncode)

    return None


async def save_pending_recording(
    daemon: _MacroCommandDaemon,
    data: JsonObject,
) -> JsonObject:
    recording_id = str_value(data.get("pending_recording_id", ""))
    if not recording_id:
        raise ValueError("pending_recording_id required")
    if not str_value(data.get("name", "")):
        raise ValueError("name required")
    snapshot = cast(
        _PendingRecording,
        await daemon.recording_manager.claim_pending_recording(recording_id),
    )
    try:
        return await asyncio.to_thread(_save_pending_recording_sync, daemon, data, snapshot)
    finally:
        is_slot_backed = bool(
            normalize_macro_recording_slot(getattr(snapshot, "recording_slot", 0))
        )
        await daemon.recording_manager.release_pending_recording_claim(
            recording_id,
            saved=not is_slot_backed,
        )


def _save_pending_recording_sync(
    daemon: _MacroCommandDaemon,
    data: JsonObject,
    snapshot: _PendingRecording,
) -> JsonObject:
    name = str_value(data.get("name", ""))
    if not name:
        raise ValueError("name required")

    payload: JsonObject = {
        "name": name,
        "created_at": datetime.now().isoformat(),
        "duration_us": int(snapshot.duration_ms) * 1000,
        "device_types": list(snapshot.device_types),
        "event_count": int(snapshot.event_count),
        "move_to_start": bool(data.get("move_to_start", False)),
        "start_x": int_like(data.get("start_x", 0), 0),
        "start_y": int_like(data.get("start_y", 0), 0),
        "block_mouse_movement": bool(data.get("block_mouse_movement", False)),
    }
    macro = daemon.macro_store.create_from_events(
        payload,
        snapshot.iter_events(),
        return_full=False,
    )
    return {"macro": macro}


async def play_macro_from_payload(daemon: _MacroCommandDaemon, data: JsonObject) -> JsonObject:
    macro_events = cast(JsonObjectList, data.get("macro_events", []))
    macro_name = str_value(data.get("macro_name", ""))
    stored_macro: JsonObject | None = None

    if macro_name and not macro_events:
        stored_macro = await asyncio.to_thread(
            _load_macro_meta_sync,
            daemon.macro_store,
            macro_name,
        )

    return await _play_macro_with_options(
        daemon,
        _macro_playback_options(data, macro_events, macro_name, stored_macro=stored_macro),
    )


async def play_macro_by_name(daemon: _MacroCommandDaemon, data: JsonObject) -> JsonObject:
    name = str_value(data.get("name", ""))
    stored_macro = await asyncio.to_thread(_load_macro_meta_sync, daemon.macro_store, name)
    return await _play_macro_with_options(
        daemon,
        _macro_playback_options(data, [], name, stored_macro=stored_macro),
    )


async def play_pending_recording(
    daemon: _MacroCommandDaemon,
    data: JsonObject,
) -> JsonObject:
    recording_id = str_value(data.get("pending_recording_id", ""))
    if not recording_id:
        raise ValueError("pending_recording_id required")
    snapshot = cast(
        _PendingRecording,
        await daemon.recording_manager.claim_pending_recording(recording_id),
    )
    try:
        macro_events = await asyncio.to_thread(lambda: list(snapshot.iter_events()))
        macro_name = str_value(data.get("macro_name", ""))
        if not macro_name:
            macro_name = recording_id
        return await _play_macro_with_options(
            daemon,
            _macro_playback_options(
                data,
                macro_events,
                macro_name,
                load_stored_macro=False,
            ),
        )
    finally:
        await daemon.recording_manager.release_pending_recording_claim(
            recording_id,
            saved=False,
        )


async def _play_macro_with_options(
    daemon: _MacroCommandDaemon,
    options: MacroPlaybackOptions,
) -> JsonObject:
    runtime_options = options.runtime_options
    return await daemon.device_manager.play_macro(
        macro_events=options.macro_events,
        macro_name=options.macro_name,
        replay_mouse_movement=options.replay_mouse_movement,
        replay_mouse_clicks=options.replay_mouse_clicks,
        speed=options.speed,
        loop_mode=runtime_options.loop_mode,
        loop_count=runtime_options.loop_count,
        loop_stop_behavior=runtime_options.loop_stop_behavior,
        move_to_start=runtime_options.move_to_start,
        start_x=runtime_options.start_x,
        start_y=runtime_options.start_y,
        block_mouse_movement=runtime_options.block_mouse_movement,
        source_device=options.source_device,
        source_button=options.source_button,
        trigger_value=options.trigger_value,
        load_stored_macro=options.load_stored_macro,
    )


async def load_macro_definitions(
    macro_store: _MacroDefinitionStore,
    macro_names: set[str],
) -> dict[str, JsonObject]:
    if not macro_names:
        return {}

    async def load_macro(name: str) -> tuple[str, JsonObject | None]:
        try:
            macro = await asyncio.to_thread(_load_macro_meta_sync, macro_store, name)
        except Exception:
            return name, None
        return name, macro

    loaded = await asyncio.gather(*(load_macro(name) for name in sorted(macro_names)))
    return {name: macro for name, macro in loaded if isinstance(macro, dict)}


def _load_macro_meta_sync(macro_store: _MacroDefinitionStore, name: str) -> JsonObject:
    return macro_store.get_meta(name)


def _macro_runtime_options(
    payload: JsonObject,
    *,
    defaults: MacroRuntimeOptions | None = None,
) -> MacroRuntimeOptions:
    if defaults is None:
        defaults = MacroRuntimeOptions()
    return MacroRuntimeOptions(
        loop_mode=str_value(payload.get("loop_mode", defaults.loop_mode), defaults.loop_mode)
        or defaults.loop_mode,
        loop_count=int_like(payload.get("loop_count", defaults.loop_count), defaults.loop_count),
        loop_stop_behavior=normalize_macro_loop_stop_behavior(
            payload.get("loop_stop_behavior", defaults.loop_stop_behavior)
        ),
        move_to_start=bool(payload.get("move_to_start", defaults.move_to_start)),
        start_x=int_like(payload.get("start_x", defaults.start_x), defaults.start_x),
        start_y=int_like(payload.get("start_y", defaults.start_y), defaults.start_y),
        block_mouse_movement=bool(
            payload.get("block_mouse_movement", defaults.block_mouse_movement)
        ),
    )


def _macro_playback_options(
    data: JsonObject,
    macro_events: JsonObjectList,
    macro_name: str,
    *,
    stored_macro: JsonObject | None = None,
    load_stored_macro: bool = True,
) -> MacroPlaybackOptions:
    defaults = _macro_runtime_options(stored_macro) if stored_macro is not None else None
    runtime_options = _macro_runtime_options(data, defaults=defaults)
    return MacroPlaybackOptions(
        macro_events=macro_events,
        macro_name=macro_name,
        replay_mouse_movement=bool(data.get("replay_mouse_movement", True)),
        replay_mouse_clicks=bool(data.get("replay_mouse_clicks", True)),
        speed=float_like(data.get("speed", 1.0), 1.0),
        runtime_options=runtime_options,
        source_device=str_value(data.get("source_device", "")),
        source_button=str_value(data.get("source_button", "")),
        trigger_value=int_like(data.get("trigger_value", 1), 1),
        load_stored_macro=load_stored_macro,
    )


def apply_macro_definition(action_data: JsonObject, macro: JsonObject) -> JsonObject:
    updated: JsonObject = dict(action_data)
    runtime_options = _macro_runtime_options(macro)
    updated["macro_loop_mode"] = runtime_options.loop_mode
    updated["macro_loop_count"] = runtime_options.loop_count
    updated["macro_loop_stop_behavior"] = runtime_options.loop_stop_behavior
    updated["macro_move_to_start"] = runtime_options.move_to_start
    updated["macro_start_x"] = runtime_options.start_x
    updated["macro_start_y"] = runtime_options.start_y
    updated["macro_block_mouse_movement"] = runtime_options.block_mouse_movement
    return updated


def _unresolved_macro_name(action_data: JsonObject) -> str:
    if str(action_data.get("action", "") or "") != "macro":
        return ""
    macro_name = str(action_data.get("macro_name", "") or "")
    if macro_name and not action_data.get("macro_events"):
        return macro_name
    return ""


def _collect_macro_names_from_action(action_data: JsonObject, macro_names: set[str]) -> None:
    def collect(action: JsonObject) -> JsonObject:
        macro_name = _unresolved_macro_name(action)
        if macro_name:
            macro_names.add(macro_name)
        return action

    _transform_action_tree(action_data, collect)


def _resolve_action_macros(action_data: JsonObject, macros: dict[str, JsonObject]) -> JsonObject:
    def resolve(action: JsonObject) -> JsonObject:
        macro_name = _unresolved_macro_name(action)
        if macro_name not in macros:
            return action
        try:
            return apply_macro_definition(action, macros[macro_name])
        except (TypeError, ValueError):
            return action

    return _transform_action_tree(action_data, resolve)


def _transform_action_tree(action_data: JsonObject, transform: ActionTransform) -> JsonObject:
    updated = transform(dict(action_data))
    action_type = str(updated.get("action", "") or "")

    if action_type == "analog_control":
        analog_control = updated.get("analog_control")
        if isinstance(analog_control, dict):
            updated["analog_control"] = _transform_analog_control_actions(
                cast(JsonObject, analog_control),
                transform,
            )
        return updated

    if action_type != "superkey":
        return updated

    superkey = updated.get("superkey")
    if isinstance(superkey, dict):
        updated["superkey"] = _transform_superkey_actions(
            cast(JsonObject, superkey),
            transform,
        )
    return updated


def _transform_superkey_actions(superkey: JsonObject, transform: ActionTransform) -> JsonObject:
    updated: JsonObject = dict(superkey)
    for key in SUPERKEY_ACTION_KEYS:
        bundle = updated.get(key)
        if not isinstance(bundle, list):
            continue
        updated[key] = [
            (
                _transform_action_tree(cast(JsonObject, item), transform)
                if isinstance(item, dict)
                else item
            )
            for item in cast(list[object], bundle)
        ]
    return updated


def _transform_analog_control_actions(
    analog_control: JsonObject,
    transform: ActionTransform,
) -> JsonObject:
    updated: JsonObject = dict(analog_control)
    thresholds = updated.get("thresholds")
    if not isinstance(thresholds, list):
        return updated

    resolved_thresholds: list[object] = []
    for threshold in cast(list[object], thresholds):
        if not isinstance(threshold, dict):
            resolved_thresholds.append(threshold)
            continue
        threshold_data: JsonObject = dict(cast(JsonObject, threshold))
        actions = threshold_data.get("actions")
        if isinstance(actions, list):
            threshold_data["actions"] = [
                (
                    _transform_action_tree(cast(JsonObject, item), transform)
                    if isinstance(item, dict)
                    else item
                )
                for item in cast(list[object], actions)
            ]
        resolved_thresholds.append(threshold_data)
    updated["thresholds"] = resolved_thresholds
    return updated


async def _macro_action_value_resolver(
    macro_store: _MacroDefinitionStore,
    action_values: Iterable[object],
) -> Callable[[object], object]:
    macro_names: set[str] = set()
    for action_raw in action_values:
        if isinstance(action_raw, dict):
            _collect_macro_names_from_action(cast(JsonObject, action_raw), macro_names)
    macros = await load_macro_definitions(macro_store, macro_names)

    def resolve(action_raw: object) -> object:
        if not isinstance(action_raw, dict):
            return action_raw
        return _resolve_action_macros(cast(JsonObject, action_raw), macros)

    return resolve


async def resolve_mapping_macros(
    macro_store: _MacroDefinitionStore,
    mapping: JsonObject,
) -> JsonObject:
    resolve_action = await _macro_action_value_resolver(macro_store, mapping.values())
    return {button_id: resolve_action(action_data) for button_id, action_data in mapping.items()}


async def resolve_combo_macros(
    macro_store: _MacroDefinitionStore,
    combos: JsonObjectList,
) -> JsonObjectList:
    resolve_action = await _macro_action_value_resolver(
        macro_store,
        (combo.get("action") for combo in combos),
    )
    resolved: JsonObjectList = []
    for combo in combos:
        updated: JsonObject = dict(combo)
        if "action" in updated:
            updated["action"] = resolve_action(updated["action"])
        resolved.append(updated)

    return resolved
