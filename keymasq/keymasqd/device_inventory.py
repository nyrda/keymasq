from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from keymasq.common.coercion import coerce_str
from keymasq.common.model.core import DeviceType
from keymasq.common.types import JsonObject


class InventoryDeviceInfo(Protocol):
    vendor: int
    product: int


class InventoryDevice(Protocol):
    path: str
    name: str | None
    info: InventoryDeviceInfo

    def capabilities(self, *, absinfo: bool = False) -> dict[int, Sequence[object]]: ...

    def close(self) -> None: ...


class LiveInterface(Protocol):
    hardware_id: str
    vendor_id: str
    product_id: str
    stable_path: str
    path: str
    interface_id: str
    phys: str
    device_type: str
    capabilities: Sequence[str]


@dataclass(frozen=True)
class InventoryScanDeps:
    clear_path_cache: Callable[[], None]
    device_paths: Callable[[], list[str]]
    open_device: Callable[[str], InventoryDevice]
    resolve_stable_path: Callable[[str], str]
    get_interface_id: Callable[[str], str]
    detect_device_types: Callable[[InventoryDevice], list[str]]
    primary_input_class: Callable[[Sequence[str]], DeviceType]
    evdev_mod: Any
    is_permission_error: Callable[[BaseException], bool]
    permission_message: Callable[[str], str]
    logger: logging.Logger


def uinput_device_path(uinput_dev: object | None) -> str | None:
    input_device = getattr(uinput_dev, "device", None)
    path = getattr(input_device, "path", None)
    return path if isinstance(path, str) and path else None


def is_virtual_input(device: object) -> bool:
    phys = str(getattr(device, "phys", "") or "").lower()
    name = str(getattr(device, "name", "") or "").lower()
    return phys == "py-evdev-uinput" or name.startswith("keymasq-")


def recording_virtual_device_metadata(
    output_state: object,
    grabbed_devices: Mapping[str, Sequence[object]],
) -> dict[str, JsonObject]:
    metadata: dict[str, JsonObject] = {}
    output_devices = {
        "keyboard": getattr(output_state, "keyboard_uinput", None),
        "mouse": getattr(output_state, "mouse_uinput", None),
    }
    for output_class, uinput_dev in output_devices.items():
        path = uinput_device_path(uinput_dev)
        if path:
            metadata[path] = {
                "recording_id": f"keymasq:output:{output_class}",
                "recording_kind": "keymasq_output",
                "keymasq_output": output_class,
            }

    raw_gamepads = getattr(output_state, "virtual_gamepad_uinputs", {})
    gamepads = cast(dict[str, object], raw_gamepads) if isinstance(raw_gamepads, dict) else {}
    for output_id, uinput_dev in sorted(gamepads.items()):
        path = uinput_device_path(uinput_dev)
        if not path:
            continue
        recording_id = (
            "keymasq:output:gamepad"
            if output_id == "virtual-gamepad-1"
            else f"keymasq:output:gamepad:{output_id}"
        )
        metadata[path] = {
            "recording_id": recording_id,
            "recording_kind": "keymasq_output",
            "keymasq_output": "gamepad",
            "keymasq_output_id": str(output_id),
        }

    for devices in grabbed_devices.values():
        for grabbed in devices:
            path = uinput_device_path(getattr(grabbed, "uinput", None))
            if not path:
                continue
            hardware_id = str(getattr(grabbed, "hardware_id", "") or "")
            interface_id = str(getattr(grabbed, "interface_id", "") or "")
            metadata[path] = {
                "recording_id": f"keymasq:passthrough:{hardware_id}:{interface_id}",
                "recording_kind": "keymasq_passthrough",
                "source_hardware_id": hardware_id,
                "source_interface_id": interface_id,
                "source_stable_path": str(getattr(grabbed, "stable_path", "") or ""),
                "source_path": str(getattr(grabbed, "path", "") or ""),
            }
    return metadata


def recording_grabbed_source_metadata(
    grabbed_devices: Mapping[str, Sequence[object]],
) -> dict[str, JsonObject]:
    metadata: dict[str, JsonObject] = {}
    for devices in grabbed_devices.values():
        for grabbed in devices:
            stable_path = str(getattr(grabbed, "stable_path", "") or "")
            if stable_path:
                metadata[stable_path] = {
                    "source_hardware_id": str(getattr(grabbed, "hardware_id", "") or ""),
                    "source_interface_id": str(getattr(grabbed, "interface_id", "") or ""),
                }
    return metadata


