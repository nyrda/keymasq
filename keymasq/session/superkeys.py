import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

import tomli_w

from keymasq.common import paths
from keymasq.common.gamepad_axes import gamepad_axis_max_value
from keymasq.common.models import (
    ActionType,
    MappingAction,
    SuperkeyAction,
    SuperkeyConfig,
    SuperkeyMode,
    mapping_action_to_superkey_action,
    normalize_macro_loop_stop_behavior,
    normalize_macro_recording_slot,
    normalize_profile_deactivation_policy,
    parse_profile_deactivation_policy,
    parse_rapidfire_fields,
    profile_deactivation_policy_to_dict,
    resolve_rapidfire_fields,
    superkey_action_to_mapping_action,
)
from keymasq.session.config_files import write_config_atomically
from keymasq.session.config_loading import ConfigLoadError, ConfigLoadFailure

log = logging.getLogger("keymasq-session.superkeys")
type TomlDict = dict[str, object]
type _IntLike = int | float | str | bytes
type _FloatLike = int | float | str | bytes


@dataclass(frozen=True)
class SuperkeySnapshot:
    superkeys: dict[str, SuperkeyConfig]
    paths: dict[str, Path]


class UnknownActionTypeError(ValueError):
    pass


def _as_toml_dict(value: object) -> TomlDict | None:
    return cast(TomlDict, value) if isinstance(value, dict) else None


def _toml_str(data: TomlDict, key: str, default: str | None = None) -> str | None:
    value = data.get(key, default)
    return value if isinstance(value, str) else default


def _toml_int(data: TomlDict, key: str, default: int) -> int:
    value = data.get(key, default)
    return value if isinstance(value, int) else default


def _int_value(value: object, default: int) -> int:
    return default if value is None else int(cast(_IntLike, value))


def _float_value(value: object, default: float) -> float:
    return default if value is None else float(cast(_FloatLike, value))


