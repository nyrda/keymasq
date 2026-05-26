import asyncio
import logging
from typing import TYPE_CHECKING

from keymasq.common.ipc import Command, CommandType

from .common import JsonObject, json_list, json_object, str_value

if TYPE_CHECKING:
    from .core import SessionManager

log = logging.getLogger("keymasq-session.output_stream")

OUTPUT_STREAM_FILTERS = frozenset({"button", "axis", "mousemove", "syn", "other", "repeat"})
DEFAULT_OUTPUT_STREAM_FILTERS = frozenset({"button"})


def _writer_id(writer: asyncio.StreamWriter) -> int:
    return id(writer)


def _normalize_filters(raw_filters: object) -> set[str]:
    filters = {
        str_value(item, "").strip().lower()
        for item in json_list(raw_filters)
        if str_value(item, "").strip()
    }
    if "all" in filters:
        return set(OUTPUT_STREAM_FILTERS)
    selected = filters & OUTPUT_STREAM_FILTERS
    return selected or set(DEFAULT_OUTPUT_STREAM_FILTERS)


def _effective_filters(manager: "SessionManager") -> set[str]:
    filters: set[str] = set()
    for owner_filters in manager.output_stream_state.owners_by_writer_id.values():
        filters.update(owner_filters)
    return filters or set(DEFAULT_OUTPUT_STREAM_FILTERS)


async def set_diagnostics_output_stream(
    manager: "SessionManager",
    enabled: bool,
    filters: object,
    writer: asyncio.StreamWriter,
) -> JsonObject:
    writer_id = _writer_id(writer)
    previous = manager.output_stream_state.owners_by_writer_id.get(writer_id)

    if enabled:
        manager.output_stream_state.owners_by_writer_id[writer_id] = _normalize_filters(filters)
    else:
        manager.output_stream_state.owners_by_writer_id.pop(writer_id, None)

    effective_filters = _effective_filters(manager)
    daemon_enabled = bool(manager.output_stream_state.owners_by_writer_id)
    result = await _send_output_stream_command(
        manager,
        enabled=daemon_enabled,
        filters=effective_filters,
    )
    if result.get("status") != "ok":
        if previous is None:
            manager.output_stream_state.owners_by_writer_id.pop(writer_id, None)
        else:
            manager.output_stream_state.owners_by_writer_id[writer_id] = previous
        return result

    manager.output_stream_state.enabled = daemon_enabled
    manager.output_stream_state.filters = effective_filters
    data = json_object(result.get("data")) or {}
    return {
        "status": "ok",
        "data": {
            "enabled": daemon_enabled,
            "filters": sorted(
                _normalize_filters(data.get("filters")) if data else effective_filters
            ),
        },
    }


async def clear_output_stream_for_writer(
    manager: "SessionManager",
    writer: asyncio.StreamWriter,
) -> None:
    writer_id = _writer_id(writer)
    if writer_id not in manager.output_stream_state.owners_by_writer_id:
        return
    manager.output_stream_state.owners_by_writer_id.pop(writer_id, None)
    try:
        result = await _send_output_stream_command(
            manager,
            enabled=bool(manager.output_stream_state.owners_by_writer_id),
            filters=_effective_filters(manager),
        )
        if result.get("status") == "ok":
            manager.output_stream_state.enabled = bool(
                manager.output_stream_state.owners_by_writer_id
            )
            manager.output_stream_state.filters = _effective_filters(manager)
    except Exception as exc:
        log.debug("Failed to clear diagnostics output stream owner: %s", exc)


def broadcast_events_to_owners(manager: "SessionManager", data: JsonObject) -> None:
    owner_ids = set(manager.output_stream_state.owners_by_writer_id)
    if not owner_ids:
        return
    manager.broadcast_to_session_client_ids(
        {"event": "diagnostics_output_event", **data},
        owner_ids,
    )


def clear_all_output_stream_state(manager: "SessionManager") -> None:
    manager.output_stream_state.enabled = False
    manager.output_stream_state.filters = set(DEFAULT_OUTPUT_STREAM_FILTERS)
    manager.output_stream_state.owners_by_writer_id.clear()


async def _send_output_stream_command(
    manager: "SessionManager",
    *,
    enabled: bool,
    filters: set[str],
) -> JsonObject:
    try:
        result = await manager.client.send_command(
            Command(
                command=CommandType.SET_DIAGNOSTICS_OUTPUT_STREAM,
                data={"enabled": bool(enabled), "filters": sorted(filters)},
            )
        )
    except Exception:
        return {"status": "error", "message": "Daemon unavailable"}

    if result.status == "ok":
        return {"status": "ok", "data": result.data or {}}
    message = result.error or "Failed to update diagnostics output stream"
    response: JsonObject = {
        "status": "error",
        "message": message,
    }
    if "recording_locked" in message.lower():
        response["error_code"] = "recording_locked"
    return response
