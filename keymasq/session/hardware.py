import contextlib
import logging
import re
import tomllib
from pathlib import Path
from typing import Any, cast

import tomli_w

from keymasq.common import paths
from keymasq.common.devices import is_gamepad_button_name
from keymasq.common.models import (
    AnalogAxisDefinition,
    AnalogInputDefinition,
    ButtonDefinition,
    DeviceType,
    EvdevDevice,
    HardwareConfig,
)
from keymasq.session.config_errors import ConfigLoadError, ConfigLoadFailure

log = logging.getLogger("keymasq-session.hardware")
MAX_HARDWARE_PATH_ATTEMPTS = 10000


def _valid_hardware_id_for_model(hardware_id: str, model_id: str) -> bool:
    return hardware_id == model_id or hardware_id.startswith(f"{model_id}@")


def _hardware_storage_stem(hardware_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", hardware_id).strip("._")
    return (safe or "hardware").lower()


class HardwareManager:
    def __init__(self) -> None:
        paths.ensure_config_dirs()
        self._cache: dict[str, HardwareConfig] = {}
        self._load_all()

    def _load_all(self, *, strict: bool = False) -> None:
        loaded_cache: dict[str, HardwareConfig] = {}
        failures: list[ConfigLoadFailure] = []

        if not paths.HARDWARE_DIR.exists():
            self._cache = loaded_cache
            return

        for config_file in paths.HARDWARE_DIR.glob("*.toml"):
            try:
                config = self._load_config(config_file)
                loaded_cache[config.hardware_id] = config
            except Exception as e:
                log.error(f"Failed to load {config_file}: {e}")
                failures.append(ConfigLoadFailure(config_file, str(e)))

        if strict and failures:
            raise ConfigLoadError("hardware", failures)

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

        return HardwareConfig(
            vendor_id=vendor_id,
            product_id=product_id,
            name=hw.get("name", model_id),
            evdev_devices=evdev_devices,
            buttons=buttons,
            analog_inputs=analog_inputs,
            image=hw.get("image"),
            id=hardware_id or None,
        )

    def get_hardware(self, hardware_id: str) -> HardwareConfig | None:
        return self._cache.get(hardware_id)

    def snapshot_hardware(self) -> dict[str, HardwareConfig]:
        return self._cache.copy()

    def restore_hardware(self, hardware: dict[str, HardwareConfig]) -> None:
        self._cache = hardware.copy()

    def list_hardware(self) -> list[HardwareConfig]:
        return list(self._cache.values())

    def list_hardware_ids(self) -> list[str]:
        return list(self._cache.keys())

    def _hardware_storage_path_candidate(self, hardware_id: str, attempt: int) -> Path:
        stem = _hardware_storage_stem(hardware_id)
        suffix = "" if attempt == 1 else f"_{attempt}"
        return paths.HARDWARE_DIR / f"{stem}{suffix}.toml"

    def _storage_file_hardware_id(self, path: Path) -> str:
        try:
            return self._load_config(path).hardware_id
        except Exception as exc:
            raise ValueError(
                f"Hardware storage path '{path.name}' already exists but could not be read"
            ) from exc

    def _path_for_hardware_id(self, hardware_id: str) -> tuple[Path, bool]:
        existing_path = self._existing_path_for_hardware_id(hardware_id)
        if existing_path is not None:
            return existing_path, False

        for attempt in range(1, MAX_HARDWARE_PATH_ATTEMPTS + 1):
            path = self._hardware_storage_path_candidate(hardware_id, attempt)
            try:
                with path.open("x", encoding="utf-8"):
                    pass
            except FileExistsError:
                continue
            return path, True
        raise ValueError(f"Could not allocate storage path for hardware '{hardware_id}'")

    def _existing_path_for_hardware_id(self, hardware_id: str) -> Path | None:
        stem = _hardware_storage_stem(hardware_id)
        for path in sorted(paths.HARDWARE_DIR.glob(f"{stem}*.toml")):
            suffix = path.stem.removeprefix(stem)
            if suffix and not (suffix.startswith("_") and suffix[1:].isdigit()):
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
                },
            }
        }

        if config.id:
            data["hardware"]["hardware_id"] = config.hardware_id

        if config.image:
            data["hardware"]["image"] = config.image

        path, reserved_path = self._path_for_hardware_id(config.hardware_id)
        try:
            with open(path, "wb") as f:
                tomli_w.dump(data, f)

            if is_keyboard_layout:
                with open(path, "a", encoding="utf-8") as f:
                    f.write("\n")
                    f.write("# Optional special keys (not shown in GUI by default)\n")
                    f.write("# Add entries below into [hardware.layout.buttons] if needed\n")
                    f.write("# Example entries:\n")
                    f.write(
                        '# { id = "key_volumedown", label = "Volume Down", '
                        'evdev = "key_volumedown", type = "key" }\n'
                    )
                    f.write(
                        '# { id = "key_volumeup", label = "Volume Up", '
                        'evdev = "key_volumeup", type = "key" }\n'
                    )
                    f.write(
                        '# { id = "key_mute", label = "Mute", evdev = "key_mute", type = "key" }\n'
                    )
                    f.write(
                        '# { id = "key_playpause", label = "Play Pause", '
                        'evdev = "key_playpause", type = "key" }\n'
                    )
        except Exception:
            if reserved_path:
                with contextlib.suppress(OSError):
                    path.unlink()
            raise

        self._cache[config.hardware_id] = config
        log.info(f"Saved hardware config: {path}")

    def delete_hardware(self, hardware_id: str) -> bool:
        if hardware_id not in self._cache:
            return False

        path = self._existing_path_for_hardware_id(hardware_id)

        if path is not None and path.exists():
            try:
                path.unlink()
            except Exception as e:
                log.error(f"Failed to delete {path}: {e}")
                return False

        del self._cache[hardware_id]
        log.info(f"Deleted hardware config: {hardware_id}")
        return True
