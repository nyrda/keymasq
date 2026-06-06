import asyncio
import copy
import io
import logging
import re
import threading
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Concatenate, cast

from keymasq.common import paths
from keymasq.common.coercion import int_value as _int_value
from keymasq.common.coercion import optional_str as _optional_str
from keymasq.common.combos import normalize_combo_evdev, normalize_combo_restore_keys
from keymasq.common.config_files import write_toml_atomically
from keymasq.common.models import (
    ActionType,
    ComboConfig,
    ComboEvent,
    ComboStep,
    DeviceProfileLayer,
    MappingAction,
    ProfileConfig,
    WindowRule,
)
from keymasq.session.action_toml import (
    mapping_action_from_toml,
    mapping_action_to_toml,
    mapping_action_type_from_toml,
)
from keymasq.session.config_loading import load_config_files_sync

log = logging.getLogger("keymasq-session.profiles")

MAX_PROFILE_PATH_ATTEMPTS = 10000
DEFAULT_PROFILE_NAME = "Default"
type TomlDict = dict[str, object]
type ComboEventSignature = tuple[str, str, str]
type ComboStepSignature = tuple[ComboEventSignature, ...]
type ComboSignature = tuple[ComboStepSignature, ...]


def _as_toml_dict(value: object) -> TomlDict | None:
    return cast(TomlDict, value) if isinstance(value, dict) else None


