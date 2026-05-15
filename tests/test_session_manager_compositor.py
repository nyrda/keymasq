from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import keymasq.session.manager.compositor as session_compositor_module
from keymasq.session.manager import SessionManager


@pytest.mark.asyncio
async def test_compositor_dispatch_calls_active_listener_even_when_unsupported() -> None:
    manager = SessionManager()
    listener = SimpleNamespace(
        supports_compositor_dispatch=False,
        dispatch=AsyncMock(return_value=(False, "x11 does not implement compositor dispatch")),
    )
    manager.compositor_state.window_listener = listener
    manager.compositor_state.compositor_id = "x11"

    await session_compositor_module.handle_compositor_dispatch_trigger(
        manager,
        {"dispatcher": "workspace", "args": "2"},
    )

    listener.dispatch.assert_awaited_once_with("workspace", "2")


@pytest.mark.asyncio
async def test_compositor_dispatch_ignores_mismatched_target_compositor() -> None:
    manager = SessionManager()
    listener = SimpleNamespace(
        supports_compositor_dispatch=True,
        dispatch=AsyncMock(return_value=(True, "ok")),
    )
    manager.compositor_state.window_listener = listener
    manager.compositor_state.compositor_id = "gnome"

    await session_compositor_module.handle_compositor_dispatch_trigger(
        manager,
        {"compositor": "hyprland", "dispatcher": "workspace", "args": "2"},
    )

    listener.dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_compositor_dispatch_returns_listener_result() -> None:
    manager = SessionManager()
    listener = SimpleNamespace(
        supports_compositor_dispatch=True,
        dispatch=AsyncMock(return_value=(True, "ok")),
    )
    manager.compositor_state.window_listener = listener
    manager.compositor_state.compositor_id = "niri"

    ok, message = await session_compositor_module.run_compositor_dispatch(
        manager,
        "niri",
        "toggle-window-floating",
        "",
    )

    assert ok is True
    assert message == "ok"
    listener.dispatch.assert_awaited_once_with("toggle-window-floating", "")


@pytest.mark.asyncio
async def test_run_compositor_setup_action_delegates_to_gnome_and_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.compositor_state.compositor_id = "gnome"

    async def run_setup_action(_cls, action: str, dbus=None) -> tuple[bool, str]:
        assert action == "enable_bridge"
        assert dbus is manager.dbus
        return True, "enabled"

    refresh = AsyncMock(return_value={"compositor_id": "gnome", "supported": True})
    monkeypatch.setattr(
        session_compositor_module.GnomeListener,
        "run_setup_action",
        classmethod(run_setup_action),
    )
    monkeypatch.setattr(session_compositor_module, "refresh_compositor_binding", refresh)

    result = await session_compositor_module.run_compositor_setup_action(
        manager,
        "gnome",
        "enable_bridge",
    )

    assert result["status"] == "ok"
    assert result["message"] == "enabled"
    assert result["compositor"] == {"compositor_id": "gnome", "supported": True}
    refresh.assert_awaited_once_with(manager)


@pytest.mark.asyncio
async def test_compositor_degraded_mode_retries_when_unsupported_or_listener_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.compositor_state.listener_retry_interval_s = 0.01

    async def unsupported(_compositor_id: str | None, _dbus=None) -> bool:
        return False

    monkeypatch.setattr("keymasq.session.manager.compositor.is_compositor_supported", unsupported)

    await session_compositor_module.switch_compositor(manager, "wayland")
    assert manager.compositor_state.compositor_id == "wayland"
    assert manager.compositor_state.window_listener is None
    assert "wayland" in manager.compositor_state.listener_retry_after

    async def supported(_compositor_id: str | None, _dbus=None) -> bool:
        return True

    monkeypatch.setattr("keymasq.session.manager.compositor.is_compositor_supported", supported)

    async def fail_listener_start(_manager: SessionManager) -> None:
        manager.compositor_state.window_listener = None
        manager.compositor_state.last_listener_start_error = "listener boot failed"

    monkeypatch.setattr(session_compositor_module, "start_window_listener", fail_listener_start)
    await session_compositor_module.switch_compositor(manager, "x11")

    assert manager.compositor_state.compositor_id == "x11"
    assert manager.compositor_state.window_listener is None
    assert "x11" in manager.compositor_state.listener_retry_after
