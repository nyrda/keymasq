import asyncio

import pytest

from keymasq.session.listeners import kde as kde_listener_module
from keymasq.session.listeners.kde import (
    KDE_CURSOR_TRACKING_REQUEST_ID,
    KDE_DISPATCH_METHODS,
    KDEListener,
    has_kde_wayland_support,
    parse_kde_cursor_payload,
    parse_kde_dispatch_payload,
    parse_kde_window_payload,
)


async def _noop_callback(_window_class: str, _window_title: str, _tags: list[str]) -> None:
    return


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


def test_parse_kde_cursor_payload_rejects_overflowing_coordinates() -> None:
    payload = '{"id":"abc123","x":"1e100000","y":456}'
    assert parse_kde_cursor_payload(payload) is None


def test_parse_kde_dispatch_payload_valid() -> None:
    payload = '{"id":"abc123","ok":true,"message":"ok"}'
    assert parse_kde_dispatch_payload(payload) == ("abc123", True, "ok")


def test_parse_kde_dispatch_payload_requires_boolean_ok() -> None:
    payload = '{"id":"abc123","ok":"true","message":"ok"}'
    assert parse_kde_dispatch_payload(payload) is None


def test_parse_kde_dispatch_payload_accepts_wrapped_json_string() -> None:
    payload = '"{\\"id\\":\\"abc123\\",\\"ok\\":false,\\"message\\":\\"bad\\"}"'
    assert parse_kde_dispatch_payload(payload) == ("abc123", False, "bad")


def test_kde_window_script_tracks_metadata_changes() -> None:
    listener = KDEListener(_noop_callback)
    script = listener._build_window_script_source()

    assert 'connectSignal(workspace, "windowActivated"' in script
    assert 'connectSignal(workspace, "activeWindowChanged"' in script
    assert 'connectSignal(workspace, "windowAdded"' in script
    assert 'connectSignal(workspace, "windowRemoved"' in script
    assert 'connectSignal(currentWindow, "captionChanged"' in script
    assert "resourceClassChanged" in script
    assert '"windowChanged"' in script


def test_kde_cursor_script_uses_cursor_position_method() -> None:
    listener = KDEListener(_noop_callback)
    script = listener._build_cursor_script_source("req1")

    assert "workspace.cursorPos" in script
    assert '"cursorPosition"' in script


def test_kde_cursor_tracking_script_emits_immediate_and_changed_positions() -> None:
    listener = KDEListener(_noop_callback)
    script = listener._build_cursor_tracking_script_source()

    assert "workspace.cursorPos" in script
    assert 'connectSignal(workspace, "cursorPosChanged"' in script
    assert KDE_CURSOR_TRACKING_REQUEST_ID in script
    assert '"cursorPosition"' in script


def test_kde_advertises_realtime_cursor_position() -> None:
    assert KDEListener(_noop_callback).supports_realtime_cursor_position is True


def test_kde_dispatch_script_uses_workspace_method_and_reply_hook() -> None:
    listener = KDEListener(_noop_callback)
    script = listener._build_dispatch_script_source("req1", KDE_DISPATCH_METHODS["tile_left"])

    assert 'const METHOD_NAME = "slotWindowQuickTileLeft"' in script
    assert 'workspace[METHOD_NAME]' in script
    assert '"dispatchResult"' in script


def test_kde_ignored_payload_logging_is_rate_limited(monkeypatch) -> None:
    listener = KDEListener(_noop_callback)

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


def test_dispatch_rejects_args() -> None:
    listener = KDEListener(_noop_callback)
    listener.running = True
    listener._kwin_scripting = object()

    ok, message = asyncio.run(listener.dispatch("tile_left", "unexpected"))

    assert ok is False
    assert message == "KDE compositor actions do not accept arguments"


def test_dispatch_rejects_unsupported_dispatcher() -> None:
    listener = KDEListener(_noop_callback)
    listener.running = True
    listener._kwin_scripting = object()

    ok, message = asyncio.run(listener.dispatch("unknown_action"))

    assert ok is False
    assert message == "unsupported KDE dispatcher: unknown_action"


async def _run_kde_cursor_cache_fallback_test(
    monkeypatch,
) -> tuple[tuple[int, int] | None, tuple[int, int] | None, int]:
    listener = KDEListener(_noop_callback)
    listener.running = True
    listener._kwin_scripting = object()
    listener._cursor_tracking_deadline_at = kde_listener_module.time.monotonic() + 1.0
    listener._cursor_tracking_cache = (7, 8)

    cached = await listener.get_cursor_position()
    fallback_calls = 0

    async def _fallback(**_kwargs) -> tuple[int, int]:
        nonlocal fallback_calls
        fallback_calls += 1
        return 9, 10

    monkeypatch.setattr(listener, "_run_ephemeral_kwin_script", _fallback)
    listener._cursor_tracking_deadline_at = kde_listener_module.time.monotonic() - 1.0
    fallback = await listener.get_cursor_position()

    return cached, fallback, fallback_calls


