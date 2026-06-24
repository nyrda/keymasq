from typing import Any, cast

from keymasq.common.devices import normalize_input_classes, primary_input_class
from keymasq.gui.session_client import session_request
from keymasq.gui.wizards.hardware_setup import inventory
from keymasq.gui.wizards.hardware_setup.identity import (
    config_path_for_detected_interface,
    in_use_row_key,
    interface_source_fields,
    logical_hardware_identity_key,
    raw_row_key,
)


def detected_identity_key(
    *,
    show_raw_evdev_devices: bool,
    model_id: str,
    device_types: list[str],
    stable_path: str,
    phys: str = "",
    path: str = "",
    config_path: str = "",
) -> str:
    if show_raw_evdev_devices:
        return f"raw:{stable_path or path}"
    return logical_hardware_identity_key(
        model_id=model_id,
        device_types=device_types,
        stable_path=stable_path,
        phys=phys,
        path=path,
        config_path=config_path,
    )


def should_skip_detected_device_info(device_info: dict[str, Any]) -> bool:
    name = str(device_info.get("name", "") or "").strip().lower()
    recording_kind = str(device_info.get("recording_kind", "") or "").strip().lower()

    if "keymasq" in name:
        return True
    if recording_kind in {"keymasq_output", "keymasq_passthrough"}:
        return True

    return False


def should_include_detected_interface(
    device_types: list[str],
    *,
    show_raw_evdev_devices: bool,
) -> bool:
    if show_raw_evdev_devices:
        return True
    return "touchpad" not in normalize_input_classes(device_types)


def allocate_hardware_id(model_id: str, used_hardware_ids: set[str]) -> str:
    if model_id not in used_hardware_ids:
        return model_id

    index = 2
    while True:
        candidate = f"{model_id}@{index}"
        if candidate not in used_hardware_ids:
            return candidate
        index += 1


def detect_devices_via_session(
    detected_devices: dict[str, dict],
    *,
    hardware_manager: object,
    show_raw_evdev_devices: bool,
) -> bool:
    result = session_request(
        {
            "command": "list_devices_for_recording",
            "include_other": show_raw_evdev_devices,
        },
        timeout=3.0,
    ) or {}
    if result.get("status") != "ok":
        return False

    used_hardware_ids = inventory.configured_hardware_ids(hardware_manager)
    configured_identity_hardware_ids = inventory.configured_identity_hardware_ids(
        hardware_manager
    )
    pending_identity_hardware_ids: dict[str, str] = {}
    has_config_inventory = callable(getattr(hardware_manager, "list_hardware", None))

    raw_devices = result.get("devices", [])
    session_devices: list[dict[str, Any]] = [
        cast(dict[str, Any], dev)
        for dev in raw_devices
        if isinstance(dev, dict)
    ]
    session_devices.sort(
        key=lambda dev: (
            str(dev.get("vendor_id", "") or "").lower(),
            str(dev.get("product_id", "") or "").lower(),
            str(dev.get("stable_path", "") or dev.get("path", "") or ""),
        )
    )

    for dev in session_devices:
        if should_skip_detected_device_info(dev):
            continue

        vendor_id = str(dev.get("vendor_id", "") or "").lower()
        product_id = str(dev.get("product_id", "") or "").lower()
        if not vendor_id or not product_id:
            continue

        vid_pid = f"{vendor_id}:{product_id}"
        path = str(dev.get("path", "") or "")
        name = str(dev.get("name", "") or path or vid_pid)
        dtype_raw = str(dev.get("device_type", "other") or "other")
        dtype = primary_input_class(dev.get("device_types") or [dtype_raw])
        device_types = normalize_input_classes(dev.get("device_types"), dtype_raw)
        if not should_include_detected_interface(
            device_types,
            show_raw_evdev_devices=show_raw_evdev_devices,
        ):
            continue
        stable_path = str(dev.get("stable_path", "") or path)
        config_path = config_path_for_detected_interface(
            vendor_id,
            product_id,
            stable_path,
        )
        phys = str(dev.get("phys", "") or "")
        identity_key = detected_identity_key(
            show_raw_evdev_devices=show_raw_evdev_devices,
            model_id=vid_pid,
            device_types=device_types,
            stable_path=stable_path,
            phys=phys,
            path=path,
            config_path=config_path,
        )
        configured_hardware_id = configured_identity_hardware_ids.get(identity_key, "")
        if not show_raw_evdev_devices and (
            configured_hardware_id
            or (
                not has_config_inventory
                and inventory.hardware_config_exists(hardware_manager, vid_pid)
            )
        ):
            continue
        source_fields = interface_source_fields(dev)
        is_grabbed = bool(source_fields.get("grabbed_by_keymasq", False))
        source_hardware_id = str(source_fields.get("source_hardware_id", "") or "")
        if show_raw_evdev_devices and is_grabbed and source_hardware_id:
            hardware_id = source_hardware_id
            device_key = in_use_row_key(hardware_id, path, stable_path)
            configured_fields: dict[str, object] = {}
        elif show_raw_evdev_devices and configured_hardware_id:
            hardware_id = configured_hardware_id
            device_key = in_use_row_key(hardware_id, path, stable_path)
            configured_fields = {"configured_hardware_id": hardware_id}
        elif show_raw_evdev_devices:
            hardware_id = vid_pid
            device_key = raw_row_key(path, stable_path)
            configured_fields = {}
        else:
            hardware_id = pending_identity_hardware_ids.get(identity_key)
            if hardware_id is None:
                hardware_id = allocate_hardware_id(vid_pid, used_hardware_ids)
                used_hardware_ids.add(hardware_id)
                pending_identity_hardware_ids[identity_key] = hardware_id
            device_key = hardware_id
            configured_fields = {}

        if device_key not in detected_devices:
            detected_devices[device_key] = {
                "name": name,
                "display_name": name,
                "hardware_id": hardware_id,
                "model_id": vid_pid,
                "vendor_id": vendor_id,
                "product_id": product_id,
                "paths": [path] if path else [],
                "interfaces": [
                    {
                        "path": path,
                        "stable_path": stable_path,
                        "name": name,
                        "phys": phys,
                        "device_type": dtype,
                        "device_types": device_types,
                        **source_fields,
                        **configured_fields,
                    }
                ]
                if path
                else [],
            }
        else:
            if path:
                detected_devices[device_key]["paths"].append(path)
                detected_devices[device_key]["interfaces"].append(
                    {
                        "path": path,
                        "stable_path": stable_path,
                        "name": name,
                        "phys": phys,
                        "device_type": dtype,
                        "device_types": device_types,
                        **source_fields,
                        **configured_fields,
                    }
                )

    return bool(detected_devices)
