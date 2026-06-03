import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

import tomli_w

from keymasq.common import paths
from keymasq.common.coercion import float_value as _float_value
from keymasq.common.coercion import int_value as _int_value
from keymasq.common.config_files import write_config_atomically
from keymasq.common.models import (
    ActionType,
    AnalogActionThreshold,
    AnalogControlConfig,
    AnalogGamepadOutputConfig,
    AnalogMouseMotionConfig,
    MappingAction,
    normalize_analog_control_features,
    validate_analog_control_config,
)
from keymasq.session.action_toml import (
    UnknownActionTypeError as UnknownActionTypeError,
)
from keymasq.session.action_toml import (
    mapping_action_from_toml,
    mapping_action_to_toml,
    mapping_action_type_from_toml,
)
from keymasq.session.config_loading import load_config_files_sync

log = logging.getLogger("keymasq-session.analog_controls")
type TomlDict = dict[str, object]


@dataclass
class _AnalogControlEntry:
    path: Path
    config: AnalogControlConfig


def _as_toml_dict(value: object) -> TomlDict | None:
    return cast(TomlDict, value) if isinstance(value, dict) else None


def _toml_str(data: TomlDict, key: str, default: str | None = None) -> str | None:
    value = data.get(key, default)
    return value if isinstance(value, str) else default