def test_kde_cursor_cache_is_used_only_while_tracking_is_active(monkeypatch) -> None:
    cached, fallback, fallback_calls = asyncio.run(
        _run_kde_cursor_cache_fallback_test(monkeypatch)
    )

    assert cached == (7, 8)
    assert fallback == (9, 10)
    assert fallback_calls == 1


async def _run_kde_cursor_payload_tracking_test() -> tuple[
    tuple[int, int] | None,
    tuple[int, int] | None,
    bool,
    tuple[int, int] | None,
    tuple[int, int],
]:
    listener = KDEListener(_noop_callback)
    tracking_id = f"{KDE_CURSOR_TRACKING_REQUEST_ID}:test"
    listener._cursor_tracking_request_id = tracking_id
    payload = f'{{"id":"{tracking_id}","x":11,"y":12}}'
    unmatched_payload = '{"id":"other-request","x":99,"y":100}'

    listener._cursor_tracking_deadline_at = kde_listener_module.time.monotonic() - 1.0
    listener.handle_cursor_payload(payload)
    inactive_cache = listener._cursor_tracking_cache

    loop = asyncio.get_running_loop()
    waiter: asyncio.Future[tuple[int, int]] = loop.create_future()
    listener._cursor_tracking_sample_waiters.add(waiter)
    listener._cursor_tracking_deadline_at = kde_listener_module.time.monotonic() + 1.0
    listener.handle_cursor_payload(unmatched_payload)
    unmatched_cache = listener._cursor_tracking_cache
    unmatched_waiter_done = waiter.done()
    listener.handle_cursor_payload(payload)
    active_cache = listener._cursor_tracking_cache
    waiter_result = await waiter

    return inactive_cache, unmatched_cache, unmatched_waiter_done, active_cache, waiter_result


def test_kde_cursor_tracking_payload_is_ignored_when_not_active() -> None:
    (
        inactive_cache,
        unmatched_cache,
        unmatched_waiter_done,
        active_cache,
        waiter_result,
    ) = asyncio.run(
        _run_kde_cursor_payload_tracking_test(),
    )

    assert inactive_cache is None
    assert unmatched_cache is None
    assert unmatched_waiter_done is False
    assert active_cache == (11, 12)
    assert waiter_result == (11, 12)


async def _run_prepare_cursor_tracking_test(monkeypatch, tmp_path) -> tuple[
    tuple[int, int] | None,
    list[str],
    bool,
]:
    listener = KDEListener(_noop_callback)
    listener.running = True
    listener._kwin_scripting = object()
    script_path = tmp_path / "cursor_tracking.js"
    calls: list[str] = []

    class _ScriptIface:
        async def call_run(self) -> None:
            calls.append("run")
            listener.handle_cursor_payload(
                f'{{"id":"{listener._cursor_tracking_request_id}","x":13,"y":14}}'
            )

        async def call_stop(self) -> None:
            calls.append("stop")

    def _write_script_file(source: str):
        calls.append(f"write:{KDE_CURSOR_TRACKING_REQUEST_ID in source}")
        script_path.write_text(source)
        return script_path

    async def _load_script(_file_path: str, plugin_name: str) -> int:
        calls.append(f"load:{plugin_name.startswith('keymasq-kde-cursor-track-')}")
        return 42

    async def _get_script_interface(_script_id: int):
        calls.append("iface")
        return _ScriptIface()

    async def _unload_script(plugin_name: str) -> None:
        calls.append(f"unload:{plugin_name.startswith('keymasq-kde-cursor-track-')}")

    monkeypatch.setattr(listener, "_write_script_file", _write_script_file)
    monkeypatch.setattr(listener, "_call_load_script", _load_script)
    monkeypatch.setattr(listener, "_get_script_interface", _get_script_interface)
    monkeypatch.setattr(listener, "_call_unload_script", _unload_script)

    await listener.prepare_cursor_position_tracking(1)
    cache = listener._cursor_tracking_cache
    for _ in range(10):
        if listener._cursor_tracking_script_iface is None:
            break
        await asyncio.sleep(0.01)

    return cache, calls, script_path.exists()


def test_prepare_cursor_tracking_loads_samples_and_unloads_after_ttl(
    monkeypatch,
    tmp_path,
) -> None:
    cache, calls, path_exists = asyncio.run(
        _run_prepare_cursor_tracking_test(monkeypatch, tmp_path)
    )

    assert cache == (13, 14)
    assert calls[:4] == ["write:True", "load:True", "iface", "run"]
    assert "stop" in calls
    assert "unload:True" in calls
    assert path_exists is False


