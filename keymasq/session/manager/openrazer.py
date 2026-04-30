import asyncio
import logging
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

from dbus_next.constants import MessageType
from dbus_next.message import Message

from keymasq.common.models import OPENRAZER_DEFAULT_POLL_RATE_TEMPLATES
from keymasq.session.dbus import DBUS_INTERFACE, DBUS_PATH, DBUS_SERVICE

from .common import JsonObject, int_value, str_value

if TYPE_CHECKING:
    from .core import SessionManager

log = logging.getLogger("keymasq-session.openrazer")

OPENRAZER_SERVICE = "org.razer"
OPENRAZER_ROOT_PATH = "/org/razer"
OPENRAZER_DEVICES_INTERFACE = "razer.devices"
OPENRAZER_MISC_INTERFACE = "razer.device.misc"
OPENRAZER_DPI_INTERFACE = "razer.device.dpi"

PROBE_FRESH_S = 2.0
READY_STALE_S = 30.0
ACTION_RETRY_INITIAL_S = 5.0
ACTION_RETRY_MAX_S = 30.0
DBUS_CALL_TIMEOUT_S = 0.8


async def start_openrazer_monitor(manager: "SessionManager") -> None:
    await install_name_owner_watch(manager)


async def stop_openrazer_monitor(manager: "SessionManager") -> None:
    state = manager.openrazer_state
    handler = state.watcher_handler
    if handler is not None:
        try:
            bus = await manager.dbus.bus()
            remove = getattr(bus, "remove_message_handler", None)
            if callable(remove):
                remove(handler)
        except Exception:
            pass
    state.watcher_handler = None
    state.watcher_installed = False


async def install_name_owner_watch(manager: "SessionManager") -> None:
    state = manager.openrazer_state
    if state.watcher_installed:
        return

    try:
        bus = await manager.dbus.bus()
        match_rule = (
            "type='signal',sender='org.freedesktop.DBus',"
            "interface='org.freedesktop.DBus',member='NameOwnerChanged',arg0='org.razer'"
        )
        reply = await bus.call(
            Message(
                destination=DBUS_SERVICE,
                path=DBUS_PATH,
                interface=DBUS_INTERFACE,
                member="AddMatch",
                signature="s",
                body=[match_rule],
            )
        )
        if reply is not None and reply.message_type == MessageType.ERROR:
            raise RuntimeError(str(reply.body[0]) if reply.body else "AddMatch failed")

        def _on_message(message: Message) -> bool | None:
            if (
                message.message_type != MessageType.SIGNAL
                or message.interface != DBUS_INTERFACE
                or message.member != "NameOwnerChanged"
                or not message.body
                or message.body[0] != OPENRAZER_SERVICE
            ):
                return None
            new_owner = str(message.body[2]) if len(message.body) >= 3 else ""
            if new_owner:
                asyncio.create_task(refresh_openrazer(manager, force=True))
            else:
                mark_openrazer_unavailable(manager, "OpenRazer daemon disconnected")
            return None

        add = getattr(bus, "add_message_handler", None)
        if callable(add):
            add(_on_message)
            state.watcher_handler = _on_message
            state.watcher_installed = True
    except Exception as exc:
        log.debug("OpenRazer DBus name watch unavailable: %s", exc)


def openrazer_status(manager: "SessionManager") -> JsonObject:
    state = manager.openrazer_state
    return {
        "status": "ok",
        "available": bool(state.available),
        "devices": list(state.devices.values()),
        "last_error": state.last_error,
        "last_probe_s": state.last_probe_s,
        "next_retry_s": state.next_retry_s,
    }


