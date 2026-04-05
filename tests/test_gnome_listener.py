import asyncio

import pytest

import keyforge.session.listeners.gnome as gnome_module
from keyforge.session.listeners.gnome import GnomeListener


class _FakeWriter:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def write(self, data: bytes) -> None:
        self.payloads.append(gnome_module.json.loads(data.decode("utf-8")))

    async def drain(self) -> None:
        return None


def test_gnome_probe_requires_shell_owner(monkeypatch, tmp_path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "bus").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    monkeypatch.delenv("XDG_SESSION_DESKTOP", raising=False)
    monkeypatch.delenv("DESKTOP_SESSION", raising=False)

    async def _protocols_true(_cls) -> bool:
        return True

    monkeypatch.setattr(
        GnomeListener,
        "_probe_missing_native_toplevel_protocols",
        classmethod(_protocols_true),
    )

    async def _owner_true(_cls, _dbus=None) -> bool:
        return True

    async def _owner_false(_cls, _dbus=None) -> bool:
        return False

    async def _shell_process_false(_cls) -> bool:
        return False

    monkeypatch.setattr(GnomeListener, "_probe_shell_owner", classmethod(_owner_true))
    monkeypatch.setattr(GnomeListener, "_probe_shell_process", classmethod(_shell_process_false))
    assert asyncio.run(GnomeListener.probe_session()) is True

    monkeypatch.setattr(GnomeListener, "_probe_shell_owner", classmethod(_owner_false))
    assert asyncio.run(GnomeListener.probe_session()) is False


def test_gnome_probe_requires_runtime_bus(monkeypatch, tmp_path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))

    async def _owner_true(_cls, _dbus=None) -> bool:
        return True

    monkeypatch.setattr(GnomeListener, "_probe_shell_owner", classmethod(_owner_true))
    assert asyncio.run(GnomeListener.probe_session()) is False


def test_gnome_runtime_prereqs(monkeypatch, tmp_path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    assert GnomeListener._has_runtime_prereqs() is False


def test_gnome_probe_prefers_desktop_hint(monkeypatch, tmp_path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "bus").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")

    async def _protocols_true(_cls) -> bool:
        return True

    monkeypatch.setattr(
        GnomeListener,
        "_probe_missing_native_toplevel_protocols",
        classmethod(_protocols_true),
    )

    async def _owner_false(_cls, _dbus=None) -> bool:
        return False

    monkeypatch.setattr(GnomeListener, "_probe_shell_owner", classmethod(_owner_false))
    assert asyncio.run(GnomeListener.probe_session()) is True


def test_gnome_probe_falls_back_to_shell_process(monkeypatch, tmp_path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "bus").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    monkeypatch.delenv("XDG_SESSION_DESKTOP", raising=False)
    monkeypatch.delenv("DESKTOP_SESSION", raising=False)

    async def _protocols_true(_cls) -> bool:
        return True

    monkeypatch.setattr(
        GnomeListener,
        "_probe_missing_native_toplevel_protocols",
        classmethod(_protocols_true),
    )

    async def _owner_false(_cls, _dbus=None) -> bool:
        return False

    async def _shell_process_true(_cls) -> bool:
        return True

    monkeypatch.setattr(GnomeListener, "_probe_shell_owner", classmethod(_owner_false))
    monkeypatch.setattr(GnomeListener, "_probe_shell_process", classmethod(_shell_process_true))
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
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "bus").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")

    async def _protocols_true(_cls) -> bool:
        return True

    monkeypatch.setattr(
        GnomeListener,
        "_probe_missing_native_toplevel_protocols",
        classmethod(_protocols_true),
    )
    monkeypatch.setattr(
        GnomeListener, "_bridge_extension_available", classmethod(lambda cls: False)
    )
    assert asyncio.run(GnomeListener.probe_available()) is False


def test_gnome_probe_available_requires_extension_enabled(monkeypatch, tmp_path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "bus").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")

    async def _protocols_true(_cls) -> bool:
        return True

    async def _extensions_enabled(_cls) -> bool | None:
        return False

    async def _extensions_not_globally_disabled(_cls) -> bool | None:
        return False

    monkeypatch.setattr(
        GnomeListener,
        "_probe_missing_native_toplevel_protocols",
        classmethod(_protocols_true),
    )
    monkeypatch.setattr(GnomeListener, "_bridge_extension_available", classmethod(lambda cls: True))
    monkeypatch.setattr(
        GnomeListener,
        "_user_extensions_globally_disabled",
        classmethod(_extensions_not_globally_disabled),
    )
    monkeypatch.setattr(
        GnomeListener, "_bridge_extension_enabled", classmethod(_extensions_enabled)
    )
    assert asyncio.run(GnomeListener.probe_available()) is False


