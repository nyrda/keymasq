import logging
import tomllib
from typing import cast

from keymasq.common import paths
from keymasq.common.config_files import write_toml_atomically
from keymasq.common.virtual_device_templates import (
    VirtualDeviceConfig,
    virtual_device_config_from_toml,
    virtual_device_config_to_toml,
)

log = logging.getLogger("keymasq-session.virtual-devices")


def load_virtual_device_config(*, strict: bool = False) -> VirtualDeviceConfig:
    path = paths.VIRTUAL_DEVICES_PATH
    if not path.exists():
        return VirtualDeviceConfig()
    try:
        with path.open("rb") as config_file:
            data = cast(dict[str, object], tomllib.load(config_file))
        return virtual_device_config_from_toml(data)
    except Exception as exc:
        if strict:
            raise
        log.warning("Failed to load virtual devices from %s: %s; using none", path, exc)
        return VirtualDeviceConfig()


def save_virtual_device_config(config: VirtualDeviceConfig) -> VirtualDeviceConfig:
    paths.ensure_config_dirs()
    write_toml_atomically(paths.VIRTUAL_DEVICES_PATH, virtual_device_config_to_toml(config))
    return config
