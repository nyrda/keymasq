import logging
import os
from typing import cast

import evdev

from keymasq.common.devices import is_by_id_path, resolve_stable_path
from keymasq.gui.wizards.hardware_setup.identity import logical_hardware_identity_key

log = logging.getLogger("keymasq.gui.hardware_setup.inventory")


def hardware_config_exists(hardware_manager: object, hardware_id: str) -> bool:
    getter = getattr(hardware_manager, "get_hardware", None)
    if callable(getter):
        return getter(hardware_id) is not None

    list_ids = getattr(hardware_manager, "list_hardware_ids", None)
    if callable(list_ids):
        configured_ids = list_ids()
        if isinstance(configured_ids, list):
            return hardware_id in [str(item) for item in configured_ids]

    return False


def configured_hardware_ids(hardware_manager: object) -> set[str]:
    list_ids = getattr(hardware_manager, "list_hardware_ids", None)
    if callable(list_ids):
        try:
            configured_ids = list_ids()
        except Exception:
            log.exception("Unable to list configured hardware IDs")
            configured_ids = []
        if isinstance(configured_ids, list):
            return {str(item) for item in configured_ids}

    list_hardware = getattr(hardware_manager, "list_hardware", None)
    if callable(list_hardware):
        try:
            return {
                str(getattr(config, "hardware_id", "") or "")
                for config in cast(list[object], list_hardware())
                if str(getattr(config, "hardware_id", "") or "")
            }
        except Exception:
            log.exception("Unable to list configured hardware")
            return set()

    return set()


def configured_identity_hardware_ids(hardware_manager: object) -> dict[str, str]:
    list_hardware = getattr(hardware_manager, "list_hardware", None)
    if not callable(list_hardware):
        return {}
    try:
        configs = cast(list[object], list_hardware())
    except Exception:
        log.exception("Unable to list configured hardware identity keys")
        return {}

    keys: dict[str, str] = {}
    for config in configs:
        model_id = str(getattr(config, "model_id", "") or "")
        if not model_id:
            continue
        hardware_id = str(getattr(config, "hardware_id", "") or model_id)
        for device in getattr(config, "evdev_devices", []):
            path = str(getattr(device, "path", "") or "")
            if not path:
                continue
            device_type = getattr(getattr(device, "device_type", None), "value", None)
            device_types = [str(device_type or "other")]
            for key in configured_raw_identity_keys(path):
                keys.setdefault(key, hardware_id)
            keys.setdefault(
                logical_hardware_identity_key(
                    model_id=model_id,
                    device_types=device_types,
                    stable_path=configured_device_stable_path(path),
                    phys=configured_device_phys(device),
                    path=path,
                    config_path=path,
                ),
                hardware_id,
            )
    return keys


def configured_raw_identity_keys(path: str) -> set[str]:
    candidates = {str(path or "")}
    stable_path = configured_device_stable_path(path)
    if stable_path:
        candidates.add(stable_path)
    try:
        real_path = os.path.realpath(path)
    except OSError:
        real_path = ""
    if real_path:
        candidates.add(real_path)
    return {f"raw:{candidate}" for candidate in candidates if candidate}


def configured_device_stable_path(path: str) -> str:
    path = str(path or "")
    if not path:
        return ""
    candidates = [path]
    try:
        real_path = os.path.realpath(path)
    except OSError:
        real_path = ""
    if real_path and real_path != path:
        candidates.append(real_path)

    for candidate in candidates:
        try:
            stable_path = resolve_stable_path(candidate)
        except (OSError, RuntimeError, ValueError) as exc:
            log.debug("Unable to resolve configured device path %s: %s", candidate, exc)
            stable_path = ""
        if stable_path and is_by_id_path(stable_path):
            return stable_path
    return path


def configured_device_phys(device: object) -> str:
    phys = str(getattr(device, "phys", "") or "")
    if phys:
        return phys

    path = str(getattr(device, "path", "") or "")
    if not path:
        return ""

    try:
        input_device = evdev.InputDevice(path)
    except (OSError, RuntimeError) as exc:
        log.debug("Unable to read configured input device %s: %s", path, exc)
        return ""
    try:
        return str(getattr(input_device, "phys", "") or "")
    finally:
        try:
            input_device.close()
        except (OSError, RuntimeError) as exc:
            log.debug("Failed to close configured input device %s: %s", path, exc)
