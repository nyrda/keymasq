import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import TypedDict, cast

from keymasq.common.asyncio_runtime import ensure_uvloop
from keymasq.session.dbus import SessionDBus
from keymasq.session.listeners.cosmic import CosmicListener
from keymasq.session.listeners.gnome import GnomeListener
from keymasq.session.listeners.hyprland import HyprlandListener
from keymasq.session.listeners.kde import KDEListener
from keymasq.session.listeners.layer_shell import LayerShellCursorListener
from keymasq.session.listeners.niri import NiriListener
from keymasq.session.listeners.wayland_wlr import WlrootsWaylandListener
from keymasq.session.listeners.x11 import X11Listener

log = logging.getLogger("keymasq-session.compositor")


type CompositorProbe = Callable[[SessionDBus | None], Awaitable[bool]]
type CompositorListener = (
    type[CosmicListener]
    | type[GnomeListener]
    | type[HyprlandListener]
    | type[KDEListener]
    | type[LayerShellCursorListener]
    | type[NiriListener]
    | type[WlrootsWaylandListener]
    | type[X11Listener]
)


class SupportedCompositor(TypedDict):
    env: str
    name: str
    capabilities: list[str]
    listener: CompositorListener
    probe_order: int


SUPPORTED_COMPOSITORS: dict[str, SupportedCompositor] = {
    "hyprland": {
        "env": "HYPRLAND_INSTANCE_SIGNATURE",
        "name": "Hyprland",
        "capabilities": ["window_tags"],
        "listener": HyprlandListener,
        "probe_order": 10,
    },
    "niri": {
        "env": "NIRI_SOCKET",
        "name": "Niri",
        "capabilities": [],
        "listener": NiriListener,
        "probe_order": 20,
    },
    "kde": {
        "env": "KDE_FULL_SESSION",
        "name": "KDE Plasma",
        "capabilities": [],
        "listener": KDEListener,
        "probe_order": 30,
    },
    "gnome": {
        "env": "XDG_CURRENT_DESKTOP",
        "name": "GNOME",
        "capabilities": [],
        "listener": GnomeListener,
        "probe_order": 40,
    },
    "cosmic": {
        "env": "XDG_CURRENT_DESKTOP",
        "name": "COSMIC",
        "capabilities": [],
        "listener": CosmicListener,
        "probe_order": 50,
    },
    "wayland": {
        "env": "WAYLAND_DISPLAY",
        "name": "Wayland",
        "capabilities": [],
        "listener": WlrootsWaylandListener,
        "probe_order": 60,
    },
    "wayland-layer-shell": {
        "env": "WAYLAND_DISPLAY",
        "name": "Wayland Layer Shell",
        "capabilities": [],
        "listener": LayerShellCursorListener,
        "probe_order": 65,
    },
    "x11": {
        "env": "DISPLAY",
        "name": "X11",
        "capabilities": [],
        "listener": X11Listener,
        "probe_order": 70,
    },
}


PROBE_ORDER: list[tuple[str, CompositorListener]] = [
    (compositor_id, metadata["listener"])
    for compositor_id, metadata in sorted(
        SUPPORTED_COMPOSITORS.items(),
        key=lambda item: item[1]["probe_order"],
    )
]


async def _probe_compositor_session(
    listener_class: CompositorListener,
    dbus: SessionDBus | None = None,
) -> bool:
    probe_session = getattr(listener_class, "probe_session", None)
    if callable(probe_session):
        probe = cast(Callable[[SessionDBus | None], Awaitable[bool]], probe_session)
        return bool(await probe(dbus))
    return await _probe_listener_available(listener_class, dbus)


async def _probe_listener_available(
    listener_class: CompositorListener,
    dbus: SessionDBus | None = None,
) -> bool:
    probe_available = cast(CompositorProbe, listener_class.probe_available)
    return bool(await probe_available(dbus))


def _run_probe_sync[ProbeResult](
    coro: Coroutine[object, object, ProbeResult],
) -> ProbeResult | None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        ensure_uvloop()
        return asyncio.run(coro)
    log.debug("Synchronous compositor probe called in running loop")
    coro.close()
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

    return compositor_id.title()


def is_compositor_supported_sync(compositor_id: str | None) -> bool:
    result = _run_probe_sync(is_compositor_supported(compositor_id))
    return bool(result)


async def is_compositor_supported(
    compositor_id: str | None,
    dbus: SessionDBus | None = None,
) -> bool:
    if not compositor_id:
        return False
    metadata = SUPPORTED_COMPOSITORS.get(compositor_id)
    if metadata is None:
        return False
    return await _probe_listener_available(metadata["listener"], dbus)


async def get_compositor_support_details(
    compositor_id: str | None,
    dbus: SessionDBus | None = None,
) -> dict[str, bool | str]:
    if compositor_id == "gnome":
        details = await GnomeListener.get_support_details(dbus)
        details.setdefault("supported", bool(details.get("supported", False)))
        details.setdefault("warning", "")
        return details

    supported = await is_compositor_supported(compositor_id, dbus)
    details: dict[str, bool | str] = {
        "supported": supported,
        "warning": "",
    }
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
    if not compositor_id:
        return None
    metadata = SUPPORTED_COMPOSITORS.get(compositor_id)
    if metadata is None:
        return None
    return metadata["listener"]
