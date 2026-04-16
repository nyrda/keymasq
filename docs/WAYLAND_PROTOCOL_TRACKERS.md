# Wayland Protocol Trackers

This document describes the reusable async trackers for event-driven active-window updates on Wayland protocol backends.

## Purpose

Keymasq includes two protocol-level tracker libraries that compositor-specific listeners can reuse:

- `ExtForeignToplevelListTracker`
- `WlrForeignToplevelManagerTracker`

Both trackers provide a shared model for:

- active window change tracking
- extraction of class/app id + title
- async event delivery for listeners without polling loops

## Location

- `keymasq/session/wayland_protocols/ext_foreign_toplevel_list.py`
- `keymasq/session/wayland_protocols/ext_foreign_toplevel_list_client.py`
- `keymasq/session/wayland_protocols/wlr_foreign_toplevel_manager.py`
- `keymasq/session/wayland_protocols/wlr_foreign_toplevel_client.py`
- `keymasq/session/wayland_protocols/__init__.py`

## Shared Behavior

Each tracker maintains per-window state and tracks the currently active handle. They expose:

- `add_toplevel(handle_id)`
- `close_toplevel(handle_id)`
- `update_app_id(handle_id, app_id)`
- `update_title(handle_id, title)`
- `update_state(handle_id, state_payload)`
- `get_active_window() -> tuple[str, str]`
- `next_active_window(timeout: float | None = None) -> tuple[str, str] | None`

`next_active_window(...)` is async and returns only when the active window tuple changed. This is intended for event-driven listener tasks.

## Protocol Notes

### `ext_foreign_toplevel_list`

- Designed for `ext_foreign_toplevel_list_v1` style event sources.
- Supports `mark_done()` to flush the initial state and emit current active tuple after initial sync.
- Activation detection accepts multiple payload forms (`int`, `list`, `dict`, `str`, `bytes`) for adapter flexibility.

### `ext_foreign_toplevel_list_client`

- Reusable async Wayland wire client for `ext_foreign_toplevel_list_v1`.
- Binds registry globals, maps toplevel metadata events (`app_id`, `title`, `closed`) to `ExtForeignToplevelListTracker`, and runs event-driven via socket events.
- Note: base `ext_foreign_toplevel_list_v1` does not carry an explicit active-window state; compositor adapters should provide activation state from companion protocol/events and call tracker `update_state(...)`.

### `wlr_foreign_toplevel_manager`

- Designed for `zwlr_foreign_toplevel_manager_v1` style event sources.
- Handles wlroots activated state enum (`2`) and byte-array packed state payloads.

### `wlr_foreign_toplevel_client`

- Reusable async Wayland wire client for `zwlr_foreign_toplevel_manager_v1`.
- Binds registry globals, maps handle events to `WlrForeignToplevelManagerTracker`, and exposes a long-running `run()` event loop without polling.

## Integration Pattern

Compositor/protocol adapters should:

1. create one tracker instance
2. map protocol callbacks to tracker update methods
3. run an async task that awaits `next_active_window()` and forwards updates into Keymasq listener callback flow

Example shape:

```python
tracker = WlrForeignToplevelManagerTracker()

async def forward_changes() -> None:
    while running:
        update = await tracker.next_active_window()
        if not update:
            continue
        window_class, window_title = update
        await callback(window_class, window_title, [])
```

## Current Integration Status

- `WlrootsWaylandListener` now uses `WlrForeignToplevelWaylandClient` with
  `zwlr_foreign_toplevel_manager_v1` for active-window tracking.
- `CosmicListener` uses `ext_foreign_toplevel_list_v1` + `zcosmic_toplevel_info_v1` and derives active-window changes from the COSMIC toplevel state events.
- Event flow is fully event-driven (Wayland socket events + tracker queue), without polling loops.
- Active window callback payloads are sourced from protocol `app_id` + `title`.

## Tests

- `tests/test_wayland_protocol_trackers.py`
- `tests/test_wayland_wlr_client.py`
- `tests/test_wayland_ext_client.py`

Coverage includes:

- active window emission on activation
- metadata updates for active window
- wlroots byte-state activation decoding
- active window switching between handles
