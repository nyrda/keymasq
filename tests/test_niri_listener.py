import asyncio

import pytest

from keymasq.session.listeners.niri import (
    NIRI_DISPATCH_BUILDERS,
    NiriListener,
    normalize_niri_dispatcher,
    parse_niri_event,
    parse_niri_reply,
)

LISTENER_LAB_APP_ID = "io.github.nyrda.Keymasq.ListenerLab"


class _FakeWriter:
    def __init__(self) -> None:
        self.payloads: list[str] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.payloads.append(data.decode("utf-8"))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _FakeReader:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


def test_parse_niri_reply_ok() -> None:
    assert parse_niri_reply('{"Ok":"Handled"}') == (True, "Handled")


def test_parse_niri_reply_error() -> None:
    assert parse_niri_reply('{"Err":"boom"}') == (False, "boom")


def test_parse_niri_event_valid() -> None:
    assert parse_niri_event('{"WindowClosed":{"id":42}}') == ("WindowClosed", {"id": 42})


def test_parse_niri_event_rejects_invalid_json() -> None:
    assert parse_niri_event("not-json") is None


def test_workspace_dispatcher_accepts_index() -> None:
    ok, message, action = NIRI_DISPATCH_BUILDERS["focus-workspace"]("2")
    assert ok is True
    assert message == ""
    assert action == {"FocusWorkspace": {"reference": {"Index": 2}}}


def test_workspace_dispatcher_accepts_name() -> None:
    ok, message, action = NIRI_DISPATCH_BUILDERS["focus-workspace"]("name:web")
    assert ok is True
    assert message == ""
    assert action == {"FocusWorkspace": {"reference": {"Name": "web"}}}


def test_workspace_dispatcher_rejects_invalid_reference() -> None:
    ok, message, action = NIRI_DISPATCH_BUILDERS["focus-workspace"]("web")
    assert ok is False
    assert "workspace reference" in message
    assert action is None


def test_no_arg_dispatcher_rejects_args() -> None:
    ok, message, action = NIRI_DISPATCH_BUILDERS["close-window"]("unexpected")
    assert ok is False
    assert message == "CloseWindow does not accept arguments"
    assert action is None


def test_window_cycle_dispatchers_map_to_scrolling_focus_actions() -> None:
    ok, message, action = NIRI_DISPATCH_BUILDERS["focus-column-left-or-last"]("")
    assert ok is True
    assert message == ""
    assert action == {"FocusColumnLeftOrLast": {}}

    ok, message, action = NIRI_DISPATCH_BUILDERS["focus-column-right-or-first"]("")
    assert ok is True
    assert message == ""
    assert action == {"FocusColumnRightOrFirst": {}}


def test_normalize_niri_dispatcher_accepts_legacy_and_cli_formats() -> None:
    assert normalize_niri_dispatcher("focus_workspace") == "focus-workspace"
    assert normalize_niri_dispatcher("FocusWorkspace") == "focus-workspace"
    assert normalize_niri_dispatcher("niri msg action focus-workspace") == "focus-workspace"


def test_probe_available_requires_socket_env(monkeypatch) -> None:
    monkeypatch.delenv("NIRI_SOCKET", raising=False)
    assert asyncio.run(NiriListener.probe_available()) is False


def test_probe_available_checks_socket_connectivity(monkeypatch, tmp_path) -> None:
    socket_path = tmp_path / "niri.sock"
    socket_path.touch()
    monkeypatch.setenv("NIRI_SOCKET", str(socket_path))

    async def _connectable(_cls, _path, timeout_s: float = 0.2) -> bool:
        _ = timeout_s
        return True

    monkeypatch.setattr(NiriListener, "_connectable", classmethod(_connectable))
    assert asyncio.run(NiriListener.probe_available()) is True


@pytest.mark.asyncio
async def test_send_event_stream_request_writes_event_stream_request() -> None:
    async def _cb(_window_class: str, _window_title: str, _tags: list[str]) -> None:
        return

    listener = NiriListener(_cb)
    listener.reader = _FakeReader([b'{"Ok":"Handled"}\n'])  # type: ignore[assignment]
    listener.writer = _FakeWriter()  # type: ignore[assignment]

    await listener._send_event_stream_request()

    assert listener.writer.payloads == ['"EventStream"\n']  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_windows_changed_updates_focused_window_and_emits_callback() -> None:
    events: list[tuple[str, str, list[str]]] = []

    async def _cb(window_class: str, window_title: str, tags: list[str]) -> None:
        events.append((window_class, window_title, tags))

    listener = NiriListener(_cb)
    await listener._handle_event(
        "WindowsChanged",
        {
            "windows": [
                {
                    "id": 1,
                    "app_id": LISTENER_LAB_APP_ID,
                    "title": "Alpha",
                    "is_focused": True,
                },
                {
                    "id": 2,
                    "app_id": LISTENER_LAB_APP_ID,
                    "title": "Beta",
                    "is_focused": False,
                },
            ]
        },
    )

    assert listener._focused_window_id == 1
    assert await listener.get_active_window() == (LISTENER_LAB_APP_ID, "Alpha", [])
    assert events == [(LISTENER_LAB_APP_ID, "Alpha", [])]


