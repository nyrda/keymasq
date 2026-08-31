import logging
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, cast

import tomli_w

from keymasq.common import paths
from keymasq.common.coercion import coerce_float
from keymasq.common.config_files import write_config_atomically
from keymasq.common.devices import is_gamepad_button_name
from keymasq.common.model.core import DeviceType
from keymasq.common.model.hardware import (
    AnalogAxisDefinition,
    AnalogInputDefinition,
    ButtonDefinition,
    EvdevDevice,
    HardwareConfig,
)
from keymasq.common.model.motion import (
    MOTION_NORMALIZATION_VERSION,
    MotionAxisDefinition,
    MotionSensorDefinition,
    canonical_motion_axis,
)
from keymasq.session.config_loading import load_config_files_sync

log = logging.getLogger("keymasq-session.hardware")
MAX_HARDWARE_PATH_ATTEMPTS = 10000
KEYBOARD_LAYOUT_FOOTER = (
    b"\n"
    b"# Optional special keys (not shown in GUI by default)\n"
    b"# Add entries below into [hardware.layout.buttons] if needed\n"
    b"# Example entries:\n"
    b'# { id = "key_volumedown", label = "Volume Down", '
    b'evdev = "key_volumedown", type = "key" }\n'
    b'# { id = "key_volumeup", label = "Volume Up", '
    b'evdev = "key_volumeup", type = "key" }\n'
    b'# { id = "key_mute", label = "Mute", evdev = "key_mute", type = "key" }\n'
    b'# { id = "key_playpause", label = "Play Pause", '
    b'evdev = "key_playpause", type = "key" }\n'
)


def _valid_hardware_id_for_model(hardware_id: str, model_id: str) -> bool:
    return hardware_id == model_id or hardware_id.startswith(f"{model_id}@")


