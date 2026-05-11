import logging
import tomllib
from pathlib import Path
from typing import Any, cast

import tomli_w

from keymasq.common import paths
from keymasq.common.devices import is_gamepad_button_name
from keymasq.common.models import (
    ButtonDefinition,
    DeviceType,
    EvdevDevice,
    HardwareConfig,
)

log = logging.getLogger("keymasq-session.hardware")


class HardwareManager:
    def __init__(self) -> None:
        paths.ensure_config_dirs()
        self._cache: dict[str, HardwareConfig] = {}
        self._load_all()

    def _load_all(self) -> None:
        if not paths.HARDWARE_DIR.exists():
            return

        for config_file in paths.HARDWARE_DIR.glob("*.toml"):
            try:
                config = self._load_config(config_file)
                self._cache[config.hardware_id] = config
            except Exception as e:
                log.error(f"Failed to load {config_file}: {e}")

    def reload(self) -> None:
        self._cache.clear()
        self._load_all()

    def _load_config(self, path: Path) -> HardwareConfig:
        with open(path, "rb") as f:
            data = tomllib.load(f)

        hw = cast(dict[str, Any], data["hardware"])

        evdev_devices: list[EvdevDevice] = []
        evdev_config = cast(dict[str, Any], hw.get("evdev", {}))
        for dev in cast(list[dict[str, Any]], evdev_config.get("devices", [])):
            evdev_devices.append(
                EvdevDevice(
                    path=dev["path"],
                    device_type=DeviceType(dev.get("type", "other")),
                    id=dev.get("id"),
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

        return HardwareConfig(
            vendor_id=hw["vendor_id"],
            product_id=hw["product_id"],
            name=hw.get("name", f"{hw['vendor_id']}:{hw['product_id']}"),
            evdev_devices=evdev_devices,
            buttons=buttons,
            image=hw.get("image"),
        )

    def get_hardware(self, hardware_id: str) -> HardwareConfig | None:
        return self._cache.get(hardware_id)

    def list_hardware(self) -> list[HardwareConfig]:
        return list(self._cache.values())

    def list_hardware_ids(self) -> list[str]:
        return list(self._cache.keys())

    def save_hardware(self, config: HardwareConfig) -> None:
        paths.ensure_config_dirs()

        path = paths.HARDWARE_DIR / f"{config.hardware_id.replace(':', '_')}.toml"

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

        evdev_devices_data: list[dict[str, object]] = []
        for d in config.evdev_devices:
            dev_data: dict[str, object] = {
                "path": d.path,
                "type": d.device_type.value,
            }
            if d.id:
                dev_data["id"] = d.id
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
                },
            }
        }

        if config.image:
            data["hardware"]["image"] = config.image

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
                f.write('# { id = "key_mute", label = "Mute", evdev = "key_mute", type = "key" }\n')
                f.write(
                    '# { id = "key_playpause", label = "Play Pause", '
                    'evdev = "key_playpause", type = "key" }\n'
                )

        self._cache[config.hardware_id] = config
        log.info(f"Saved hardware config: {path}")

    def delete_hardware(self, hardware_id: str) -> bool:
        if hardware_id not in self._cache:
            return False

        path = paths.HARDWARE_DIR / f"{hardware_id.replace(':', '_')}.toml"

        if path.exists():
            try:
                path.unlink()
            except Exception as e:
                log.error(f"Failed to delete {path}: {e}")
                return False

        del self._cache[hardware_id]
        log.info(f"Deleted hardware config: {hardware_id}")
        return True
