import asyncio
import contextlib
import inspect
import logging
from typing import TYPE_CHECKING, cast

from keyforge.session.compositor import (
    detect_compositor,
    get_compositor_capabilities,
    get_compositor_name,
    get_compositor_support_details,
    get_listener_class,
    is_compositor_supported,
)

from . import profiles as runtime_profiles
from .common import JsonObject, json_list, merge_support_details, str_value

if TYPE_CHECKING:
    from .core import SessionManager

log = logging.getLogger("keyforge-session")


def get_compositor_payload(manager: "SessionManager") -> JsonObject:
    raise RuntimeError("get_compositor_payload must be awaited")


async def build_compositor_payload(manager: "SessionManager") -> JsonObject:
    details = merge_support_details(
        await get_compositor_support_details(manager.compositor_state.compositor_id, manager.dbus),
        manager.compositor_state.window_listener,
    )
    return {
        "compositor_id": manager.compositor_state.compositor_id,
        "compositor_name": get_compositor_name(manager.compositor_state.compositor_id),
        "supported": bool(details.get("supported", False)),
        "capabilities": get_compositor_capabilities(manager.compositor_state.compositor_id),
        "details": details,
        "listener_active": manager.compositor_state.window_listener is not None,
        "listener_name": (
            getattr(manager.compositor_state.window_listener, "name", "")
            if manager.compositor_state.window_listener is not None
            else ""
        ),
        "compositor_dispatch_available": compositor_dispatch_available(manager),
    }


async def get_active_window_payload(manager: "SessionManager") -> JsonObject:
    if manager.compositor_state.window_listener is not None:
        try:
            window_class, window_title, window_tags = (
                await manager.compositor_state.window_listener.get_active_window()
            )
            window_info = normalize_window_info(window_class, window_title, window_tags)
            if window_info["class"] or window_info["title"] or window_info["tags"]:
                manager.compositor_state.current_window = cast(JsonObject, window_info)
                return {"status": "ok", **window_info}
        except Exception as e:
            log.debug(
                "Active window query failed (compositor_id=%s listener=%s): %s",
                manager.compositor_state.compositor_id,
                getattr(manager.compositor_state.window_listener, "name", "unknown"),
                e,
            )

    if manager.compositor_state.current_window:
        return {
            "status": "ok",
            **normalize_window_info_from_dict(manager.compositor_state.current_window),
        }

    return {
        "status": "error",
        "message": "Active window is unavailable on this compositor",
    }


def normalize_window_info(
    window_class: str,
    window_title: str,
    window_tags: list[str],
) -> dict[str, str | list[str]]:
    return {
        "class": str(window_class or ""),
        "title": str(window_title or ""),
        "tags": [str(tag) for tag in window_tags if str(tag or "").strip()],
    }


def normalize_window_info_from_dict(
    window_info: JsonObject,
) -> dict[str, str | list[str]]:
    return normalize_window_info(
        str_value(window_info.get("class"), ""),
        str_value(window_info.get("title"), ""),
        [str(tag) for tag in json_list(window_info.get("tags")) if str(tag or "").strip()],
    )


