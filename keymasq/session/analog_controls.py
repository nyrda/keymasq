import logging
import tomllib
from pathlib import Path
from typing import cast

import tomli_w

from keymasq.common import paths
from keymasq.common.models import (
    ActionType,
    AnalogActionThreshold,
    AnalogControlConfig,
    AnalogGamepadOutputConfig,
    AnalogMouseMotionConfig,
    MappingAction,
    normalize_macro_loop_stop_behavior,
    parse_rapidfire_fields,
    validate_analog_control_config,
)

log = logging.getLogger("keymasq-session.analog_controls")
type TomlDict = dict[str, object]
type _IntLike = int | float | str | bytes
type _FloatLike = int | float | str | bytes


class UnknownActionTypeError(ValueError):
    pass


def _as_toml_dict(value: object) -> TomlDict | None:
    return cast(TomlDict, value) if isinstance(value, dict) else None


def _toml_str(data: TomlDict, key: str, default: str | None = None) -> str | None:
    value = data.get(key, default)
    return value if isinstance(value, str) else default


def _int_value(value: object, default: int = 0) -> int:
    return default if value is None else int(cast(_IntLike, value))


def _float_value(value: object, default: float = 0.0) -> float:
    return default if value is None else float(cast(_FloatLike, value))


class AnalogControlManager:
    def __init__(self) -> None:
        paths.ensure_config_dirs()
        self._analog_controls: dict[str, AnalogControlConfig] = {}
        self._load_all()

    def _load_all(self) -> None:
        self._analog_controls.clear()
        if not paths.ANALOG_CONTROLS_DIR.exists():
            return
        for config_file in paths.ANALOG_CONTROLS_DIR.glob("*.toml"):
            try:
                config = self._load_analog_control(config_file)
                if config is not None:
                    self._analog_controls[config.name] = config
            except Exception as exc:
                log.error("Failed to load analog control %s: %s", config_file, exc)

    def _load_analog_control(self, path: Path) -> AnalogControlConfig | None:
        with open(path, "rb") as f:
            data = cast(TomlDict, tomllib.load(f))

        mouse_data = _as_toml_dict(data.get("mouse_motion")) or {}
        gamepad_data = _as_toml_dict(data.get("gamepad_output")) or {}
        thresholds: list[AnalogActionThreshold] = []
        raw_thresholds = data.get("thresholds")
        if isinstance(raw_thresholds, list):
            for item in cast(list[object], raw_thresholds):
                threshold_data = _as_toml_dict(item)
                if threshold_data is None:
                    continue
                thresholds.append(self._parse_threshold(threshold_data))

        config = AnalogControlConfig(
            name=_toml_str(data, "name", path.stem) or path.stem,
            description=_toml_str(data, "description"),
            input_type=_toml_str(data, "input_type", "stick") or "stick",
            mouse_motion=AnalogMouseMotionConfig(
                enabled=bool(mouse_data.get("enabled", False)),
                speed=_float_value(mouse_data.get("speed"), 900.0),
                deadzone=_float_value(mouse_data.get("deadzone"), 0.15),
                curve=str(mouse_data.get("curve", "soft") or "soft"),
                invert_x=bool(mouse_data.get("invert_x", False)),
                invert_y=bool(mouse_data.get("invert_y", False)),
                tick_ms=_int_value(mouse_data.get("tick_ms"), 8),
            ),
            gamepad_output=AnalogGamepadOutputConfig(
                enabled=bool(gamepad_data.get("enabled", False)),
                output_id=_toml_str(gamepad_data, "output_id"),
                deadzone=_float_value(gamepad_data.get("deadzone"), 0.15),
            ),
            thresholds=thresholds,
        )
        validate_analog_control_config(config)
        return config

    def _parse_threshold(self, data: TomlDict) -> AnalogActionThreshold:
        actions: list[MappingAction] = []
        raw_actions = data.get("actions")
        if isinstance(raw_actions, list):
            for raw_action in cast(list[object], raw_actions):
                action_data = _as_toml_dict(raw_action)
                if action_data is None:
                    raise ValueError("analog threshold actions must be TOML tables")
                actions.append(self._parse_mapping_action(action_data))
        return AnalogActionThreshold(
            axis=str(data.get("axis", "") or ""),
            trigger_min=_float_value(data.get("trigger_min"), 0.0),
            trigger_max=_float_value(data.get("trigger_max"), 0.0),
            release_min=_float_value(data.get("release_min"), 0.0),
            release_max=_float_value(data.get("release_max"), 0.0),
            actions=actions,
        )

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
                "Ignoring rapidfire for unsupported %s action in analog control config",
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
            ActionType.CANCEL_MACRO_PLAYBACK,
            ActionType.EMERGENCY_RESET,
        ):
            return MappingAction(action_type=action_type)

        if action_type in (
            ActionType.PROFILE_ENABLE,
            ActionType.PROFILE_DISABLE,
            ActionType.PROFILE_TOGGLE,
        ):
            return MappingAction(
                action_type=action_type,
                profile_name=str(
                    action_data.get("profile_name", "") or action_data.get("target", "") or ""
                ),
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

    def get_analog_control(self, name: str) -> AnalogControlConfig | None:
        return self._analog_controls.get(name)

    def list_analog_controls(self) -> list[str]:
        return sorted(self._analog_controls.keys())

    def get_all_analog_controls(self) -> dict[str, AnalogControlConfig]:
        return self._analog_controls.copy()

    def save_analog_control(
        self,
        config: AnalogControlConfig,
        *,
        replacing_name: str | None = None,
    ) -> None:
        paths.ensure_config_dirs()
        validate_analog_control_config(config)
        path = self._path_for_name(config.name)
        self._ensure_storage_path_available(config.name, path, replacing_name=replacing_name)

        data: dict[str, object] = {
            "name": config.name,
            "input_type": config.input_type,
            "mouse_motion": {
                "enabled": bool(config.mouse_motion.enabled),
                "speed": float(config.mouse_motion.speed),
                "deadzone": float(config.mouse_motion.deadzone),
                "curve": config.mouse_motion.curve,
                "invert_x": bool(config.mouse_motion.invert_x),
                "invert_y": bool(config.mouse_motion.invert_y),
                "tick_ms": int(config.mouse_motion.tick_ms),
            },
            "gamepad_output": {
                "enabled": bool(config.gamepad_output.enabled),
                "deadzone": float(config.gamepad_output.deadzone),
            },
        }
        if config.gamepad_output.output_id:
            gamepad_output = cast(dict[str, object], data["gamepad_output"])
            gamepad_output["output_id"] = config.gamepad_output.output_id
        if config.description:
            data["description"] = config.description
        if config.thresholds:
            data["thresholds"] = [
                self._serialize_threshold(threshold) for threshold in config.thresholds
            ]

        with open(path, "wb") as f:
            tomli_w.dump(data, f)
        if replacing_name and replacing_name != config.name:
            old_path = self._path_for_name(replacing_name)
            if old_path != path and old_path.exists():
                old_path.unlink()
            self._analog_controls.pop(replacing_name, None)
        self._analog_controls[config.name] = config
        log.info("Saved analog control: %s", config.name)

    def _serialize_threshold(self, threshold: AnalogActionThreshold) -> TomlDict:
        return {
            "axis": threshold.axis,
            "trigger_min": threshold.trigger_min,
            "trigger_max": threshold.trigger_max,
            "release_min": threshold.release_min,
            "release_max": threshold.release_max,
            "actions": [self._serialize_mapping_action(action) for action in threshold.actions],
        }

    def _serialize_mapping_action(self, action: MappingAction) -> TomlDict:
        action_data: dict[str, object] = {"action": action.action_type.value}
        if action.target:
            action_data["target"] = action.target
        if action.action_type == ActionType.GAMEPAD and action.output_id:
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
            action_data["rapidfire_hold_ms"] = int(action.rapidfire_hold_ms)
            action_data["rapidfire_wait_ms"] = int(action.rapidfire_wait_ms)
        if action.tap_enabled:
            action_data["tap_enabled"] = True
            action_data["tap_hold_ms"] = int(action.tap_hold_ms)
        return action_data

    def delete_analog_control(self, name: str) -> bool:
        if name not in self._analog_controls:
            return False
        path = self._path_for_name(name)
        if path.exists():
            path.unlink()
        del self._analog_controls[name]
        log.info("Deleted analog control: %s", name)
        return True

    def rename_analog_control(self, old_name: str, new_name: str) -> bool:
        if old_name not in self._analog_controls:
            return False
        if new_name in self._analog_controls and new_name != old_name:
            log.warning("Analog control '%s' already exists", new_name)
            return False

        config = self._analog_controls[old_name]
        old_path = self._path_for_name(old_name)
        new_path = self._path_for_name(new_name)
        self._ensure_storage_path_available(new_name, new_path, replacing_name=old_name)
        config.name = new_name
        try:
            self.save_analog_control(config, replacing_name=old_name)
        except Exception:
            config.name = old_name
            raise
        if old_path != new_path and old_path.exists():
            old_path.unlink()
        self._analog_controls.pop(old_name, None)
        log.info("Renamed analog control: %s -> %s", old_name, new_name)
        return True

    def _sanitize_name(self, name: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        return safe.lower()

    def _path_for_name(self, name: str) -> Path:
        return paths.ANALOG_CONTROLS_DIR / f"{self._sanitize_name(name)}.toml"

    def _storage_file_name(self, path: Path) -> str | None:
        if not path.exists():
            return None
        try:
            with open(path, "rb") as f:
                data = cast(TomlDict, tomllib.load(f))
            return _toml_str(data, "name", path.stem) or path.stem
        except Exception as exc:
            raise ValueError(
                f"Analog control storage path '{path.name}' already exists but could not be read"
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
            f"Analog control name '{name}' conflicts with existing analog control '{stored_name}'"
        )

    def reload(self) -> None:
        self._load_all()
        log.info("Reloaded all analog controls")


def analog_control_wasd_template() -> list[AnalogActionThreshold]:
    return _direction_template(
        {
            "up": "key_w",
            "down": "key_s",
            "left": "key_a",
            "right": "key_d",
        }
    )


def analog_control_arrow_template() -> list[AnalogActionThreshold]:
    return _direction_template(
        {
            "up": "key_up",
            "down": "key_down",
            "left": "key_left",
            "right": "key_right",
        }
    )


def analog_control_mouse_wheel_template() -> list[AnalogActionThreshold]:
    return [
        AnalogActionThreshold(
            axis="y",
            trigger_min=-1.0,
            trigger_max=-0.55,
            release_min=-1.0,
            release_max=-0.45,
            actions=[
                MappingAction(
                    action_type=ActionType.MOUSE,
                    target="rel_wheel:1",
                    rapidfire_enabled=True,
                    rapidfire_hold_ms=20,
                    rapidfire_wait_ms=60,
                )
            ],
        ),
        AnalogActionThreshold(
            axis="y",
            trigger_min=0.55,
            trigger_max=1.0,
            release_min=0.45,
            release_max=1.0,
            actions=[
                MappingAction(
                    action_type=ActionType.MOUSE,
                    target="rel_wheel:-1",
                    rapidfire_enabled=True,
                    rapidfire_hold_ms=20,
                    rapidfire_wait_ms=60,
                )
            ],
        ),
    ]


def _direction_template(targets: dict[str, str]) -> list[AnalogActionThreshold]:
    specs = [
        ("y", -1.0, -0.65, -1.0, -0.55, targets["up"]),
        ("y", 0.65, 1.0, 0.55, 1.0, targets["down"]),
        ("x", -1.0, -0.65, -1.0, -0.55, targets["left"]),
        ("x", 0.65, 1.0, 0.55, 1.0, targets["right"]),
    ]
    return [
        AnalogActionThreshold(
            axis=axis,
            trigger_min=trigger_min,
            trigger_max=trigger_max,
            release_min=release_min,
            release_max=release_max,
            actions=[MappingAction(action_type=ActionType.KEYBOARD, target=target)],
        )
        for axis, trigger_min, trigger_max, release_min, release_max, target in specs
    ]