def scan_devices(
    deps: InventoryScanDeps,
    *,
    virtual_metadata: Mapping[str, JsonObject],
    grabbed_metadata: Mapping[str, JsonObject],
) -> JsonObject:
    deps.clear_path_cache()
    devices: list[JsonObject] = []

    for path in deps.device_paths():
        device: InventoryDevice | None = None
        try:
            device = deps.open_device(path)
            info = device.info
            capabilities = _capability_names(device, deps.evdev_mod)
            abs_info = _abs_axis_info(device, deps.evdev_mod)
            driver = _input_driver(path)
            device_types = deps.detect_device_types(device)
            device_type = deps.primary_input_class(device_types)
            stable_path = deps.resolve_stable_path(path)
            interface_id = deps.get_interface_id(stable_path)
            metadata = virtual_metadata.get(path, {})
            grabbed_source = grabbed_metadata.get(stable_path)
            recording_kind = str(metadata.get("recording_kind", "") or "")
            if not recording_kind:
                recording_kind = "physical" if not is_virtual_input(device) else "other_virtual"
            recording_id = str(metadata.get("recording_id", "") or "")
            if not recording_id:
                recording_id = (
                    f"physical:{stable_path}"
                    if recording_kind == "physical"
                    else f"virtual:{device.name or path}:{stable_path}"
                )

            devices.append(
                {
                    "path": path,
                    "open_path": path,
                    "stable_path": stable_path,
                    "interface_id": str(interface_id or ""),
                    "name": device.name,
                    "phys": coerce_str(getattr(device, "phys", None), None),
                    "uniq": coerce_str(getattr(device, "uniq", None), None),
                    "vendor_id": f"{info.vendor:04x}",
                    "product_id": f"{info.product:04x}",
                    "capabilities": capabilities,
                    "abs_info": abs_info,
                    "driver": driver,
                    "device_types": device_types,
                    "device_type": device_type.value,
                    "recording_id": recording_id,
                    "recording_kind": recording_kind,
                    "grabbed_by_keymasq": grabbed_source is not None,
                    **metadata,
                    **(grabbed_source or {}),
                }
            )
        except OSError as exc:
            if deps.is_permission_error(exc):
                deps.logger.warning(
                    deps.permission_message("Skipping unreadable device %s: %s"),
                    path,
                    exc,
                )
            else:
                deps.logger.debug("Skipping unreadable device %s: %s", path, exc)
        except Exception:
            deps.logger.exception("Could not read device %s", path)
        finally:
            if device is not None:
                close = getattr(device, "close", None)
                if callable(close):
                    with contextlib.suppress(OSError, RuntimeError):
                        close()

    return {"devices": devices}


def _capability_names(device: InventoryDevice, evdev_mod: Any) -> list[str]:
    names: list[str] = []
    for ev_type, codes in device.capabilities().items():
        for code in codes:
            code_value: object = (
                cast(tuple[object, ...], code)[0] if isinstance(code, tuple) else code
            )
            names.append(f"{evdev_mod.ecodes.EV[ev_type]}_{code_value}")
    return names


def _abs_axis_info(device: InventoryDevice, evdev_mod: Any) -> JsonObject:
    """Serialize ABS metadata while the privileged daemon has the device open."""
    try:
        entries = device.capabilities(absinfo=True).get(evdev_mod.ecodes.EV_ABS, [])
    except TypeError:
        # Lightweight test and integration adapters may only implement capabilities().
        return {}

    result: JsonObject = {}
    for entry in entries:
        if not isinstance(entry, (tuple, list)):
            continue
        values = cast(Sequence[object], entry)
        if len(values) < 2:
            continue
        code, info = values[0], values[1]
        if not isinstance(code, int):
            continue
        try:
            abs_info = cast(Any, info)
            result[str(code)] = {
                "value": int(abs_info.value),
                "minimum": int(abs_info.min),
                "maximum": int(abs_info.max),
                "fuzz": int(abs_info.fuzz),
                "flat": int(abs_info.flat),
                "resolution": int(abs_info.resolution),
            }
        except (TypeError, ValueError, AttributeError):
            continue
    return result


def _input_driver(event_path: str) -> str:
    """Return the bound input driver's sysfs name from the daemon process."""
    event_name = Path(event_path).name
    try:
        current = (Path("/sys/class/input") / event_name / "device").resolve()
    except OSError:
        return ""
    for candidate in (current, *current.parents):
        driver_link = candidate / "driver"
        try:
            if driver_link.is_symlink():
                return os.path.basename(os.readlink(driver_link))
        except OSError:
            continue
    return ""


def runtime_status(
    live_snapshot: Mapping[str, LiveInterface],
    grabbed_devices: Mapping[str, Sequence[object]],
) -> JsonObject:
    live_interfaces: list[JsonObject] = []
    for info in sorted(
        live_snapshot.values(),
        key=lambda item: (item.hardware_id, item.interface_id, item.stable_path),
    ):
        item: JsonObject = {
            "hardware_id": info.hardware_id,
            "vendor_id": info.vendor_id,
            "product_id": info.product_id,
            "stable_path": info.stable_path,
            "path": info.path,
            "interface_id": info.interface_id,
        }
        if info.phys:
            item["phys"] = info.phys
        if info.device_type:
            item["device_type"] = info.device_type
        if info.capabilities:
            item["capabilities"] = list(info.capabilities)
        live_interfaces.append(item)

    grabbed_interfaces: list[JsonObject] = []
    for hardware_id, devices in sorted(grabbed_devices.items()):
        for device in devices:
            device_type = getattr(device, "device_type", "")
            grabbed_interfaces.append(
                {
                    "hardware_id": str(hardware_id or ""),
                    "interface_id": str(getattr(device, "interface_id", "") or ""),
                    "path": str(getattr(device, "path", "") or ""),
                    "resolved_path": str(getattr(device, "resolved_event_path", "") or ""),
                    "stable_path": str(getattr(device, "stable_path", "") or ""),
                    "device_type": getattr(device_type, "value", str(device_type or "")),
                }
            )
    grabbed_interfaces.sort(
        key=lambda item: (
            str(item.get("hardware_id", "")),
            str(item.get("interface_id", "")),
            str(item.get("stable_path", "")),
        )
    )
    return {
        "status": "ok",
        "interfaces": live_interfaces,
        "grabbed_interfaces": grabbed_interfaces,
    }
