import asyncio
import copy
import io
import logging
import re
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import tomli_w

from keymasq.common import paths
from keymasq.common.combos import normalize_combo_evdev, normalize_combo_restore_keys
from keymasq.common.models import (
    ActionType,
    ComboConfig,
    ComboEvent,
    ComboStep,
    DeviceProfileLayer,
    MappingAction,
    ProfileConfig,
    WindowRule,
    normalize_macro_loop_stop_behavior,
    parse_rapidfire_fields,
    resolve_rapidfire_fields,
)

log = logging.getLogger("keymasq-session.profiles")

MAX_PROFILE_PATH_ATTEMPTS = 10000
DEFAULT_PROFILE_NAME = "Default"
type TomlDict = dict[str, object]
type _IntLike = int | float | str | bytes
type _FloatLike = int | float | str | bytes


def _as_toml_dict(value: object) -> TomlDict | None:
    return cast(TomlDict, value) if isinstance(value, dict) else None


def _as_toml_list(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def _int_value(value: object, default: int) -> int:
    return default if value is None else int(cast(_IntLike, value))


def _float_value(value: object, default: float) -> float:
    return default if value is None else float(cast(_FloatLike, value))


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass
class ProfileInfo:
    path: Path
    config: ProfileConfig


@dataclass
class ResolvedDeviceProfile:
    hardware_id: str
    active_profile_names: list[str] = field(default_factory=list)
    mappings: dict[str, MappingAction] = field(default_factory=dict)
    always_grab_all: bool = False
    notify_profiles: list[str] = field(default_factory=list)
    combo_event_count: int = 0
    combo_sources: set[str] = field(default_factory=set)

    @property
    def mapping_count(self) -> int:
        return len(self.mappings)

    @property
    def has_effective_mapping(self) -> bool:
        return self.always_grab_all or bool(self.mappings) or self.combo_event_count > 0


@dataclass
class ResolvedCombo:
    id: str
    name: str
    steps: list[ComboStep] = field(default_factory=list)
    action: MappingAction | None = None
    profile_name: str = ""
    recall_trigger_keys: bool = False
    restore_trigger_keys: list[str] = field(default_factory=list)


@dataclass
class ResolvedProfiles:
    active_profiles: list[ProfileConfig] = field(default_factory=list)
    devices: dict[str, ResolvedDeviceProfile] = field(default_factory=dict)
    combos: list[ResolvedCombo] = field(default_factory=list)


if TYPE_CHECKING:
    from keymasq.session.superkeys import SuperkeyManager


class ProfileManager:
    def __init__(
        self,
        superkey_manager: "SuperkeyManager | None" = None,
        auto_create_default_if_empty: bool = False,
    ) -> None:
        paths.ensure_config_dirs()
        self._superkey_manager = superkey_manager
        self._auto_create_default_if_empty = auto_create_default_if_empty
        self._profiles: dict[str, ProfileInfo] = {}
        self._pending_repairs: set[asyncio.Task[None]] = set()
        self._load_all()
        self._ensure_default_profile_exists()

    def _load_all(self) -> None:
        self._profiles.clear()

        if not paths.PROFILES_DIR.exists():
            return

        for profile_file in sorted(paths.PROFILES_DIR.glob("*.toml")):
            try:
                config = self._load_profile(profile_file)
                self._profiles[config.name] = ProfileInfo(path=profile_file, config=config)
            except Exception as e:
                log.error("Failed to load %s: %s", profile_file, e)

    def reload(self) -> None:
        self._load_all()
        self._ensure_default_profile_exists()

    def _ensure_default_profile_exists(self) -> None:
        if not self._auto_create_default_if_empty or self._profiles:
            return

        config = ProfileConfig(
            name=DEFAULT_PROFILE_NAME,
            enabled=True,
            is_permanent=True,
            priority=0,
            notify_on_activation=False,
            created_at=datetime.now(),
        )
        path = paths.PROFILES_DIR / f"{self._sanitize_profile_storage_stem(config.name)}.toml"

        try:
            self._write_profile_file(
                config,
                path,
                validate_window_rules=False,
                exclusive=True,
            )
        except FileExistsError:
            self._load_all()
            return

        self._profiles[config.name] = ProfileInfo(path=path, config=config)
        log.info("Created default profile: %s", path)

    def _load_profile(self, path: Path) -> ProfileConfig:
        with open(path, "rb") as f:
            data = cast(TomlDict, tomllib.load(f))

        profile = _as_toml_dict(data.get("profile")) or {}

        window_rules = [
            WindowRule(
                field=str(rule_dict.get("field", "class")),
                pattern=str(rule_dict.get("pattern", "")),
            )
            for rule_data in _as_toml_list(profile.get("window_rules", []))
            if (rule_dict := _as_toml_dict(rule_data)) is not None
        ]

        created_at = datetime.now()
        created_at_repair_reason: str | None = None
        created_at_raw = profile.get("created_at")
        if isinstance(created_at_raw, str):
            try:
                created_at = datetime.fromisoformat(created_at_raw)
            except ValueError:
                created_at_repair_reason = f"malformed created_at '{created_at_raw}'"
        else:
            created_at_repair_reason = "missing created_at"

        device_layers: dict[str, DeviceProfileLayer] = {}
        devices_data = data.get("devices", {})
        if isinstance(devices_data, dict):
            for hardware_id, layer_data in cast(dict[object, object], devices_data).items():
                layer_dict = _as_toml_dict(layer_data)
                if layer_dict is None:
                    continue
                mappings: dict[str, MappingAction] = {}
                mapping_table = layer_dict.get("mapping", {})
                if isinstance(mapping_table, dict):
                    for button_id, action_data in cast(dict[object, object], mapping_table).items():
                        parsed_action_data = _as_toml_dict(action_data)
                        if parsed_action_data is not None:
                            mappings[str(button_id)] = self._parse_action(parsed_action_data)
                        elif isinstance(action_data, str):
                            mappings[str(button_id)] = self._parse_action(action_data)
                device_layers[str(hardware_id)] = DeviceProfileLayer(
                    hardware_id=str(hardware_id),
                    always_grab_all=bool(layer_dict.get("always_grab_all", False)),
                    mappings=mappings,
                )

        config = ProfileConfig(
            name=str(profile.get("name", path.stem)),
            enabled=bool(profile.get("enabled", True)),
            is_permanent=bool(profile.get("is_permanent", False)),
            priority=_int_value(profile.get("priority"), 0),
            notify_on_activation=bool(profile.get("notify_on_activation", True)),
            activation_macro_name=_optional_str(profile.get("activation_macro")),
            deactivation_macro_name=_optional_str(profile.get("deactivation_macro")),
            window_rules=window_rules,
            device_layers=device_layers,
            combos=self._parse_combos(data.get("combos", [])),
            image=str(profile.get("image")) if profile.get("image") is not None else None,
            created_at=created_at,
        )

        if created_at_repair_reason is not None:
            self._schedule_created_at_repair(config, path, created_at_repair_reason)

        return config

    def _schedule_created_at_repair(
        self,
        config: ProfileConfig,
        path: Path,
        reason: str,
    ) -> None:
        repair_config = copy.deepcopy(config)
        log.warning(
            "Profile %s has %s; rewriting created_at with current time",
            path,
            reason,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                self._write_profile_file(repair_config, path, validate_window_rules=False)
            except Exception as exc:
                log.error("Failed to repair created_at for %s: %s", path, exc)
            return

        task = loop.create_task(self._repair_created_at_async(repair_config, path))
        self._pending_repairs.add(task)
        task.add_done_callback(self._pending_repairs.discard)

    async def _repair_created_at_async(self, config: ProfileConfig, path: Path) -> None:
        try:
            await asyncio.to_thread(
                self._write_profile_file,
                config,
                path,
                False,
            )
        except Exception as exc:
            log.error("Failed to repair created_at for %s: %s", path, exc)

    def _parse_action(self, action_data: TomlDict | str) -> MappingAction:
        if isinstance(action_data, str):
            return MappingAction(action_type=ActionType.KEYBOARD, target=action_data)

        action_type_str = str(action_data.get("action", "passthrough"))
        if action_type_str == "rapidfire":
            action_type_str = "keyboard"
            action_data = dict(action_data)
            action_data["rapidfire_enabled"] = True
            action_data["action"] = "keyboard"

        try:
            action_type = ActionType(action_type_str)
        except ValueError:
            log.warning("Unknown action type '%s', defaulting to passthrough", action_type_str)
            action_type = ActionType.PASSTHROUGH

        if action_type == ActionType.SUPERKEY:
            superkey_name_raw = action_data.get("superkey_name")
            superkey_name = str(superkey_name_raw) if superkey_name_raw is not None else None
            if superkey_name:
                if self._superkey_manager and not self._superkey_manager.get_superkey(
                    superkey_name
                ):
                    log.warning("Unknown superkey '%s', replacing with suppress", superkey_name)
                    return MappingAction(action_type=ActionType.SUPPRESS)
                return MappingAction(
                    action_type=ActionType.SUPERKEY,
                    superkey_name=superkey_name,
                )
            log.warning("Superkey action missing superkey_name, replacing with suppress")
            return MappingAction(action_type=ActionType.SUPPRESS)

        if action_type == ActionType.MACRO:
            return MappingAction(
                action_type=ActionType.MACRO,
                macro_name=str(action_data.get("target", "")),
                macro_replay_mouse_movement=bool(action_data.get("replay_mouse_movement", True)),
                macro_replay_mouse_clicks=bool(action_data.get("replay_mouse_clicks", True)),
                macro_speed=_float_value(action_data.get("speed"), 1.0),
                macro_loop_mode=str(action_data.get("loop_mode", "none") or "none"),
                macro_loop_count=_int_value(action_data.get("loop_count"), 1),
                macro_loop_stop_behavior=normalize_macro_loop_stop_behavior(
                    action_data.get("loop_stop_behavior")
                ),
                macro_move_to_start=bool(action_data.get("move_to_start", False)),
                macro_start_x=_int_value(action_data.get("start_x"), 0),
                macro_start_y=_int_value(action_data.get("start_y"), 0),
                macro_block_mouse_movement=bool(action_data.get("block_mouse_movement", False)),
            )

        if action_type in (
            ActionType.START_MACRO_RECORDING,
            ActionType.STOP_MACRO_RECORDING,
            ActionType.CANCEL_MACRO_PLAYBACK,
            ActionType.EMERGENCY_RESET,
        ):
            return MappingAction(action_type=action_type)

        if action_type in (
            ActionType.PROFILE_ENABLE,
            ActionType.PROFILE_DISABLE,
            ActionType.PROFILE_TOGGLE,
        ):
            profile_name = str(action_data.get("profile_name", "") or "")
            if not profile_name:
                profile_name = str(action_data.get("target", "") or "")
            return MappingAction(
                action_type=action_type,
                profile_name=profile_name,
            )

        if action_type == ActionType.COMPOSITOR_DISPATCH:
            return MappingAction(
                action_type=action_type,
                compositor_id=str(action_data.get("compositor", "") or "") or None,
                compositor_dispatcher=str(action_data.get("dispatcher", "") or ""),
                compositor_args=str(action_data.get("args", "") or ""),
            )

        (
            rapidfire_enabled,
            rapidfire_hold_ms,
            rapidfire_wait_ms,
            unsupported_rapidfire,
        ) = parse_rapidfire_fields(
            action_type,
            rapidfire_enabled=action_data.get("rapidfire_enabled", False),
            rapidfire_hold_ms=action_data.get("rapidfire_hold_ms"),
            rapidfire_wait_ms=action_data.get("rapidfire_wait_ms"),
            int_value=_int_value,
        )
        if unsupported_rapidfire:
            log.warning(
                "Ignoring rapidfire for unsupported %s action in profile config",
                action_type.value,
            )

        if action_type in (ActionType.MOUSE_MOVE_REL, ActionType.MOUSE_MOVE_ABS):
            return MappingAction(
                action_type=action_type,
                move_x=_int_value(action_data.get("x"), 0),
                move_y=_int_value(action_data.get("y"), 0),
                rapidfire_enabled=rapidfire_enabled,
                rapidfire_hold_ms=rapidfire_hold_ms,
                rapidfire_wait_ms=rapidfire_wait_ms,
                tap_enabled=bool(action_data.get("tap_enabled", False)),
                tap_hold_ms=_int_value(action_data.get("tap_hold_ms"), 10),
            )

        target = action_data.get("target")
        cmd = action_data.get("cmd")
        return MappingAction(
            action_type=action_type,
            target=str(target) if target is not None else None,
            output_id=str(action_data.get("output_id", "") or "") or None,
            keys=cast(list[str] | None, action_data.get("keys")),
            cmd=str(cmd) if cmd is not None else None,
            rapidfire_enabled=rapidfire_enabled,
            rapidfire_hold_ms=rapidfire_hold_ms,
            rapidfire_wait_ms=rapidfire_wait_ms,
            tap_enabled=bool(action_data.get("tap_enabled", False)),
            tap_hold_ms=_int_value(action_data.get("tap_hold_ms"), 10),
        )

    def _serialize_action(self, action: MappingAction) -> dict[str, object]:
        action_data: dict[str, object] = {"action": action.action_type.value}
        if action.target:
            action_data["target"] = action.target
        if action.action_type == ActionType.GAMEPAD and action.output_id:
            action_data["output_id"] = action.output_id
        if action.keys:
            action_data["keys"] = action.keys
        if action.cmd:
            action_data["cmd"] = action.cmd
        if action.superkey_name:
            action_data["superkey_name"] = action.superkey_name
        if action.action_type == ActionType.MACRO:
            action_data["target"] = action.macro_name or ""
            action_data["replay_mouse_movement"] = action.macro_replay_mouse_movement
            action_data["replay_mouse_clicks"] = action.macro_replay_mouse_clicks
            action_data["speed"] = action.macro_speed
            action_data["loop_mode"] = action.macro_loop_mode
            action_data["loop_count"] = int(action.macro_loop_count)
            action_data["loop_stop_behavior"] = action.macro_loop_stop_behavior
            action_data["move_to_start"] = bool(action.macro_move_to_start)
            action_data["start_x"] = int(action.macro_start_x)
            action_data["start_y"] = int(action.macro_start_y)
            action_data["block_mouse_movement"] = bool(action.macro_block_mouse_movement)
        if action.action_type in (ActionType.MOUSE_MOVE_REL, ActionType.MOUSE_MOVE_ABS):
            action_data["x"] = int(action.move_x)
            action_data["y"] = int(action.move_y)
        if action.action_type in (
            ActionType.PROFILE_ENABLE,
            ActionType.PROFILE_DISABLE,
            ActionType.PROFILE_TOGGLE,
        ):
            action_data["target"] = action.profile_name or ""
            action_data["profile_name"] = action.profile_name or ""
        if action.action_type == ActionType.COMPOSITOR_DISPATCH:
            if action.compositor_id:
                action_data["compositor"] = action.compositor_id
            action_data["dispatcher"] = action.compositor_dispatcher or ""
            action_data["args"] = action.compositor_args or ""
        (
            rapidfire_enabled,
            rapidfire_hold_ms,
            rapidfire_wait_ms,
            unsupported_rapidfire,
        ) = resolve_rapidfire_fields(
            action.action_type,
            rapidfire_enabled=bool(action.rapidfire_enabled),
            rapidfire_hold_ms=int(action.rapidfire_hold_ms),
            rapidfire_wait_ms=int(action.rapidfire_wait_ms),
        )
        if unsupported_rapidfire:
            log.warning(
                "Dropping rapidfire for unsupported %s action while saving profile config",
                action.action_type.value,
            )
        if rapidfire_enabled:
            action_data["rapidfire_enabled"] = True
            action_data["rapidfire_hold_ms"] = rapidfire_hold_ms
            action_data["rapidfire_wait_ms"] = rapidfire_wait_ms
        if action.tap_enabled:
            action_data["tap_enabled"] = True
            action_data["tap_hold_ms"] = action.tap_hold_ms
        return action_data

    def _parse_combos(self, combos_data: object) -> list[ComboConfig]:
        if not isinstance(combos_data, list):
            return []

        combos: list[ComboConfig] = []
        for combo_data in cast(list[object], combos_data):
            combo_dict = _as_toml_dict(combo_data)
            if combo_dict is None:
                continue
            steps_data = combo_dict.get("steps", [])
            steps: list[ComboStep] = []
            if isinstance(steps_data, list):
                for step_data in cast(list[object], steps_data):
                    step_dict = _as_toml_dict(step_data)
                    if step_dict is None:
                        continue
                    events_data = step_dict.get("events", [])
                    events: list[ComboEvent] = []
                    if isinstance(events_data, list):
                        for event_data in cast(list[object], events_data):
                            event_dict = _as_toml_dict(event_data)
                            if event_dict is None:
                                continue
                            evdev = str(event_dict.get("evdev", "") or "")
                            hardware_id = str(event_dict.get("hardware_id", "") or "")
                            if not evdev or not hardware_id:
                                continue
                            source_raw = event_dict.get("source")
                            source = str(source_raw) if source_raw is not None else None
                            events.append(
                                ComboEvent(
                                    evdev=normalize_combo_evdev(evdev),
                                    hardware_id=hardware_id,
                                    source=source,
                                )
                            )
                    if events:
                        timeout_raw = step_dict.get("timeout_ms")
                        timeout_ms = _int_value(timeout_raw, 0) if timeout_raw is not None else None
                        steps.append(ComboStep(events=events, timeout_ms=timeout_ms))

            action_data = combo_dict.get("action")
            action_dict = _as_toml_dict(action_data)
            action = (
                self._parse_action(action_data)
                if isinstance(action_data, str)
                else self._parse_action(action_dict)
                if action_dict is not None
                else None
            )
            combo_id = str(combo_dict.get("id", "") or "")
            if not combo_id:
                continue
            combos.append(
                ComboConfig(
                    id=combo_id,
                    name=str(combo_dict.get("name", "") or ""),
                    steps=steps,
                    action=action,
                    recall_trigger_keys=bool(combo_dict.get("recall_trigger_keys", False)),
                    restore_trigger_keys=normalize_combo_restore_keys(
                        _as_toml_list(combo_dict.get("restore_trigger_keys", []))
                    ),
                )
            )
        return combos

    def list_profiles(self) -> list[ProfileInfo]:
        return list(self._profiles.values())

    def get_all_profiles(self) -> dict[str, ProfileInfo]:
        return self._profiles.copy()

    def get_profile(self, profile_name: str) -> ProfileInfo | None:
        return self._profiles.get(profile_name)

    def get_next_priority(self) -> int:
        if not self._profiles:
            return 0
        return max(info.config.priority for info in self._profiles.values()) + 1

    def _sanitize_profile_storage_stem(self, profile_name: str) -> str:
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", profile_name).strip("._")
        return safe_name or "profile"

    def _profile_path_for_name(
        self,
        profile_name: str,
        current_path: Path | None = None,
    ) -> Path:
        base_stem = self._sanitize_profile_storage_stem(profile_name)
        candidate = paths.PROFILES_DIR / f"{base_stem}.toml"
        suffix = 2

        occupied_paths = {
            info.path
            for info in self._profiles.values()
            if current_path is None or info.path != current_path
        }

        attempts = 0
        while candidate in occupied_paths or (candidate.exists() and candidate != current_path):
            attempts += 1
            if attempts >= MAX_PROFILE_PATH_ATTEMPTS:
                raise RuntimeError(f"Unable to allocate profile storage path for '{profile_name}'")
            candidate = paths.PROFILES_DIR / f"{base_stem}_{suffix}.toml"
            suffix += 1

        return candidate

    def set_profile_enabled(self, profile_name: str, enabled: bool | None) -> ProfileConfig | None:
        profile = self.get_profile(profile_name)
        if profile is None:
            return None

        target_enabled = (not profile.config.enabled) if enabled is None else bool(enabled)
        if profile.config.enabled == target_enabled:
            return profile.config

        profile.config.enabled = target_enabled
        self.save_profile(profile.config)
        return profile.config

    def has_unsupported_rules(self, config: ProfileConfig, capabilities: list[str]) -> bool:
        has_tag_support = "window_tags" in capabilities
        for rule in config.window_rules:
            if rule.field == "tag" and not has_tag_support:
                return True
        return False

    def validate_window_rules(self, window_rules: list[WindowRule]) -> None:
        for index, rule in enumerate(window_rules, start=1):
            try:
                re.compile(rule.pattern)
            except re.error as exc:
                raise ValueError(
                    f"Invalid regex in window rule {index} for field '{rule.field}': {exc}"
                ) from exc

    def _matches_window_rules(self, profile: ProfileConfig, window_info: TomlDict | None) -> bool:
        if not profile.window_rules:
            return False

        if not window_info:
            return False

        for rule in profile.window_rules:
            try:
                if rule.field == "tag":
                    window_tags = window_info.get("tags", [])
                    if not isinstance(window_tags, list):
                        window_tags = []
                    tags = cast(list[object], window_tags)
                    if not any(re.search(rule.pattern, str(tag)) for tag in tags):
                        return False
                else:
                    field_value = window_info.get(rule.field, "")
                    if not field_value:
                        return False
                    if not re.search(rule.pattern, cast(str, field_value)):
                        return False
            except re.error as exc:
                log.warning(
                    "Invalid window rule regex for profile '%s' field '%s': %s",
                    profile.name,
                    rule.field,
                    exc,
                )
                return False

        return True

    def resolve_active_profiles(
        self,
        window_info: TomlDict | None = None,
        capabilities: list[str] | None = None,
        hardware_ids: list[str] | None = None,
    ) -> ResolvedProfiles:
        capabilities = capabilities or []
        known_hardware_ids = set(hardware_ids or [])
        active_profiles: list[ProfileConfig] = []

        for info in self.list_profiles():
            profile = info.config
            if not profile.enabled:
                continue
            if self.has_unsupported_rules(profile, capabilities):
                continue
            if profile.is_permanent or self._matches_window_rules(profile, window_info):
                active_profiles.append(profile)
                known_hardware_ids.update(profile.device_layers.keys())

        active_profiles.sort(
            key=lambda p: (
                0 if p.is_permanent else 1,
                p.priority,
                p.created_at or datetime.min,
                p.name.casefold(),
            )
        )

        devices: dict[str, ResolvedDeviceProfile] = {}
        combos_by_signature: dict[
            tuple[tuple[tuple[str, str, str], ...], ...],
            ResolvedCombo,
        ] = {}
        for hardware_id in sorted(known_hardware_ids):
            resolved = ResolvedDeviceProfile(hardware_id=hardware_id)
            for profile in active_profiles:
                layer = profile.get_layer(hardware_id)
                if layer is None:
                    continue
                resolved.active_profile_names.append(profile.name)
                resolved.always_grab_all = resolved.always_grab_all or layer.always_grab_all
                if profile.notify_on_activation:
                    resolved.notify_profiles.append(profile.name)
                for button_id, action in layer.mappings.items():
                    if action.action_type == ActionType.PASSTHROUGH:
                        resolved.mappings.pop(button_id, None)
                    else:
                        resolved.mappings[button_id] = copy.deepcopy(action)
            devices[hardware_id] = resolved

        for profile in active_profiles:
            for combo in profile.combos:
                if combo.action is None or not combo.steps:
                    continue
                normalized_steps: list[tuple[tuple[str, str, str], ...]] = []
                combo_steps: list[ComboStep] = []
                for step in combo.steps:
                    if not step.events:
                        normalized_steps = []
                        break
                    normalized_events = sorted(
                        (
                            event.hardware_id,
                            event.source or "",
                            normalize_combo_evdev(event.evdev),
                        )
                        for event in step.events
                        if event.hardware_id and event.evdev
                    )
                    if not normalized_events:
                        normalized_steps = []
                        break
                    normalized_steps.append(tuple(normalized_events))
                    combo_steps.append(copy.deepcopy(step))
                    for hardware_id, _source, _evdev in normalized_events:
                        resolved = devices.setdefault(
                            hardware_id,
                            ResolvedDeviceProfile(hardware_id=hardware_id),
                        )
                        if profile.name not in resolved.active_profile_names:
                            resolved.active_profile_names.append(profile.name)
                        resolved.combo_event_count += 1
                        if (
                            profile.notify_on_activation
                            and profile.name not in resolved.notify_profiles
                        ):
                            resolved.notify_profiles.append(profile.name)
                        if _source:
                            resolved.combo_sources.add(_source)
                if not normalized_steps:
                    continue
                signature = tuple(normalized_steps)
                if signature in combos_by_signature:
                    combos_by_signature.pop(signature, None)
                combos_by_signature[signature] = ResolvedCombo(
                    id=combo.id,
                    name=combo.name,
                    steps=combo_steps,
                    action=copy.deepcopy(combo.action),
                    profile_name=profile.name,
                    recall_trigger_keys=bool(combo.recall_trigger_keys),
                    restore_trigger_keys=normalize_combo_restore_keys(
                        copy.deepcopy(combo.restore_trigger_keys)
                    ),
                )

        return ResolvedProfiles(
            active_profiles=active_profiles,
            devices=devices,
            combos=list(combos_by_signature.values()),
        )

    def save_profile(self, config: ProfileConfig, path: Path | None = None) -> None:
        paths.ensure_config_dirs()
        self.validate_window_rules(config.window_rules)
        if config.created_at is None:
            config.created_at = datetime.now()

        profile_name = config.name
        current_path = path
        existing_profile = self._profiles.get(profile_name)
        if existing_profile is not None:
            if current_path is None:
                if existing_profile.config is not config:
                    raise ValueError(f"Profile '{profile_name}' already exists")
                current_path = existing_profile.path
            elif existing_profile.path != current_path:
                raise ValueError(f"Profile '{profile_name}' already exists")
        path = self._profile_path_for_name(profile_name, current_path=current_path)

        self._write_profile_file(config, path, validate_window_rules=False)

        self._profiles[config.name] = ProfileInfo(path=path, config=config)

        log.info("Saved profile: %s", path)

    def _write_profile_file(
        self,
        config: ProfileConfig,
        path: Path,
        validate_window_rules: bool = True,
        exclusive: bool = False,
    ) -> None:
        if validate_window_rules:
            self.validate_window_rules(config.window_rules)

        profile_data: dict[str, object] = {
            "name": config.name,
            "enabled": config.enabled,
            "is_permanent": config.is_permanent,
            "priority": config.priority,
            "notify_on_activation": config.notify_on_activation,
            "created_at": (config.created_at or datetime.now()).isoformat(),
        }

        if config.activation_macro_name:
            profile_data["activation_macro"] = config.activation_macro_name
        if config.deactivation_macro_name:
            profile_data["deactivation_macro"] = config.deactivation_macro_name

        if config.window_rules:
            profile_data["window_rules"] = [
                {"field": rule.field, "pattern": rule.pattern} for rule in config.window_rules
            ]

        if config.image:
            profile_data["image"] = config.image

        devices_data: dict[str, dict[str, object]] = {}
        for hardware_id in sorted(config.device_layers):
            layer = config.device_layers[hardware_id]
            action_map: dict[str, dict[str, object]] = {}
            for button_id, action in layer.mappings.items():
                action_map[button_id] = self._serialize_action(action)

            devices_data[hardware_id] = {
                "always_grab_all": layer.always_grab_all,
                "mapping": action_map,
            }

        data: dict[str, object] = {
            "profile": profile_data,
            "devices": devices_data,
        }
        if config.combos:
            data["combos"] = [
                {
                    "id": combo.id,
                    "name": combo.name,
                    "steps": [
                        {
                            **(
                                {"timeout_ms": int(step.timeout_ms)}
                                if step.timeout_ms is not None
                                else {}
                            ),
                            "events": [
                                {
                                    "evdev": event.evdev,
                                    "hardware_id": event.hardware_id,
                                    **({"source": event.source} if event.source else {}),
                                }
                                for event in step.events
                            ],
                        }
                        for step in combo.steps
                    ],
                    **(
                        {"action": self._serialize_action(combo.action)}
                        if combo.action is not None
                        else {}
                    ),
                    **({"recall_trigger_keys": True} if combo.recall_trigger_keys else {}),
                    **(
                        {
                            "restore_trigger_keys": normalize_combo_restore_keys(
                                combo.restore_trigger_keys
                            )
                        }
                        if combo.restore_trigger_keys
                        else {}
                    ),
                }
                for combo in config.combos
            ]

        if exclusive:
            buffer = io.BytesIO()
            tomli_w.dump(data, buffer)
            with open(path, "xb") as f:
                f.write(buffer.getvalue())
            return

        with open(path, "wb") as f:
            tomli_w.dump(data, f)

    def delete_profile(self, name: str) -> bool:
        profile = self._profiles.get(name)
        if profile is None:
            return False

        if profile.path.exists():
            self._trash_profile_file(profile.path)
        self._profiles.pop(name, None)
        log.info("Deleted profile: %s", name)
        return True

    def _trash_profile_file(self, path: Path) -> None:
        trash_dir = paths.CONFIG_DIR / "trash" / "profiles"
        trash_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        trashed_path = trash_dir / f"{timestamp}_{path.name}"
        try:
            path.rename(trashed_path)
            log.warning("Moved deleted profile to trash: %s", trashed_path)
        except Exception:
            path.unlink()

    def rename_profile(self, old_name: str, new_name: str) -> ProfileInfo:
        if new_name in self._profiles and new_name != old_name:
            raise ValueError(f"Profile '{new_name}' already exists")

        profile = self._profiles.get(old_name)
        if profile is None:
            raise ValueError(f"Profile '{old_name}' not found")

        old_path = profile.path
        profile.config.name = new_name
        self._profiles.pop(old_name, None)
        self.save_profile(profile.config, path=old_path)
        renamed_profile = self._profiles[new_name]
        new_path = renamed_profile.path

        if old_path != new_path and old_path.exists():
            try:
                old_path.unlink()
            except OSError:
                pass

        log.info("Renamed profile: %s -> %s", old_name, new_name)
        return renamed_profile

    def find_profiles_using_superkey(self, superkey_name: str) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        for info in self.list_profiles():
            for hardware_id, layer in info.config.device_layers.items():
                for action in layer.mappings.values():
                    if (
                        action.action_type == ActionType.SUPERKEY
                        and action.superkey_name == superkey_name
                    ):
                        result.append((hardware_id, info.config.name))
                        break
            for combo in info.config.combos:
                action = combo.action
                if (
                    action is not None
                    and action.action_type == ActionType.SUPERKEY
                    and action.superkey_name == superkey_name
                ):
                    result.append(("combo", info.config.name))
                    break
        return result

    def replace_superkey_with_suppress(self, superkey_name: str) -> int:
        count = 0
        for info in self.list_profiles():
            modified = False
            for layer in info.config.device_layers.values():
                for button_id, action in list(layer.mappings.items()):
                    if (
                        action.action_type == ActionType.SUPERKEY
                        and action.superkey_name == superkey_name
                    ):
                        layer.mappings[button_id] = MappingAction(action_type=ActionType.SUPPRESS)
                        modified = True
                        count += 1
            for combo in info.config.combos:
                action = combo.action
                if (
                    action is not None
                    and action.action_type == ActionType.SUPERKEY
                    and action.superkey_name == superkey_name
                ):
                    combo.action = MappingAction(action_type=ActionType.SUPPRESS)
                    modified = True
                    count += 1
            if modified:
                self.save_profile(info.config)
        if count > 0:
            log.info("Replaced superkey '%s' with suppress in %d references", superkey_name, count)
        return count

    def remove_device_layers(self, hardware_id: str) -> int:
        updated = 0
        for info in self.list_profiles():
            if hardware_id not in info.config.device_layers:
                continue
            info.config.device_layers.pop(hardware_id, None)
            self.save_profile(info.config)
            updated += 1
        return updated

    def remove_device_button_mappings(self, hardware_id: str, button_id: str) -> int:
        updated = 0
        for info in self.list_profiles():
            layer = info.config.get_layer(hardware_id)
            if layer is None or button_id not in layer.mappings:
                continue
            layer.mappings.pop(button_id, None)
            self.save_profile(info.config)
            updated += 1
        return updated
