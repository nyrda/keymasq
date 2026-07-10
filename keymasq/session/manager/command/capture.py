import asyncio
from typing import TYPE_CHECKING, cast

from keymasq.common.coercion import coerce_bool, coerce_float, coerce_str

from .. import recording_capture, recording_device_selection
from ..common import JsonObject, json_list

if TYPE_CHECKING:
    from ..core import SessionManager


async def handle_capture_commands(
    manager: "SessionManager",
    command: str,
    request: JsonObject,
    writer: asyncio.StreamWriter,
) -> JsonObject | None:
    if command == "list_devices_for_recording":
        include_other = coerce_bool(request.get("include_other"), False)
        device_types = recording_device_selection.recording_device_filter_types(include_other)
        devices = await recording_device_selection.get_devices_for_recording(
            manager,
            device_types,
            include_grabbed=True,
        )
        manager.recording_state.devices_cache = devices
        manager.recording_state.devices_cache_ready = True
        manager.recording_state.devices_cache_include_other = include_other
        recording_device_selection.update_selected_recording_devices_cache(manager)
        return {"status": "ok", "devices": devices}

    if command == "begin_capture":
        hardware_id = coerce_str(request.get("hardware_id"), "")
        if not hardware_id:
            return {"error": "missing hardware_id"}
        evdev_paths = [
            coerce_str(path, "")
            for path in json_list(request.get("evdev_paths"))
            if coerce_str(path, "")
        ]
        evdev_interfaces_raw = request.get("evdev_interfaces")
        evdev_interfaces = (
            [
                cast(JsonObject, item)
                for item in json_list(evdev_interfaces_raw)
                if isinstance(item, dict)
            ]
            if evdev_interfaces_raw is not None
            else None
        )
        mode = coerce_str(request.get("mode"), "button")
        return await recording_capture.capture_begin_for_paths(
            manager,
            hardware_id,
            evdev_paths,
            evdev_interfaces=evdev_interfaces,
            mode=mode,
            owner_writer=writer if bool(request.get("end_on_disconnect", False)) else None,
        )

    if command == "capture_read":
        hardware_id = coerce_str(request.get("hardware_id"), "")
        if not hardware_id:
            return {"error": "missing hardware_id"}
        return await recording_capture.capture_read(manager, hardware_id)

    if command == "end_capture":
        hardware_id = coerce_str(request.get("hardware_id"), "")
        if not hardware_id:
            return {"error": "missing hardware_id"}
        return await recording_capture.capture_end(manager, hardware_id)

    if command == "capture_combo":
        profile_name = coerce_str(request.get("profile_name"), "")
        if not profile_name:
            return {"error": "missing profile_name"}
        timeout_s = coerce_float(request.get("timeout_s"), 15.0)
        return await recording_capture.capture_combo(manager, profile_name, timeout_s)

    return None
