import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Protocol, cast

from keymasq.common.coercion import coerce_bool, coerce_int, coerce_str
from keymasq.common.ipc import CommandType
from keymasq.common.models import normalize_macro_recording_slot
from keymasq.common.types import JsonObject, JsonObjectList
from keymasq.keymasqd.runtime import macros as runtime_macros

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
log = logging.getLogger("keymasqd.macros")


class _MacroCommandDeviceManager(Protocol):
    async def play_macro(
        self,
        playback_options: runtime_macros.MacroPlaybackOptions,
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
        name = coerce_str(data.get("name", ""))
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
        name = coerce_str(data.get("name", ""))
        raw_payload = data.get("macro", {})
        if not isinstance(raw_payload, dict):
            raise ValueError("macro payload must be an object")
        expected_revision = data.get("expected_revision")
        revision = (
            coerce_int(expected_revision, 0) if expected_revision is not None else None
        )
        macro = await asyncio.to_thread(
            daemon.macro_store.update,
            name,
            cast(JsonObject, raw_payload),
            revision,
        )
        return {"macro": macro}

    if command_type == CommandType.MACRO_RENAME:
        old_name = coerce_str(data.get("old_name", ""))
        new_name = coerce_str(data.get("new_name", ""))
        expected_revision = data.get("expected_revision")
        revision = (
            coerce_int(expected_revision, 0) if expected_revision is not None else None
        )
        macro = await asyncio.to_thread(daemon.macro_store.rename, old_name, new_name, revision)
        return {"macro": macro}

    if command_type == CommandType.MACRO_DELETE:
        name = coerce_str(data.get("name", ""))
        expected_revision = data.get("expected_revision")
        revision = (
            coerce_int(expected_revision, 0) if expected_revision is not None else None
        )
        await asyncio.to_thread(daemon.macro_store.delete, name, revision)
        return {"status": "ok"}

    if command_type == CommandType.MACRO_SAVE_RECORDING:
        return await save_pending_recording(daemon, data)

    if command_type == CommandType.MACRO_LIST_RECORDINGS:
        recordings = await daemon.recording_manager.list_pending_recordings()
        return {"recordings": recordings}

    if command_type == CommandType.MACRO_DELETE_RECORDING:
        recording_id = coerce_str(data.get("pending_recording_id", ""))
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
        wait_id = coerce_str(data.get("wait_id", ""))
        returncode = coerce_int(data.get("returncode", 0), 0)
        return daemon.device_manager.complete_macro_exec_wait(wait_id, returncode)

    return None


async def save_pending_recording(
    daemon: _MacroCommandDaemon,
    data: JsonObject,
) -> JsonObject:
    recording_id = coerce_str(data.get("pending_recording_id", ""))
    if not recording_id:
        raise ValueError("pending_recording_id required")
    if not coerce_str(data.get("name", "")):
        raise ValueError("name required")
    snapshot = cast(
        _PendingRecording,
        await daemon.recording_manager.claim_pending_recording(recording_id),
    )
    save_succeeded = False
    try:
        result = await asyncio.to_thread(_save_pending_recording_sync, daemon, data, snapshot)
        save_succeeded = True
        return result
    finally:
        is_slot_backed = bool(
            normalize_macro_recording_slot(getattr(snapshot, "recording_slot", 0))
        )
        await daemon.recording_manager.release_pending_recording_claim(
            recording_id,
            saved=save_succeeded and not is_slot_backed,
        )


def _save_pending_recording_sync(
    daemon: _MacroCommandDaemon,
    data: JsonObject,
    snapshot: _PendingRecording,
) -> JsonObject:
    name = coerce_str(data.get("name", ""))
    if not name:
        raise ValueError("name required")

    payload: JsonObject = {
        "name": name,
        "created_at": datetime.now().isoformat(),
        "duration_us": int(snapshot.duration_ms) * 1000,
        "device_types": list(snapshot.device_types),
        "event_count": int(snapshot.event_count),
        "block_mouse_movement": coerce_bool(data.get("block_mouse_movement"), False),
    }
    macro = daemon.macro_store.create_from_events(
        payload,
        snapshot.iter_events(),
        return_full=False,
    )
    return {"macro": macro}


async def play_macro_from_payload(daemon: _MacroCommandDaemon, data: JsonObject) -> JsonObject:
    macro_events = cast(JsonObjectList, data.get("macro_events", []))
    macro_name = coerce_str(data.get("macro_name", ""))
    stored_macro: JsonObject | None = None

    if macro_name and not macro_events:
        stored_macro = await asyncio.to_thread(
            _load_macro_meta_sync,
            daemon.macro_store,
            macro_name,
        )

    return await daemon.device_manager.play_macro(
        _macro_playback_options(data, macro_events, macro_name, stored_macro=stored_macro),
    )


async def play_macro_by_name(daemon: _MacroCommandDaemon, data: JsonObject) -> JsonObject:
    name = coerce_str(data.get("name", ""))
    stored_macro = await asyncio.to_thread(_load_macro_meta_sync, daemon.macro_store, name)
    return await daemon.device_manager.play_macro(
        _macro_playback_options(data, [], name, stored_macro=stored_macro),
    )


async def play_pending_recording(
    daemon: _MacroCommandDaemon,
    data: JsonObject,
) -> JsonObject:
    recording_id = coerce_str(data.get("pending_recording_id", ""))
    if not recording_id:
        raise ValueError("pending_recording_id required")
    snapshot = cast(
        _PendingRecording,
        await daemon.recording_manager.claim_pending_recording(recording_id),
    )
    try:
        macro_events = await asyncio.to_thread(lambda: list(snapshot.iter_events()))
        macro_name = coerce_str(data.get("macro_name", ""))
        if not macro_name:
            macro_name = recording_id
        return await daemon.device_manager.play_macro(
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


async def load_macro_definitions(
    macro_store: _MacroDefinitionStore,
    macro_names: set[str],
) -> dict[str, JsonObject]:
    if not macro_names:
        return {}

    async def load_macro(name: str) -> tuple[str, JsonObject | None]:
        try:
            macro = await asyncio.to_thread(_load_macro_meta_sync, macro_store, name)
        except FileNotFoundError as exc:
            log.warning("Macro definition %s is referenced but not installed: %s", name, exc)
            return name, None
        except PermissionError as exc:
            log.error(
                "Could not load macro definition %s: %s. Check macro file ownership and "
                "permissions for the keymasqd user.",
                name,
                exc,
            )
            return name, None
        except OSError as exc:
            log.error(
                "Could not load macro definition %s due to a filesystem error: %s. Check "
                "the macro directory and file permissions for the keymasqd user.",
                name,
                exc,
            )
            return name, None
        except Exception:
            log.exception("Could not load macro definition %s", name)
            return name, None
        return name, macro

    loaded = await asyncio.gather(*(load_macro(name) for name in sorted(macro_names)))
    return {name: macro for name, macro in loaded if isinstance(macro, dict)}


def _load_macro_meta_sync(macro_store: _MacroDefinitionStore, name: str) -> JsonObject:
    return macro_store.get_meta(name)


def _macro_runtime_options(
    payload: JsonObject,
    *,
    defaults: JsonObject | None = None,
    lenient: bool = True,
) -> JsonObject:
    return runtime_macros.macro_runtime_options(
        payload,
        defaults=defaults,
        lenient=lenient,
    )


def _macro_playback_options(
    data: JsonObject,
    macro_events: JsonObjectList,
    macro_name: str,
    *,
    stored_macro: JsonObject | None = None,
    load_stored_macro: bool = True,
) -> runtime_macros.MacroPlaybackOptions:
    defaults = _macro_runtime_options(stored_macro) if stored_macro is not None else None
    return runtime_macros.macro_playback_options_from_mapping(
        data,
        defaults=defaults,
        macro_events=macro_events,
        macro_name=macro_name,
        load_stored_macro=load_stored_macro,
    )


def apply_macro_definition(action_data: JsonObject, macro: JsonObject) -> JsonObject:
    updated: JsonObject = dict(action_data)
    runtime_options = _macro_runtime_options(macro, lenient=False)
    for option_name, value in runtime_options.items():
        updated[f"macro_{option_name}"] = value
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
