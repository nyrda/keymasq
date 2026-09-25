import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import keymasq.session.manager.compositor as session_compositor_module
from keymasq.common.model.profiles import ProfileConfig, WindowRule
from keymasq.session.manager.core import SessionManager


def _save_conditional_profile(
    manager: SessionManager,
    name: str,
    field: str,
    pattern: str,
) -> None:
    manager.profiles.save_profile(
        ProfileConfig(
            name=name,
            enabled=True,
            is_permanent=False,
            window_rules=[WindowRule(field=field, pattern=pattern)],
        )
    )


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
@pytest.mark.parametrize("target_compositor", [None, "wayland"])
async def test_switch_compositor_clears_stale_window_and_reevaluates(
    monkeypatch: pytest.MonkeyPatch,
    target_compositor: str | None,
) -> None:
    manager = SessionManager()
    listener = SimpleNamespace(stop=AsyncMock())
    manager.compositor_state.window_listener = listener
    manager.compositor_state.compositor_id = "x11"
    manager.compositor_state.current_window = {
        "class": "Game",
        "title": "stale",
        "tags": [],
    }

    async def unsupported(_compositor_id: str | None, _dbus=None) -> bool:
        return False

    reevaluate_profiles = AsyncMock()
    monkeypatch.setattr(session_compositor_module, "is_compositor_supported", unsupported)
    monkeypatch.setattr(
        session_compositor_module.coordinator,
        "reevaluate_profiles",
        reevaluate_profiles,
    )

    await session_compositor_module.switch_compositor(manager, target_compositor)

    assert manager.compositor_state.current_window == {}
    assert manager.compositor_state.compositor_id == target_compositor
    listener.stop.assert_awaited_once()
    reevaluate_profiles.assert_awaited_once_with(manager, reason="compositor changed")


@pytest.mark.asyncio
async def test_refresh_current_window_clears_stale_window_on_empty_listener_data() -> None:
    manager = SessionManager()
    manager.compositor_state.window_listener = SimpleNamespace(
        active_layer="",
        get_active_window=AsyncMock(return_value=("", "", []))
    )
    manager.compositor_state.current_window = {
        "class": "Game",
        "title": "stale",
        "tags": [],
    }

    result = await session_compositor_module.refresh_current_window_from_listener(manager)

    assert result is None
    assert manager.compositor_state.current_window == {}


@pytest.mark.asyncio
async def test_get_active_window_reevaluates_when_listener_updates_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    _save_conditional_profile(manager, "Steam", "class", "steam")
    manager.compositor_state.window_listener = SimpleNamespace(
        active_layer="",
        get_active_window=AsyncMock(return_value=("steam", "Game", ["fullscreen"]))
    )
    reevaluate_profiles = AsyncMock()
    monkeypatch.setattr(
        session_compositor_module.coordinator,
        "reevaluate_profiles",
        reevaluate_profiles,
    )

    result = await session_compositor_module.get_active_window_payload(manager)

    assert result == {
        "status": "ok",
        "class": "steam",
        "title": "Game",
        "tags": ["fullscreen"],
    }
    reevaluate_profiles.assert_awaited_once_with(manager, reason="active window changed")


@pytest.mark.asyncio
async def test_focused_layer_reaches_window_state_and_reevaluates_layer_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    _save_conditional_profile(manager, "Launcher", "layer", "^launcher$")
    manager.compositor_state.compositor_capabilities = ["layer_focus"]
    manager.compositor_state.window_listener = SimpleNamespace(
        active_layer="launcher",
        get_active_window=AsyncMock(return_value=("", "", [])),
    )
    reevaluate_profiles = AsyncMock()
    monkeypatch.setattr(
        session_compositor_module.coordinator,
        "reevaluate_profiles",
        reevaluate_profiles,
    )
    focused_layer = {"class": "", "title": "", "tags": [], "layer": "launcher"}

    await manager.on_window_change("", "", [])

    assert manager.compositor_state.current_window == focused_layer
    reevaluate_profiles.assert_awaited_once_with(manager, reason="window changed")

    manager.compositor_state.current_window = {}
    reevaluate_profiles.reset_mock()

    assert await session_compositor_module.get_active_window_payload(manager) == {
        "status": "ok",
        **focused_layer,
    }
    reevaluate_profiles.assert_awaited_once_with(manager, reason="active window changed")


