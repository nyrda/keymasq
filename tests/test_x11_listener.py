import asyncio
import logging
from types import SimpleNamespace

import pytest

from keymasq.session.listeners import x11 as x11_listener_module
from keymasq.session.listeners.x11 import X11Listener


async def _cb(_window_class: str, _window_title: str, _tags: list[str]) -> None:
    return


@pytest.mark.parametrize(
    (
        "listener_state",
        "event_window_id",
        "event_atom",
        "expected_handled",
        "expected_sync",
    ),
    [
        pytest.param(
            {"_root": SimpleNamespace(id=10), "_atom_active": 99},
            10,
            99,
            True,
            True,
            id="root-active-window-property",
        ),
        pytest.param(
            {"_active_window_id": 42, "_window_watch_atoms": {7, 8}},
            42,
            7,
            True,
            False,
            id="active-window-metadata",
        ),
        pytest.param(
            {"_active_window_id": 42, "_window_watch_atoms": {7, 8}},
            99,
            7,
            False,
            False,
            id="unrelated-window-metadata",
        ),
    ],
)
def test_x11_handle_property_notify_event_routes(
    monkeypatch,
    listener_state: dict[str, object],
    event_window_id: int,
    event_atom: int,
    expected_handled: bool,
    expected_sync: bool,
) -> None:
    listener = X11Listener(_cb)
    for name, value in listener_state.items():
        setattr(listener, name, value)

    sync_called = False

    def _sync() -> None:
        nonlocal sync_called
        sync_called = True

    listener._sync_active_window_watch_unlocked = _sync

    monkeypatch.setattr(
        x11_listener_module,
        "xlib_constants",
        SimpleNamespace(PropertyNotify=1),
    )

    event = SimpleNamespace(type=1, window=SimpleNamespace(id=event_window_id), atom=event_atom)
    assert listener._handle_x_event_unlocked(event) is expected_handled
    assert sync_called is expected_sync


def test_x11_probe_available_requires_openable_display(monkeypatch) -> None:
    async def _unexpected_socket_probe(*_args, **_kwargs):
        raise AssertionError("probe_available should validate displays through Xlib")

    monkeypatch.setattr(x11_listener_module, "has_x11_support", lambda: True)
    monkeypatch.setattr(X11Listener, "_candidate_displays", classmethod(lambda _cls: [":0"]))
    monkeypatch.setattr(X11Listener, "_can_open_display", classmethod(lambda _cls, _name: False))
    monkeypatch.setattr(
        x11_listener_module.asyncio,
        "open_unix_connection",
        _unexpected_socket_probe,
    )

    assert asyncio.run(X11Listener.probe_available()) is False


def test_x11_can_open_display_logs_expected_failures(monkeypatch, caplog) -> None:
    class _DisplayModule:
        def Display(self, _display: str) -> object:  # noqa: N802 - Xlib API
            raise OSError("display unavailable")

    monkeypatch.setattr(x11_listener_module, "xlib_display", _DisplayModule())

    with caplog.at_level(logging.DEBUG, logger="keymasq-session.listeners.x11"):
        assert X11Listener._can_open_display(":99") is False

    assert "Could not open X11 display :99: display unavailable" in caplog.text


def test_x11_can_open_display_logs_unexpected_failures(monkeypatch, caplog) -> None:
    class _DisplayModule:
        def Display(self, _display: str) -> object:  # noqa: N802 - Xlib API
            raise RuntimeError("display bug")

    monkeypatch.setattr(x11_listener_module, "xlib_display", _DisplayModule())

    with caplog.at_level(logging.ERROR, logger="keymasq-session.listeners.x11"):
        assert X11Listener._can_open_display(":99") is False

    assert "Could not open X11 display :99" in caplog.text
    assert "display bug" in caplog.text


def test_x11_get_active_window_logs_unexpected_query_failures(caplog) -> None:
    async def run() -> None:
        listener = X11Listener(_cb)

        def _raise_query() -> tuple[str, str, list[str]]:
            raise RuntimeError("query bug")

        listener._query_active_window = _raise_query

        with caplog.at_level(logging.ERROR, logger="keymasq-session.listeners.x11"):
            assert await listener.get_active_window() == ("", "", [])

        assert "X11 active window query failed" in caplog.text
        assert "query bug" in caplog.text

    asyncio.run(run())


def test_x11_query_active_window_id_ignores_malformed_property(monkeypatch) -> None:
    listener = X11Listener(_cb)
    listener._root = SimpleNamespace(
        get_full_property=lambda *_args: SimpleNamespace(value=["not-int"])
    )
    listener._atom_active = 1
    monkeypatch.setattr(
        x11_listener_module,
        "xlib_constants",
        SimpleNamespace(AnyPropertyType=0),
    )

    assert listener._query_active_window_id_unlocked() is None


def test_x11_listener_waits_for_fd_event_before_draining(monkeypatch) -> None:
    async def run() -> None:
        real_event = asyncio.Event

        class _RecordingEvent:
            def __init__(self) -> None:
                self.wait_started = real_event()
                self.release = real_event()
                self.wait_count = 0
                self.clear_count = 0
                self._is_set = False
                events.append(self)

            async def wait(self) -> bool:
                self.wait_count += 1
                self.wait_started.set()
                await self.release.wait()
                return True

            def set(self) -> None:
                self._is_set = True
                self.release.set()

            def clear(self) -> None:
                self.clear_count += 1
                self._is_set = False

            def is_set(self) -> bool:
                return self._is_set

        events: list[_RecordingEvent] = []
        emitted: list[None] = []
        drain_calls: list[None] = []
        listener = X11Listener(_cb)
        listener._xdisplay = object()
        listener.running = True

        async def _emit() -> None:
            emitted.append(None)

        def _drain() -> bool:
            drain_calls.append(None)
            listener.running = False
            return False

        monkeypatch.setattr(x11_listener_module.asyncio, "Event", _RecordingEvent)
        listener._get_display_fd = lambda: -1
        listener._emit_active_window_if_changed = _emit
        listener._drain_events = _drain

        task = asyncio.create_task(listener._listen())
        try:
            while not events:
                await asyncio.sleep(0)
            event = events[0]
            await asyncio.wait_for(event.wait_started.wait(), timeout=1.0)

            assert emitted == [None]
            assert drain_calls == []
            assert event.wait_count == 1

            event.set()
            await asyncio.wait_for(task, timeout=1.0)

            assert drain_calls == [None]
            assert event.clear_count == 1
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(run())
