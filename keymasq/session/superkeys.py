import logging
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

import tomli_w

from keymasq.common import paths
from keymasq.common.config_files import write_config_atomically
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import (
    ActionType,
    SuperkeyMode,
)
from keymasq.common.model.superkeys import (
    SuperkeyAction,
    SuperkeyConfig,
    mapping_action_to_superkey_action,
    superkey_action_to_mapping_action,
)
from keymasq.session.action_toml import (
    UnknownActionTypeError,
    mapping_action_from_toml,
    mapping_action_to_toml,
    mapping_action_type_from_toml,
)
from keymasq.session.config_loading import ConfigLoadError, ConfigLoadFailure

log = logging.getLogger("keymasq-session.superkeys")
type TomlDict = dict[str, object]


@dataclass(frozen=True)
class SuperkeySnapshot:
    superkeys: dict[str, SuperkeyConfig]
    paths: dict[str, Path]


def _as_toml_dict(value: object) -> TomlDict | None:
    return cast(TomlDict, value) if isinstance(value, dict) else None


def _toml_str(data: TomlDict, key: str, default: str | None = None) -> str | None:
    value = data.get(key, default)
    return value if isinstance(value, str) else default


def _toml_int(data: TomlDict, key: str, default: int) -> int:
    value = data.get(key, default)
    return value if isinstance(value, int) else default


def _parse_superkey_mode(value: object) -> SuperkeyMode:
    if not isinstance(value, str):
        raise ValueError("superkey mode must be set to 'pattern' or 'overload'")
    try:
        return SuperkeyMode(value)
    except ValueError as exc:
        raise ValueError(f"unknown superkey mode '{value}'") from exc


def _parse_action_bundle[ActionT](
    data: object,
    *,
    bundle_error: str,
    item_error: str,
    parse_item: Callable[[TomlDict], ActionT],
) -> list[ActionT]:
    if data is None:
        return []
    if not isinstance(data, list):
        raise ValueError(bundle_error)

    actions: list[ActionT] = []
    for item in cast(list[object], data):
        action_data = _as_toml_dict(item)
        if action_data is None:
            raise ValueError(item_error)
        actions.append(parse_item(action_data))
    return actions


