import asyncio
import logging
import tomllib
from typing import TYPE_CHECKING, cast

from keymasq.common.coercion import coerce_str
from keymasq.common.config_files import write_toml_atomically
from keymasq.common.devices import normalize_input_classes
from keymasq.common.ipc import Command, CommandType

from .common import JsonObject, json_list, json_object

if TYPE_CHECKING:
    from .core import SessionManager

log = logging.getLogger("keymasq-session")
CORE_RECORDING_DEVICE_FILTER_TYPES: tuple[str, ...] = ("keyboard", "gamepad", "mouse")
OTHER_RECORDING_DEVICE_FILTER_TYPES: tuple[str, ...] = ("touchpad", "pointstick", "other")
_PERSISTENCE_WARNING = (
    "Recording settings were applied for this session but could not be saved. "
    "They may revert after Keymasq restarts."
)


def recording_device_filter_types(
    include_other: bool = False,
    *,
    include_motion: bool = False,
) -> list[str]:
    device_types = list(CORE_RECORDING_DEVICE_FILTER_TYPES)
    if include_motion:
        device_types.append("motion")
    if include_other:
        device_types.extend(OTHER_RECORDING_DEVICE_FILTER_TYPES)
    return device_types


def update_recording_settings(manager: "SessionManager", request: JsonObject) -> None:
    setting_keys = {
        "include_mouse_movement",
        "include_mouse_clicks",
        "record_start_position",
        "device_overrides",
    }
    if not any(key in request for key in setting_keys):
        return

    settings = manager.recording_state.settings
    if "include_mouse_movement" in request:
        settings["include_mouse_movement"] = bool(request.get("include_mouse_movement"))
    if "include_mouse_clicks" in request:
        settings["include_mouse_clicks"] = bool(request.get("include_mouse_clicks"))
    if "record_start_position" in request:
        settings["record_start_position"] = bool(request.get("record_start_position"))
    if "device_overrides" in request:
        overrides = json_object(request.get("device_overrides"))
        if overrides is not None:
            settings["device_overrides"] = {
                str(recording_id): bool(enabled) for recording_id, enabled in overrides.items()
            }
    prune_stale_recording_device_overrides(manager, settings)
    update_selected_recording_devices_cache(manager)
    queue_recording_settings_save(manager, dict(settings))


def queue_recording_settings_save(
    manager: "SessionManager",
    settings: JsonObject,
) -> None:
    manager.recording_state.settings_pending_save = settings
    save_task = manager.recording_state.settings_save_task
    if save_task is not None and not save_task.done():
        return
    manager.recording_state.settings_save_task = asyncio.create_task(
        flush_recording_settings_saves(manager)
    )


async def flush_recording_settings_saves(manager: "SessionManager") -> None:
    latest_save_succeeded = True
    try:
        while manager.recording_state.settings_pending_save is not None:
            pending = manager.recording_state.settings_pending_save
            manager.recording_state.settings_pending_save = None
            latest_save_succeeded = await asyncio.to_thread(
                save_recording_settings_to_disk, manager, pending
            )
    finally:
        manager.recording_state.settings_save_task = None
        if not latest_save_succeeded and manager.running:
            manager.send_notification("Keymasq Settings Warning", _PERSISTENCE_WARNING)


def load_recording_settings_from_disk(manager: "SessionManager") -> None:
    try:
        if not manager.RECORDING_SETTINGS_PATH.exists():
            return
        with manager.RECORDING_SETTINGS_PATH.open("rb") as f:
            data = tomllib.load(f)
        settings = manager.recording_state.settings
        if "include_mouse_movement" in data:
            settings["include_mouse_movement"] = bool(data.get("include_mouse_movement"))
        if "include_mouse_clicks" in data:
            settings["include_mouse_clicks"] = bool(data.get("include_mouse_clicks"))
        if "record_start_position" in data:
            settings["record_start_position"] = bool(data.get("record_start_position"))
        overrides = json_object(data.get("device_overrides"))
        if overrides is not None:
            settings["device_overrides"] = {
                str(recording_id): bool(enabled) for recording_id, enabled in overrides.items()
            }
    except Exception:
        log.exception(
            "Failed to load recording settings from %s",
            manager.RECORDING_SETTINGS_PATH,
        )


def save_recording_settings_to_disk(
    manager: "SessionManager",
    settings: JsonObject | None = None,
) -> bool:
    settings = settings or manager.recording_state.settings
    try:
        data: JsonObject = {
            "include_mouse_movement": bool(settings.get("include_mouse_movement", False)),
            "include_mouse_clicks": bool(settings.get("include_mouse_clicks", False)),
            "record_start_position": bool(settings.get("record_start_position", False)),
            "device_overrides": {
                str(recording_id): bool(enabled)
                for recording_id, enabled in (
                    json_object(settings.get("device_overrides")) or {}
                ).items()
            },
        }

        write_toml_atomically(manager.RECORDING_SETTINGS_PATH, data)
        return True
    except Exception:
        log.exception(
            "Failed to save recording settings to %s",
            manager.RECORDING_SETTINGS_PATH,
        )
        return False