class AnalogControlManager:
    def __init__(self) -> None:
        paths.ensure_config_dirs()
        self._analog_controls: dict[str, _AnalogControlEntry] = {}
        self._load_all()

    def _load_all(self, *, strict: bool = False) -> None:
        loaded_analog_controls: dict[str, _AnalogControlEntry] = {}
        for config_file, config in load_config_files_sync(
            paths.ANALOG_CONTROLS_DIR,
            config_kind="analog control",
            strict=strict,
            load_config=self._load_analog_control,
            logger=log,
            failure_log_message="Failed to load analog control %s: %s",
        ):
            if config is not None:
                self._add_loaded_analog_control(
                    config_file,
                    config,
                    loaded_analog_controls,
                )

        self._analog_controls = loaded_analog_controls

    def _add_loaded_analog_control(
        self,
        path: Path,
        config: AnalogControlConfig,
        analog_controls: dict[str, _AnalogControlEntry],
    ) -> None:
        entry = _AnalogControlEntry(path=path, config=config)
        existing = analog_controls.get(config.name)
        if existing is None:
            analog_controls[config.name] = entry
            return

        selected = self._select_duplicate_analog_control(config.name, existing, entry)
        ignored = entry if selected is existing else existing
        analog_controls[config.name] = selected
        log.warning(
            "Ignoring duplicate analog control name '%s' from %s; using %s",
            config.name,
            ignored.path,
            selected.path,
        )

    def _select_duplicate_analog_control(
        self,
        name: str,
        first: _AnalogControlEntry,
        second: _AnalogControlEntry,
    ) -> _AnalogControlEntry:
        first_is_canonical = self._is_canonical_storage_path(name, first.path)
        second_is_canonical = self._is_canonical_storage_path(name, second.path)
        if first_is_canonical and not second_is_canonical:
            return first
        if second_is_canonical and not first_is_canonical:
            return second
        return first

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
                mode=_toml_str(mouse_data, "mode", "velocity") or "velocity",
                speed=_float_value(mouse_data.get("speed"), 900.0),
                speed_x=(
                    _float_value(mouse_data.get("speed_x"), 900.0)
                    if "speed_x" in mouse_data
                    else None
                ),
                speed_y=(
                    _float_value(mouse_data.get("speed_y"), 900.0)
                    if "speed_y" in mouse_data
                    else None
                ),
                area_radius_x=_float_value(mouse_data.get("area_radius_x"), 400.0),
                area_radius_y=_float_value(mouse_data.get("area_radius_y"), 400.0),
                area_start_enabled=bool(mouse_data.get("area_start_enabled", False)),
                area_start_x=_int_value(mouse_data.get("area_start_x"), 0),
                area_start_y=_int_value(mouse_data.get("area_start_y"), 0),
                deadzone=_float_value(mouse_data.get("deadzone"), 0.15),
                sensitivity=_float_value(mouse_data.get("sensitivity"), 1.0),
                response_curve=_float_value(mouse_data.get("response_curve"), 1.0),
                direction=_toml_str(mouse_data, "direction", "right") or "right",
                invert_x=bool(mouse_data.get("invert_x", False)),
                invert_y=bool(mouse_data.get("invert_y", False)),
                tick_ms=_int_value(mouse_data.get("tick_ms"), 8),
            ),
            gamepad_output=AnalogGamepadOutputConfig(
                enabled=bool(gamepad_data.get("enabled", False)),
                output_id=_toml_str(gamepad_data, "output_id"),
                deadzone=_float_value(gamepad_data.get("deadzone"), 0.0),
                target=_toml_str(gamepad_data, "target", "same") or "same",
                target_analog_id=_toml_str(gamepad_data, "target_analog_id"),
                output_rest=(
                    _int_value(gamepad_data.get("output_rest"), 0)
                    if gamepad_data.get("output_rest") is not None
                    else None
                ),
                output_direction=_toml_str(gamepad_data, "output_direction", "max") or "max",
                output_invert=bool(gamepad_data.get("output_invert", False)),
                sensitivity=_float_value(gamepad_data.get("sensitivity"), 1.0),
                response_curve=_float_value(gamepad_data.get("response_curve"), 1.0),
            ),
            thresholds=thresholds,
        )
        config = normalize_analog_control_features(config)
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
        action_type, normalized_action_data = mapping_action_type_from_toml(
            action_data,
            unknown_action="raise",
        )
        return mapping_action_from_toml(
            normalized_action_data,
            action_type,
            logger=log,
            rapidfire_warning_context="analog control config",
            preparse_rapidfire_for_special_actions=True,
        )

    def get_analog_control(self, name: str) -> AnalogControlConfig | None:
        entry = self._analog_controls.get(name)
        return entry.config if entry is not None else None

    def list_analog_controls(self) -> list[str]:
        return sorted(self._analog_controls.keys())

    def get_all_analog_controls(self) -> dict[str, AnalogControlConfig]:
        return {name: entry.config for name, entry in self._analog_controls.items()}

    def snapshot_analog_controls(self) -> dict[str, _AnalogControlEntry]:
        return self._analog_controls.copy()

    def restore_analog_controls(
        self,
        analog_controls: dict[str, _AnalogControlEntry],
    ) -> None:
        self._analog_controls = analog_controls.copy()

    def save_analog_control(
        self,
        config: AnalogControlConfig,
        *,
        replacing_name: str | None = None,
    ) -> None:
        paths.ensure_config_dirs()
        config = normalize_analog_control_features(config)
        validate_analog_control_config(config)
        existing_entry = self._analog_controls.get(config.name)
        if replacing_name and replacing_name != config.name:
            if existing_entry is not None:
                raise ValueError(f"Analog control '{config.name}' already exists")
            path = self._path_for_name(config.name)
        else:
            path = existing_entry.path if existing_entry is not None else self._path_for_name(
                config.name
            )
        self._ensure_storage_path_available(config.name, path, replacing_name=replacing_name)

        data: dict[str, object] = {
            "name": config.name,
            "input_type": config.input_type,
            "mouse_motion": {
                "enabled": bool(config.mouse_motion.enabled),
                "mode": config.mouse_motion.mode,
                "speed": float(config.mouse_motion.speed),
                "speed_x": float(
                    config.mouse_motion.speed_x
                    if config.mouse_motion.speed_x is not None
                    else config.mouse_motion.speed
                ),
                "speed_y": float(
                    config.mouse_motion.speed_y
                    if config.mouse_motion.speed_y is not None
                    else config.mouse_motion.speed
                ),
                "area_radius_x": float(config.mouse_motion.area_radius_x),
                "area_radius_y": float(config.mouse_motion.area_radius_y),
                "area_start_enabled": bool(config.mouse_motion.area_start_enabled),
                "area_start_x": int(config.mouse_motion.area_start_x),
                "area_start_y": int(config.mouse_motion.area_start_y),
                "deadzone": float(config.mouse_motion.deadzone),
                "sensitivity": float(config.mouse_motion.sensitivity),
                "response_curve": float(config.mouse_motion.response_curve),
                "direction": config.mouse_motion.direction,
                "invert_x": bool(config.mouse_motion.invert_x),
                "invert_y": bool(config.mouse_motion.invert_y),
                "tick_ms": int(config.mouse_motion.tick_ms),
            },
            "gamepad_output": {
                "enabled": bool(config.gamepad_output.enabled),
                "deadzone": float(config.gamepad_output.deadzone),
                "target": config.gamepad_output.target,
                "sensitivity": float(config.gamepad_output.sensitivity),
                "response_curve": float(config.gamepad_output.response_curve),
            },
        }
        if config.gamepad_output.output_id:
            gamepad_output = cast(dict[str, object], data["gamepad_output"])
            gamepad_output["output_id"] = config.gamepad_output.output_id
        if config.gamepad_output.target_analog_id:
            gamepad_output = cast(dict[str, object], data["gamepad_output"])
            gamepad_output["target_analog_id"] = config.gamepad_output.target_analog_id
        if config.gamepad_output.output_rest is not None:
            gamepad_output = cast(dict[str, object], data["gamepad_output"])
            gamepad_output["output_rest"] = int(config.gamepad_output.output_rest)
        if config.gamepad_output.output_direction != "max":
            gamepad_output = cast(dict[str, object], data["gamepad_output"])
            gamepad_output["output_direction"] = config.gamepad_output.output_direction
        if config.gamepad_output.output_invert:
            gamepad_output = cast(dict[str, object], data["gamepad_output"])
            gamepad_output["output_invert"] = True
        if config.description:
            data["description"] = config.description
        if config.thresholds:
            data["thresholds"] = [
                self._serialize_threshold(threshold) for threshold in config.thresholds
            ]

        def write_config(config_file: BinaryIO) -> None:
            tomli_w.dump(data, config_file)

        write_config_atomically(path, write_config)
        if replacing_name and replacing_name != config.name:
            old_entry = self._analog_controls.get(replacing_name)
            old_path = old_entry.path if old_entry is not None else self._path_for_name(
                replacing_name
            )
            if old_path != path and old_path.exists():
                old_path.unlink()
            self._analog_controls.pop(replacing_name, None)
        self._analog_controls[config.name] = _AnalogControlEntry(path=path, config=config)
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
        return mapping_action_to_toml(
            action,
            logger=log,
            rapidfire_warning_context="analog control config",
        )

    def delete_analog_control(self, name: str) -> bool:
        entry = self._analog_controls.get(name)
        if entry is None:
            return False
        if entry.path.exists():
            entry.path.unlink()
        del self._analog_controls[name]
        log.info("Deleted analog control: %s", name)
        return True

    def rename_analog_control(self, old_name: str, new_name: str) -> bool:
        entry = self._analog_controls.get(old_name)
        if entry is None:
            return False
        if old_name == new_name:
            return True
        if new_name in self._analog_controls and new_name != old_name:
            log.warning("Analog control '%s' already exists", new_name)
            return False

        config = entry.config
        new_path = self._path_for_name(new_name)
        self._ensure_storage_path_available(new_name, new_path, replacing_name=old_name)
        config.name = new_name
        try:
            self.save_analog_control(config, replacing_name=old_name)
        except Exception:
            config.name = old_name
            raise
        log.info("Renamed analog control: %s -> %s", old_name, new_name)
        return True

    def _sanitize_name(self, name: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        return safe.lower()

    def _path_for_name(self, name: str) -> Path:
        return paths.ANALOG_CONTROLS_DIR / f"{self._sanitize_name(name)}.toml"

    def _is_canonical_storage_path(self, name: str, path: Path) -> bool:
        return path == self._path_for_name(name)

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
        self._load_all(strict=True)
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
