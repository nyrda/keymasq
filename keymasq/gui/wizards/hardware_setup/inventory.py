import logging
import os

import evdev

from keymasq.common.devices import is_by_id_path, resolve_stable_path
from keymasq.common.model.hardware import EvdevDevice
from keymasq.gui.wizards.hardware_setup.identity import logical_hardware_identity_key
from keymasq.session.hardware import HardwareManager

log = logging.getLogger("keymasq.gui.hardware_setup.inventory")


def configured_hardware_ids(hardware_manager: HardwareManager) -> set[str]:
    return set(hardware_manager.list_hardware_ids())


def configured_identity_hardware_ids(hardware_manager: HardwareManager) -> dict[str, str]:
    configs = hardware_manager.list_hardware()

    keys: dict[str, str] = {}
    for config in configs:
        model_id = config.model_id
        hardware_id = config.hardware_id
        for device in config.evdev_devices:
            path = device.path
            if not path:
                continue
            device_types = [device.device_type.value]
            for key in configured_raw_identity_keys(path):
                keys.setdefault(key, hardware_id)
            stable_path = configured_device_stable_path(path)
            phys = "" if is_by_id_path(stable_path) else configured_device_phys(device)
            keys.setdefault(
                logical_hardware_identity_key(
                    model_id=model_id,
                    device_types=device_types,
                    stable_path=stable_path,
                    phys=phys,
                    path=path,
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


def configured_device_phys(device: EvdevDevice) -> str:
    phys = device.phys
    if phys:
        return phys

    path = device.path
    if not path:
        return ""

    try:
        input_device = evdev.InputDevice(path)
    except (OSError, RuntimeError) as exc:
        log.debug("Unable to read configured input device %s: %s", path, exc)
        return ""
    try:
        return input_device.phys or ""
    finally:
        try:
            input_device.close()
        except (OSError, RuntimeError) as exc:
            log.debug("Failed to close configured input device %s: %s", path, exc)
