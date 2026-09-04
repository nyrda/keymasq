import asyncio
import logging
from functools import partial

import pytest

from keymasq.session.listeners.niri import (
    NIRI_DISPATCH_BUILDERS,
    NiriListener,
    normalize_niri_dispatcher,
    parse_niri_event,
    parse_niri_focused_window_response,
    parse_niri_reply,
)
from tests.async_fakes import FakeProcess as _FakeProcess
from tests.async_fakes import FakeStreamWriter, make_stream_reader

LISTENER_LAB_APP_ID = "tools.keymasq.ListenerLab"


async def _noop_callback(_window_class: str, _window_title: str, _tags: list[str]) -> None:
    return


def _decode_niri_payload(data: bytes) -> str:
    return data.decode("utf-8")


_FakeWriter = partial(FakeStreamWriter, payload_decoder=_decode_niri_payload)


def test_parse_niri_reply_ok() -> None:
    assert parse_niri_reply('{"Ok":"Handled"}') == (True, "Handled")


def test_parse_niri_reply_error() -> None:
    assert parse_niri_reply('{"Err":"boom"}') == (False, "boom")


def test_parse_niri_event_valid() -> None:
    assert parse_niri_event('{"WindowClosed":{"id":42}}') == ("WindowClosed", {"id": 42})


def test_parse_niri_event_rejects_invalid_json() -> None:
    assert parse_niri_event("not-json") is None


def test_parse_niri_focused_window_response_valid() -> None:
    assert parse_niri_focused_window_response(
        '{"Ok":{"FocusedWindow":{"id":42,"app_id":"app","title":"Title"}}}'
    ) == {"id": 42, "app_id": "app", "title": "Title"}


def test_parse_niri_focused_window_response_rejects_other_variants() -> None:
    assert parse_niri_focused_window_response('{"Ok":{"Windows":[]}}') is None


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
async def test_send_cmd_request_retries_after_eof(monkeypatch) -> None:
    listener = NiriListener(_noop_callback)
    pairs = [
        (make_stream_reader([b""]), _FakeWriter()),
        (make_stream_reader([b'{"Ok":"Handled"}\n']), _FakeWriter()),
    ]

    async def fake_ensure() -> bool:
        if listener._cmd_reader is None or listener._cmd_writer is None:
            if not pairs:
                return False
            listener._cmd_reader, listener._cmd_writer = pairs.pop(0)  # type: ignore[assignment]
        return True

    monkeypatch.setattr(listener, "_ensure_cmd_connection", fake_ensure)

    ok, body = await listener._send_cmd_request("Windows", timeout_s=0.5)

    assert ok is True
    assert body == "Handled"


