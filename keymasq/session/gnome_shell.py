import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

from dbus_next.constants import MessageType
from dbus_next.message import Message
from dbus_next.signature import Variant

from keymasq.session.dbus import SessionDBus, temporary_session_dbus

GNOME_EXTENSIONS_SERVICE = "org.gnome.Shell.Extensions"
GNOME_EXTENSIONS_PATH = "/org/gnome/Shell/Extensions"
GNOME_EXTENSIONS_INTERFACE = "org.gnome.Shell.Extensions"
DBUS_PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
GNOME_SESSION_MANAGER_SERVICE = "org.gnome.SessionManager"
GNOME_SESSION_MANAGER_PATH = "/org/gnome/SessionManager"
GNOME_SESSION_MANAGER_INTERFACE = "org.gnome.SessionManager"
SYSTEMD_SERVICE = "org.freedesktop.systemd1"
SYSTEMD_PATH = "/org/freedesktop/systemd1"
SYSTEMD_MANAGER_INTERFACE = "org.freedesktop.systemd1.Manager"

type ExtensionInfo = dict[str, object]


class GnomeShellDBusError(RuntimeError):
    pass


@asynccontextmanager
async def _dbus_context(dbus: SessionDBus | None) -> AsyncGenerator[SessionDBus]:
    if dbus is not None:
        yield dbus
        return

    async with temporary_session_dbus() as temporary_dbus:
        yield temporary_dbus


def _unwrap_variant(value: object) -> object:
    while isinstance(value, Variant):
        value = value.value

    if isinstance(value, dict):
        raw_dict = cast(dict[object, object], value)
        return {str(key): _unwrap_variant(item) for key, item in raw_dict.items()}
    if isinstance(value, list):
        raw_list = cast(list[object], value)
        return [_unwrap_variant(item) for item in raw_list]
    return value


def _reply_error_message(reply: Message) -> str:
    body = reply.body or []
    message = str(body[0]) if body else ""
    error_name = str(getattr(reply, "error_name", "") or "")
    if message and error_name:
        return f"{error_name}: {message}"
    return message or error_name or "GNOME Shell DBus call failed"


async def _call(
    dbus: SessionDBus,
    *,
    destination: str,
    path: str,
    interface: str,
    member: str,
    signature: str = "",
    body: list[object] | None = None,
    timeout: float = 0.8,
) -> Message:
    bus = await dbus.bus()
    try:
        reply = await asyncio.wait_for(
            bus.call(
                Message(
                    destination=destination,
                    path=path,
                    interface=interface,
                    member=member,
                    signature=signature,
                    body=body or [],
                )
            ),
            timeout=timeout,
        )
    except Exception:
        await dbus.disconnect()
        raise
    if reply is None:
        raise GnomeShellDBusError("GNOME Shell DBus call returned no reply")
    if reply.message_type == MessageType.ERROR:
        raise GnomeShellDBusError(_reply_error_message(reply))
    return reply


async def get_extension_info(
    uuid: str,
    dbus: SessionDBus | None = None,
    *,
    timeout: float = 0.8,
) -> ExtensionInfo:
    async with _dbus_context(dbus) as session_dbus:
        reply = await _call(
            session_dbus,
            destination=GNOME_EXTENSIONS_SERVICE,
            path=GNOME_EXTENSIONS_PATH,
            interface=GNOME_EXTENSIONS_INTERFACE,
            member="GetExtensionInfo",
            signature="s",
            body=[uuid],
            timeout=timeout,
        )

    if not reply.body:
        return {}
    raw_info = _unwrap_variant(reply.body[0])
    if not isinstance(raw_info, dict):
        return {}
    return cast(ExtensionInfo, raw_info)


async def extension_visible(
    uuid: str,
    dbus: SessionDBus | None = None,
    *,
    timeout: float = 0.8,
) -> bool:
    info = await get_extension_info(uuid, dbus, timeout=timeout)
    return str(info.get("uuid", "") or "") == uuid


async def extension_enabled(
    uuid: str,
    dbus: SessionDBus | None = None,
    *,
    timeout: float = 0.8,
) -> bool:
    info = await get_extension_info(uuid, dbus, timeout=timeout)
    enabled = info.get("enabled")
    if isinstance(enabled, bool):
        return enabled

    state = info.get("state")
    if isinstance(state, int | float):
        return int(state) == 1
    return False


async def set_extension_enabled(
    uuid: str,
    enabled: bool,
    dbus: SessionDBus | None = None,
    *,
    timeout: float = 1.5,
) -> bool:
    async with _dbus_context(dbus) as session_dbus:
        reply = await _call(
            session_dbus,
            destination=GNOME_EXTENSIONS_SERVICE,
            path=GNOME_EXTENSIONS_PATH,
            interface=GNOME_EXTENSIONS_INTERFACE,
            member="EnableExtension" if enabled else "DisableExtension",
            signature="s",
            body=[uuid],
            timeout=timeout,
        )

    if not reply.body:
        return False
    result = _unwrap_variant(reply.body[0])
    return bool(result)


async def user_extensions_enabled(
    dbus: SessionDBus | None = None,
    *,
    timeout: float = 0.8,
) -> bool:
    async with _dbus_context(dbus) as session_dbus:
        reply = await _call(
            session_dbus,
            destination=GNOME_EXTENSIONS_SERVICE,
            path=GNOME_EXTENSIONS_PATH,
            interface=DBUS_PROPERTIES_INTERFACE,
            member="Get",
            signature="ss",
            body=[GNOME_EXTENSIONS_INTERFACE, "UserExtensionsEnabled"],
            timeout=timeout,
        )

    if not reply.body:
        return False
    return bool(_unwrap_variant(reply.body[0]))


async def set_user_extensions_enabled(
    enabled: bool,
    dbus: SessionDBus | None = None,
    *,
    timeout: float = 1.5,
) -> None:
    async with _dbus_context(dbus) as session_dbus:
        await _call(
            session_dbus,
            destination=GNOME_EXTENSIONS_SERVICE,
            path=GNOME_EXTENSIONS_PATH,
            interface=DBUS_PROPERTIES_INTERFACE,
            member="Set",
            signature="ssv",
            body=[
                GNOME_EXTENSIONS_INTERFACE,
                "UserExtensionsEnabled",
                Variant("b", enabled),
            ],
            timeout=timeout,
        )


async def request_logout(
    dbus: SessionDBus | None = None,
    *,
    timeout: float = 1.5,
) -> None:
    async with _dbus_context(dbus) as session_dbus:
        await _call(
            session_dbus,
            destination=GNOME_SESSION_MANAGER_SERVICE,
            path=GNOME_SESSION_MANAGER_PATH,
            interface=GNOME_SESSION_MANAGER_INTERFACE,
            member="Logout",
            signature="u",
            body=[0],
            timeout=timeout,
        )


async def request_user_service_restart(
    service: str,
    dbus: SessionDBus | None = None,
    *,
    timeout: float = 1.5,
) -> None:
    async with _dbus_context(dbus) as session_dbus:
        await _call(
            session_dbus,
            destination=SYSTEMD_SERVICE,
            path=SYSTEMD_PATH,
            interface=SYSTEMD_MANAGER_INTERFACE,
            member="RestartUnit",
            signature="ss",
            body=[service, "replace"],
            timeout=timeout,
        )
