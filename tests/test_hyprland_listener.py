from unittest.mock import AsyncMock

import pytest

from keymasq.session.listeners.hyprland import HyprlandListener


async def _noop_callback(_window_class: str, _window_title: str, _tags: list[str]) -> None:
    return


@pytest.mark.asyncio
async def test_hyprland_set_cursor_position_uses_movecursor_dispatcher() -> None:
    listener = HyprlandListener(_noop_callback)
    listener.dispatch = AsyncMock(return_value=(True, "ok"))  # type: ignore[method-assign]

    assert await listener.set_cursor_position(123, 456) == (True, "ok")

    listener.dispatch.assert_awaited_once_with("movecursor", "123 456")