class SuperkeyManager:
    def __init__(self) -> None:
        paths.ensure_config_dirs()
        self._superkeys: dict[str, SuperkeyConfig] = {}
        self._superkey_paths: dict[str, Path] = {}
        self._load_all()

    def _load_all(self, *, strict: bool = False) -> None:
        loaded_superkeys: dict[str, SuperkeyConfig] = {}
        loaded_paths: dict[str, Path] = {}
        failures: list[ConfigLoadFailure] = []

        if not paths.SUPERKEYS_DIR.exists():
            self._superkeys = loaded_superkeys
            self._superkey_paths = loaded_paths
            return

        for superkey_file in paths.SUPERKEYS_DIR.glob("*.toml"):
            try:
                config = self._load_superkey(superkey_file)
                if config:
                    loaded_superkeys[config.name] = config
                    loaded_paths[config.name] = superkey_file
            except tomllib.TOMLDecodeError as exc:
                log.error("Failed to load superkey %s: %s", superkey_file, exc)
                failures.append(ConfigLoadFailure(superkey_file, str(exc)))
            except (OSError, ValueError, KeyError) as exc:
                log.error("Failed to load superkey %s: %s", superkey_file, exc)
                failures.append(ConfigLoadFailure(superkey_file, str(exc)))
            except Exception as exc:
                log.exception("Unexpected error while loading superkey %s", superkey_file)
                failures.append(
                    ConfigLoadFailure(
                        superkey_file,
                        str(exc) or type(exc).__name__,
                    )
                )

        if strict and failures:
            raise ConfigLoadError("superkey", failures)

        self._superkeys = loaded_superkeys
        self._superkey_paths = loaded_paths

    def _load_superkey(self, path: Path) -> SuperkeyConfig | None:
        with open(path, "rb") as f:
            data = cast(TomlDict, tomllib.load(f))

        name = _toml_str(data, "name", path.stem) or path.stem
        timing = _as_toml_dict(data.get("timing")) or {}
        actions_data = _as_toml_dict(data.get("actions")) or {}

        tap_actions = self._parse_superkey_action_bundle(actions_data.get("tap"))
        double_tap_actions = self._parse_superkey_action_bundle(actions_data.get("double_tap"))
        hold_actions = self._parse_superkey_action_bundle(actions_data.get("hold"))
        tap_hold_actions = self._parse_superkey_action_bundle(actions_data.get("tap_hold"))
        overload_actions = self._parse_overload_action_bundle(actions_data.get("overload"))
        overload_down_actions = self._parse_overload_action_bundle(
            actions_data.get("overload_down")
        )
        overload_up_actions = self._parse_overload_action_bundle(actions_data.get("overload_up"))

        mode = _parse_superkey_mode(data.get("mode"))

        config = SuperkeyConfig(
            name=name,
            description=_toml_str(data, "description"),
            mode=mode,
            tap_actions=tap_actions,
            double_tap_actions=double_tap_actions,
            hold_actions=hold_actions,
            tap_hold_actions=tap_hold_actions,
            overload_actions=overload_actions,
            overload_down_actions=overload_down_actions,
            overload_up_actions=overload_up_actions,
            tap_timeout_ms=_toml_int(timing, "tap_timeout_ms", 200),
            double_tap_window_ms=_toml_int(timing, "double_tap_window_ms", 300),
            hold_threshold_ms=_toml_int(timing, "hold_threshold_ms", 300),
        )
        self._validate_before_save(config)
        return config

    def _parse_superkey_action_bundle(self, data: object) -> list[SuperkeyAction]:
        return _parse_action_bundle(
            data,
            bundle_error="pattern action bundles must be TOML arrays",
            item_error="pattern action bundle items must be TOML tables",
            parse_item=self._parse_superkey_action,
        )

    def _parse_superkey_action(self, data: TomlDict | None) -> SuperkeyAction:
        if not data:
            raise ValueError("pattern action must be a TOML table")
        action_type_str = _toml_str(data, "action", "passthrough") or "passthrough"
        try:
            action = self._parse_mapping_action(data)
        except UnknownActionTypeError as exc:
            raise ValueError(f"unknown pattern superkey action type '{action_type_str}'") from exc
        except ValueError:
            raise
        try:
            return mapping_action_to_superkey_action(action)
        except ValueError as exc:
            raise ValueError(f"invalid pattern superkey action type '{action_type_str}'") from exc

    def _parse_overload_action_bundle(self, data: object) -> list[MappingAction]:
        return _parse_action_bundle(
            data,
            bundle_error="overload actions must be a TOML array",
            item_error="overload action items must be TOML tables",
            parse_item=self._parse_mapping_action,
        )

    def _parse_mapping_action(self, action_data: TomlDict) -> MappingAction:
        action_type, normalized_action_data = mapping_action_type_from_toml(
            action_data,
            unknown_action="raise",
        )
        if action_type == ActionType.SUPERKEY:
            raise ValueError("nested superkeys are not allowed inside superkeys")

        return mapping_action_from_toml(
            normalized_action_data,
            action_type,
            logger=log,
            rapidfire_warning_context="superkey config",
            preparse_rapidfire_for_special_actions=True,
        )

    def _validate_overload_action(self, action: MappingAction) -> None:
        if action.action_type == ActionType.SUPERKEY:
            raise ValueError("nested superkeys are not allowed inside superkeys")
        if action.action_type == ActionType.PASSTHROUGH:
            raise ValueError("passthrough is not allowed inside overload superkeys")
        if action.action_type == ActionType.REPEAT:
            raise ValueError("repeat is not allowed inside overload superkeys")

    def get_superkey(self, name: str) -> SuperkeyConfig | None:
        return self._superkeys.get(name)

    def list_superkeys(self) -> list[str]:
        return sorted(self._superkeys.keys())

    def get_all_superkeys(self) -> dict[str, SuperkeyConfig]:
        return self._superkeys.copy()

    def snapshot_superkeys(self) -> SuperkeySnapshot:
        return SuperkeySnapshot(
            superkeys=self._superkeys.copy(),
            paths=self._superkey_paths.copy(),
        )

    def restore_superkeys(
        self,
        snapshot: SuperkeySnapshot | dict[str, SuperkeyConfig],
    ) -> None:
        if isinstance(snapshot, SuperkeySnapshot):
            self._superkeys = snapshot.superkeys.copy()
            self._superkey_paths = snapshot.paths.copy()
            return

        self._superkeys = snapshot.copy()
        self._superkey_paths = {
            name: self._superkey_paths.get(name, self._path_for_name(config.name))
            for name, config in self._superkeys.items()
        }

    def save_superkey(self, config: SuperkeyConfig, *, replacing_name: str | None = None) -> None:
        paths.ensure_config_dirs()
        self._validate_before_save(config)
        if (
            replacing_name is not None
            and replacing_name != config.name
            and config.name in self._superkeys
        ):
            raise ValueError(f"Superkey '{config.name}' already exists")

        tracked_name = replacing_name or config.name
        path = self._superkey_paths.get(tracked_name, self._path_for_name(config.name))
        self._ensure_storage_path_available(config.name, path, replacing_name=replacing_name)
        canonical_path = self._path_for_name(config.name)
        if path != canonical_path:
            self._ensure_storage_path_available(
                config.name,
                canonical_path,
                replacing_name=replacing_name,
            )

        data: dict[str, object] = {
            "name": config.name,
            "mode": config.mode.value,
        }
        if config.description:
            data["description"] = config.description

        timing: dict[str, object] = {}
        if config.tap_timeout_ms != 200:
            timing["tap_timeout_ms"] = config.tap_timeout_ms
        if config.double_tap_window_ms != 300:
            timing["double_tap_window_ms"] = config.double_tap_window_ms
        if config.hold_threshold_ms != 300:
            timing["hold_threshold_ms"] = config.hold_threshold_ms
        if timing:
            data["timing"] = timing

        actions: dict[str, object] = {}
        if config.mode == SuperkeyMode.PATTERN:
            if config.tap_actions:
                actions["tap"] = [
                    self._serialize_pattern_action(action) for action in config.tap_actions
                ]
            if config.double_tap_actions:
                actions["double_tap"] = [
                    self._serialize_pattern_action(action) for action in config.double_tap_actions
                ]
            if config.hold_actions:
                actions["hold"] = [
                    self._serialize_pattern_action(action) for action in config.hold_actions
                ]
            if config.tap_hold_actions:
                actions["tap_hold"] = [
                    self._serialize_pattern_action(action) for action in config.tap_hold_actions
                ]
        else:
            if config.overload_actions:
                actions["overload"] = [
                    self._serialize_mapping_action(action) for action in config.overload_actions
                ]
            if config.overload_down_actions:
                actions["overload_down"] = [
                    self._serialize_mapping_action(action)
                    for action in config.overload_down_actions
                ]
            if config.overload_up_actions:
                actions["overload_up"] = [
                    self._serialize_mapping_action(action) for action in config.overload_up_actions
                ]
        if actions:
            data["actions"] = actions

        def write_config(config_file: BinaryIO) -> None:
            tomli_w.dump(data, config_file)

        write_config_atomically(path, write_config)

        if replacing_name is not None and replacing_name != config.name:
            self._superkeys.pop(replacing_name, None)
            self._superkey_paths.pop(replacing_name, None)
        self._superkeys[config.name] = config
        self._superkey_paths[config.name] = path
        log.info("Saved superkey: %s", config.name)

    def _validate_before_save(self, config: SuperkeyConfig) -> None:
        if config.mode == SuperkeyMode.OVERLOAD:
            if config.has_pattern_actions():
                raise ValueError("overload superkeys cannot define pattern slots")
            for action in config.overload_actions:
                self._validate_overload_action(action)
            for action in config.overload_down_actions:
                self._validate_overload_action(action)
            for action in config.overload_up_actions:
                self._validate_overload_action(action)
            return

        if config.has_overload_actions():
            raise ValueError("pattern superkeys cannot define overload actions")
        for actions in (
            config.tap_actions,
            config.double_tap_actions,
            config.hold_actions,
            config.tap_hold_actions,
        ):
            for action in actions:
                if not action.is_valid():
                    raise ValueError(
                        f"invalid pattern superkey action type: {action.action_type.value}"
                    )

    def _serialize_pattern_action(self, action: SuperkeyAction) -> TomlDict:
        return self._serialize_mapping_action(superkey_action_to_mapping_action(action))

    def _serialize_mapping_action(self, action: MappingAction) -> TomlDict:
        return mapping_action_to_toml(
            action,
            logger=log,
            rapidfire_warning_context="superkey config",
        )

    def delete_superkey(self, name: str) -> bool:
        if name not in self._superkeys:
            return False

        config = self._superkeys[name]
        path = self._superkey_paths.get(name, self._path_for_name(config.name))

        if path.exists():
            path.unlink()

        del self._superkeys[name]
        self._superkey_paths.pop(name, None)
        log.info("Deleted superkey: %s", name)
        return True

    def rename_superkey(self, old_name: str, new_name: str) -> bool:
        if old_name not in self._superkeys:
            return False

        if old_name == new_name:
            return True

        if new_name in self._superkeys and new_name != old_name:
            log.warning("Superkey '%s' already exists", new_name)
            return False

        config = self._superkeys[old_name]
        old_path = self._superkey_paths.get(old_name, self._path_for_name(old_name))
        new_path = self._path_for_name(new_name)
        self._ensure_storage_path_available(new_name, new_path, replacing_name=old_name)

        config.name = new_name
        try:
            self.save_superkey(config, replacing_name=old_name)
        except Exception:
            config.name = old_name
            raise

        saved_path = self._superkey_paths.get(new_name, new_path)
        if old_path != saved_path and old_path.exists():
            old_path.unlink()

        self._superkeys.pop(old_name, None)
        self._superkey_paths.pop(old_name, None)

        log.info("Renamed superkey: %s -> %s", old_name, new_name)
        return True

    def _sanitize_name(self, name: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        return safe.lower()

    def _path_for_name(self, name: str) -> Path:
        return paths.SUPERKEYS_DIR / f"{self._sanitize_name(name)}.toml"

    def _storage_file_name(self, path: Path) -> str | None:
        if not path.exists():
            return None
        try:
            with open(path, "rb") as f:
                data = cast(TomlDict, tomllib.load(f))
            return _toml_str(data, "name", path.stem) or path.stem
        except Exception as exc:
            raise ValueError(
                f"Superkey storage path '{path.name}' already exists but could not be read"
            ) from exc

    def _ensure_storage_path_available(
        self,
        name: str,
        path: Path,
        *,
        replacing_name: str | None = None,
    ) -> None:
        stored_name = self._storage_file_name(path)
        if stored_name is None or stored_name == name or stored_name == replacing_name:
            return
        raise ValueError(f"Superkey name '{name}' conflicts with existing superkey '{stored_name}'")

    def reload(self) -> None:
        self._load_all(strict=True)
        log.info("Reloaded all superkeys")
