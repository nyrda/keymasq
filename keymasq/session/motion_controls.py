"""Persistence for reusable motion controls."""

import copy
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

import tomli_w

from keymasq.common import paths
from keymasq.common.coercion import coerce_float
from keymasq.common.config_files import write_config_atomically
from keymasq.common.model.analog import SAME_DEVICE_OUTPUT_ID
from keymasq.common.model.motion import (
    MotionAnalogConfig,
    MotionAxisRoutingConfig,
    MotionControlConfig,
    MotionGamepadConfig,
    MotionMouseConfig,
    MotionTiltConfig,
)
from keymasq.session.config_loading import load_config_files_sync

log = logging.getLogger("keymasq-session.motion_controls")


@dataclass
class _MotionControlEntry:
    path: Path
    config: MotionControlConfig


class MotionControlManager:
    def __init__(self) -> None:
        paths.ensure_config_dirs()
        self._motion_controls: dict[str, _MotionControlEntry] = {}
        self._load_all()

    def reload(self) -> None:
        self._load_all(strict=True)

    def _load_all(self, *, strict: bool = False) -> None:
        loaded: dict[str, _MotionControlEntry] = {}
        for path, config in load_config_files_sync(
            paths.MOTION_CONTROLS_DIR,
            config_kind="motion control",
            strict=strict,
            load_config=self._load,
            logger=log,
        ):
            if config.name in loaded:
                log.warning("Ignoring duplicate motion control '%s' from %s", config.name, path)
                continue
            loaded[config.name] = _MotionControlEntry(path, config)
        self._motion_controls = loaded

    def _load(self, path: Path) -> MotionControlConfig:
        with open(path, "rb") as config_file:
            data = cast(dict[str, object], tomllib.load(config_file))
        mouse = cast(dict[str, object], data.get("mouse", {}))
        gamepad = cast(dict[str, object], data.get("gamepad", {}))
        tilt = cast(dict[str, object], data.get("tilt", {}))
        analog = cast(dict[str, object], data.get("analog", {}))
        axis_routing = cast(dict[str, object], data.get("axis_routing", {}))
        return MotionControlConfig(
            name=str(data.get("name", path.stem)),
            description=str(data["description"]) if data.get("description") else None,
            mode=str(data.get("mode", "mouse")),
            axis_routing=MotionAxisRoutingConfig(
                yaw=str(axis_routing.get("yaw", "horizontal")),
                pitch=str(axis_routing.get("pitch", "vertical")),
                roll=str(axis_routing.get("roll", "horizontal")),
            ),
            mouse=MotionMouseConfig(
                sensitivity_x=coerce_float(mouse.get("sensitivity_x"), 8.0),
                sensitivity_y=coerce_float(mouse.get("sensitivity_y"), 8.0),
                deadzone_dps=coerce_float(mouse.get("deadzone_dps"), 0.5),
                smoothing=coerce_float(mouse.get("smoothing"), 0.15),
                response_curve=coerce_float(mouse.get("response_curve"), 1.0),
                invert_x=bool(mouse.get("invert_x", False)),
                invert_y=bool(mouse.get("invert_y", False)),
            ),
            gamepad=MotionGamepadConfig(
                output_id=(
                    str(gamepad["output_id"]) if gamepad.get("output_id") else SAME_DEVICE_OUTPUT_ID
                ),
                target=str(gamepad.get("target", "right")),
                target_analog_id=(
                    str(gamepad["target_analog_id"]) if gamepad.get("target_analog_id") else None
                ),
                max_rate_dps=coerce_float(gamepad.get("max_rate_dps"), 90.0),
                minimum_output=coerce_float(gamepad.get("minimum_output"), 0.25),
                deadzone_dps=coerce_float(gamepad.get("deadzone_dps"), 0.0),
                smoothing=coerce_float(gamepad.get("smoothing"), 0.15),
                response_curve=coerce_float(gamepad.get("response_curve"), 1.0),
                invert_x=bool(gamepad.get("invert_x", False)),
                invert_y=bool(gamepad.get("invert_y", False)),
            ),
            tilt=MotionTiltConfig(
                reference=str(tilt.get("reference", "activation")),
                pitch=str(tilt.get("pitch", "vertical")),
                roll=str(tilt.get("roll", "horizontal")),
                deadzone_deg=coerce_float(tilt.get("deadzone_deg"), 2.0),
                full_scale_deg=coerce_float(tilt.get("full_scale_deg"), 30.0),
                smoothing=coerce_float(tilt.get("smoothing"), 0.8),
                response_curve=coerce_float(tilt.get("response_curve"), 1.0),
                invert_x=bool(tilt.get("invert_x", False)),
                invert_y=bool(tilt.get("invert_y", False)),
                speed_x=coerce_float(tilt.get("speed_x"), 900.0),
                speed_y=coerce_float(tilt.get("speed_y"), 900.0),
            ),
            analog=MotionAnalogConfig(
                analog_control_name=(
                    str(analog["analog_control_name"])
                    if analog.get("analog_control_name")
                    else None
                ),
                source=str(analog.get("source", "tilt")),
                x_axis=str(analog.get("x_axis", "roll")),
                y_axis=str(analog.get("y_axis", "pitch")),
                reference=str(analog.get("reference", "activation")),
                full_scale_dps=coerce_float(analog.get("full_scale_dps"), 360.0),
                full_scale_deg=coerce_float(analog.get("full_scale_deg"), 30.0),
                smoothing_mode=str(analog.get("smoothing_mode", "fixed")),
                adaptive_min_cutoff_hz=coerce_float(analog.get("adaptive_min_cutoff_hz"), 1.0),
                adaptive_beta=coerce_float(analog.get("adaptive_beta"), 10.0),
                smoothing=coerce_float(analog.get("smoothing"), 0.15),
                invert_x=bool(analog.get("invert_x", False)),
                invert_y=bool(analog.get("invert_y", False)),
            ),
        )

    def get_motion_control(self, name: str) -> MotionControlConfig | None:
        entry = self._motion_controls.get(name)
        return entry.config if entry else None

    def list_motion_controls(self) -> list[str]:
        return sorted(self._motion_controls)

    def get_all_motion_controls(self) -> dict[str, MotionControlConfig]:
        return {name: entry.config for name, entry in self._motion_controls.items()}

    def rename_analog_control_references(self, old_name: str, new_name: str) -> int:
        if old_name == new_name:
            return 0
        changed = 0
        for name, entry in list(self._motion_controls.items()):
            if entry.config.analog.analog_control_name != old_name:
                continue
            config = copy.deepcopy(entry.config)
            config.analog.analog_control_name = new_name
            self.save_motion_control(config, replacing_name=name)
            changed += 1
        return changed

    def clear_analog_control_references(self, analog_control_name: str) -> int:
        changed = 0
        for name, entry in list(self._motion_controls.items()):
            if entry.config.analog.analog_control_name != analog_control_name:
                continue
            config = copy.deepcopy(entry.config)
            config.analog.analog_control_name = None
            config.analog.analog_control_config = None
            self.save_motion_control(config, replacing_name=name)
            changed += 1
        return changed

    def unique_motion_control_name(self, base: str = "Motion Control") -> str:
        name = str(base or "Motion Control").strip() or "Motion Control"
        candidate = name
        index = 2
        while candidate in self._motion_controls or self._path(candidate).exists():
            candidate = f"{name} {index}"
            index += 1
        return candidate

    def save_motion_control(
        self,
        config: MotionControlConfig,
        *,
        replacing_name: str | None = None,
    ) -> None:
        existing = self._motion_controls.get(config.name)
        if existing is not None and replacing_name not in {None, config.name}:
            raise ValueError(f"Motion control '{config.name}' already exists")
        path = existing.path if existing else self._path(config.name)
        if path.exists() and existing is None:
            raise ValueError(f"Motion control storage path '{path.name}' already exists")
        data: dict[str, object] = {
            "name": config.name,
            "mode": config.mode,
            "axis_routing": {
                "yaw": config.axis_routing.yaw,
                "pitch": config.axis_routing.pitch,
                "roll": config.axis_routing.roll,
            },
            "mouse": {
                "sensitivity_x": config.mouse.sensitivity_x,
                "sensitivity_y": config.mouse.sensitivity_y,
                "deadzone_dps": config.mouse.deadzone_dps,
                "smoothing": config.mouse.smoothing,
                "response_curve": config.mouse.response_curve,
                "invert_x": config.mouse.invert_x,
                "invert_y": config.mouse.invert_y,
            },
            "gamepad": {
                "target": config.gamepad.target,
                "max_rate_dps": config.gamepad.max_rate_dps,
                "minimum_output": config.gamepad.minimum_output,
                "deadzone_dps": config.gamepad.deadzone_dps,
                "smoothing": config.gamepad.smoothing,
                "response_curve": config.gamepad.response_curve,
                "invert_x": config.gamepad.invert_x,
                "invert_y": config.gamepad.invert_y,
                **({"output_id": config.gamepad.output_id} if config.gamepad.output_id else {}),
                **(
                    {"target_analog_id": config.gamepad.target_analog_id}
                    if config.gamepad.target_analog_id
                    else {}
                ),
            },
            "tilt": {
                "reference": config.tilt.reference,
                "pitch": config.tilt.pitch,
                "roll": config.tilt.roll,
                "deadzone_deg": config.tilt.deadzone_deg,
                "full_scale_deg": config.tilt.full_scale_deg,
                "smoothing": config.tilt.smoothing,
                "response_curve": config.tilt.response_curve,
                "invert_x": config.tilt.invert_x,
                "invert_y": config.tilt.invert_y,
                "speed_x": config.tilt.speed_x,
                "speed_y": config.tilt.speed_y,
            },
            "analog": {
                "source": config.analog.source,
                "x_axis": config.analog.x_axis,
                "y_axis": config.analog.y_axis,
                "reference": config.analog.reference,
                "full_scale_dps": config.analog.full_scale_dps,
                "full_scale_deg": config.analog.full_scale_deg,
                "smoothing_mode": config.analog.smoothing_mode,
                "adaptive_min_cutoff_hz": config.analog.adaptive_min_cutoff_hz,
                "adaptive_beta": config.analog.adaptive_beta,
                "smoothing": config.analog.smoothing,
                "invert_x": config.analog.invert_x,
                "invert_y": config.analog.invert_y,
                **(
                    {"analog_control_name": config.analog.analog_control_name}
                    if config.analog.analog_control_name
                    else {}
                ),
            },
        }
        if config.description:
            data["description"] = config.description

        def write_config(config_file: BinaryIO) -> None:
            tomli_w.dump(data, config_file)

        write_config_atomically(path, write_config)
        if replacing_name and replacing_name != config.name:
            old = self._motion_controls.pop(replacing_name, None)
            if old and old.path != path and old.path.exists():
                old.path.unlink()
        self._motion_controls[config.name] = _MotionControlEntry(path, config)

    def delete_motion_control(self, name: str) -> bool:
        entry = self._motion_controls.pop(name, None)
        if entry is None:
            return False
        if entry.path.exists():
            entry.path.unlink()
        return True

    def snapshot_motion_controls(self) -> dict[str, _MotionControlEntry]:
        return self._motion_controls.copy()

    def restore_motion_controls(self, values: dict[str, _MotionControlEntry]) -> None:
        self._motion_controls = values.copy()

    @staticmethod
    def _sanitize(name: str) -> str:
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in name).lower()

    def _path(self, name: str) -> Path:
        return paths.MOTION_CONTROLS_DIR / f"{self._sanitize(name)}.toml"
