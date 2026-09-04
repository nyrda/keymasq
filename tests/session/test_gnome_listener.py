import asyncio
import logging
from functools import partial
from unittest.mock import AsyncMock

import pytest

import keymasq.session.listeners.gnome as gnome_module
from keymasq.session.listeners.gnome import GnomeListener
from tests.async_fakes import FakeStreamWriter, make_stream_reader


def _decode_bridge_payload(data: bytes) -> object:
    return gnome_module.json.loads(data.decode("utf-8"))


_FakeWriter = partial(FakeStreamWriter, payload_decoder=_decode_bridge_payload)


_UNSET = object()


def _async_bool_probe_result(value: bool):
    async def _probe(_cls, _dbus=None) -> bool:
        return value

    return classmethod(_probe)


def _async_optional_probe_result(value: bool | None):
    async def _probe(_cls, _dbus=None) -> bool | None:
        return value

    return classmethod(_probe)


def _sync_bool_probe_result(value: bool):
    def _probe(_cls) -> bool:
        return value

    return classmethod(_probe)


def _set_gnome_probe_state(
    monkeypatch,
    tmp_path,
    *,
    desktop: str | None = "GNOME",
    runtime_bus: bool = True,
    missing_native_toplevel_protocols: bool = True,
    shell_owner=_UNSET,
    shell_process=_UNSET,
    extension_available: bool | None = None,
    extension_visible=_UNSET,
    user_extensions_disabled=_UNSET,
    extension_enabled=_UNSET,
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    if runtime_bus:
        (runtime_dir / "bus").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    monkeypatch.delenv("XDG_SESSION_DESKTOP", raising=False)
    monkeypatch.delenv("DESKTOP_SESSION", raising=False)
    if desktop is not None:
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", desktop)
    monkeypatch.setattr(
        GnomeListener,
        "_probe_missing_native_toplevel_protocols",
        _async_bool_probe_result(missing_native_toplevel_protocols),
    )
    if shell_owner is not _UNSET:
        monkeypatch.setattr(
            GnomeListener,
            "_probe_shell_owner",
            _async_bool_probe_result(shell_owner),
        )
    if shell_process is not _UNSET:
        monkeypatch.setattr(
            GnomeListener,
            "_probe_shell_process",
            _async_bool_probe_result(shell_process),
        )
    if extension_available is not None:
        monkeypatch.setattr(
            GnomeListener,
            "_bridge_extension_available",
            _sync_bool_probe_result(extension_available),
        )
    if extension_visible is not _UNSET:
        monkeypatch.setattr(
            GnomeListener,
            "_bridge_extension_visible_to_shell",
            _async_optional_probe_result(extension_visible),
        )
    if user_extensions_disabled is not _UNSET:
        monkeypatch.setattr(
            GnomeListener,
            "_user_extensions_globally_disabled",
            _async_optional_probe_result(user_extensions_disabled),
        )
    if extension_enabled is not _UNSET:
        monkeypatch.setattr(
            GnomeListener,
            "_bridge_extension_enabled",
            _async_optional_probe_result(extension_enabled),
        )


def test_gnome_probe_requires_shell_owner(monkeypatch, tmp_path) -> None:
    _set_gnome_probe_state(
        monkeypatch,
        tmp_path,
        desktop=None,
        shell_owner=True,
        shell_process=False,
    )
    assert asyncio.run(GnomeListener.probe_session()) is True

    monkeypatch.setattr(GnomeListener, "_probe_shell_owner", _async_bool_probe_result(False))
    assert asyncio.run(GnomeListener.probe_session()) is False


def test_gnome_probe_requires_runtime_bus(monkeypatch, tmp_path) -> None:
    _set_gnome_probe_state(
        monkeypatch,
        tmp_path,
        desktop=None,
        runtime_bus=False,
        shell_owner=True,
    )
    assert asyncio.run(GnomeListener.probe_session()) is False


def test_gnome_runtime_prereqs(monkeypatch, tmp_path) -> None:
    _set_gnome_probe_state(monkeypatch, tmp_path, runtime_bus=False)
    assert GnomeListener._has_runtime_prereqs() is False


def test_gnome_probe_prefers_desktop_hint(monkeypatch, tmp_path) -> None:
    _set_gnome_probe_state(monkeypatch, tmp_path, shell_owner=False)
    assert asyncio.run(GnomeListener.probe_session()) is True


def test_gnome_probe_falls_back_to_shell_process(monkeypatch, tmp_path) -> None:
    _set_gnome_probe_state(
        monkeypatch,
        tmp_path,
        desktop=None,
        shell_owner=False,
        shell_process=True,
    )
    assert asyncio.run(GnomeListener.probe_session()) is True


def test_gnome_probe_shell_process_uses_to_thread(monkeypatch) -> None:
    calls: list[tuple[object, tuple[object, ...]]] = []

    def _sync_true(_cls) -> bool:
        return True

    async def _fake_to_thread(func, /, *args, **kwargs):
        assert not kwargs
        calls.append((func, args))
        return func(*args)

    monkeypatch.setattr(GnomeListener, "_probe_shell_process_sync", classmethod(_sync_true))
    monkeypatch.setattr(gnome_module.asyncio, "to_thread", _fake_to_thread)

    assert asyncio.run(GnomeListener._probe_shell_process()) is True
    assert len(calls) == 1


def test_gnome_probe_requires_extension(monkeypatch, tmp_path) -> None:
    _set_gnome_probe_state(monkeypatch, tmp_path, extension_available=False)
    assert asyncio.run(GnomeListener.probe_available()) is False


def test_gnome_probe_available_requires_extension_enabled(monkeypatch, tmp_path) -> None:
    _set_gnome_probe_state(
        monkeypatch,
        tmp_path,
        extension_available=True,
        extension_visible=True,
        user_extensions_disabled=False,
        extension_enabled=False,
    )
    assert asyncio.run(GnomeListener.probe_available()) is False


def test_gnome_support_details_reports_disabled_extension(monkeypatch, tmp_path) -> None:
    _set_gnome_probe_state(
        monkeypatch,
        tmp_path,
        extension_available=True,
        extension_visible=True,
        user_extensions_disabled=False,
        extension_enabled=False,
    )

    details = asyncio.run(GnomeListener.get_support_details())
    assert details["session_detected"] is True
    assert details["supported"] is False
    assert details["extension_installed"] is True
    assert details["extension_enabled"] is False
    assert details["gnome_bridge_state"] == "bridge_disabled"
    assert details["gnome_bridge_action"] == "enable_bridge"
    assert "not enabled" in str(details["warning"])
    assert "window-aware profiles" in str(details["warning"])


def test_gnome_support_details_reports_shell_not_rescanned(monkeypatch, tmp_path) -> None:
    _set_gnome_probe_state(
        monkeypatch,
        tmp_path,
        extension_available=True,
        extension_visible=False,
    )

    details = asyncio.run(GnomeListener.get_support_details())
    assert details["session_detected"] is True
    assert details["supported"] is False
    assert details["extension_installed"] is True
    assert details["extension_enabled"] is False
    assert details["gnome_bridge_state"] == "shell_not_rescanned"
    assert details["gnome_bridge_action"] == "logout"
    assert "does not see" in str(details["warning"])


def test_gnome_support_details_reports_shell_dbus_unavailable(monkeypatch, tmp_path) -> None:
    _set_gnome_probe_state(
        monkeypatch,
        tmp_path,
        extension_available=True,
        extension_visible=None,
    )

    details = asyncio.run(GnomeListener.get_support_details())
    assert details["session_detected"] is True
    assert details["supported"] is False
    assert details["gnome_bridge_state"] == "shell_dbus_unavailable"
    assert details["gnome_bridge_action"] == "refresh"
    assert "DBus" in str(details["warning"])


def test_gnome_support_details_reports_ready(monkeypatch, tmp_path) -> None:
    _set_gnome_probe_state(
        monkeypatch,
        tmp_path,
        extension_available=True,
        extension_visible=True,
        user_extensions_disabled=False,
        extension_enabled=True,
    )

    details = asyncio.run(GnomeListener.get_support_details())
    assert details["supported"] is True
    assert details["extension_enabled"] is True
    assert details["gnome_bridge_state"] == "ready"
    assert details["gnome_bridge_action"] == ""


def test_gnome_support_details_reports_missing_extension(monkeypatch, tmp_path) -> None:
    _set_gnome_probe_state(monkeypatch, tmp_path, extension_available=False)

    details = asyncio.run(GnomeListener.get_support_details())
    assert details["session_detected"] is True
    assert details["supported"] is False
    assert details["extension_installed"] is False
    assert details["gnome_bridge_state"] == "missing_files"
    assert details["gnome_bridge_action"] == "reinstall"
    assert "not installed" in str(details["warning"])


@pytest.mark.asyncio
async def test_gnome_hello_marks_bridge_protocol_compatible() -> None:
    listener = GnomeListener(lambda *_args: asyncio.sleep(0))
    listener.running = True
    listener._writer = _FakeWriter()
    listener._bridge_connected = True

    await listener._handle_bridge_message({"type": "hello", "protocol": 1})

    assert listener.compositor_dispatch_available is True
    assert listener.runtime_support_details()["warning"] == ""


@pytest.mark.asyncio
async def test_gnome_stale_bridge_reader_does_not_clear_new_connection() -> None:
    listener = GnomeListener(lambda *_args: asyncio.sleep(0))
    listener.running = True
    stale_writer = _FakeWriter()
    active_writer = _FakeWriter()
    listener._writer = active_writer
    listener._bridge_connected = True
    listener._bridge_protocol = 1
    listener._bridge_protocol_compatible = True

    await listener._bridge_read_loop(make_stream_reader([]), stale_writer)

    assert listener._writer is active_writer
    assert stale_writer.closed is True
    assert listener._bridge_connected is True
    assert listener._bridge_protocol == 1
    assert listener._bridge_protocol_compatible is True
    assert listener.compositor_dispatch_available is True


@pytest.mark.asyncio
async def test_gnome_bridge_read_loop_logs_and_skips_malformed_frames(
    caplog: pytest.LogCaptureFixture,
) -> None:
    observed: list[tuple[str, str, list[str]]] = []

    async def _callback(window_class: str, window_title: str, tags: list[str]) -> None:
        observed.append((window_class, window_title, tags))

    listener = GnomeListener(_callback)
    listener.running = True
    writer = _FakeWriter()
    listener._writer = writer
    listener._bridge_connected = True
    reader = make_stream_reader(
        [
            b"\xff\n",
            b"{not-json\n",
            b"[]\n",
            gnome_module.json.dumps(
                {
                    "type": "focus_changed",
                    "app_id": "org.gnome.Nautilus",
                    "title": "Home",
                }
            ).encode("utf-8")
            + b"\n",
        ]
    )

    with caplog.at_level(logging.DEBUG, logger="keymasq-session.listeners.gnome"):
        await listener._bridge_read_loop(reader, writer)

    assert observed == [("org.gnome.Nautilus", "Home", [])]
    assert "Ignoring GNOME bridge message with invalid UTF-8" in caplog.text
    assert "Ignoring malformed GNOME bridge JSON message" in caplog.text
    assert "Ignoring GNOME bridge JSON message that is not an object" in caplog.text


@pytest.mark.asyncio
async def test_gnome_bridge_read_loop_logs_unexpected_handler_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _callback(_window_class: str, _window_title: str, _tags: list[str]) -> None:
        raise RuntimeError("callback bug")

    listener = GnomeListener(_callback)
    listener.running = True
    writer = _FakeWriter()
    listener._writer = writer
    listener._bridge_connected = True
    reader = make_stream_reader(
        [
            gnome_module.json.dumps(
                {
                    "type": "focus_changed",
                    "app_id": "org.gnome.Nautilus",
                    "title": "Home",
                }
            ).encode("utf-8")
            + b"\n",
        ]
    )

    with caplog.at_level(logging.ERROR, logger="keymasq-session.listeners.gnome"):
        await listener._bridge_read_loop(reader, writer)

    assert "Unexpected GNOME bridge read loop error" in caplog.text
    assert "callback bug" in caplog.text


@pytest.mark.asyncio
async def test_gnome_bridge_read_loop_logs_unexpected_close_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    listener = GnomeListener(lambda *_args: asyncio.sleep(0))
    listener.running = True
    writer = _FakeWriter(wait_closed_error=RuntimeError("close bug"))
    listener._writer = writer
    listener._bridge_connected = True

    with caplog.at_level(logging.ERROR, logger="keymasq-session.listeners.gnome"):
        await listener._bridge_read_loop(make_stream_reader([]), writer)

    assert "Unexpected failure while closing GNOME bridge client writer" in caplog.text
    assert "close bug" in caplog.text


@pytest.mark.asyncio
async def test_gnome_hello_reports_stale_bridge_protocol_warning() -> None:
    listener = GnomeListener(lambda *_args: asyncio.sleep(0))
    listener.running = True
    listener._writer = _FakeWriter()
    listener._bridge_connected = True

    await listener._handle_bridge_message({"type": "hello", "protocol": 0})

    details = listener.runtime_support_details()
    assert listener.compositor_dispatch_available is False
    assert details["bridge_protocol"] == 0
    assert details["gnome_bridge_state"] == "protocol_stale"
    assert details["gnome_bridge_action"] == "logout"
    assert "Log out and back in" in str(details["warning"])


@pytest.mark.asyncio
async def test_gnome_setup_action_enable_bridge_uses_shell_dbus(monkeypatch) -> None:
    calls: list[tuple[str, bool, object | None]] = []
    dbus = object()

    async def _set_extension_enabled(uuid: str, enabled: bool, dbus_arg=None) -> bool:
        calls.append((uuid, enabled, dbus_arg))
        return True

    monkeypatch.setattr(
        gnome_module.gnome_shell,
        "set_extension_enabled",
        _set_extension_enabled,
    )

    ok, message = await GnomeListener.run_setup_action("enable_bridge", dbus)  # type: ignore[arg-type]

    assert ok is True
    assert "enabled" in message
    assert calls == [("gnome-bridge@keymasq.tools", True, dbus)]


def test_gnome_probe_requires_missing_native_toplevel_protocols(monkeypatch, tmp_path) -> None:
    _set_gnome_probe_state(
        monkeypatch,
        tmp_path,
        missing_native_toplevel_protocols=False,
        extension_available=True,
    )
    assert asyncio.run(GnomeListener.probe_available()) is False


@pytest.mark.asyncio
async def test_gnome_get_active_window_queries_bridge_when_cache_empty() -> None:
    observed: list[tuple[str, str, list[str]]] = []

    async def _callback(window_class: str, window_title: str, tags: list[str]) -> None:
        observed.append((window_class, window_title, tags))

    listener = GnomeListener(_callback)
    listener._writer = _FakeWriter()

    async def _respond() -> None:
        await asyncio.sleep(0)
        await listener._handle_bridge_message(
            {
                "type": "active_window",
                "request_id": 1,
                "app_id": "tools.keymasq.ListenerLab",
                "title": "Alpha",
            }
        )

    asyncio.create_task(_respond())

    assert await listener.get_active_window() == ("tools.keymasq.ListenerLab", "Alpha", [])
    assert observed == [("tools.keymasq.ListenerLab", "Alpha", [])]
    assert listener._writer.payloads == [{"type": "get_active_window", "request_id": 1}]


@pytest.mark.asyncio
async def test_gnome_send_request_logs_unexpected_writer_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    listener = GnomeListener(lambda *_args: asyncio.sleep(0))
    listener._writer = _FakeWriter(write_error=RuntimeError("write bug"))

    with caplog.at_level(logging.ERROR, logger="keymasq-session.listeners.gnome"):
        assert await listener._send_request({"type": "get_active_window"}, timeout=0.1) is None

    assert "Unexpected GNOME bridge get_active_window request failure" in caplog.text
    assert "write bug" in caplog.text
    assert listener._pending_window == {}


@pytest.mark.asyncio
async def test_gnome_dispatch_sends_bridge_request_and_resolves_result() -> None:
    observed: list[tuple[str, str, list[str]]] = []

    async def _callback(window_class: str, window_title: str, tags: list[str]) -> None:
        observed.append((window_class, window_title, tags))

    listener = GnomeListener(_callback)
    listener._writer = _FakeWriter()
    listener._bridge_connected = True
    await listener._handle_bridge_message({"type": "hello", "protocol": 1})

    async def _respond() -> None:
        await asyncio.sleep(0)
        await listener._handle_bridge_message(
            {
                "type": "dispatch_result",
                "request_id": 1,
                "ok": True,
                "message": "switched to workspace 2",
                "app_id": "org.gnome.Nautilus",
                "title": "Home",
            }
        )

    asyncio.create_task(_respond())

    assert await listener.dispatch("workspace", "2") == (True, "switched to workspace 2")
    assert listener._writer.payloads == [
        {
            "type": "dispatch",
            "request_id": 1,
            "dispatcher": "workspace",
            "args": "2",
        }
    ]
    assert observed == [("org.gnome.Nautilus", "Home", [])]


@pytest.mark.asyncio
async def test_gnome_set_cursor_position_sends_bridge_request_and_resolves_result() -> None:
    listener = GnomeListener(lambda *_args: asyncio.sleep(0))
    listener._writer = _FakeWriter()
    listener._bridge_connected = True
    await listener._handle_bridge_message({"type": "hello", "protocol": 1})

    async def _respond() -> None:
        await asyncio.sleep(0)
        await listener._handle_bridge_message(
            {
                "type": "pointer_set_result",
                "request_id": 1,
                "ok": True,
                "message": "ok",
                "x": 123,
                "y": 456,
            }
        )

    asyncio.create_task(_respond())

    assert await listener.set_cursor_position(123, 456) == (True, "ok")
    assert listener._writer.payloads == [
        {
            "type": "set_pointer",
            "request_id": 1,
            "x": 123,
            "y": 456,
        }
    ]


@pytest.mark.asyncio
async def test_gnome_dispatch_set_cursor_position_uses_special_dispatcher() -> None:
    listener = GnomeListener(lambda *_args: asyncio.sleep(0))
    listener.set_cursor_position = AsyncMock(return_value=(True, "ok"))  # type: ignore[method-assign]

    assert await listener.dispatch("set_cursor_position", "123 456") == (True, "ok")

    listener.set_cursor_position.assert_awaited_once_with(123, 456)


@pytest.mark.asyncio
async def test_gnome_set_cursor_position_rejects_stale_bridge_protocol() -> None:
    listener = GnomeListener(lambda *_args: asyncio.sleep(0))
    listener._writer = _FakeWriter()
    listener._bridge_connected = True
    await listener._handle_bridge_message({"type": "hello", "protocol": 0})

    ok, message = await listener.set_cursor_position(123, 456)

    assert ok is False
    assert "Log out and back in" in message
    assert listener._writer.payloads == []


@pytest.mark.asyncio
async def test_gnome_set_cursor_position_fails_when_bridge_missing() -> None:
    listener = GnomeListener(lambda *_args: asyncio.sleep(0))

    assert await listener.set_cursor_position(123, 456) == (
        False,
        "GNOME bridge not connected",
    )


@pytest.mark.asyncio
async def test_gnome_dispatch_rejects_invalid_dispatcher_without_bridge_write() -> None:
    listener = GnomeListener(lambda *_args: asyncio.sleep(0))
    assert await listener.dispatch("togglefloating", "") == (
        False,
        "unsupported GNOME dispatcher: togglefloating",
    )


@pytest.mark.asyncio
async def test_gnome_dispatch_fails_when_bridge_missing() -> None:
    listener = GnomeListener(lambda *_args: asyncio.sleep(0))

    assert await listener.dispatch("workspace", "next") == (
        False,
        "GNOME bridge not connected",
    )


@pytest.mark.asyncio
async def test_gnome_activated_message_updates_window_state() -> None:
    observed: list[tuple[str, str, list[str]]] = []

    async def _callback(window_class: str, window_title: str, tags: list[str]) -> None:
        observed.append((window_class, window_title, tags))

    listener = GnomeListener(_callback)
    future = asyncio.get_running_loop().create_future()
    listener._pending_activate[7] = future

    await listener._handle_bridge_message(
        {
            "type": "activated",
            "request_id": 7,
            "found": True,
            "app_id": "tools.keymasq.ListenerLab",
            "title": "Alpha",
        }
    )

    assert await future == {
        "type": "activated",
        "request_id": 7,
        "found": True,
        "app_id": "tools.keymasq.ListenerLab",
        "title": "Alpha",
    }
    assert await listener.get_active_window() == ("tools.keymasq.ListenerLab", "Alpha", [])
    assert observed == [("tools.keymasq.ListenerLab", "Alpha", [])]