@pytest.mark.asyncio
async def test_window_opened_or_changed_tracks_new_focused_window() -> None:
    events: list[tuple[str, str, list[str]]] = []

    async def _cb(window_class: str, window_title: str, tags: list[str]) -> None:
        events.append((window_class, window_title, tags))

    listener = NiriListener(_cb)
    listener._windows = {
        1: {
            "id": 1,
            "app_id": LISTENER_LAB_APP_ID,
            "title": "Alpha",
            "is_focused": True,
        }
    }
    listener._focused_window_id = 1
    listener._last_class = LISTENER_LAB_APP_ID
    listener._last_title = "Alpha"

    await listener._handle_event(
        "WindowOpenedOrChanged",
        {
            "window": {
                "id": 2,
                "app_id": LISTENER_LAB_APP_ID,
                "title": "Beta",
                "is_focused": True,
            }
        },
    )

    assert listener._focused_window_id == 2
    assert listener._windows[1]["is_focused"] is False
    assert events == [(LISTENER_LAB_APP_ID, "Beta", [])]


@pytest.mark.asyncio
async def test_window_focus_changed_to_none_clears_active_window() -> None:
    events: list[tuple[str, str, list[str]]] = []

    async def _cb(window_class: str, window_title: str, tags: list[str]) -> None:
        events.append((window_class, window_title, tags))

    listener = NiriListener(_cb)
    listener._windows = {
        1: {
            "id": 1,
            "app_id": LISTENER_LAB_APP_ID,
            "title": "Alpha",
            "is_focused": True,
        }
    }
    listener._focused_window_id = 1
    listener._last_class = LISTENER_LAB_APP_ID
    listener._last_title = "Alpha"

    await listener._handle_event("WindowFocusChanged", {"id": None})

    assert listener._focused_window_id is None
    assert events == [("", "", [])]


@pytest.mark.asyncio
async def test_get_active_window_refreshes_from_focused_window_request(monkeypatch) -> None:
    async def _cb(_window_class: str, _window_title: str, _tags: list[str]) -> None:
        return

    async def _send_cmd_request(_request: object, timeout_s: float) -> tuple[bool, object | None]:
        _ = timeout_s
        return (
            True,
            {
                "FocusedWindow": {
                    "id": 3,
                    "app_id": LISTENER_LAB_APP_ID,
                    "title": "Gamma",
                    "is_focused": True,
                }
            }
        )

    listener = NiriListener(_cb)
    listener.running = True
    monkeypatch.setattr(listener, "_send_cmd_request", _send_cmd_request)

    assert await listener.get_active_window() == (LISTENER_LAB_APP_ID, "Gamma", [])


@pytest.mark.asyncio
async def test_get_active_window_falls_back_to_windows_snapshot(monkeypatch) -> None:
    async def _cb(_window_class: str, _window_title: str, _tags: list[str]) -> None:
        return

    requests: list[object] = []

    async def _send_cmd_request(_request: object, timeout_s: float) -> tuple[bool, object | None]:
        _ = timeout_s
        requests.append(_request)
        if _request == "FocusedWindow":
            return True, {"FocusedWindow": None}
        if _request == "Windows":
            return True, {
                "Windows": [
                    {
                        "id": 7,
                        "app_id": LISTENER_LAB_APP_ID,
                        "title": "Alpha",
                        "is_focused": False,
                    }
                ]
            }
        return False, None

    listener = NiriListener(_cb)
    listener.running = True
    monkeypatch.setattr(listener, "_send_cmd_request", _send_cmd_request)

    assert await listener.get_active_window() == (LISTENER_LAB_APP_ID, "Alpha", [])
    assert requests == ["FocusedWindow", "Windows"]


@pytest.mark.asyncio
async def test_activate_window_by_title_updates_cached_window_state(monkeypatch) -> None:
    events: list[tuple[str, str, list[str]]] = []

    async def _cb(window_class: str, window_title: str, tags: list[str]) -> None:
        events.append((window_class, window_title, tags))

    requests: list[object] = []

    async def _send_cmd_request(request: object, timeout_s: float) -> tuple[bool, object | None]:
        _ = timeout_s
        requests.append(request)
        if request == "Windows":
            return True, {
                "Windows": [
                    {
                        "id": 5,
                        "app_id": LISTENER_LAB_APP_ID,
                        "title": "Alpha",
                        "is_focused": False,
                    },
                    {
                        "id": 6,
                        "app_id": LISTENER_LAB_APP_ID,
                        "title": "Beta",
                        "is_focused": False,
                    },
                ]
            }
        if request == {"Action": {"FocusWindow": {"id": 6}}}:
            return True, "Handled"
        return False, None

    listener = NiriListener(_cb)
    monkeypatch.setattr(listener, "_send_cmd_request", _send_cmd_request)

    result = await listener.activate_window_by_title("Beta")

    assert result == {"found": True, "id": 6, "title": "Beta"}
    assert listener._focused_window_id == 6
    assert await listener.get_active_window() == (LISTENER_LAB_APP_ID, "Beta", [])
    assert requests == ["Windows", {"Action": {"FocusWindow": {"id": 6}}}]
    assert events == [(LISTENER_LAB_APP_ID, "Beta", [])]