def _hardware_storage_stem(hardware_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", hardware_id).strip("._")
    return (safe or "hardware").lower()


def _hardware_id_sort_key(hardware_id: str) -> tuple[str, int, int, str]:
    normalized = str(hardware_id or "").strip().lower()
    model_id, separator, suffix = normalized.partition("@")
    if not separator:
        return model_id, 0, 0, ""
    if suffix.isdecimal():
        return model_id, 1, int(suffix), suffix
    return model_id, 2, 0, suffix


@dataclass
class _HardwareEntry:
    path: Path
    config: HardwareConfig


class HardwareManager:
    def __init__(self) -> None:
        paths.ensure_config_dirs()
        self._cache: dict[str, _HardwareEntry] = {}
        self._load_all()

    def _load_all(self, *, strict: bool = False) -> None:
        loaded_cache: dict[str, _HardwareEntry] = {}
        for config_file, config in load_config_files_sync(
            paths.HARDWARE_DIR,
            config_kind="hardware",
            strict=strict,
            load_config=self._load_config,
            logger=log,
        ):
            self._add_loaded_hardware(
                _HardwareEntry(path=config_file, config=config),
                loaded_cache,
            )

        self._cache = loaded_cache

    def reload(self) -> None:
        self._load_all(strict=True)

    def _load_config(self, path: Path) -> HardwareConfig:
        with open(path, "rb") as f:
            data = tomllib.load(f)

        hw = cast(dict[str, Any], data["hardware"])
        vendor_id = str(hw["vendor_id"])
        product_id = str(hw["product_id"])
        model_id = f"{vendor_id}:{product_id}"
        hardware_id = str(hw.get("hardware_id", "") or "")
        if hardware_id and not _valid_hardware_id_for_model(hardware_id, model_id):
            raise ValueError(
                f"hardware_id '{hardware_id}' does not match vendor/product '{model_id}'"
            )

        evdev_devices: list[EvdevDevice] = []
        evdev_config = cast(dict[str, Any], hw.get("evdev", {}))
        for dev in cast(list[dict[str, Any]], evdev_config.get("devices", [])):
            evdev_devices.append(
                EvdevDevice(
                    path=dev["path"],
                    device_type=DeviceType(dev.get("type", "other")),
                    id=dev.get("id"),
                    phys=dev.get("phys"),
                    capabilities=dev.get("capabilities", []),
                )
            )

        buttons: list[ButtonDefinition] = []
        layout = cast(dict[str, Any], hw.get("layout", {}))
        for btn in cast(list[dict[str, Any]], layout.get("buttons", [])):
            buttons.append(
                ButtonDefinition(
                    id=btn["id"],
                    label=btn.get("label", btn["id"]),
                    evdev=btn["evdev"],
                    evdev_code=btn.get("evdev_code"),
                    evdev_value=btn.get("evdev_value"),
                    source=btn.get("source"),
                    zone=btn.get("zone"),
                    row=btn.get("row"),
                    col=btn.get("col"),
                    type=btn.get("type"),
                )
            )

        analog_inputs: list[AnalogInputDefinition] = []
        for analog in cast(list[dict[str, Any]], layout.get("analogs", [])):
            axes = [
                AnalogAxisDefinition(
                    role=str(axis.get("role", "")),
                    evdev=str(axis.get("evdev", "")),
                    evdev_code=axis.get("evdev_code"),
                    minimum=axis.get("minimum") if isinstance(axis.get("minimum"), int) else None,
                    maximum=axis.get("maximum") if isinstance(axis.get("maximum"), int) else None,
                    center=axis.get("center") if isinstance(axis.get("center"), int) else None,
                    rest=axis.get("rest") if isinstance(axis.get("rest"), int) else None,
                    invert=bool(axis.get("invert", False)),
                )
                for axis in cast(list[dict[str, Any]], analog.get("axes", []))
                if axis.get("role") and axis.get("evdev")
            ]
            analog_inputs.append(
                AnalogInputDefinition(
                    id=analog["id"],
                    label=analog.get("label", analog["id"]),
                    type=analog.get("type", "stick"),
                    source=analog.get("source"),
                    axes=axes,
                )
            )

        motion_sensors: list[MotionSensorDefinition] = []
        for sensor in cast(list[dict[str, Any]], layout.get("motion_sensors", [])):
            driver = cast(str | None, sensor.get("driver"))
            calibration_version = int(sensor.get("calibration_version", 1))
            gyro_axes = self._load_motion_axes(sensor.get("gyro_axes", []))
            accelerometer_axes = self._load_motion_axes(sensor.get("accelerometer_axes", []))
            if calibration_version < MOTION_NORMALIZATION_VERSION:
                gyro_axes = self._canonicalize_motion_axes(gyro_axes, driver, "gyro")
                accelerometer_axes = self._canonicalize_motion_axes(
                    accelerometer_axes, driver, "accelerometer"
                )
                calibration_version = MOTION_NORMALIZATION_VERSION
            motion_sensors.append(
                MotionSensorDefinition(
                    id=str(sensor["id"]),
                    label=str(sensor.get("label", sensor["id"])),
                    source=cast(str | None, sensor.get("source")),
                    driver=driver,
                    gyro_axes=gyro_axes,
                    accelerometer_axes=accelerometer_axes,
                    calibration_version=calibration_version,
                    calibrated_at=cast(str | None, sensor.get("calibrated_at")),
                    calibration_samples=int(sensor.get("calibration_samples", 0)),
                )
            )

        return HardwareConfig(
            vendor_id=vendor_id,
            product_id=product_id,
            name=hw.get("name", model_id),
            evdev_devices=evdev_devices,
            buttons=buttons,
            analog_inputs=analog_inputs,
            motion_sensors=motion_sensors,
            image=hw.get("image"),
            id=hardware_id or None,
        )

    @staticmethod
    def _load_motion_axes(value: object) -> list[MotionAxisDefinition]:
        if not isinstance(value, list):
            return []
        axes: list[MotionAxisDefinition] = []
        for raw_item in cast(list[object], value):
            if not isinstance(raw_item, dict):
                continue
            item = cast(dict[str, object], raw_item)
            if not item.get("role") or not item.get("evdev"):
                continue
            raw_code = item.get("evdev_code")
            axes.append(
                MotionAxisDefinition(
                    role=str(item["role"]),
                    evdev=str(item["evdev"]),
                    evdev_code=raw_code if isinstance(raw_code, int) else None,
                    offset=coerce_float(item.get("offset"), 0.0),
                    scale=coerce_float(item.get("scale"), 1.0),
                    invert=bool(item.get("invert", False)),
                    noise=coerce_float(item.get("noise"), 0.0),
                )
            )
        return axes

    @staticmethod
    def _canonicalize_motion_axes(
        axes: list[MotionAxisDefinition],
        driver: str | None,
        kind: str,
    ) -> list[MotionAxisDefinition]:
        canonical_axes: list[MotionAxisDefinition] = []
        for axis in axes:
            canonical = canonical_motion_axis(driver, kind, axis.evdev)
            if canonical is None:
                canonical_axes.append(axis)
                continue
            role, invert = canonical
            canonical_axes.append(
                MotionAxisDefinition(
                    role=role,
                    evdev=axis.evdev,
                    evdev_code=axis.evdev_code,
                    offset=axis.offset,
                    scale=axis.scale,
                    invert=axis.invert != invert,
                    noise=axis.noise,
                )
            )
        return canonical_axes

    def _add_loaded_hardware(
        self,
        entry: _HardwareEntry,
        hardware: dict[str, _HardwareEntry],
    ) -> None:
        hardware_id = entry.config.hardware_id
        existing = hardware.get(hardware_id)
        if existing is None:
            hardware[hardware_id] = entry
            return

        selected = self._select_duplicate_hardware(existing, entry)
        ignored = entry if selected is existing else existing
        hardware[hardware_id] = selected
        log.warning(
            "Ignoring duplicate hardware_id '%s' from %s; using %s",
            hardware_id,
            ignored.path,
            selected.path,
        )

    def _select_duplicate_hardware(
        self,
        first: _HardwareEntry,
        second: _HardwareEntry,
    ) -> _HardwareEntry:
        hardware_id = first.config.hardware_id
        first_is_canonical = self._is_canonical_storage_path(hardware_id, first.path)
        second_is_canonical = self._is_canonical_storage_path(hardware_id, second.path)
        if first_is_canonical and not second_is_canonical:
            return first
        if second_is_canonical and not first_is_canonical:
            return second
        return first

    def get_hardware(self, hardware_id: str) -> HardwareConfig | None:
        entry = self._cache.get(hardware_id)
        return entry.config if entry is not None else None

    def snapshot_hardware(self) -> dict[str, _HardwareEntry]:
        return self._cache.copy()

    def restore_hardware(self, hardware: dict[str, _HardwareEntry]) -> None:
        self._cache = hardware.copy()

    def list_hardware(self) -> list[HardwareConfig]:
        return [
            self._cache[hardware_id].config
            for hardware_id in sorted(self._cache, key=_hardware_id_sort_key)
        ]

    def list_hardware_ids(self) -> list[str]:
        return sorted(self._cache, key=_hardware_id_sort_key)

    def _hardware_storage_path_candidate(self, hardware_id: str, attempt: int) -> Path:
        stem = _hardware_storage_stem(hardware_id)
        suffix = "" if attempt == 1 else f"_{attempt}"
        return paths.HARDWARE_DIR / f"{stem}{suffix}.toml"

    def _storage_file_hardware_id(self, path: Path) -> str:
        try:
            return self._load_config(path).hardware_id
        except (OSError, ValueError, KeyError) as exc:
            raise ValueError(
                f"Hardware storage path '{path.name}' already exists but could not be read"
            ) from exc
        except Exception as exc:
            log.exception("Unexpected error while reading hardware storage path %s", path)
            raise ValueError(
                f"Hardware storage path '{path.name}' already exists but could not be read"
            ) from exc

    def _storage_path_matches_hardware_id(self, path: Path, hardware_id: str) -> bool:
        try:
            return self._load_config(path).hardware_id == hardware_id
        except (OSError, ValueError, KeyError) as exc:
            log.debug(
                "Hardware storage path %s does not match %s: %s",
                path,
                hardware_id,
                exc,
            )
            return False
        except Exception:
            log.exception("Unexpected error while checking hardware storage path %s", path)
            return False

    def _path_for_hardware_id(self, hardware_id: str) -> tuple[Path, bool]:
        existing_entry = self._cache.get(hardware_id)
        if existing_entry is not None:
            if self._storage_path_matches_hardware_id(existing_entry.path, hardware_id):
                return existing_entry.path, False
            self._cache.pop(hardware_id, None)

        existing_path = self._existing_path_for_hardware_id(hardware_id)
        if existing_path is not None:
            return existing_path, False

        for attempt in range(1, MAX_HARDWARE_PATH_ATTEMPTS + 1):
            path = self._hardware_storage_path_candidate(hardware_id, attempt)
            if path.exists():
                continue
            return path, True
        raise ValueError(f"Could not allocate storage path for hardware '{hardware_id}'")

    def _is_canonical_storage_path(self, hardware_id: str, path: Path) -> bool:
        stem = _hardware_storage_stem(hardware_id)
        if not path.stem.startswith(stem):
            return False
        suffix = path.stem.removeprefix(stem)
        return not suffix or (suffix.startswith("_") and suffix[1:].isdigit())

    def _existing_path_for_hardware_id(self, hardware_id: str) -> Path | None:
        stem = _hardware_storage_stem(hardware_id)
        for path in sorted(paths.HARDWARE_DIR.glob(f"{stem}*.toml")):
            if not self._is_canonical_storage_path(hardware_id, path):
                continue
            if self._storage_file_hardware_id(path) == hardware_id:
                return path
        return None

    def save_hardware(self, config: HardwareConfig) -> None:
        paths.ensure_config_dirs()

        buttons_data: list[dict[str, object]] = []
        for btn in config.buttons:
            btn_data: dict[str, object] = {
                "id": btn.id,
                "label": btn.label,
                "evdev": btn.evdev,
            }
            if btn.evdev_code is not None:
                btn_data["evdev_code"] = btn.evdev_code
            if btn.evdev_value is not None:
                btn_data["evdev_value"] = btn.evdev_value
            if btn.source:
                btn_data["source"] = btn.source
            if btn.zone:
                btn_data["zone"] = btn.zone
            if btn.row is not None:
                btn_data["row"] = btn.row
            if btn.col is not None:
                btn_data["col"] = btn.col
            if btn.type:
                btn_data["type"] = btn.type
            buttons_data.append(btn_data)

        analogs_data: list[dict[str, object]] = []
        for analog in config.analog_inputs:
            analog_data: dict[str, object] = {
                "id": analog.id,
                "label": analog.label,
                "type": analog.type,
            }
            if analog.source:
                analog_data["source"] = analog.source
            axes_data: list[dict[str, object]] = []
            for axis in analog.axes:
                axis_data: dict[str, object] = {
                    "role": axis.role,
                    "evdev": axis.evdev,
                }
                if axis.evdev_code is not None:
                    axis_data["evdev_code"] = axis.evdev_code
                if axis.minimum is not None:
                    axis_data["minimum"] = axis.minimum
                if axis.maximum is not None:
                    axis_data["maximum"] = axis.maximum
                if axis.center is not None:
                    axis_data["center"] = axis.center
                if axis.rest is not None:
                    axis_data["rest"] = axis.rest
                if axis.invert:
                    axis_data["invert"] = True
                axes_data.append(axis_data)
            analog_data["axes"] = axes_data
            analogs_data.append(analog_data)

        motion_sensors_data: list[dict[str, object]] = []
        for sensor in config.motion_sensors:
            sensor_data: dict[str, object] = {
                "id": sensor.id,
                "label": sensor.label,
                "calibration_version": sensor.calibration_version,
            }
            if sensor.source:
                sensor_data["source"] = sensor.source
            if sensor.driver:
                sensor_data["driver"] = sensor.driver
            if sensor.calibrated_at:
                sensor_data["calibrated_at"] = sensor.calibrated_at
            if sensor.calibration_samples:
                sensor_data["calibration_samples"] = sensor.calibration_samples
            sensor_data["gyro_axes"] = self._dump_motion_axes(sensor.gyro_axes)
            sensor_data["accelerometer_axes"] = self._dump_motion_axes(sensor.accelerometer_axes)
            motion_sensors_data.append(sensor_data)

        evdev_devices_data: list[dict[str, object]] = []
        for d in config.evdev_devices:
            dev_data: dict[str, object] = {
                "path": d.path,
                "type": d.device_type.value,
            }
            if d.id:
                dev_data["id"] = d.id
            if d.phys:
                dev_data["phys"] = d.phys
            if d.capabilities:
                dev_data["capabilities"] = d.capabilities
            evdev_devices_data.append(dev_data)

        evdev_data = {"devices": evdev_devices_data}

        is_keyboard_layout = sum(1 for b in config.buttons if b.id.startswith("key_")) >= 40
        is_gamepad_layout = not is_keyboard_layout and any(
            is_gamepad_button_name(btn.evdev) or is_gamepad_button_name(btn.id)
            for btn in config.buttons
        )
        layout_type = (
            "keyboard" if is_keyboard_layout else "gamepad" if is_gamepad_layout else "mouse"
        )

        data: dict[str, dict[str, object]] = {
            "hardware": {
                "name": config.name,
                "vendor_id": config.vendor_id,
                "product_id": config.product_id,
                "evdev": evdev_data,
                "layout": {
                    "type": layout_type,
                    "buttons": buttons_data,
                    **({"analogs": analogs_data} if analogs_data else {}),
                    **({"motion_sensors": motion_sensors_data} if motion_sensors_data else {}),
                },
            }
        }

        if config.id:
            data["hardware"]["hardware_id"] = config.hardware_id

        if config.image:
            data["hardware"]["image"] = config.image

        path, new_path = self._path_for_hardware_id(config.hardware_id)

        def write_config(config_file: BinaryIO) -> None:
            tomli_w.dump(data, config_file)
            if is_keyboard_layout:
                config_file.write(KEYBOARD_LAYOUT_FOOTER)

        write_config_atomically(path, write_config, overwrite=not new_path)

        self._cache[config.hardware_id] = _HardwareEntry(path=path, config=config)
        log.info(f"Saved hardware config: {path}")

    @staticmethod
    def _dump_motion_axes(axes: list[MotionAxisDefinition]) -> list[dict[str, object]]:
        values: list[dict[str, object]] = []
        for axis in axes:
            value: dict[str, object] = {
                "role": axis.role,
                "evdev": axis.evdev,
                "offset": axis.offset,
                "scale": axis.scale,
            }
            if axis.evdev_code is not None:
                value["evdev_code"] = axis.evdev_code
            if axis.invert:
                value["invert"] = True
            if axis.noise:
                value["noise"] = axis.noise
            values.append(value)
        return values

    def delete_hardware(self, hardware_id: str) -> bool:
        entry = self._cache.get(hardware_id)
        if entry is None:
            return False

        path = entry.path

        if not path.exists():
            resolved_path = self._existing_path_for_hardware_id(hardware_id)
            if resolved_path is None:
                del self._cache[hardware_id]
                log.info(f"Dropped stale hardware cache entry: {hardware_id}")
                return False
            path = resolved_path

        if not self._storage_path_matches_hardware_id(path, hardware_id):
            del self._cache[hardware_id]
            return False
        try:
            path.unlink()
        except OSError as exc:
            log.error("Failed to delete %s: %s", path, exc)
            return False
        except Exception:
            log.exception("Unexpected error deleting hardware config %s", path)
            return False

        del self._cache[hardware_id]
        log.info(f"Deleted hardware config: {hardware_id}")
        return True