async def refresh_openrazer(
    manager: "SessionManager",
    *,
    force: bool = False,
) -> JsonObject:
    state = manager.openrazer_state
    now = time.monotonic()
    if not force:
        if state.available and now - state.last_probe_s < READY_STALE_S:
            return openrazer_status(manager)
        if now < state.next_retry_s:
            return openrazer_status(manager)
        if now - state.last_probe_s < PROBE_FRESH_S:
            return openrazer_status(manager)

    async with state.refresh_lock:
        now = time.monotonic()
        if not force:
            if state.available and now - state.last_probe_s < READY_STALE_S:
                return openrazer_status(manager)
            if now < state.next_retry_s:
                return openrazer_status(manager)
            if now - state.last_probe_s < PROBE_FRESH_S:
                return openrazer_status(manager)

        state.last_probe_s = now
        try:
            has_owner = await manager.dbus.name_has_owner(
                OPENRAZER_SERVICE,
                timeout=DBUS_CALL_TIMEOUT_S,
            )
            if not has_owner:
                _record_probe_failure(manager, "OpenRazer daemon is not running")
                return openrazer_status(manager)

            devices = await _load_openrazer_devices(manager)
            state.available = True
            state.devices = devices
            state.last_error = ""
            state.retry_delay_s = ACTION_RETRY_INITIAL_S
            state.next_retry_s = 0.0
            return openrazer_status(manager)
        except Exception as exc:
            log.debug("OpenRazer probe failed: %s", exc)
            _record_probe_failure(manager, str(exc).strip() or exc.__class__.__name__)
            return openrazer_status(manager)


def _record_probe_failure(manager: "SessionManager", message: str) -> None:
    state = manager.openrazer_state
    now = time.monotonic()
    state.available = False
    state.devices = {}
    state.last_error = message
    state.next_retry_s = now + state.retry_delay_s
    state.retry_delay_s = min(state.retry_delay_s * 2, ACTION_RETRY_MAX_S)


def mark_openrazer_unavailable(manager: "SessionManager", message: str) -> None:
    state = manager.openrazer_state
    state.available = False
    state.devices = {}
    state.last_error = message
    state.next_retry_s = 0.0


async def _load_openrazer_devices(manager: "SessionManager") -> dict[str, JsonObject]:
    devices_iface = await manager.dbus.get_interface(
        OPENRAZER_SERVICE,
        OPENRAZER_ROOT_PATH,
        OPENRAZER_DEVICES_INTERFACE,
    )
    serials_raw = await _call_dbus(devices_iface.call_get_devices())
    serials = [str(serial) for serial in cast(list[object], serials_raw or [])]

    devices: dict[str, JsonObject] = {}
    for serial in serials:
        path = _device_path(serial)
        try:
            devices[serial] = await _load_device(manager, serial, path)
        except Exception as exc:
            log.debug("Skipping OpenRazer device %s: %s", serial, exc)
    return devices