@pytest.mark.asyncio
async def test_dispatch_requires_socket_for_custom_niri_actions() -> None:
    async def _cb(_window_class: str, _window_title: str, _tags: list[str]) -> None:
        return

    listener = NiriListener(_cb)
    ok, message = await listener.dispatch("toggle-overview")
    assert ok is False
    assert message == "NIRI_SOCKET is not available"


@pytest.mark.asyncio
async def test_dispatch_sends_action_request(monkeypatch) -> None:
    async def _cb(_window_class: str, _window_title: str, _tags: list[str]) -> None:
        return

    requests: list[tuple[object, float]] = []

    async def _send_cmd_request(
        request: object,
        timeout_s: float,
    ) -> tuple[bool, object | None]:
        requests.append((request, timeout_s))
        return True, "Handled"

    listener = NiriListener(_cb)
    monkeypatch.setattr(listener, "_send_cmd_request", _send_cmd_request)

    ok, message = await listener.dispatch("focus-workspace", "2")

    assert ok is True
    assert message == "ok"
    assert requests == [
        ({"Action": {"FocusWorkspace": {"reference": {"Index": 2}}}}, 1.5)
    ]


@pytest.mark.asyncio
async def test_dispatch_uses_cached_window_id_for_focused_window_actions(monkeypatch) -> None:
    async def _cb(_window_class: str, _window_title: str, _tags: list[str]) -> None:
        return

    requests: list[tuple[object, float]] = []

    async def _send_cmd_request(
        request: object,
        timeout_s: float,
    ) -> tuple[bool, object | None]:
        requests.append((request, timeout_s))
        return True, "Handled"

    listener = NiriListener(_cb)
    listener._focused_window_id = 9
    monkeypatch.setattr(listener, "_send_cmd_request", _send_cmd_request)

    ok, message = await listener.dispatch("toggle-window-floating")

    assert ok is True
    assert message == "ok"
    assert requests == [
        ({"Action": {"ToggleWindowFloating": {"id": 9}}}, 1.5)
    ]


@pytest.mark.asyncio
async def test_dispatch_falls_back_to_niri_msg_action_for_custom_dispatchers(monkeypatch) -> None:
    async def _cb(_window_class: str, _window_title: str, _tags: list[str]) -> None:
        return

    recorded: dict[str, object] = {}

    class _FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def _create_subprocess_exec(*cmd: object, **kwargs: object) -> _FakeProcess:
        recorded["cmd"] = cmd
        recorded["env"] = kwargs.get("env")
        return _FakeProcess()

    listener = NiriListener(_cb)
    listener.socket_path = "/tmp/niri.sock"
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess_exec)

    ok, message = await listener.dispatch("toggle-overview")

    assert ok is True
    assert message == "ok"
    assert recorded["cmd"] == ("niri", "msg", "action", "toggle-overview")
    assert isinstance(recorded["env"], dict)
    assert recorded["env"]["NIRI_SOCKET"] == "/tmp/niri.sock"


@pytest.mark.asyncio
async def test_dispatch_accepts_prefixed_niri_msg_action_syntax(monkeypatch) -> None:
    async def _cb(_window_class: str, _window_title: str, _tags: list[str]) -> None:
        return

    recorded: dict[str, object] = {}

    class _FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def _create_subprocess_exec(*cmd: object, **kwargs: object) -> _FakeProcess:
        recorded["cmd"] = cmd
        return _FakeProcess()

    listener = NiriListener(_cb)
    listener.socket_path = "/tmp/niri.sock"
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess_exec)

    ok, message = await listener.dispatch("niri msg action focus-window", "--id 17")

    assert ok is True
    assert message == "ok"
    assert recorded["cmd"] == ("niri", "msg", "action", "focus-window", "--id", "17")


@pytest.mark.asyncio
async def test_dispatch_accepts_full_niri_msg_action_command_in_dispatcher_field(
    monkeypatch,
) -> None:
    async def _cb(_window_class: str, _window_title: str, _tags: list[str]) -> None:
        return

    requests: list[tuple[object, float]] = []

    async def _send_cmd_request(
        request: object,
        timeout_s: float,
    ) -> tuple[bool, object | None]:
        requests.append((request, timeout_s))
        return True, "Handled"

    listener = NiriListener(_cb)
    listener.socket_path = "/tmp/niri.sock"
    monkeypatch.setattr(listener, "_send_cmd_request", _send_cmd_request)

    ok, message = await listener.dispatch("niri msg action focus-workspace 2")

    assert ok is True
    assert message == "ok"
    assert requests == [
        ({"Action": {"FocusWorkspace": {"reference": {"Index": 2}}}}, 1.5)
    ]
