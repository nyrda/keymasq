import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import TypedDict, cast

from keyforge.session.dbus import SessionDBus
from keyforge.session.listeners.cosmic import CosmicListener
from keyforge.session.listeners.gnome import GnomeListener
from keyforge.session.listeners.hyprland import HyprlandListener
from keyforge.session.listeners.kde import KDEListener
from keyforge.session.listeners.niri import NiriListener
from keyforge.session.listeners.wayland_wlr import WlrootsWaylandListener
from keyforge.session.listeners.x11 import X11Listener

log = logging.getLogger("keyforge-session.compositor")


type CompositorProbe = Callable[[SessionDBus | None], Awaitable[bool]]
type CompositorListener = (
    type[CosmicListener]
    | type[GnomeListener]
    | type[HyprlandListener]
    | type[KDEListener]
    | type[NiriListener]
    | type[WlrootsWaylandListener]
    | type[X11Listener]
)


class SupportedCompositor(TypedDict):
    env: str
    name: str
    capabilities: list[str]


SUPPORTED_COMPOSITORS: dict[str, SupportedCompositor] = {
    "hyprland": {
        "env": "HYPRLAND_INSTANCE_SIGNATURE",
        "name": "Hyprland",
        "capabilities": ["window_tags"],
    },
    "niri": {
        "env": "NIRI_SOCKET",
        "name": "Niri",
        "capabilities": [],
    },
    "x11": {
        "env": "DISPLAY",
        "name": "X11",
        "capabilities": [],
    },
    "kde": {
        "env": "KDE_FULL_SESSION",
        "name": "KDE Plasma",
        "capabilities": [],
    },
    "cosmic": {
        "env": "XDG_CURRENT_DESKTOP",
        "name": "COSMIC",
        "capabilities": [],
    },
    "gnome": {
        "env": "XDG_CURRENT_DESKTOP",
        "name": "GNOME",
        "capabilities": [],
    },
}


PROBE_ORDER: list[tuple[str, CompositorListener]] = [
    ("hyprland", HyprlandListener),
    ("niri", NiriListener),
    ("kde", KDEListener),
    ("gnome", GnomeListener),
    ("cosmic", CosmicListener),
    ("wayland", WlrootsWaylandListener),
    ("x11", X11Listener),
]


async def _probe_compositor_session(
    listener_class: CompositorListener,
    dbus: SessionDBus | None = None,
) -> bool:
    probe_session = getattr(listener_class, "probe_session", None)
    if callable(probe_session):
        probe = cast(Callable[[SessionDBus | None], Awaitable[bool]], probe_session)
        return bool(await probe(dbus))
    probe_available = cast(CompositorProbe, listener_class.probe_available)
    return bool(await probe_available(dbus))

def _run_probe_sync[ProbeResult](
    coro: Coroutine[object, object, ProbeResult],
) -> ProbeResult | None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    log.debug("Synchronous compositor probe called in running loop")
    return None


async def detect_compositor(dbus: SessionDBus | None = None) -> str | None:
    for compositor_id, listener_class in PROBE_ORDER:
        if await _probe_compositor_session(listener_class, dbus):
            return compositor_id
    return None


def detect_compositor_sync() -> str | None:
    result = _run_probe_sync(detect_compositor())
    if isinstance(result, str) or result is None:
        return result
    return None


def get_compositor_name(compositor_id: str | None) -> str:
    if not compositor_id:
        return "Unknown"

    if compositor_id in SUPPORTED_COMPOSITORS:
        return SUPPORTED_COMPOSITORS[compositor_id]["name"]

    names = {
        "kde": "KDE Plasma",
        "cosmic": "COSMIC",
        "gnome": "GNOME",
        "niri": "Niri",
        "x11": "X11",
        "wayland": "Wayland",
    }
    return names.get(compositor_id, compositor_id.title())


def is_compositor_supported_sync(compositor_id: str | None) -> bool:
    result = _run_probe_sync(is_compositor_supported(compositor_id))
    return bool(result)


async def is_compositor_supported(
    compositor_id: str | None,
    dbus: SessionDBus | None = None,
) -> bool:
    if not compositor_id:
        return False
    if compositor_id == "x11":
        return await X11Listener.probe_available(dbus)
    if compositor_id == "wayland":
        return await WlrootsWaylandListener.probe_available(dbus)
    if compositor_id == "kde":
        return await KDEListener.probe_available(dbus)
    if compositor_id == "cosmic":
        return await CosmicListener.probe_available(dbus)
    if compositor_id == "gnome":
        return await GnomeListener.probe_available(dbus)
    if compositor_id == "niri":
        return await NiriListener.probe_available(dbus)
    if compositor_id == "hyprland":
        return await HyprlandListener.probe_available(dbus)
    return compositor_id in SUPPORTED_COMPOSITORS


async def get_compositor_support_details(
    compositor_id: str | None,
    dbus: SessionDBus | None = None,
) -> dict[str, bool | str]:
    supported = await is_compositor_supported(compositor_id, dbus)
    details: dict[str, bool | str] = {
        "supported": supported,
        "warning": "",
    }
    if compositor_id == "gnome":
        details.update(await GnomeListener.get_support_details(dbus))
    return details


def get_compositor_support_details_sync(compositor_id: str | None) -> dict[str, bool | str]:
    result = _run_probe_sync(get_compositor_support_details(compositor_id))
    if isinstance(result, dict):
        return result
    return {"supported": False, "warning": ""}


def get_compositor_capabilities(compositor_id: str | None) -> list[str]:
    if not compositor_id or compositor_id not in SUPPORTED_COMPOSITORS:
        return []
    return SUPPORTED_COMPOSITORS[compositor_id].get("capabilities", [])


def has_capability(compositor_id: str | None, capability: str) -> bool:
    return capability in get_compositor_capabilities(compositor_id)


def get_listener_class(compositor_id: str | None) -> CompositorListener | None:
    if compositor_id == "hyprland":
        return HyprlandListener

    if compositor_id == "kde":
        return KDEListener

    if compositor_id == "niri":
        return NiriListener

    if compositor_id == "x11":
        return X11Listener

    if compositor_id == "cosmic":
        return CosmicListener

    if compositor_id == "gnome":
        return GnomeListener

    if compositor_id == "wayland":
        return WlrootsWaylandListener

    return None