async def _load_device(manager: "SessionManager", serial: str, path: str) -> JsonObject:
    features = await _device_features(manager, path)
    misc_iface = await manager.dbus.get_interface(
        OPENRAZER_SERVICE,
        path,
        OPENRAZER_MISC_INTERFACE,
    )

    vid_pid = await _optional_call(misc_iface, "call_get_vid_pid")
    vendor_id = ""
    product_id = ""
    vid_pid_values = _object_sequence(vid_pid)
    if len(vid_pid_values) >= 2:
        vendor_id = f"{int_value(vid_pid_values[0]):04x}"
        product_id = f"{int_value(vid_pid_values[1]):04x}"

    name = await _optional_call(misc_iface, "call_get_device_name")
    device_type = await _optional_call(misc_iface, "call_get_device_type")
    device: JsonObject = {
        "serial": serial,
        "name": str(name or serial),
        "device_type": str(device_type or ""),
        "vendor_id": vendor_id,
        "product_id": product_id,
        "hardware_id": f"{vendor_id}:{product_id}" if vendor_id and product_id else "",
        "has_dpi": _has_methods(features, OPENRAZER_DPI_INTERFACE, {"getDPI", "setDPI"}),
        "has_available_dpi": _has_methods(features, OPENRAZER_DPI_INTERFACE, {"availableDPI"}),
        "has_poll_rate": _has_methods(
            features,
            OPENRAZER_MISC_INTERFACE,
            {"getPollRate", "setPollRate"},
        ),
        "has_supported_poll_rates": _has_methods(
            features,
            OPENRAZER_MISC_INTERFACE,
            {"getSupportedPollRates"},
        ),
    }

    if bool(device["has_dpi"]):
        dpi_iface = await manager.dbus.get_interface(
            OPENRAZER_SERVICE,
            path,
            OPENRAZER_DPI_INTERFACE,
        )
        dpi = await _optional_call(dpi_iface, "call_get_dpi")
        dpi_values = _object_sequence(dpi)
        if dpi_values:
            device["dpi"] = [
                int_value(dpi_values[0]),
                int_value(dpi_values[1]) if len(dpi_values) > 1 else 0,
            ]
        max_dpi = await _optional_call(dpi_iface, "call_max_dpi")
        if max_dpi is not None:
            device["max_dpi"] = int_value(max_dpi)
        available_dpi = await _optional_call(dpi_iface, "call_available_dpi")
        if isinstance(available_dpi, list | tuple):
            available_values = cast(Sequence[object], available_dpi)
            device["available_dpi"] = [int_value(value) for value in available_values]

    if bool(device["has_poll_rate"]):
        poll_rate = await _optional_call(misc_iface, "call_get_poll_rate")
        if poll_rate is not None:
            device["poll_rate"] = int_value(poll_rate)
        supported = await _optional_call(misc_iface, "call_get_supported_poll_rates")
        if isinstance(supported, list | tuple):
            supported_values = cast(Sequence[object], supported)
            device["supported_poll_rates"] = [int_value(value) for value in supported_values]
            templates = sorted(
                {int_value(value) for value in supported_values if int_value(value) > 0}
            )
            if templates:
                device["poll_rate_templates"] = templates
                device["poll_rate_templates_source"] = "openrazer"
            else:
                device["poll_rate_templates"] = list(OPENRAZER_DEFAULT_POLL_RATE_TEMPLATES)
                device["poll_rate_templates_source"] = "default"
        else:
            device["poll_rate_templates"] = list(OPENRAZER_DEFAULT_POLL_RATE_TEMPLATES)
            device["poll_rate_templates_source"] = "default"

    return device


async def _device_features(manager: "SessionManager", path: str) -> dict[str, set[str]]:
    introspection = await manager.dbus.introspect(OPENRAZER_SERVICE, path)
    features: dict[str, set[str]] = {}
    for interface in getattr(introspection, "interfaces", []):
        name = str(getattr(interface, "name", "") or "")
        if not name:
            continue
        features[name] = {
            str(getattr(method, "name", "") or "")
            for method in getattr(interface, "methods", [])
            if getattr(method, "name", None)
        }
    return features


def _has_methods(features: dict[str, set[str]], interface: str, methods: set[str]) -> bool:
    available = features.get(interface, set())
    return methods.issubset(available)


async def handle_openrazer_action(manager: "SessionManager", data: JsonObject) -> JsonObject:
    status = await refresh_openrazer(manager)
    if not bool(status.get("available")):
        message = str_value(status.get("last_error"), "OpenRazer unavailable")
        log.warning("OpenRazer action skipped: %s", message)
        return {"status": "error", "message": message}

    try:
        serial = _resolve_action_serial(manager, data)
        setting = str_value(data.get("setting"), "").strip().lower()
        if setting == "dpi":
            return await _apply_dpi_action(manager, serial, data)
        if setting == "poll_rate":
            return await _apply_poll_rate_action(manager, serial, data)
        return {"status": "error", "message": f"unsupported OpenRazer setting '{setting}'"}
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        log.warning("OpenRazer action failed: %s", message)
        return {"status": "error", "message": message}


def _resolve_action_serial(manager: "SessionManager", data: JsonObject) -> str:
    serial = str_value(data.get("serial"), "").strip()
    if not serial:
        raise ValueError("OpenRazer action requires a device serial")
    if serial in manager.openrazer_state.devices:
        return serial
    raise ValueError(f"OpenRazer device serial '{serial}' is not available")


