import inspect
from types import SimpleNamespace

from keymasq.session.listeners import x11 as x11_listener_module
from keymasq.session.listeners.x11 import X11Listener


async def _cb(_window_class: str, _window_title: str, _tags: list[str]) -> None:
    return


def test_x11_handle_event_syncs_on_active_window_property(monkeypatch) -> None:
    listener = X11Listener(_cb)
    listener._root = SimpleNamespace(id=10)
    listener._atom_active = 99

    called = {"sync": False}

    def _sync() -> None:
        called["sync"] = True

    listener._sync_active_window_watch_unlocked = _sync
    monkeypatch.setattr(
        x11_listener_module,
        "X",
        SimpleNamespace(PropertyNotify=1),
    )

    event = SimpleNamespace(type=1, window=SimpleNamespace(id=10), atom=99)
    assert listener._handle_x_event_unlocked(event) is True
    assert called["sync"] is True


def test_x11_handle_event_tracks_active_window_metadata(monkeypatch) -> None:
    listener = X11Listener(_cb)
    listener._active_window_id = 42
    listener._window_watch_atoms = {7, 8}

    monkeypatch.setattr(
        x11_listener_module,
        "X",
        SimpleNamespace(PropertyNotify=1),
    )

    event = SimpleNamespace(type=1, window=SimpleNamespace(id=42), atom=7)
    assert listener._handle_x_event_unlocked(event) is True


def test_x11_handle_event_ignores_unrelated_window_metadata(monkeypatch) -> None:
    listener = X11Listener(_cb)
    listener._active_window_id = 42
    listener._window_watch_atoms = {7, 8}

    monkeypatch.setattr(
        x11_listener_module,
        "X",
        SimpleNamespace(PropertyNotify=1),
    )

    event = SimpleNamespace(type=1, window=SimpleNamespace(id=99), atom=7)
    assert listener._handle_x_event_unlocked(event) is False


def test_x11_listener_uses_event_wait_not_poll_sleep() -> None:
    source = inspect.getsource(X11Listener._listen)
    assert "await self._fd_event.wait()" in source
    assert "asyncio.sleep" not in source
