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
    strip_input_suffix,
)
from keymasq.gui.wizards.hardware_setup.types import DetectedDevice, DetectedInterface


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


def _session_device_sort_key(
    dev: dict[str, Any],
    *,
    motion_siblings_last: bool,
) -> tuple[str, str, bool, str]:
    dtype_raw = str(dev.get("device_type", "other") or "other")
    device_types = normalize_input_classes(dev.get("device_types"), dtype_raw)
    motion_only = (
        motion_siblings_last
        and "motion" in device_types
        and "gamepad" not in device_types
    )
    return (
        str(dev.get("vendor_id", "") or "").lower(),
        str(dev.get("product_id", "") or "").lower(),
        motion_only,
        str(dev.get("stable_path", "") or dev.get("path", "") or ""),
    )


def detect_devices_via_session(
    detected_devices: dict[str, DetectedDevice],
    *,
    hardware_manager: object,
    show_raw_evdev_devices: bool,
) -> bool:
    result = (
        session_request(
            {
                "command": "list_devices_for_recording",
                "include_other": show_raw_evdev_devices,
                "include_motion": True,
            },
            timeout=3.0,
        )
        or {}
    )
    if result.get("status") != "ok":
        return False

    used_hardware_ids = inventory.configured_hardware_ids(hardware_manager)
    configured_identity_hardware_ids = inventory.configured_identity_hardware_ids(hardware_manager)
    pending_identity_hardware_ids: dict[str, str] = {}
    has_config_inventory = callable(getattr(hardware_manager, "list_hardware", None))

    raw_devices = result.get("devices", [])
    session_devices: list[dict[str, Any]] = [
        cast(dict[str, Any], dev) for dev in raw_devices if isinstance(dev, dict)
    ]
    session_devices.sort(
        key=lambda dev: _session_device_sort_key(
            dev,
            motion_siblings_last=not show_raw_evdev_devices,
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
            detected_devices[device_key] = cast(
                DetectedDevice,
                {
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
                            **(
                                {"capabilities": list(dev.get("capabilities", []) or [])}
                                if dev.get("capabilities")
                                else {}
                            ),
                            **(
                                {"abs_info": dict(dev.get("abs_info", {}) or {})}
                                if dev.get("abs_info")
                                else {}
                            ),
                            **(
                                {"driver": str(dev.get("driver", "") or "")}
                                if dev.get("driver")
                                else {}
                            ),
                            **source_fields,
                            **configured_fields,
                        }
                    ]
                    if path
                    else [],
                },
            )
        else:
            if path:
                device_info = detected_devices[device_key]
                paths = device_info.get("paths")
                if paths is None:
                    paths = []
                    device_info["paths"] = paths
                paths.append(path)

                interfaces = device_info.get("interfaces")
                if interfaces is None:
                    interfaces = []
                    device_info["interfaces"] = interfaces
                interfaces.append(
                    cast(
                        DetectedInterface,
                        {
                            "path": path,
                            "stable_path": stable_path,
                            "name": name,
                            "phys": phys,
                            "device_type": dtype,
                            "device_types": device_types,
                            **(
                                {"capabilities": list(dev.get("capabilities", []) or [])}
                                if dev.get("capabilities")
                                else {}
                            ),
                            **(
                                {"abs_info": dict(dev.get("abs_info", {}) or {})}
                                if dev.get("abs_info")
                                else {}
                            ),
                            **(
                                {"driver": str(dev.get("driver", "") or "")}
                                if dev.get("driver")
                                else {}
                            ),
                            **source_fields,
                            **configured_fields,
                        },
                    )
                )

    if not show_raw_evdev_devices:
        _attach_motion_siblings(detected_devices)
    return bool(detected_devices)


def _attach_motion_siblings(detected_devices: dict[str, DetectedDevice]) -> None:
    """Attach a controller's separate motion evdev node to its gamepad row."""
    for motion_key, motion_device in list(detected_devices.items()):
        motion_interfaces = list(motion_device.get("interfaces", []) or [])
        if not motion_interfaces or any(
            "gamepad" in normalize_input_classes(iface.get("device_types"))
            for iface in motion_interfaces
        ):
            continue
        if not any(
            "motion" in normalize_input_classes(iface.get("device_types"))
            for iface in motion_interfaces
        ):
            continue
        motion_phys = {
            strip_input_suffix(str(iface.get("phys", "") or ""))
            for iface in motion_interfaces
            if iface.get("phys")
        }
        candidates: list[DetectedDevice] = []
        for key, candidate in detected_devices.items():
            if key == motion_key or candidate.get("model_id") != motion_device.get("model_id"):
                continue
            candidate_interfaces = list(candidate.get("interfaces", []) or [])
            if not any(
                "gamepad" in normalize_input_classes(iface.get("device_types"))
                for iface in candidate_interfaces
            ):
                continue
            candidate_phys = {
                strip_input_suffix(str(iface.get("phys", "") or ""))
                for iface in candidate_interfaces
                if iface.get("phys")
            }
            if motion_phys and motion_phys.intersection(candidate_phys):
                candidates.append(candidate)
        if len(candidates) != 1:
            continue
        target = candidates[0]
        target.setdefault("interfaces", []).extend(motion_interfaces)
        target.setdefault("paths", []).extend(list(motion_device.get("paths", []) or []))
        detected_devices.pop(motion_key, None)