async def _run_prepare_cursor_tracking_failure_test(monkeypatch, tmp_path) -> tuple[
    str,
    object | None,
    tuple[int, int] | None,
    int,
    bool,
]:
    listener = KDEListener(_noop_callback)
    listener.running = True
    listener._kwin_scripting = object()
    script_path = tmp_path / "cursor_tracking_failure.js"

    def _write_script_file(source: str):
        script_path.write_text(source)
        return script_path

    async def _load_script(_file_path: str, _plugin_name: str) -> int:
        raise RuntimeError("load failed")

    monkeypatch.setattr(listener, "_write_script_file", _write_script_file)
    monkeypatch.setattr(listener, "_call_load_script", _load_script)

    await listener.prepare_cursor_position_tracking(100)

    return (
        listener._cursor_tracking_plugin_name,
        listener._cursor_tracking_script_iface,
        listener._cursor_tracking_cache,
        len(listener._cursor_tracking_sample_waiters),
        script_path.exists(),
    )


def test_prepare_cursor_tracking_cleans_up_on_load_error(monkeypatch, tmp_path) -> None:
    plugin_name, script_iface, cache, waiter_count, path_exists = asyncio.run(
        _run_prepare_cursor_tracking_failure_test(monkeypatch, tmp_path)
    )

    assert plugin_name == ""
    assert script_iface is None
    assert cache is None
    assert waiter_count == 0
    assert path_exists is False


async def _run_prepare_cursor_tracking_cancellation_test(monkeypatch, tmp_path) -> list[str]:
    listener = KDEListener(_noop_callback)
    listener.running = True
    listener._kwin_scripting = object()
    script_path = tmp_path / "cursor_tracking_cancel.js"
    calls: list[str] = []
    started = asyncio.Event()

    class _ScriptIface:
        async def call_run(self) -> None:
            calls.append("run")
            started.set()
            await asyncio.Future()

        async def call_stop(self) -> None:
            calls.append("stop")

    def _write_script_file(source: str):
        script_path.write_text(source)
        return script_path

    async def _load_script(_file_path: str, _plugin_name: str) -> int:
        calls.append("load")
        return 42

    async def _get_script_interface(_script_id: int):
        calls.append("iface")
        return _ScriptIface()

    async def _unload_script(_plugin_name: str) -> None:
        calls.append("unload")

    monkeypatch.setattr(listener, "_write_script_file", _write_script_file)
    monkeypatch.setattr(listener, "_call_load_script", _load_script)
    monkeypatch.setattr(listener, "_get_script_interface", _get_script_interface)
    monkeypatch.setattr(listener, "_call_unload_script", _unload_script)

    task = asyncio.create_task(listener.prepare_cursor_position_tracking(100))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    calls.append(f"iface:{listener._cursor_tracking_script_iface is None}")
    calls.append(f"plugin:{listener._cursor_tracking_plugin_name == ''}")
    calls.append(f"path:{script_path.exists()}")
    return calls


def test_prepare_cursor_tracking_cleans_up_on_cancellation(monkeypatch, tmp_path) -> None:
    calls = asyncio.run(_run_prepare_cursor_tracking_cancellation_test(monkeypatch, tmp_path))

    assert calls == [
        "load",
        "iface",
        "run",
        "stop",
        "unload",
        "iface:True",
        "plugin:True",
        "path:False",
    ]


def test_dispatch_runs_one_shot_kwin_script(monkeypatch, tmp_path) -> None:
    class _Uuid:
        hex = "abc12345def67890"

    class _ScriptIface:
        async def call_run(self) -> None:
            listener._on_dispatch_payload('{"id":"abc12345def67890","ok":true,"message":"ok"}')

        async def call_stop(self) -> None:
            return

    async def _load_script(_file_path: str, _plugin_name: str) -> int:
        return 42

    async def _get_script_interface(_script_id: int):
        return _ScriptIface()

    unloaded: list[str] = []

    async def _unload_script(plugin_name: str) -> None:
        unloaded.append(plugin_name)

    listener = KDEListener(_noop_callback)
    listener.running = True
    listener._kwin_scripting = object()
    script_path = tmp_path / "dispatch.js"

    def _write_script_file(source: str):
        script_path.write_text(source)
        return script_path

    monkeypatch.setattr(kde_listener_module.uuid, "uuid4", lambda: _Uuid())
    monkeypatch.setattr(listener, "_write_script_file", _write_script_file)
    monkeypatch.setattr(listener, "_call_load_script", _load_script)
    monkeypatch.setattr(listener, "_get_script_interface", _get_script_interface)
    monkeypatch.setattr(listener, "_call_unload_script", _unload_script)

    ok, message = asyncio.run(listener.dispatch("tile_left"))

    assert ok is True
    assert message == "ok"
    assert listener._dispatch_waiters == {}
    assert unloaded == [f"keymasq-kde-dispatch-{kde_listener_module.os.getpid()}-abc12345"]