def test_gnome_support_details_reports_disabled_extension(monkeypatch, tmp_path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "bus").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")

    async def _protocols_true(_cls) -> bool:
        return True

    async def _extensions_enabled(_cls) -> bool | None:
        return False

    async def _extensions_not_globally_disabled(_cls) -> bool | None:
        return False

    monkeypatch.setattr(
        GnomeListener,
        "_probe_missing_native_toplevel_protocols",
        classmethod(_protocols_true),
    )
    monkeypatch.setattr(GnomeListener, "_bridge_extension_available", classmethod(lambda cls: True))
    monkeypatch.setattr(
        GnomeListener,
        "_user_extensions_globally_disabled",
        classmethod(_extensions_not_globally_disabled),
    )
    monkeypatch.setattr(
        GnomeListener, "_bridge_extension_enabled", classmethod(_extensions_enabled)
    )

    details = asyncio.run(GnomeListener.get_support_details())
    assert details["session_detected"] is True
    assert details["supported"] is False
    assert details["extension_installed"] is True
    assert details["extension_enabled"] is False
    assert "not enabled" in str(details["warning"])
    assert "log out and log back in" in str(details["warning"])


def test_gnome_support_details_reports_missing_extension(monkeypatch, tmp_path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "bus").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")

    async def _protocols_true(_cls) -> bool:
        return True

    monkeypatch.setattr(
        GnomeListener,
        "_probe_missing_native_toplevel_protocols",
        classmethod(_protocols_true),
    )
    monkeypatch.setattr(
        GnomeListener, "_bridge_extension_available", classmethod(lambda cls: False)
    )

    details = asyncio.run(GnomeListener.get_support_details())
    assert details["session_detected"] is True
    assert details["supported"] is False
    assert details["extension_installed"] is False
    assert "not installed" in str(details["warning"])


def test_gnome_probe_requires_missing_native_toplevel_protocols(monkeypatch, tmp_path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "bus").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    monkeypatch.setattr(GnomeListener, "_bridge_extension_available", classmethod(lambda cls: True))

    async def _protocols_false(_cls) -> bool:
        return False

    monkeypatch.setattr(
        GnomeListener,
        "_probe_missing_native_toplevel_protocols",
        classmethod(_protocols_false),
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
                "app_id": "org.keyforge.ListenerLab",
                "title": "Alpha",
            }
        )

    asyncio.create_task(_respond())

    assert await listener.get_active_window() == ("org.keyforge.ListenerLab", "Alpha", [])
    assert observed == [("org.keyforge.ListenerLab", "Alpha", [])]
    assert listener._writer.payloads == [{"type": "get_active_window", "request_id": 1}]


@pytest.mark.asyncio
async def test_gnome_dispatch_sends_bridge_request_and_resolves_result() -> None:
    observed: list[tuple[str, str, list[str]]] = []

    async def _callback(window_class: str, window_title: str, tags: list[str]) -> None:
        observed.append((window_class, window_title, tags))

    listener = GnomeListener(_callback)
    listener._writer = _FakeWriter()

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
async def test_gnome_dispatch_rejects_invalid_dispatcher_without_bridge_write() -> None:
    listener = GnomeListener(lambda *_args: asyncio.sleep(0))
    writer = _FakeWriter()
    listener._writer = writer

    assert await listener.dispatch("togglefloating", "") == (
        False,
        "unsupported GNOME dispatcher: togglefloating",
    )
    assert writer.payloads == []


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
            "app_id": "org.keyforge.ListenerLab",
            "title": "Alpha",
        }
    )

    assert await future == {
        "type": "activated",
        "request_id": 7,
        "found": True,
        "app_id": "org.keyforge.ListenerLab",
        "title": "Alpha",
    }
    assert await listener.get_active_window() == ("org.keyforge.ListenerLab", "Alpha", [])
    assert observed == [("org.keyforge.ListenerLab", "Alpha", [])]