def _parse_superkey_mode(value: object) -> SuperkeyMode:
    if not isinstance(value, str):
        raise ValueError("superkey mode must be set to 'pattern' or 'overload'")
    try:
        return SuperkeyMode(value)
    except ValueError as exc:
        raise ValueError(f"unknown superkey mode '{value}'") from exc


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
            except Exception as e:
                log.error(f"Failed to load superkey {superkey_file}: {e}")
                failures.append(ConfigLoadFailure(superkey_file, str(e)))

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
        if data is None:
            return []
        if not isinstance(data, list):
            raise ValueError("pattern action bundles must be TOML arrays")

        actions: list[SuperkeyAction] = []
        for item in cast(list[object], data):
            action_data = _as_toml_dict(item)
            if action_data is None:
                raise ValueError("pattern action bundle items must be TOML tables")
            actions.append(self._parse_superkey_action(action_data))
        return actions

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
        if data is None:
            return []
        if not isinstance(data, list):
            raise ValueError("overload actions must be a TOML array")

        actions: list[MappingAction] = []
        for item in cast(list[object], data):
            action_data = _as_toml_dict(item)
            if action_data is None:
                raise ValueError("overload action items must be TOML tables")
            actions.append(self._parse_mapping_action(action_data))
        return actions

    def _parse_mapping_action(self, action_data: TomlDict) -> MappingAction:
        action_type_str = str(action_data.get("action", "passthrough"))
        if action_type_str == "rapidfire":
            action_type_str = "keyboard"
            action_data = dict(action_data)
            action_data["rapidfire_enabled"] = True
            action_data["action"] = "keyboard"

        try:
            action_type = ActionType(action_type_str)
        except ValueError as exc:
            raise UnknownActionTypeError(f"unknown action type '{action_type_str}'") from exc

        if action_type == ActionType.SUPERKEY:
            raise ValueError("nested superkeys are not allowed inside superkeys")

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
                "Ignoring rapidfire for unsupported %s action in superkey config",
                action_type.value,
            )

        if action_type == ActionType.MACRO:
            return MappingAction(
                action_type=ActionType.MACRO,
                macro_name=str(action_data.get("target", "") or "")
                or str(action_data.get("macro_name", "") or ""),
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
            ActionType.PLAY_MACRO_SLOT,
            ActionType.CANCEL_MACRO_PLAYBACK,
            ActionType.EMERGENCY_RESET,
        ):
            if action_type in (
                ActionType.START_MACRO_RECORDING,
                ActionType.STOP_MACRO_RECORDING,
                ActionType.PLAY_MACRO_SLOT,
            ):
                return MappingAction(
                    action_type=action_type,
                    macro_recording_slot=normalize_macro_recording_slot(
                        action_data.get("recording_slot", action_data.get("slot"))
                    ),
                )
            return MappingAction(action_type=action_type)

        if action_type in (
            ActionType.PROFILE_ENABLE,
            ActionType.PROFILE_DISABLE,
            ActionType.PROFILE_TOGGLE,
        ):
            profile_name = str(action_data.get("profile_name", "") or "")
            if not profile_name:
                profile_name = str(action_data.get("target", "") or "")
            deactivation = normalize_profile_deactivation_policy(
                action_type,
                parse_profile_deactivation_policy(action_data.get("deactivation")),
            )
            return MappingAction(
                action_type=action_type,
                profile_name=profile_name,
                profile_deactivation=deactivation,
            )

        if action_type == ActionType.COMPOSITOR_DISPATCH:
            return MappingAction(
                action_type=action_type,
                compositor_id=str(action_data.get("compositor", "") or "") or None,
                compositor_dispatcher=str(action_data.get("dispatcher", "") or ""),
                compositor_args=str(action_data.get("args", "") or ""),
            )

        if action_type == ActionType.REPEAT:
            return MappingAction(
                action_type=action_type,
                repeat_categories=cast(list[str] | None, action_data.get("repeat_categories")),
                rapidfire_enabled=rapidfire_enabled,
                rapidfire_hold_ms=rapidfire_hold_ms,
                rapidfire_wait_ms=rapidfire_wait_ms,
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
        axis_value = 0
        if action_type == ActionType.GAMEPAD_AXIS:
            axis_value = _int_value(
                action_data.get("value"),
                gamepad_axis_max_value(target),
            )
        cmd = action_data.get("cmd")
        return MappingAction(
            action_type=action_type,
            target=str(target) if target is not None else None,
            output_id=str(action_data.get("output_id", "") or "") or None,
            keys=cast(list[str] | None, action_data.get("keys")),
            cmd=str(cmd) if cmd is not None else None,
            axis_value=axis_value,
            rapidfire_enabled=rapidfire_enabled,
            rapidfire_hold_ms=rapidfire_hold_ms,
            rapidfire_wait_ms=rapidfire_wait_ms,
            tap_enabled=bool(action_data.get("tap_enabled", False)),
            tap_hold_ms=_int_value(action_data.get("tap_hold_ms"), 10),
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
        action_data: dict[str, object] = {"action": action.action_type.value}
        if action.target:
            action_data["target"] = action.target
        if action.action_type in (ActionType.GAMEPAD, ActionType.GAMEPAD_AXIS) and action.output_id:
            action_data["output_id"] = action.output_id
        if action.keys:
            action_data["keys"] = action.keys
        if action.cmd:
            action_data["cmd"] = action.cmd
        if action.action_type == ActionType.MACRO:
            action_data["target"] = action.macro_name or ""
            action_data["macro_name"] = action.macro_name or ""
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
        if action.action_type in (
            ActionType.START_MACRO_RECORDING,
            ActionType.STOP_MACRO_RECORDING,
            ActionType.PLAY_MACRO_SLOT,
        ) and action.macro_recording_slot:
            action_data["recording_slot"] = int(action.macro_recording_slot)
        if action.action_type in (ActionType.MOUSE_MOVE_REL, ActionType.MOUSE_MOVE_ABS):
            action_data["x"] = int(action.move_x)
            action_data["y"] = int(action.move_y)
        if action.action_type == ActionType.GAMEPAD_AXIS:
            action_data["value"] = int(action.axis_value)
        if action.action_type in (
            ActionType.PROFILE_ENABLE,
            ActionType.PROFILE_DISABLE,
            ActionType.PROFILE_TOGGLE,
        ):
            action_data["target"] = action.profile_name or ""
            action_data["profile_name"] = action.profile_name or ""
            deactivation = normalize_profile_deactivation_policy(
                action.action_type,
                action.profile_deactivation,
            )
            deactivation_data = profile_deactivation_policy_to_dict(deactivation)
            if deactivation_data is not None:
                action_data["deactivation"] = deactivation_data
        if action.action_type == ActionType.COMPOSITOR_DISPATCH:
            if action.compositor_id:
                action_data["compositor"] = action.compositor_id
            action_data["dispatcher"] = action.compositor_dispatcher or ""
            action_data["args"] = action.compositor_args or ""
        if action.action_type == ActionType.REPEAT:
            action_data["repeat_categories"] = list(action.repeat_categories or [])
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
                "Dropping rapidfire for unsupported %s action while saving superkey config",
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
        raise ValueError(
            f"Superkey name '{name}' conflicts with existing superkey '{stored_name}'"
        )

    def reload(self) -> None:
        self._load_all(strict=True)
        log.info("Reloaded all superkeys")