@pytest.mark.asyncio
async def test_send_cmd_request_logs_malformed_replies(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    listener = NiriListener(_noop_callback)
    pairs = [
        (make_stream_reader([b"{not-json\n"]), _FakeWriter()),
    ]

    async def fake_ensure() -> bool:
        if listener._cmd_reader is None or listener._cmd_writer is None:
            if not pairs:
                return False
            listener._cmd_reader, listener._cmd_writer = pairs.pop(0)  # type: ignore[assignment]
        return True

    monkeypatch.setattr(listener, "_ensure_cmd_connection", fake_ensure)

    with caplog.at_level(logging.DEBUG, logger="keymasq-session.listeners.niri"):
        ok, body = await listener._send_cmd_request({"Action": {}}, timeout_s=0.5)

    assert ok is False
    assert body is None
    assert "Niri command reply was invalid" in caplog.text


@pytest.mark.asyncio
async def test_send_cmd_request_does_not_retry_sent_action_after_eof(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    listener = NiriListener(_noop_callback)
    first_writer = _FakeWriter()
    second_writer = _FakeWriter()
    pairs = [
        (make_stream_reader([b""]), first_writer),
        (make_stream_reader([b'{"Ok":"Handled"}\n']), second_writer),
    ]

    async def fake_ensure() -> bool:
        if listener._cmd_reader is None or listener._cmd_writer is None:
            if not pairs:
                return False
            listener._cmd_reader, listener._cmd_writer = pairs.pop(0)  # type: ignore[assignment]
        return True

    monkeypatch.setattr(listener, "_ensure_cmd_connection", fake_ensure)

    with caplog.at_level(logging.DEBUG, logger="keymasq-session.listeners.niri"):
        ok, body = await listener._send_cmd_request({"Action": {}}, timeout_s=0.5)

    assert ok is False
    assert body is None
    assert first_writer.payloads == ['{"Action": {}}\n']
    assert second_writer.payloads == []
    assert listener._cmd_writer is first_writer
    assert first_writer.closed is False
    assert "Not retrying sent non-read-only Niri command request: Action" in caplog.text


@pytest.mark.asyncio
async def test_send_cmd_request_resets_dropped_command_socket_errors(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    listener = NiriListener(_noop_callback)
    listener._cmd_reader = make_stream_reader([b'{"Ok":"Handled"}\n'])
    listener._cmd_writer = _FakeWriter(  # type: ignore[assignment]
        write_error=RuntimeError("write bug"),
    )

    async def fake_ensure() -> bool:
        return True

    monkeypatch.setattr(listener, "_ensure_cmd_connection", fake_ensure)

    with caplog.at_level(logging.DEBUG, logger="keymasq-session.listeners.niri"):
        ok, body = await listener._send_cmd_request({"Action": {}}, timeout_s=0.5)

    assert ok is False
    assert body is None
    assert listener._cmd_reader is None
    assert listener._cmd_writer is None
    assert "Niri command socket dropped" in caplog.text
    assert "write bug" in caplog.text


@pytest.mark.asyncio
async def test_send_cmd_request_resets_closed_command_socket(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    listener = NiriListener(_noop_callback)
    listener._cmd_reader = make_stream_reader([b""])
    listener._cmd_writer = _FakeWriter()  # type: ignore[assignment]

    async def fake_ensure() -> bool:
        return True

    monkeypatch.setattr(listener, "_ensure_cmd_connection", fake_ensure)

    with caplog.at_level(logging.DEBUG, logger="keymasq-session.listeners.niri"):
        ok, body = await listener._send_cmd_request("Windows", timeout_s=0.5)

    assert ok is False
    assert body is None
    assert listener._cmd_reader is None
    assert listener._cmd_writer is None
    assert "Niri command socket dropped" in caplog.text
    assert "Niri command socket closed" in caplog.text


@pytest.mark.asyncio
async def test_send_cmd_request_logs_unexpected_close_errors(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    listener = NiriListener(_noop_callback)
    listener._cmd_reader = make_stream_reader([b"{not-json\n"])
    listener._cmd_writer = _FakeWriter(  # type: ignore[assignment]
        wait_closed_error=RuntimeError("close bug"),
    )

    async def fake_ensure() -> bool:
        return True

    monkeypatch.setattr(listener, "_ensure_cmd_connection", fake_ensure)

    with caplog.at_level(logging.ERROR, logger="keymasq-session.listeners.niri"):
        ok, body = await listener._send_cmd_request("Windows", timeout_s=0.5)

    assert ok is False
    assert body is None
    assert "Unexpected failure while closing failed Niri command writer" in caplog.text
    assert "close bug" in caplog.text


@pytest.mark.asyncio
async def test_send_event_stream_request_waits_for_fragmented_acknowledgment() -> None:
    listener = NiriListener(_noop_callback)
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"Ok":')
    writer = _FakeWriter()
    listener.reader = reader
    listener.writer = writer  # type: ignore[assignment]

    request_task = asyncio.create_task(listener._send_event_stream_request())
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert writer.payloads == ['"EventStream"\n']
        assert not request_task.done()

        reader.feed_data(b'"Handled"}\n')
        reader.feed_eof()
        await asyncio.wait_for(request_task, timeout=0.5)
    finally:
        request_task.cancel()
        await asyncio.gather(request_task, return_exceptions=True)


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

    listener = NiriListener(_noop_callback)
    listener.running = True
    monkeypatch.setattr(listener, "_send_cmd_request", _send_cmd_request)

    assert await listener.get_active_window() == (LISTENER_LAB_APP_ID, "Gamma", [])


@pytest.mark.asyncio
async def test_get_active_window_falls_back_to_windows_snapshot(monkeypatch) -> None:
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

    listener = NiriListener(_noop_callback)
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
    listener = NiriListener(_noop_callback)
    ok, message = await listener.dispatch("toggle-overview")
    assert ok is False
    assert message == "NIRI_SOCKET is not available"


@pytest.mark.asyncio
async def test_dispatch_sends_action_request(monkeypatch) -> None:
    requests: list[tuple[object, float]] = []

    async def _send_cmd_request(
        request: object,
        timeout_s: float,
    ) -> tuple[bool, object | None]:
        requests.append((request, timeout_s))
        return True, "Handled"

    listener = NiriListener(_noop_callback)
    monkeypatch.setattr(listener, "_send_cmd_request", _send_cmd_request)

    ok, message = await listener.dispatch("focus-workspace", "2")

    assert ok is True
    assert message == "ok"
    assert requests == [
        ({"Action": {"FocusWorkspace": {"reference": {"Index": 2}}}}, 1.5)
    ]


@pytest.mark.asyncio
async def test_dispatch_uses_cached_window_id_for_focused_window_actions(monkeypatch) -> None:
    requests: list[tuple[object, float]] = []

    async def _send_cmd_request(
        request: object,
        timeout_s: float,
    ) -> tuple[bool, object | None]:
        requests.append((request, timeout_s))
        return True, "Handled"

    listener = NiriListener(_noop_callback)
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
    recorded: dict[str, object] = {}

    async def _create_subprocess_exec(*cmd: object, **kwargs: object) -> _FakeProcess:
        recorded["cmd"] = cmd
        recorded["env"] = kwargs.get("env")
        return _FakeProcess()

    listener = NiriListener(_noop_callback)
    listener.socket_path = "/tmp/niri.sock"
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess_exec)

    ok, message = await listener.dispatch("toggle-overview")

    assert ok is True
    assert message == "ok"
    assert recorded["cmd"] == ("niri", "msg", "action", "toggle-overview")
    assert isinstance(recorded["env"], dict)
    assert recorded["env"]["NIRI_SOCKET"] == "/tmp/niri.sock"


@pytest.mark.asyncio
async def test_dispatch_scrubs_appimage_environment_for_niri_msg(
    monkeypatch,
    tmp_path,
) -> None:
    appdir = tmp_path / "AppDir"
    monkeypatch.setenv("APPDIR", str(appdir))
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "Keymasq.AppImage"))
    monkeypatch.setenv("LD_LIBRARY_PATH", f"{appdir / 'lib'}:/host/lib")
    monkeypatch.setenv("PYTHONHOME", str(appdir))
    monkeypatch.setenv("PYTHONPATH", "/host/pythonpath")
    monkeypatch.setenv("XDG_DATA_DIRS", f"{appdir / 'share'}:/host/share")
    recorded: dict[str, object] = {}

    async def _create_subprocess_exec(*_cmd: object, **kwargs: object) -> _FakeProcess:
        recorded["env"] = kwargs.get("env")
        return _FakeProcess()

    listener = NiriListener(_noop_callback)
    listener.socket_path = "/tmp/niri.sock"
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess_exec)

    ok, message = await listener.dispatch("toggle-overview")

    assert ok is True
    assert message == "ok"
    assert isinstance(recorded["env"], dict)
    assert recorded["env"]["NIRI_SOCKET"] == "/tmp/niri.sock"
    assert recorded["env"]["LD_LIBRARY_PATH"] == "/host/lib"
    assert recorded["env"]["XDG_DATA_DIRS"] == "/host/share"
    for key in ("APPDIR", "APPIMAGE", "PYTHONHOME", "PYTHONPATH"):
        assert key not in recorded["env"]


@pytest.mark.asyncio
async def test_dispatch_accepts_prefixed_niri_msg_action_syntax(monkeypatch) -> None:
    recorded: dict[str, object] = {}

    async def _create_subprocess_exec(*cmd: object, **kwargs: object) -> _FakeProcess:
        recorded["cmd"] = cmd
        return _FakeProcess()

    listener = NiriListener(_noop_callback)
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
    requests: list[tuple[object, float]] = []

    async def _send_cmd_request(
        request: object,
        timeout_s: float,
    ) -> tuple[bool, object | None]:
        requests.append((request, timeout_s))
        return True, "Handled"

    listener = NiriListener(_noop_callback)
    listener.socket_path = "/tmp/niri.sock"
    monkeypatch.setattr(listener, "_send_cmd_request", _send_cmd_request)

    ok, message = await listener.dispatch("niri msg action focus-workspace 2")

    assert ok is True
    assert message == "ok"
    assert requests == [
        ({"Action": {"FocusWorkspace": {"reference": {"Index": 2}}}}, 1.5)
    ]