class _CompletingKWinScriptIface:
    def __init__(self, future: asyncio.Future[tuple[str, str]], calls: list[str]) -> None:
        self._future = future
        self._calls = calls

    async def call_run(self) -> None:
        await asyncio.sleep(0)
        self._calls.append("run")
        self._future.set_result(("ok", "done"))

    async def call_stop(self) -> None:
        self._calls.append("stop")


class _HangingKWinScriptIface:
    def __init__(self, _future: asyncio.Future[tuple[str, str]], calls: list[str]) -> None:
        self._calls = calls

    async def call_run(self) -> None:
        self._calls.append("run")

    async def call_stop(self) -> None:
        self._calls.append("stop")


class _NoRunKWinScriptIface:
    def __init__(self, _future: asyncio.Future[tuple[str, str]], calls: list[str]) -> None:
        self._calls = calls

    async def call_stop(self) -> None:
        self._calls.append("stop")


async def _run_fake_ephemeral_kwin_script(
    monkeypatch,
    tmp_path,
    script_iface_factory,
    *,
    script_id: int = 42,
    timeout: float = 0.1,
) -> tuple[object, list[str], bool]:
    listener = KDEListener(_noop_callback)
    listener._kwin_scripting = object()
    script_path = tmp_path / "ephemeral.js"
    calls: list[str] = []
    loop = asyncio.get_running_loop()
    future: asyncio.Future[tuple[str, str]] = loop.create_future()
    script_iface = script_iface_factory(future, calls)

    def _write_script_file(source: str):
        calls.append(f"write:{source}")
        script_path.write_text(source)
        return script_path

    async def _load_script(file_path: str, plugin_name: str) -> int:
        assert file_path == str(script_path)
        assert plugin_name == "plugin"
        calls.append("load")
        return script_id

    async def _get_script_interface(loaded_script_id: int):
        assert loaded_script_id == script_id
        calls.append("iface")
        return script_iface

    async def _unload_script(plugin_name: str) -> None:
        assert plugin_name == "plugin"
        calls.append("unload")

    monkeypatch.setattr(listener, "_write_script_file", _write_script_file)
    monkeypatch.setattr(listener, "_call_load_script", _load_script)
    monkeypatch.setattr(listener, "_get_script_interface", _get_script_interface)
    monkeypatch.setattr(listener, "_call_unload_script", _unload_script)

    try:
        result = await listener._run_ephemeral_kwin_script(
            source="source",
            plugin_name="plugin",
            result_future=future,
            timeout=timeout,
        )
    except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as exc:
        result = exc

    return result, calls, script_path.exists()


def test_ephemeral_kwin_script_helper_runs_and_cleans_up(monkeypatch, tmp_path) -> None:
    result, calls, path_exists = asyncio.run(
        _run_fake_ephemeral_kwin_script(monkeypatch, tmp_path, _CompletingKWinScriptIface)
    )

    assert result == ("ok", "done")
    assert calls == ["write:source", "load", "iface", "run", "stop", "unload"]
    assert path_exists is False


def test_ephemeral_kwin_script_helper_cleans_up_on_timeout(monkeypatch, tmp_path) -> None:
    result, calls, path_exists = asyncio.run(
        _run_fake_ephemeral_kwin_script(
            monkeypatch,
            tmp_path,
            _HangingKWinScriptIface,
            timeout=0.01,
        )
    )

    assert isinstance(result, TimeoutError)
    assert calls == ["write:source", "load", "iface", "run", "stop", "unload"]
    assert path_exists is False


def test_ephemeral_kwin_script_helper_cleans_up_on_load_failure(monkeypatch, tmp_path) -> None:
    result, calls, path_exists = asyncio.run(
        _run_fake_ephemeral_kwin_script(
            monkeypatch,
            tmp_path,
            _CompletingKWinScriptIface,
            script_id=-1,
        )
    )

    assert isinstance(result, kde_listener_module._KDEEphemeralScriptLoadError)
    assert calls == ["write:source", "load", "unload"]
    assert path_exists is False


def test_ephemeral_kwin_script_helper_stops_on_run_failure(monkeypatch, tmp_path) -> None:
    result, calls, path_exists = asyncio.run(
        _run_fake_ephemeral_kwin_script(monkeypatch, tmp_path, _NoRunKWinScriptIface)
    )

    assert isinstance(result, kde_listener_module._KDEEphemeralScriptRunError)
    assert calls == ["write:source", "load", "iface", "stop", "unload"]
    assert path_exists is False