@pytest.mark.asyncio
async def test_get_active_window_reevaluates_when_listener_clears_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    _save_conditional_profile(manager, "Games", "class", "Game")
    manager.compositor_state.window_listener = SimpleNamespace(
        active_layer="",
        get_active_window=AsyncMock(return_value=("", "", []))
    )
    manager.compositor_state.current_window = {
        "class": "Game",
        "title": "stale",
        "tags": [],
    }
    reevaluate_profiles = AsyncMock()
    monkeypatch.setattr(
        session_compositor_module.coordinator,
        "reevaluate_profiles",
        reevaluate_profiles,
    )

    result = await session_compositor_module.get_active_window_payload(manager)

    assert result == {
        "status": "error",
        "message": "Active window is unavailable on this compositor",
    }
    assert manager.compositor_state.current_window == {}
    reevaluate_profiles.assert_awaited_once_with(manager, reason="active window changed")


@pytest.mark.asyncio
async def test_on_window_change_skips_reevaluate_for_irrelevant_title_churn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    _save_conditional_profile(manager, "Browser", "class", "firefox")
    manager.compositor_state.current_window = {
        "class": "firefox",
        "title": "old tab",
        "tags": [],
    }
    reevaluate_profiles = AsyncMock()
    monkeypatch.setattr(
        session_compositor_module.coordinator,
        "reevaluate_profiles",
        reevaluate_profiles,
    )

    await session_compositor_module.on_window_change(manager, "firefox", "new tab", [])

    assert manager.compositor_state.current_window == {
        "class": "firefox",
        "title": "new tab",
        "tags": [],
    }
    reevaluate_profiles.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_window_change_reevaluates_when_relevant_field_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    _save_conditional_profile(manager, "Music", "title", "Now Playing")
    manager.compositor_state.current_window = {
        "class": "player",
        "title": "paused",
        "tags": [],
    }
    reevaluate_profiles = AsyncMock()
    monkeypatch.setattr(
        session_compositor_module.coordinator,
        "reevaluate_profiles",
        reevaluate_profiles,
    )

    await session_compositor_module.on_window_change(manager, "player", "Now Playing: song", [])

    assert manager.compositor_state.current_window == {
        "class": "player",
        "title": "Now Playing: song",
        "tags": [],
    }
    reevaluate_profiles.assert_awaited_once_with(manager, reason="window changed")


@pytest.mark.asyncio
async def test_on_window_change_skips_reevaluate_without_conditional_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.compositor_state.current_window = {
        "class": "firefox",
        "title": "old tab",
        "tags": [],
    }
    reevaluate_profiles = AsyncMock()
    monkeypatch.setattr(
        session_compositor_module.coordinator,
        "reevaluate_profiles",
        reevaluate_profiles,
    )

    await session_compositor_module.on_window_change(manager, "firefox", "new tab", [])

    assert manager.compositor_state.current_window == {
        "class": "firefox",
        "title": "new tab",
        "tags": [],
    }
    reevaluate_profiles.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_active_window_skips_reevaluate_for_irrelevant_title_churn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    _save_conditional_profile(manager, "Browser", "class", "firefox")
    manager.compositor_state.current_window = {
        "class": "firefox",
        "title": "old tab",
        "tags": [],
    }
    manager.compositor_state.window_listener = SimpleNamespace(
        active_layer="",
        get_active_window=AsyncMock(return_value=("firefox", "new tab", []))
    )
    reevaluate_profiles = AsyncMock()
    monkeypatch.setattr(
        session_compositor_module.coordinator,
        "reevaluate_profiles",
        reevaluate_profiles,
    )

    result = await session_compositor_module.get_active_window_payload(manager)

    assert result == {
        "status": "ok",
        "class": "firefox",
        "title": "new tab",
        "tags": [],
    }
    reevaluate_profiles.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_switch_compositor_times_out_gnome_support_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    monkeypatch.setattr(session_compositor_module, "SUPPORT_DETAILS_TIMEOUT_S", 0.01)

    async def hanging_support_details(
        _compositor_id: str | None,
        _dbus=None,
    ) -> dict[str, bool | str]:
        await asyncio.Event().wait()
        return {"supported": True}

    start_listener = AsyncMock()
    monkeypatch.setattr(
        session_compositor_module,
        "get_compositor_support_details",
        hanging_support_details,
    )
    monkeypatch.setattr(session_compositor_module, "start_window_listener", start_listener)

    await session_compositor_module.switch_compositor(manager, "gnome")

    assert manager.compositor_state.compositor_id == "gnome"
    assert manager.compositor_state.window_listener is None
    assert manager.compositor_state.support_details_cache == {
        "supported": False,
        "warning": "Compositor support status query timed out.",
    }
    assert manager.compositor_state.listener_last_error["gnome"] == (
        "Compositor support status query timed out."
    )
    assert "gnome" in manager.compositor_state.listener_retry_after
    start_listener.assert_not_awaited()