def prune_stale_recording_device_overrides(
    manager: "SessionManager",
    settings: JsonObject | None = None,
) -> None:
    if not manager.recording_state.devices_cache_ready:
        return

    settings = settings if settings is not None else manager.recording_state.settings
    known_ids = {
        current_id
        for device in manager.recording_state.devices_cache
        if (current_id := recording_device_id(device))
    }
    if not known_ids:
        return

    overrides = json_object(settings.get("device_overrides"))
    if not overrides:
        settings["device_overrides"] = {}
        return

    settings["device_overrides"] = {
        str(recording_id): bool(enabled)
        for recording_id, enabled in overrides.items()
        if str(recording_id) in known_ids
    }


async def refresh_recording_devices_cache(
    manager: "SessionManager",
    include_other: bool | None = None,
) -> None:
    include_other = (
        manager.recording_state.devices_cache_include_other
        if include_other is None
        else include_other
    )
    try:
        devices = await get_devices_for_recording(
            manager,
            recording_device_filter_types(
                include_other,
                include_motion=manager.recording_state.devices_cache_include_motion,
            ),
            include_grabbed=True,
        )
        manager.recording_state.devices_cache = devices
        manager.recording_state.devices_cache_ready = True
        manager.recording_state.devices_cache_include_other = include_other
        update_selected_recording_devices_cache(manager)
    except OSError as exc:
        log.debug("Failed to refresh recording devices cache: %s", exc)
    except Exception:
        log.exception("Unexpected failure refreshing recording devices cache")


def update_selected_recording_devices_cache(manager: "SessionManager") -> None:
    overrides = json_object(manager.recording_state.settings.get("device_overrides")) or {}
    manager.recording_state.selected_devices_cache = [
        d for d in manager.recording_state.devices_cache if recording_device_enabled(d, overrides)
    ]


def recording_device_types(device: JsonObject) -> list[str]:
    return normalize_input_classes(
        cast(list[str] | None, json_list(device.get("device_types")) or None),
        coerce_str(device.get("device_type"), "other"),
    )


def recording_device_id(device: JsonObject) -> str:
    recording_id = coerce_str(device.get("recording_id"), "")
    if recording_id:
        return recording_id
    stable_path = coerce_str(device.get("stable_path"), "")
    if stable_path:
        return f"physical:{stable_path}"
    path = coerce_str(device.get("path"), "")
    return f"physical:{path}" if path else ""


def recording_device_selected_by_default(device: JsonObject) -> bool:
    return coerce_str(device.get("recording_kind"), "physical") in {
        "keymasq_output",
        "keymasq_passthrough",
    }


def recording_device_enabled(
    device: JsonObject,
    overrides: JsonObject,
) -> bool:
    recording_id = recording_device_id(device)
    if recording_id in overrides:
        return bool(overrides.get(recording_id))
    return recording_device_selected_by_default(device)


async def get_devices_for_recording(
    manager: "SessionManager",
    device_types: list[str],
    include_grabbed: bool = False,
) -> list[JsonObject]:
    try:
        result = await manager.client.send_command(Command(command=CommandType.LIST_DEVICES))
    except OSError as exc:
        log.debug("Failed to list devices for recording: %s", exc)
        return []
    except Exception:
        log.exception("Unexpected failure listing devices for recording")
        return []
    result_data = json_object(result.data)
    if result.status != "ok" or result_data is None:
        return []

    devices: list[JsonObject] = []
    for raw_device in json_list(result_data.get("devices")):
        d = json_object(raw_device)
        if d is None:
            continue
        path = coerce_str(d.get("path"), "")
        stable_path = coerce_str(d.get("stable_path"), path)
        dtype = coerce_str(d.get("device_type"), "other")
        resolved_types = recording_device_types(d)
        if not path or not set(device_types).intersection(resolved_types):
            continue

        is_grabbed = bool(d.get("grabbed_by_keymasq", False))
        if is_grabbed and not include_grabbed:
            continue

        devices.append(
            {
                "path": path,
                "open_path": coerce_str(d.get("open_path"), path),
                "stable_path": stable_path,
                "interface_id": coerce_str(d.get("interface_id"), ""),
                "name": coerce_str(d.get("name"), path),
                "phys": coerce_str(d.get("phys"), ""),
                "uniq": coerce_str(d.get("uniq"), ""),
                "vendor_id": str(d.get("vendor_id", "") or ""),
                "product_id": str(d.get("product_id", "") or ""),
                "device_type": dtype,
                "device_types": resolved_types,
                "capabilities": [str(value) for value in json_list(d.get("capabilities"))],
                "abs_info": json_object(d.get("abs_info")) or {},
                "driver": coerce_str(d.get("driver"), ""),
                "recording_id": coerce_str(d.get("recording_id"), f"physical:{stable_path}"),
                "recording_kind": coerce_str(d.get("recording_kind"), "physical"),
                "grabbed_by_keymasq": is_grabbed,
                "source_hardware_id": coerce_str(d.get("source_hardware_id"), ""),
                "source_interface_id": coerce_str(d.get("source_interface_id"), ""),
                "source_stable_path": coerce_str(d.get("source_stable_path"), ""),
                "source_path": coerce_str(d.get("source_path"), ""),
                "keymasq_output": coerce_str(d.get("keymasq_output"), ""),
            }
        )

    return devices