async def _apply_dpi_action(
    manager: "SessionManager",
    serial: str,
    data: JsonObject,
) -> JsonObject:
    device = _device(manager, serial)
    if not bool(device.get("has_dpi")):
        raise ValueError(f"OpenRazer device '{serial}' does not support DPI changes")

    dpi_iface = await manager.dbus.get_interface(
        OPENRAZER_SERVICE,
        _device_path(serial),
        OPENRAZER_DPI_INTERFACE,
    )
    current_raw = await _call_dbus(dpi_iface.call_get_dpi())
    current = [int_value(value) for value in _object_sequence(current_raw)]
    if not current:
        current = [0, 0]
    if len(current) == 1:
        current.append(0)

    requested_x = int_value(data.get("dpi_x"), 0)
    requested_y = int_value(data.get("dpi_y"), requested_x)
    if requested_y <= 0:
        requested_y = requested_x
    single_axis = bool(device.get("has_available_dpi")) or int(current[1]) == 0
    dpi_x, dpi_y = requested_x, 0 if single_axis else requested_y

    _validate_dpi(device, dpi_x, dpi_y)
    await _call_dbus(dpi_iface.call_set_dpi(dpi_x, dpi_y))
    device["dpi"] = [dpi_x, dpi_y]
    return {"status": "ok", "serial": serial, "setting": "dpi", "dpi": [dpi_x, dpi_y]}


async def _apply_poll_rate_action(
    manager: "SessionManager",
    serial: str,
    data: JsonObject,
) -> JsonObject:
    device = _device(manager, serial)
    if not bool(device.get("has_poll_rate")):
        raise ValueError(f"OpenRazer device '{serial}' does not support polling rate changes")
    poll_rate = int_value(data.get("poll_rate"), 0)
    if poll_rate <= 0:
        raise ValueError("poll rate must be positive")

    misc_iface = await manager.dbus.get_interface(
        OPENRAZER_SERVICE,
        _device_path(serial),
        OPENRAZER_MISC_INTERFACE,
    )
    await _call_dbus(misc_iface.call_set_poll_rate(poll_rate))
    device["poll_rate"] = poll_rate
    return {"status": "ok", "serial": serial, "setting": "poll_rate", "poll_rate": poll_rate}


def _validate_dpi(device: JsonObject, dpi_x: int, dpi_y: int) -> None:
    if dpi_x <= 0:
        raise ValueError("DPI must be positive")
    if dpi_y < 0:
        raise ValueError("DPI Y cannot be negative")
    available = _int_list(device.get("available_dpi"))
    if available and dpi_x not in available:
        raise ValueError(f"DPI {dpi_x} is not one of the available values: {available}")
    max_dpi = int_value(device.get("max_dpi"), 0)
    if max_dpi > 0 and (dpi_x > max_dpi or dpi_y > max_dpi):
        raise ValueError(f"DPI exceeds device maximum {max_dpi}")


def _device(manager: "SessionManager", serial: str) -> JsonObject:
    device = manager.openrazer_state.devices.get(serial)
    if device is None:
        raise ValueError(f"OpenRazer device serial '{serial}' is not available")
    return device


def _device_path(serial: str) -> str:
    return f"{OPENRAZER_ROOT_PATH}/device/{serial}"


def _int_list(value: object) -> list[int]:
    return [int_value(item) for item in _object_sequence(value)]


def _object_sequence(value: object) -> Sequence[object]:
    if not isinstance(value, list | tuple):
        return ()
    return cast(Sequence[object], value)


async def _optional_call(iface: object, method_name: str) -> object | None:
    method = getattr(iface, method_name, None)
    if not callable(method):
        return None
    try:
        return await _call_dbus(method())
    except Exception:
        return None


async def _call_dbus(awaitable: Any) -> object:
    return await asyncio.wait_for(awaitable, timeout=DBUS_CALL_TIMEOUT_S)
