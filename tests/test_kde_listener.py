import asyncio

from keyforge.session.listeners import kde as kde_listener_module
from keyforge.session.listeners.kde import (
    KDEListener,
    has_kde_wayland_support,
    parse_kde_cursor_payload,
    parse_kde_window_payload,
)


def test_parse_kde_window_payload_valid() -> None:
    payload = '{"class":"org.kde.konsole","title":"Konsole"}'
    assert parse_kde_window_payload(payload) == ("org.kde.konsole", "Konsole")


def test_parse_kde_window_payload_rejects_invalid_json() -> None:
    assert parse_kde_window_payload("not-json") is None


def test_parse_kde_window_payload_accepts_wrapped_json_string() -> None:
    payload = '"{\\"class\\":\\"org.kde.konsole\\",\\"title\\":\\"Konsole\\"}"'
    assert parse_kde_window_payload(payload) == ("org.kde.konsole", "Konsole")


def test_parse_kde_cursor_payload_valid() -> None:
    payload = '{"id":"abc123","x":123.4,"y":456.7}'
    assert parse_kde_cursor_payload(payload) == ("abc123", 123, 456)


def test_parse_kde_cursor_payload_requires_request_id() -> None:
    payload = '{"x":123,"y":456}'
    assert parse_kde_cursor_payload(payload) is None


def test_parse_kde_cursor_payload_accepts_wrapped_json_string() -> None:
    payload = '"{\\"id\\":\\"abc123\\",\\"x\\":123.4,\\"y\\":456.7}"'
    assert parse_kde_cursor_payload(payload) == ("abc123", 123, 456)


def test_kde_window_script_tracks_metadata_changes() -> None:
    async def _cb(_window_class: str, _window_title: str, _tags: list[str]) -> None:
        return

    listener = KDEListener(_cb)
    script = listener._build_window_script_source()

    assert 'connectSignal(workspace, "windowActivated"' in script
    assert 'connectSignal(workspace, "activeWindowChanged"' in script
    assert 'connectSignal(workspace, "windowAdded"' in script
    assert 'connectSignal(workspace, "windowRemoved"' in script
    assert 'connectSignal(currentWindow, "captionChanged"' in script
    assert "resourceClassChanged" in script
    assert '"windowChanged"' in script


def test_kde_cursor_script_uses_cursor_position_method() -> None:
    async def _cb(_window_class: str, _window_title: str, _tags: list[str]) -> None:
        return

    listener = KDEListener(_cb)
    script = listener._build_cursor_script_source("req1")

    assert "workspace.cursorPos" in script
    assert '"cursorPosition"' in script


def test_kde_ignored_payload_logging_is_rate_limited(monkeypatch) -> None:
    async def _cb(_window_class: str, _window_title: str, _tags: list[str]) -> None:
        return

    listener = KDEListener(_cb)

    monkeypatch.setattr(kde_listener_module.time, "monotonic", lambda: 100.0)
    listener._log_ignored_payload("window", "a")
    assert listener._ignored_window_payloads == 0
    assert listener._last_window_payload_log_at == 100.0

    monkeypatch.setattr(kde_listener_module.time, "monotonic", lambda: 101.0)
    listener._log_ignored_payload("window", "b")
    assert listener._ignored_window_payloads == 1
    assert listener._last_window_payload_log_at == 100.0

    monkeypatch.setattr(kde_listener_module.time, "monotonic", lambda: 111.0)
    listener._log_ignored_payload("window", "c")
    assert listener._ignored_window_payloads == 0
    assert listener._last_window_payload_log_at == 111.0


def test_has_kde_wayland_support_uses_runtime_bus(monkeypatch, tmp_path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "bus").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))

    assert has_kde_wayland_support() is True


def test_probe_available_requires_kwin_owner(monkeypatch, tmp_path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "bus").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))

    async def _owner_false(_dbus=None) -> bool:
        return False

    monkeypatch.setattr(kde_listener_module, "_probe_kwin_owner", _owner_false)

    assert asyncio.run(KDEListener.probe_available()) is False


def test_probe_available_true_when_kwin_owner_present(monkeypatch, tmp_path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "bus").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))

    async def _owner_true(_dbus=None) -> bool:
        return True

    monkeypatch.setattr(kde_listener_module, "_probe_kwin_owner", _owner_true)

    assert asyncio.run(KDEListener.probe_available()) is True