async def activate_title(manager: "SessionManager", title: str) -> JsonObject:
    if not title:
        return {"status": "error", "message": "title parameter required"}
    listener = manager.compositor_state.window_listener
    activate_window_by_title = (
        getattr(listener, "activate_window_by_title", None) if listener is not None else None
    )
    if not callable(activate_window_by_title):
        return {
            "status": "error",
            "message": "Window activation not supported on this compositor",
        }
    try:
        result_obj = activate_window_by_title(title)
        if not inspect.isawaitable(result_obj):
            return {
                "status": "error",
                "message": "Window activation not supported on this compositor",
            }
        result = await result_obj
        if result and result.get("found"):
            return {"status": "ok", "title": title, "found": True}
        return {
            "status": "error",
            "message": f"Window with title {title!r} not found",
            "details": result,
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


async def get_cursor_position_payload(manager: "SessionManager") -> JsonObject:
    pos = None

    if manager.compositor_state.window_listener:
        try:
            pos = await manager.compositor_state.window_listener.get_cursor_position()
        except Exception as e:
            log.debug(
                "Cursor query failed (compositor_id=%s listener=%s): %s",
                manager.compositor_state.compositor_id,
                getattr(manager.compositor_state.window_listener, "name", "unknown"),
                e,
            )

    if pos is None:
        return {
            "status": "error",
            "message": "Cursor position is unavailable on this compositor",
        }
    return {"status": "ok", "x": int(pos[0]), "y": int(pos[1])}


async def compositor_supervisor_loop(manager: "SessionManager") -> None:
    while manager.running:
        try:
            await ensure_compositor_listener(manager)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("Compositor supervisor error: %s", e)

        stable = (
            manager.compositor_state.window_listener is not None
            and manager.compositor_state.compositor_id is not None
            and manager.compositor_state.candidate == manager.compositor_state.compositor_id
            and manager.compositor_state.candidate_hits >= 2
        )
        await asyncio.sleep(
            manager.compositor_state.probe_slow_s
            if stable
            else manager.compositor_state.probe_fast_s
        )


async def ensure_compositor_listener(manager: "SessionManager") -> None:
    detected = await detect_compositor(manager.dbus)

    if detected == manager.compositor_state.candidate:
        manager.compositor_state.candidate_hits += 1
    else:
        manager.compositor_state.candidate = detected
        manager.compositor_state.candidate_hits = 1

    current_healthy = False
    if manager.compositor_state.window_listener is not None:
        with contextlib.suppress(Exception):
            current_healthy = await manager.compositor_state.window_listener.health_check()

    if manager.compositor_state.window_listener is not None and not current_healthy:
        log.warning("Window listener became unhealthy, restarting compositor binding")
        await stop_window_listener(manager)
        manager.compositor_state.compositor_id = None

    if manager.compositor_state.candidate_hits < 2:
        return

    target = manager.compositor_state.candidate
    if (
        target == manager.compositor_state.compositor_id
        and manager.compositor_state.window_listener is not None
    ):
        return

    await switch_compositor(manager, target)


async def switch_compositor(manager: "SessionManager", compositor_id: str | None) -> None:
    if (
        compositor_id == manager.compositor_state.compositor_id
        and manager.compositor_state.window_listener is not None
    ):
        return

    if (
        compositor_id
        and compositor_id == manager.compositor_state.compositor_id
        and manager.compositor_state.window_listener is None
    ):
        if not listener_retry_ready(manager, compositor_id):
            return

    previous = manager.compositor_state.compositor_id
    await stop_window_listener(manager)

    manager.compositor_state.compositor_id = compositor_id
    manager.compositor_state.compositor_capabilities = get_compositor_capabilities(
        manager.compositor_state.compositor_id
    )

    if compositor_id is None:
        if previous is not None:
            log.info("Compositor transitioned %s -> none (headless mode)", previous)
        return

    compositor_name = get_compositor_name(compositor_id)
    support_details: dict[str, bool | str] = {}
    if compositor_id == "gnome":
        support_details = await get_compositor_support_details(compositor_id, manager.dbus)
        supported = bool(support_details.get("supported", False))
    else:
        supported = await is_compositor_supported(compositor_id, manager.dbus)
    if not supported:
        note_listener_failure(
            manager,
            compositor_id,
            str(
                support_details.get("warning", "")
                or f"listener support unavailable for {compositor_name}"
            ),
        )
        return

    await start_window_listener(manager)
    if manager.compositor_state.window_listener is None:
        note_listener_failure(
            manager,
            compositor_id,
            manager.compositor_state.last_listener_start_error,
        )
        return

    manager.compositor_state.listener_retry_after.pop(compositor_id, None)
    manager.compositor_state.listener_last_error.pop(compositor_id, None)
    manager.compositor_state.listener_last_log_at.pop(compositor_id, None)

    if previous != compositor_id:
        log.info("Compositor transitioned %s -> %s", previous or "none", compositor_id)
    else:
        log.info("Compositor listener restarted for %s", compositor_id)


async def stop_window_listener(manager: "SessionManager") -> None:
    if manager.compositor_state.window_listener is None:
        return
    try:
        await manager.compositor_state.window_listener.stop()
    except Exception as e:
        log.debug("Error stopping window listener: %s", e)
    manager.compositor_state.window_listener = None


def listener_retry_ready(manager: "SessionManager", compositor_id: str) -> bool:
    now = asyncio.get_running_loop().time()
    return now >= float(manager.compositor_state.listener_retry_after.get(compositor_id, 0.0))


def note_listener_failure(
    manager: "SessionManager",
    compositor_id: str,
    error: str,
) -> None:
    now = asyncio.get_running_loop().time()
    error_text = (error or "listener startup failed").strip()

    previous_error = manager.compositor_state.listener_last_error.get(compositor_id)
    last_log_at = float(manager.compositor_state.listener_last_log_at.get(compositor_id, 0.0))
    should_log = (
        previous_error != error_text
        or (now - last_log_at) >= manager.compositor_state.listener_log_interval_s
    )

    if should_log:
        log.warning(
            "No compatible window listener environment detected for '%s': %s. "
            "Window tracking is disabled until environment changes.",
            compositor_id,
            error_text,
        )
        manager.compositor_state.listener_last_log_at[compositor_id] = now

    manager.compositor_state.listener_last_error[compositor_id] = error_text
    manager.compositor_state.listener_retry_after[compositor_id] = (
        now + manager.compositor_state.listener_retry_interval_s
    )


async def start_window_listener(manager: "SessionManager") -> None:
    listener_class = get_listener_class(manager.compositor_state.compositor_id)
    log.debug(
        "Window listener class for %s: %s",
        manager.compositor_state.compositor_id,
        listener_class,
    )
    if not listener_class:
        log.debug(
            "No window listener available for compositor: %s",
            manager.compositor_state.compositor_id,
        )
        manager.compositor_state.last_listener_start_error = "no listener class available"
        return

    try:
        manager.compositor_state.window_listener = listener_class(
            manager.on_window_change,
            manager.client,
            manager.dbus,
        )
        log.debug(
            "Window listener instance created: %s",
            manager.compositor_state.window_listener,
        )
        await manager.compositor_state.window_listener.start()
        log.info("Started %s window listener", manager.compositor_state.window_listener.name)
        manager.compositor_state.last_listener_start_error = ""
    except NotImplementedError as e:
        manager.compositor_state.last_listener_start_error = str(e)
        manager.compositor_state.window_listener = None
    except Exception as e:
        manager.compositor_state.last_listener_start_error = str(e)
        log.debug("Failed to start window listener: %s", e)
        manager.compositor_state.window_listener = None


async def handle_compositor_dispatch_trigger(
    manager: "SessionManager",
    data: JsonObject,
) -> None:
    target_compositor = str_value(data.get("compositor"), "").strip()
    dispatcher = str_value(data.get("dispatcher"), "").strip()
    args = str_value(data.get("args"), "").strip()
    if not dispatcher:
        return

    current_compositor = str(manager.compositor_state.compositor_id or "").strip()
    if target_compositor and target_compositor != current_compositor:
        log.warning(
            (
                "Ignored compositor dispatch for mismatched target: "
                "target=%s current=%s dispatcher=%s"
            ),
            target_compositor,
            current_compositor or "none",
            dispatcher,
        )
        return

    listener = manager.compositor_state.window_listener
    if listener is None:
        log.warning(
            (
                "Ignored compositor dispatch trigger while listener inactive: "
                "dispatcher=%s compositor=%s"
            ),
            dispatcher,
            manager.compositor_state.compositor_id or "none",
        )
        return

    ok, message = await listener.dispatch(dispatcher, args)
    if not ok:
        log.warning(
            "Compositor dispatch failed: dispatcher=%s args=%s message=%s",
            dispatcher,
            args,
            message,
        )


async def on_window_change(
    manager: "SessionManager",
    window_class: str,
    window_title: str,
    window_tags: list[str],
) -> None:
    window_info = normalize_window_info(window_class, window_title, window_tags)

    if manager.verbosity >= 1:
        log.debug(
            "Window changed: class=%s, title=%s, tags=%s",
            window_class,
            window_title,
            window_tags,
        )

    manager.compositor_state.current_window = cast(JsonObject, window_info)
    await runtime_profiles.reevaluate_profiles(manager)


def compositor_dispatch_available(manager: "SessionManager") -> bool:
    listener = manager.compositor_state.window_listener
    if listener is None:
        return False
    available = getattr(listener, "compositor_dispatch_available", None)
    if available is not None:
        return bool(available)
    return bool(
        getattr(listener, "running", False)
        and getattr(listener, "supports_compositor_dispatch", False)
    )