def _as_toml_list(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


@dataclass
class ProfileInfo:
    path: Path
    config: ProfileConfig


@dataclass
class ResolvedDeviceProfile:
    hardware_id: str
    active_profile_names: list[str] = field(default_factory=list)
    mappings: dict[str, MappingAction] = field(default_factory=dict)
    mapping_profile_names: dict[str, str] = field(default_factory=dict)
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
    match_across_devices: bool = False


@dataclass
class ResolvedProfiles:
    active_profiles: list[ProfileConfig] = field(default_factory=list)
    devices: dict[str, ResolvedDeviceProfile] = field(default_factory=dict)
    combos: list[ResolvedCombo] = field(default_factory=list)


if TYPE_CHECKING:
    from keymasq.session.analog_controls import AnalogControlManager
    from keymasq.session.superkeys import SuperkeyManager


class ProfileManager:
    @staticmethod
    def _with_profile_file_lock[**P, R](
        method: Callable[Concatenate["ProfileManager", P], R],
    ) -> Callable[Concatenate["ProfileManager", P], R]:
        @wraps(method)
        def wrapper(self: "ProfileManager", *args: P.args, **kwargs: P.kwargs) -> R:
            with self._profile_file_lock:
                return method(self, *args, **kwargs)

        return wrapper

    def __init__(
        self,
        superkey_manager: "SuperkeyManager | None" = None,
        analog_control_manager: "AnalogControlManager | None" = None,
        auto_create_default_if_empty: bool = False,
    ) -> None:
        paths.ensure_config_dirs()
        self._superkey_manager = superkey_manager
        self._analog_control_manager = analog_control_manager
        self._auto_create_default_if_empty = auto_create_default_if_empty
        self._profiles: dict[str, ProfileInfo] = {}
        self._pending_repairs: set[asyncio.Task[None]] = set()
        self._profile_file_lock = threading.RLock()
        self._load_all()
        self._ensure_default_profile_exists()

    def _load_all(self, *, strict: bool = False) -> None:
        loaded_profiles: dict[str, ProfileInfo] = {}
        for profile_file, config in load_config_files_sync(
            paths.PROFILES_DIR,
            config_kind="profile",
            strict=strict,
            load_config=self._load_profile,
            logger=log,
            sort_paths=True,
        ):
            self._add_loaded_profile(
                ProfileInfo(path=profile_file, config=config),
                loaded_profiles,
            )

        self._profiles = loaded_profiles

    def _add_loaded_profile(
        self,
        profile: ProfileInfo,
        profiles: dict[str, ProfileInfo] | None = None,
    ) -> None:
        target_profiles = self._profiles if profiles is None else profiles
        existing = target_profiles.get(profile.config.name)
        if existing is None:
            target_profiles[profile.config.name] = profile
            return

        selected = self._select_duplicate_profile(existing, profile)
        ignored = profile if selected is existing else existing
        target_profiles[profile.config.name] = selected
        log.warning(
            "Ignoring duplicate profile name '%s' from %s; using %s",
            profile.config.name,
            ignored.path,
            selected.path,
        )

    def _select_duplicate_profile(
        self,
        first: ProfileInfo,
        second: ProfileInfo,
    ) -> ProfileInfo:
        first_is_canonical = self._is_canonical_profile_storage_path(first.config.name, first.path)
        second_is_canonical = self._is_canonical_profile_storage_path(
            second.config.name,
            second.path,
        )
        if first_is_canonical and not second_is_canonical:
            return first
        if second_is_canonical and not first_is_canonical:
            return second
        return first

    @_with_profile_file_lock
    def reload(self) -> None:
        self._load_all(strict=True)
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
        created_at = config.created_at or datetime.now()
        log.warning(
            "Profile %s has %s; repairing created_at with current time",
            path,
            reason,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                self._repair_created_at_if_needed(created_at, path)
            except (OSError, tomllib.TOMLDecodeError) as exc:
                log.error("Failed to repair created_at for %s: %s", path, exc)
            except Exception:
                log.exception("Unexpected failure repairing created_at for %s", path)
            return

        task = loop.create_task(self._repair_created_at_async(created_at, path))
        self._pending_repairs.add(task)
        task.add_done_callback(self._pending_repairs.discard)

    async def _repair_created_at_async(self, created_at: datetime, path: Path) -> None:
        try:
            await asyncio.to_thread(
                self._repair_created_at_if_needed,
                created_at,
                path,
            )
        except (OSError, tomllib.TOMLDecodeError) as exc:
            log.error("Failed to repair created_at for %s: %s", path, exc)
        except Exception:
            log.exception("Unexpected failure repairing created_at for %s", path)

    def _repair_created_at_if_needed(self, created_at: datetime, path: Path) -> None:
        with self._profile_file_lock:
            data = cast(TomlDict, tomllib.load(io.BytesIO(path.read_bytes())))
            profile = _as_toml_dict(data.get("profile"))
            if profile is None:
                profile = {}
                data["profile"] = profile

            current_created_at = profile.get("created_at")
            if isinstance(current_created_at, str):
                try:
                    datetime.fromisoformat(current_created_at)
                except ValueError:
                    pass
                else:
                    return

            profile["created_at"] = created_at.isoformat()
            write_toml_atomically(path, data)

    def _parse_action(self, action_data: TomlDict | str) -> MappingAction:
        if isinstance(action_data, str):
            return MappingAction(action_type=ActionType.KEYBOARD, target=action_data)

        action_type, normalized_action_data = mapping_action_type_from_toml(
            action_data,
            unknown_action="passthrough",
            logger=log,
        )

        if action_type == ActionType.SUPERKEY:
            superkey_name_raw = normalized_action_data.get("superkey_name")
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

        if action_type == ActionType.ANALOG_CONTROL:
            raw_names = normalized_action_data.get("analog_control_names")
            if isinstance(raw_names, list):
                raw_analog_control_names = cast(list[object], raw_names)
            else:
                raw_name: object = normalized_action_data.get("analog_control_name")
                raw_analog_control_names = [raw_name] if raw_name is not None else []
            analog_control_names: list[str] = []
            for raw_name in raw_analog_control_names:
                name = str(raw_name).strip()
                if name:
                    analog_control_names.append(name)
            if analog_control_names:
                if (
                    self._analog_control_manager
                    and any(
                        self._analog_control_manager.get_analog_control(name) is None
                        for name in analog_control_names
                    )
                ):
                    missing = next(
                        name
                        for name in analog_control_names
                        if self._analog_control_manager.get_analog_control(name) is None
                    )
                    log.warning("Unknown analog control '%s', replacing with suppress", missing)
                    return MappingAction(action_type=ActionType.SUPPRESS)
                return MappingAction(
                    action_type=ActionType.ANALOG_CONTROL,
                    analog_control_names=analog_control_names,
                )
            log.warning(
                "Analog control action missing analog_control_names, replacing with suppress"
            )
            return MappingAction(action_type=ActionType.SUPPRESS)

        return mapping_action_from_toml(
            normalized_action_data,
            action_type,
            logger=log,
            rapidfire_warning_context="profile config",
        )

    def _serialize_action(self, action: MappingAction) -> dict[str, object]:
        return mapping_action_to_toml(
            action,
            include_profile_refs=True,
            logger=log,
            rapidfire_warning_context="profile config",
        )

    def _parse_combos(self, combos_data: object) -> list[ComboConfig]:
        if not isinstance(combos_data, list):
            return []

        combos: list[ComboConfig] = []
        for combo_data in cast(list[object], combos_data):
            combo = self._parse_combo(combo_data)
            if combo is not None:
                combos.append(combo)
        return combos

    def _parse_combo(self, combo_data: object) -> ComboConfig | None:
        combo_dict = _as_toml_dict(combo_data)
        if combo_dict is None:
            return None

        steps = self._parse_combo_steps(combo_dict.get("steps", []))
        action = self._parse_combo_action(combo_dict.get("action"))
        combo_id = str(combo_dict.get("id", "") or "")
        if not combo_id:
            return None

        return ComboConfig(
            id=combo_id,
            name=str(combo_dict.get("name", "") or ""),
            steps=steps,
            action=action,
            recall_trigger_keys=bool(combo_dict.get("recall_trigger_keys", False)),
            restore_trigger_keys=normalize_combo_restore_keys(
                _as_toml_list(combo_dict.get("restore_trigger_keys", []))
            ),
            match_across_devices=bool(combo_dict.get("match_across_devices", False)),
        )

    def _parse_combo_steps(self, steps_data: object) -> list[ComboStep]:
        if not isinstance(steps_data, list):
            return []

        steps: list[ComboStep] = []
        for step_data in cast(list[object], steps_data):
            step = self._parse_combo_step(step_data)
            if step is not None:
                steps.append(step)
        return steps

    def _parse_combo_step(self, step_data: object) -> ComboStep | None:
        step_dict = _as_toml_dict(step_data)
        if step_dict is None:
            return None

        events = self._parse_combo_events(step_dict.get("events", []))
        if not events:
            return None

        timeout_raw = step_dict.get("timeout_ms")
        timeout_ms = _int_value(timeout_raw, 0) if timeout_raw is not None else None
        return ComboStep(events=events, timeout_ms=timeout_ms)

    def _parse_combo_events(self, events_data: object) -> list[ComboEvent]:
        if not isinstance(events_data, list):
            return []

        events: list[ComboEvent] = []
        for event_data in cast(list[object], events_data):
            event = self._parse_combo_event(event_data)
            if event is not None:
                events.append(event)
        return events

    def _parse_combo_event(self, event_data: object) -> ComboEvent | None:
        event_dict = _as_toml_dict(event_data)
        if event_dict is None:
            return None

        evdev = str(event_dict.get("evdev", "") or "")
        if not evdev:
            return None

        source_raw = event_dict.get("source")
        source = str(source_raw) if source_raw is not None else None
        return ComboEvent(
            evdev=normalize_combo_evdev(evdev),
            hardware_id=str(event_dict.get("hardware_id", "") or ""),
            source=source,
        )

    def _parse_combo_action(self, action_data: object) -> MappingAction | None:
        action_dict = _as_toml_dict(action_data)
        if isinstance(action_data, str):
            return self._parse_action(action_data)
        if action_dict is not None:
            return self._parse_action(action_dict)
        return None

    @_with_profile_file_lock
    def list_profiles(self) -> list[ProfileInfo]:
        return list(self._profiles.values())

    @_with_profile_file_lock
    def snapshot_profiles(self) -> dict[str, ProfileInfo]:
        return self._profiles.copy()

    @_with_profile_file_lock
    def restore_profiles(self, profiles: dict[str, ProfileInfo]) -> None:
        self._profiles = profiles.copy()

    @_with_profile_file_lock
    def get_profile(self, profile_name: str) -> ProfileInfo | None:
        return self._profiles.get(profile_name)

    @_with_profile_file_lock
    def get_next_priority(self) -> int:
        if not self._profiles:
            return 0
        return max(info.config.priority for info in self._profiles.values()) + 1

    def _sanitize_profile_storage_stem(self, profile_name: str) -> str:
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", profile_name).strip("._")
        return safe_name or "profile"

    def _canonical_profile_storage_path(self, profile_name: str) -> Path:
        return paths.PROFILES_DIR / f"{self._sanitize_profile_storage_stem(profile_name)}.toml"

    def _is_canonical_profile_storage_path(self, profile_name: str, path: Path) -> bool:
        return path == self._canonical_profile_storage_path(profile_name)

    def _profile_path_for_name(
        self,
        profile_name: str,
        current_path: Path | None = None,
    ) -> Path:
        base_stem = self._sanitize_profile_storage_stem(profile_name)
        candidate = self._canonical_profile_storage_path(profile_name)
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

    @_with_profile_file_lock
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

    @_with_profile_file_lock
    def resolve_active_profiles(
        self,
        window_info: TomlDict | None = None,
        capabilities: list[str] | None = None,
        hardware_ids: list[str] | None = None,
        runtime_profile_names: list[str] | None = None,
    ) -> ResolvedProfiles:
        capabilities = capabilities or []
        known_hardware_ids = set(hardware_ids or [])
        active_profiles: list[ProfileConfig] = []
        runtime_names = [
            str(name).strip()
            for name in (runtime_profile_names or [])
            if str(name).strip()
        ]

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

        runtime_profiles: list[ProfileConfig] = []
        seen_runtime_names: set[str] = set()
        for name in runtime_names:
            if name in seen_runtime_names:
                continue
            seen_runtime_names.add(name)
            info = self.get_profile(name)
            if info is not None:
                runtime_profiles.append(info.config)
        if runtime_profiles:
            runtime_name_set = {profile.name for profile in runtime_profiles}
            active_profiles = [
                profile for profile in active_profiles if profile.name not in runtime_name_set
            ]
            active_profiles.extend(runtime_profiles)

        for profile in active_profiles:
            known_hardware_ids.update(profile.device_layers.keys())

        devices: dict[str, ResolvedDeviceProfile] = {}
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
                        resolved.mapping_profile_names.pop(button_id, None)
                    else:
                        copied_action = copy.deepcopy(action)
                        copied_action.source_profile_name = profile.name
                        resolved.mappings[button_id] = copied_action
                        resolved.mapping_profile_names[button_id] = profile.name
            devices[hardware_id] = resolved

        combos = self._resolve_profile_combos(active_profiles, devices)

        return ResolvedProfiles(
            active_profiles=active_profiles,
            devices=devices,
            combos=combos,
        )

    def _resolve_profile_combos(
        self,
        active_profiles: list[ProfileConfig],
        devices: dict[str, ResolvedDeviceProfile],
    ) -> list[ResolvedCombo]:
        combos_by_signature: dict[ComboSignature, ResolvedCombo] = {}
        for profile in active_profiles:
            for combo in profile.combos:
                action = combo.action
                if action is None or not combo.steps:
                    continue
                normalized = self._normalize_combo_steps(combo)
                if normalized is None:
                    continue
                signature, combo_steps = normalized
                self._mark_combo_devices(devices, profile, signature)
                if signature in combos_by_signature:
                    combos_by_signature.pop(signature, None)
                combo_action = copy.deepcopy(action)
                combo_action.source_profile_name = profile.name
                combos_by_signature[signature] = ResolvedCombo(
                    id=combo.id,
                    name=combo.name,
                    steps=combo_steps,
                    action=combo_action,
                    profile_name=profile.name,
                    recall_trigger_keys=bool(combo.recall_trigger_keys),
                    restore_trigger_keys=normalize_combo_restore_keys(
                        copy.deepcopy(combo.restore_trigger_keys)
                    ),
                    match_across_devices=bool(combo.match_across_devices),
                )
        return list(combos_by_signature.values())

    @staticmethod
    def _normalize_combo_steps(combo: ComboConfig) -> tuple[ComboSignature, list[ComboStep]] | None:
        normalized_steps: list[ComboStepSignature] = []
        combo_steps: list[ComboStep] = []
        for step in combo.steps:
            if not step.events:
                return None
            effective_step = copy.deepcopy(step)
            if combo.match_across_devices:
                for event in effective_step.events:
                    event.hardware_id = ""
                    event.source = None
            normalized_events: ComboStepSignature = tuple(
                sorted(
                    (
                        event.hardware_id or "",
                        event.source or "",
                        normalize_combo_evdev(event.evdev),
                    )
                    for event in effective_step.events
                    if event.evdev
                )
            )
            if not normalized_events:
                return None
            normalized_steps.append(normalized_events)
            combo_steps.append(effective_step)
        return tuple(normalized_steps), combo_steps

    @staticmethod
    def _mark_combo_devices(
        devices: dict[str, ResolvedDeviceProfile],
        profile: ProfileConfig,
        signature: ComboSignature,
    ) -> None:
        for normalized_events in signature:
            for hardware_id, source, _evdev in normalized_events:
                if not hardware_id:
                    continue
                resolved = devices.setdefault(
                    hardware_id,
                    ResolvedDeviceProfile(hardware_id=hardware_id),
                )
                if profile.name not in resolved.active_profile_names:
                    resolved.active_profile_names.append(profile.name)
                resolved.combo_event_count += 1
                if profile.notify_on_activation and profile.name not in resolved.notify_profiles:
                    resolved.notify_profiles.append(profile.name)
                if source:
                    resolved.combo_sources.add(source)

    @_with_profile_file_lock
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
                path = existing_profile.path
            elif existing_profile.path != current_path:
                raise ValueError(f"Profile '{profile_name}' already exists")
            else:
                path = self._profile_path_for_name(profile_name, current_path=current_path)
        else:
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
                                    **(
                                        {"hardware_id": event.hardware_id}
                                        if event.hardware_id
                                        else {}
                                    ),
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
                    **(
                        {"match_across_devices": True}
                        if combo.match_across_devices
                        else {}
                    ),
                }
                for combo in config.combos
            ]

        with self._profile_file_lock:
            write_toml_atomically(path, data, overwrite=not exclusive)
            return

    @_with_profile_file_lock
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
        except OSError as exc:
            log.warning(
                "Failed to move deleted profile to trash %s; deleting permanently: %s",
                path,
                exc,
            )
            try:
                path.unlink()
            except OSError as unlink_exc:
                log.warning(
                    "Failed to delete profile file %s after trash move failed: %s",
                    path,
                    unlink_exc,
                )
                raise

    @_with_profile_file_lock
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

    @_with_profile_file_lock
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

    @_with_profile_file_lock
    def find_profiles_using_analog_control(
        self,
        analog_control_name: str,
    ) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        for info in self.list_profiles():
            for hardware_id, layer in info.config.device_layers.items():
                for action in layer.mappings.values():
                    if (
                        action.action_type == ActionType.ANALOG_CONTROL
                        and analog_control_name in action.analog_control_names
                    ):
                        result.append((hardware_id, info.config.name))
                        break
        return result

    @_with_profile_file_lock
    def replace_analog_control_with_suppress(self, analog_control_name: str) -> int:
        count = 0
        for info in self.list_profiles():
            modified = False
            for layer in info.config.device_layers.values():
                for button_id, action in list(layer.mappings.items()):
                    if (
                        action.action_type == ActionType.ANALOG_CONTROL
                        and analog_control_name in action.analog_control_names
                    ):
                        names = [
                            name
                            for name in action.analog_control_names
                            if name != analog_control_name
                        ]
                        layer.mappings[button_id] = (
                            MappingAction(
                                action_type=ActionType.ANALOG_CONTROL,
                                analog_control_names=names,
                            )
                            if names
                            else MappingAction(action_type=ActionType.SUPPRESS)
                        )
                        modified = True
                        count += 1
            if modified:
                self.save_profile(info.config)
        if count > 0:
            log.info(
                "Replaced analog control '%s' with suppress in %d references",
                analog_control_name,
                count,
            )
        return count

    @_with_profile_file_lock
    def rename_analog_control_references(self, old_name: str, new_name: str) -> int:
        if old_name == new_name:
            return 0
        count = 0
        for info in self.list_profiles():
            modified = False
            for layer in info.config.device_layers.values():
                for action in layer.mappings.values():
                    if (
                        action.action_type == ActionType.ANALOG_CONTROL
                        and old_name in action.analog_control_names
                    ):
                        action.analog_control_names = [
                            new_name if name == old_name else name
                            for name in action.analog_control_names
                        ]
                        action.analog_control_name = action.analog_control_names[0]
                        modified = True
                        count += 1
            if modified:
                self.save_profile(info.config)
        if count > 0:
            log.info(
                "Renamed analog control references '%s' -> '%s' in %d mappings",
                old_name,
                new_name,
                count,
            )
        return count

    @_with_profile_file_lock
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
            log.info(
                "Replaced superkey '%s' with suppress in %d references",
                superkey_name,
                count,
            )
        return count

    @_with_profile_file_lock
    def remove_device_layers(self, hardware_id: str) -> int:
        updated = 0
        for info in self.list_profiles():
            if hardware_id not in info.config.device_layers:
                continue
            info.config.device_layers.pop(hardware_id, None)
            self.save_profile(info.config)
            updated += 1
        return updated

    @_with_profile_file_lock
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
