import re
from collections.abc import Mapping
from typing import Any

from keymasq.common.devices import (
    get_interface_id,
    input_classes_include_gamepad,
    is_by_id_path,
    make_keymasq_device_path,
    normalize_input_classes,
    primary_input_class,
)
from keymasq.common.models import DeviceType


def device_search_text(hardware_id: str, dev_info: Mapping[str, Any]) -> str:
    interfaces = dev_info.get("interfaces", [])
    interface_text: list[str] = []
    if isinstance(interfaces, list):
        for iface in interfaces:
            if not isinstance(iface, dict):
                continue
            interface_text.extend(
                str(iface.get(key, "") or "")
                for key in (
                    "path",
                    "stable_path",
                    "config_path",
                    "phys",
                    "interface_id",
                    "device_type",
                )
            )
            interface_text.extend(str(t) for t in iface.get("device_types", []) or [])
    return " ".join(
        [
            hardware_id,
            str(dev_info.get("name", "") or ""),
            str(dev_info.get("display_name", "") or ""),
            str(dev_info.get("model_id", "") or ""),
            str(dev_info.get("vendor_id", "") or ""),
            str(dev_info.get("product_id", "") or ""),
            " ".join(str(t) for t in dev_info.get("device_types", []) or []),
            " ".join(interface_text),
        ]
    )


def strip_input_suffix(phys: str) -> str:
    return re.sub(r"/input\d+$", "", str(phys or "").strip())


def is_usb_phys(phys: str) -> bool:
    return str(phys or "").startswith("usb-")


def by_id_device_stem(stable_path: str) -> str:
    name = str(stable_path or "").rsplit("/", 1)[-1]
    name = re.sub(r"-event-[^-]+$", "", name)
    name = re.sub(r"-event$", "", name)
    name = re.sub(r"-(mouse|joystick|kbd)$", "", name)
    name = re.sub(r"-if\d+(?:_[^-]+)?$", "", name)
    return name


def fallback_interface_id_for_type(device_type: DeviceType) -> str:
    if device_type == DeviceType.GAMEPAD:
        return "gamepad"
    if device_type == DeviceType.KEYBOARD:
        return "kbd"
    if device_type == DeviceType.MOUSE:
        return "mouse"
    return "input"


def dedupe_interface_id(base_id: str, used_ids: set[str]) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_]+", "_", str(base_id or "").strip().lower()).strip("_")
    candidate = clean or "input"
    if candidate not in used_ids:
        used_ids.add(candidate)
        return candidate
    index = 2
    while f"{candidate}_{index}" in used_ids:
        index += 1
    deduped = f"{candidate}_{index}"
    used_ids.add(deduped)
    return deduped


def interface_id_for_config(iface: Mapping[str, Any], used_ids: set[str]) -> str:
    stable_path = str(iface.get("stable_path", "") or "")
    config_path = str(iface.get("config_path", "") or "")
    if is_by_id_path(stable_path) or is_by_id_path(config_path):
        by_id_path = stable_path if is_by_id_path(stable_path) else config_path
        return dedupe_interface_id(get_interface_id(by_id_path), used_ids)
    return dedupe_interface_id(
        fallback_interface_id_for_type(primary_input_class(iface.get("device_types"))),
        used_ids,
    )


def config_path_for_detected_interface(
    vendor_id: str,
    product_id: str,
    stable_path: str,
) -> str:
    if is_by_id_path(stable_path):
        return stable_path
    return make_keymasq_device_path(vendor_id, product_id)


def interface_source_fields(dev: Mapping[str, Any]) -> dict[str, object]:
    fields: dict[str, object] = {}
    if bool(dev.get("grabbed_by_keymasq", False)):
        fields["grabbed_by_keymasq"] = True
    for key in (
        "source_hardware_id",
        "source_interface_id",
        "source_stable_path",
        "source_path",
    ):
        value = str(dev.get(key, "") or "")
        if value:
            fields[key] = value
    return fields


def in_use_row_key(hardware_id: str, path: str, stable_path: str) -> str:
    suffix = path or stable_path or "in-use"
    return f"{hardware_id}#{suffix}"


def raw_row_key(path: str, stable_path: str) -> str:
    return f"raw:{stable_path or path}"


def logical_hardware_identity_key(
    *,
    model_id: str,
    device_types: list[str],
    stable_path: str,
    phys: str = "",
    path: str = "",
    config_path: str = "",
) -> str:
    normalized_types = normalize_input_classes(device_types)
    stable_key = str(stable_path or "").strip()
    config_key = str(config_path or "").strip()
    identity_path = stable_key
    # Preserve a durable by-id config identity when live udev only exposes eventN.
    if not is_by_id_path(identity_path) and is_by_id_path(config_key):
        identity_path = config_key
    if input_classes_include_gamepad(normalized_types) and is_by_id_path(identity_path):
        return f"path:{identity_path}"
    if is_by_id_path(identity_path):
        return f"by-id:{by_id_device_stem(identity_path)}"
    phys_key = str(phys or "").strip()
    phys_base = strip_input_suffix(phys_key)
    if phys_base == "py-evdev-uinput":
        return f"uinput-model:{model_id}"
    if phys_base and not is_usb_phys(phys_base):
        return f"phys:{phys_base}"
    path_key = str(stable_key or path or "").strip()
    if path_key:
        return f"path:{path_key}"
    if config_key and not config_key.startswith("keymasq:"):
        return f"path:{config_key}"
    return f"model:{model_id}"
