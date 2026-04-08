import logging
import tomllib
from pathlib import Path
from typing import cast

import tomli_w

from keyforge.common import paths
from keyforge.common.models import (
    ActionType,
    MappingAction,
    SuperkeyAction,
    SuperkeyConfig,
    SuperkeyMode,
)

log = logging.getLogger("keyforge-session.superkeys")
type TomlDict = dict[str, object]
type _IntLike = int | float | str | bytes
type _FloatLike = int | float | str | bytes


def _as_toml_dict(value: object) -> TomlDict | None:
    return cast(TomlDict, value) if isinstance(value, dict) else None


def _toml_str(data: TomlDict, key: str, default: str | None = None) -> str | None:
    value = data.get(key, default)
    return value if isinstance(value, str) else default


def _toml_bool(data: TomlDict, key: str, default: bool) -> bool:
    value = data.get(key, default)
    return value if isinstance(value, bool) else default


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
        self._load_all()

    def _load_all(self) -> None:
        self._superkeys.clear()

        if not paths.SUPERKEYS_DIR.exists():
            return

        for superkey_file in paths.SUPERKEYS_DIR.glob("*.toml"):
            try:
                config = self._load_superkey(superkey_file)
                if config:
                    self._superkeys[config.name] = config
            except Exception as e:
                log.error(f"Failed to load superkey {superkey_file}: {e}")

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
            action_type = ActionType(action_type_str)
        except ValueError as exc:
            raise ValueError(f"unknown pattern superkey action type '{action_type_str}'") from exc

        if action_type not in (
            ActionType.KEYBOARD,
            ActionType.MOUSE,
            ActionType.GAMEPAD,
            ActionType.EXEC,
            ActionType.MACRO,
        ):
            raise ValueError(f"invalid pattern superkey action type '{action_type_str}'")

        return SuperkeyAction(
            action_type=action_type,
            target=_toml_str(data, "target"),
            cmd=_toml_str(data, "cmd"),
            macro_name=(
                _toml_str(data, "macro_name")
                or (_toml_str(data, "target") if action_type == ActionType.MACRO else None)
            ),
            rapidfire_enabled=_toml_bool(data, "rapidfire_enabled", False),
            rapidfire_hold_ms=_toml_int(data, "rapidfire_hold_ms", 20),
            rapidfire_wait_ms=_toml_int(data, "rapidfire_wait_ms", 20),
        )

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
        if action_type_str == "hyprland_dispatch":
            action_data = dict(action_data)
            action_data.setdefault("compositor", "hyprland")
            action_type_str = "compositor_dispatch"
        if action_type_str == "rapidfire":
            action_type_str = "keyboard"
            action_data = dict(action_data)
            action_data["rapidfire_enabled"] = True
            action_data["action"] = "keyboard"

        try:
            action_type = ActionType(action_type_str)
        except ValueError as exc:
            raise ValueError(f"unknown overload action type '{action_type_str}'") from exc

        if action_type == ActionType.SUPERKEY:
            raise ValueError("nested superkeys are not allowed inside overload superkeys")

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
            )

        if action_type in (
            ActionType.START_MACRO_RECORDING,
            ActionType.STOP_MACRO_RECORDING,
            ActionType.CANCEL_MACRO_PLAYBACK,
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

        if action_type in (ActionType.MOUSE_MOVE_REL, ActionType.MOUSE_MOVE_ABS):
            return MappingAction(
                action_type=action_type,
                move_x=_int_value(action_data.get("x"), 0),
                move_y=_int_value(action_data.get("y"), 0),
                rapidfire_enabled=bool(action_data.get("rapidfire_enabled", False)),
                rapidfire_hold_ms=_int_value(action_data.get("rapidfire_hold_ms"), 20),
                rapidfire_wait_ms=_int_value(action_data.get("rapidfire_wait_ms"), 20),
                tap_enabled=bool(action_data.get("tap_enabled", False)),
                tap_hold_ms=_int_value(action_data.get("tap_hold_ms"), 10),
            )

        target = action_data.get("target")
        cmd = action_data.get("cmd")
        return MappingAction(
            action_type=action_type,
            target=str(target) if target is not None else None,
            keys=cast(list[str] | None, action_data.get("keys")),
            cmd=str(cmd) if cmd is not None else None,
            rapidfire_enabled=bool(action_data.get("rapidfire_enabled", False)),
            rapidfire_hold_ms=_int_value(action_data.get("rapidfire_hold_ms"), 20),
            rapidfire_wait_ms=_int_value(action_data.get("rapidfire_wait_ms"), 20),
            tap_enabled=bool(action_data.get("tap_enabled", False)),
            tap_hold_ms=_int_value(action_data.get("tap_hold_ms"), 10),
        )

    def _is_valid_overload_action(self, config_name: str, action: MappingAction) -> bool:
        if action.action_type == ActionType.SUPERKEY:
            log.warning(
                "Nested superkeys are not allowed in overload superkey '%s'",
                config_name,
            )
            return False
        return action.action_type not in (ActionType.PASSTHROUGH,)

    def get_superkey(self, name: str) -> SuperkeyConfig | None:
        return self._superkeys.get(name)

    def list_superkeys(self) -> list[str]:
        return sorted(self._superkeys.keys())

    def get_all_superkeys(self) -> dict[str, SuperkeyConfig]:
        return self._superkeys.copy()

    def save_superkey(self, config: SuperkeyConfig) -> None:
        paths.ensure_config_dirs()
        self._validate_before_save(config)

        safe_name = self._sanitize_name(config.name)
        path = paths.SUPERKEYS_DIR / f"{safe_name}.toml"

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
        elif config.overload_actions:
            actions["overload"] = [
                self._serialize_mapping_action(action) for action in config.overload_actions
            ]
        if actions:
            data["actions"] = actions

        with open(path, "wb") as f:
            tomli_w.dump(data, f)

        self._superkeys[config.name] = config
        log.info("Saved superkey: %s", config.name)

    def _validate_before_save(self, config: SuperkeyConfig) -> None:
        if config.mode == SuperkeyMode.OVERLOAD:
            if config.has_pattern_actions():
                raise ValueError("overload superkeys cannot define pattern slots")
            for action in config.overload_actions:
                if not self._is_valid_overload_action(config.name, action):
                    raise ValueError("invalid overload action")
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
                        "invalid pattern superkey action type: "
                        f"{action.action_type.value}"
                    )

    def _serialize_pattern_action(self, action: SuperkeyAction) -> TomlDict:
        data: dict[str, object] = {"action": action.action_type.value}

        if action.target:
            data["target"] = action.target
        if action.cmd:
            data["cmd"] = action.cmd
        if action.macro_name:
            data["macro_name"] = action.macro_name

        if action.rapidfire_enabled:
            data["rapidfire_enabled"] = True
            data["rapidfire_hold_ms"] = action.rapidfire_hold_ms
            data["rapidfire_wait_ms"] = action.rapidfire_wait_ms

        return data

    def _serialize_mapping_action(self, action: MappingAction) -> TomlDict:
        action_data: dict[str, object] = {"action": action.action_type.value}
        if action.target:
            action_data["target"] = action.target
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
        if action.rapidfire_enabled:
            action_data["rapidfire_enabled"] = True
            action_data["rapidfire_hold_ms"] = action.rapidfire_hold_ms
            action_data["rapidfire_wait_ms"] = action.rapidfire_wait_ms
        if action.tap_enabled:
            action_data["tap_enabled"] = True
            action_data["tap_hold_ms"] = action.tap_hold_ms
        return action_data

    def delete_superkey(self, name: str) -> bool:
        if name not in self._superkeys:
            return False

        config = self._superkeys[name]
        safe_name = self._sanitize_name(config.name)
        path = paths.SUPERKEYS_DIR / f"{safe_name}.toml"

        if path.exists():
            path.unlink()

        del self._superkeys[name]
        log.info("Deleted superkey: %s", name)
        return True

    def rename_superkey(self, old_name: str, new_name: str) -> bool:
        if old_name not in self._superkeys:
            return False

        if new_name in self._superkeys and new_name != old_name:
            log.warning("Superkey '%s' already exists", new_name)
            return False

        config = self._superkeys[old_name]
        old_path = paths.SUPERKEYS_DIR / f"{self._sanitize_name(old_name)}.toml"

        config.name = new_name
        self.save_superkey(config)

        if old_path.exists():
            old_path.unlink()

        del self._superkeys[old_name]

        log.info("Renamed superkey: %s -> %s", old_name, new_name)
        return True

    def _sanitize_name(self, name: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        return safe.lower()

    def reload(self) -> None:
        self._load_all()
        log.info("Reloaded all superkeys")
