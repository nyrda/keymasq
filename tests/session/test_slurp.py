import asyncio
import logging
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

import pytest

from keymasq.common.slurp import SlurpCapture, SlurpMode, SlurpResult
from tests.async_fakes import FakeProcess as _FakeSlurpProcess


@pytest.fixture(autouse=True)
def reset_slurp_capture_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SlurpCapture, "_instance", None)


def _patch_slurp_process(
    monkeypatch: pytest.MonkeyPatch,
    process: _FakeSlurpProcess,
) -> None:
    async def _create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeSlurpProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess_exec)


def test_slurp_capture_available_without_slurp_binary() -> None:
    capture = SlurpCapture()
    capture._slurp_path = None
    capture._available = None
    capture.set_compositor("wayland-wlr")
    assert capture.available is False


def test_slurp_capture_available_without_compositor() -> None:
    capture = SlurpCapture()
    capture._available = None
    capture.set_compositor(None)
    assert capture.available is False


def test_slurp_capture_available_with_incompatible_compositor() -> None:
    capture = SlurpCapture()
    capture._available = None
    capture.set_compositor("x11")
    assert capture.available is False


def test_slurp_parse_output() -> None:
    capture = SlurpCapture()
    result = capture._parse_output("100,200")
    assert result is not None
    assert result.x == 100
    assert result.y == 200


def test_slurp_parse_output_invalid() -> None:
    capture = SlurpCapture()
    result = capture._parse_output("invalid")
    assert result is None


def test_slurp_parse_output_empty() -> None:
    capture = SlurpCapture()
    result = capture._parse_output("")
    assert result is None


def test_slurp_mode_values() -> None:
    assert SlurpMode.POINT.value == "point"
    assert SlurpMode.POINT_IMMEDIATE.value == "point_immediate"


def test_slurp_available_caches_success_result() -> None:
    capture = SlurpCapture()
    capture._slurp_path = "/usr/bin/slurp"
    capture._available = None
    capture.set_compositor("wayland-wlr")

    assert capture.available is True

    capture._slurp_path = None
    assert capture.available is True


def test_slurp_capture_available_with_niri_is_enabled() -> None:
    capture = SlurpCapture()
    capture._slurp_path = "/usr/bin/slurp"
    capture._available = None
    capture.set_compositor("niri")

    assert capture.available is True


def test_slurp_capture_available_with_layer_shell_fallback_is_enabled() -> None:
    capture = SlurpCapture()
    capture._slurp_path = "/usr/bin/slurp"
    capture._available = None
    capture.set_compositor("wayland-layer-shell")

    assert capture.available is True


def test_capture_point_unavailable_calls_callback_none() -> None:
    capture = SlurpCapture()
    capture._available = False
    values: list[SlurpResult | None] = []

    capture.capture_point(values.append)
    assert values == [None]


def test_run_async_task_without_running_loop_executes_with_asyncio_run(
    monkeypatch,
) -> None:
    capture = SlurpCapture()
    values: list[str] = []

    async def _mark() -> None:
        values.append("done")

    run_calls: list[Awaitable[object]] = []
    real_asyncio_run = asyncio.run

    def _run(coro: Awaitable[object]) -> None:
        run_calls.append(coro)
        real_asyncio_run(coro)

    monkeypatch.setattr(asyncio, "run", _run)

    capture._run_async_task(_mark())

    assert len(run_calls) == 1
    assert values == ["done"]


def test_capture_point_async_returns_none_when_slurp_fails(monkeypatch) -> None:
    capture = SlurpCapture()
    capture._available = True
    capture._slurp_path = "/usr/bin/slurp"
    process = _FakeSlurpProcess(returncode=1, stderr=b"boom")

    _patch_slurp_process(monkeypatch, process)

    result = asyncio.run(capture.capture_point_async())
    assert result is None


def test_capture_point_async_returns_parsed_result_and_calls_on_ready(monkeypatch) -> None:
    capture = SlurpCapture()
    capture._available = True
    capture._slurp_path = "/usr/bin/slurp"
    events: list[str] = []

    async def _communicate() -> tuple[bytes, bytes]:
        events.append("communicate")
        return b"50,60\n", b""

    process = _FakeSlurpProcess(communicate=_communicate)

    async def _create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeSlurpProcess:
        assert args[0] == "/usr/bin/slurp"
        events.append("spawn")
        return process

    async def _on_ready() -> None:
        events.append("ready")

    async def _sleep(_delay: float) -> None:
        events.append("sleep")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess_exec)
    monkeypatch.setattr(asyncio, "sleep", _sleep)

    result = asyncio.run(
        capture.capture_point_async(mode=SlurpMode.POINT_IMMEDIATE, on_ready=_on_ready)
    )

    assert result == SlurpResult(x=50, y=60)
    assert events == ["spawn", "sleep", "ready", "communicate"]


def test_capture_point_async_keeps_environment_for_bundled_slurp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    appdir = tmp_path / "AppDir"
    bundled_slurp = appdir / "bin/slurp"
    bundled_slurp.parent.mkdir(parents=True)
    bundled_slurp.write_text("#!/bin/sh\n", encoding="utf-8")
    bundled_slurp.chmod(0o755)
    capture = SlurpCapture()
    capture._available = True
    capture._slurp_path = str(bundled_slurp)
    captured_kwargs: dict[str, Any] = {}

    monkeypatch.setenv("APPDIR", str(appdir))
    monkeypatch.setenv("LD_LIBRARY_PATH", f"{appdir / 'lib'}:/host/lib")
    monkeypatch.setenv("PYTHONHOME", str(appdir))

    async def _create_subprocess_exec(*_args: Any, **kwargs: Any) -> _FakeSlurpProcess:
        captured_kwargs.update(kwargs)
        return _FakeSlurpProcess(stdout=b"50,60\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess_exec)

    result = asyncio.run(capture.capture_point_async())

    assert result == SlurpResult(x=50, y=60)
    assert "env" not in captured_kwargs


def test_capture_point_async_scrubs_appimage_environment_for_host_slurp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    appdir = tmp_path / "AppDir"
    appdir.mkdir()
    capture = SlurpCapture()
    capture._available = True
    capture._slurp_path = "/usr/bin/slurp"
    captured_env: dict[str, str] = {}

    monkeypatch.setenv("APPDIR", str(appdir))
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "Keymasq.AppImage"))
    monkeypatch.setenv("LD_LIBRARY_PATH", f"{appdir / 'lib'}:/host/lib")
    monkeypatch.setenv("PYTHONHOME", str(appdir))
    monkeypatch.setenv("PYTHONPATH", str(appdir / "pythonpath"))
    monkeypatch.setenv("XDG_DATA_DIRS", f"{appdir / 'share'}:/usr/local/share:/usr/share")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")

    async def _create_subprocess_exec(*_args: Any, **kwargs: Any) -> _FakeSlurpProcess:
        captured_env.update(kwargs["env"])
        return _FakeSlurpProcess(stdout=b"50,60\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess_exec)

    result = asyncio.run(capture.capture_point_async())

    assert result == SlurpResult(x=50, y=60)
    assert captured_env["LD_LIBRARY_PATH"] == "/host/lib"
    assert captured_env["XDG_DATA_DIRS"] == "/usr/local/share:/usr/share"
    assert captured_env["WAYLAND_DISPLAY"] == "wayland-0"
    assert captured_env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"
    for key in ("APPDIR", "APPIMAGE", "PYTHONHOME", "PYTHONPATH"):
        assert key not in captured_env


def test_capture_point_async_terminates_process_when_on_ready_fails(monkeypatch) -> None:
    capture = SlurpCapture()
    capture._available = True
    capture._slurp_path = "/usr/bin/slurp"
    process = _FakeSlurpProcess(stdout=b"50,60\n")

    async def _on_ready() -> None:
        raise RuntimeError("ready failed")

    async def _sleep(_delay: float) -> None:
        return None

    _patch_slurp_process(monkeypatch, process)
    monkeypatch.setattr(asyncio, "sleep", _sleep)

    result = asyncio.run(
        capture.capture_point_async(mode=SlurpMode.POINT_IMMEDIATE, on_ready=_on_ready)
    )

    assert result is None
    assert process.terminated is True
    assert process.communicate_calls == 0


def test_capture_point_async_returns_none_for_empty_output(monkeypatch) -> None:
    capture = SlurpCapture()
    capture._available = True
    capture._slurp_path = "/usr/bin/slurp"
    process = _FakeSlurpProcess()

    _patch_slurp_process(monkeypatch, process)

    result = asyncio.run(capture.capture_point_async())
    assert result is None


def test_capture_point_async_returns_none_and_kills_on_communicate_timeout(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = SlurpCapture()
    capture._available = True
    capture._slurp_path = "/usr/bin/slurp"
    process = _FakeSlurpProcess()

    async def _wait_for(awaitable: Awaitable[object], timeout: float) -> object:
        awaitable.close()
        raise TimeoutError

    _patch_slurp_process(monkeypatch, process)
    monkeypatch.setattr(asyncio, "wait_for", _wait_for)
    caplog.set_level(logging.WARNING, logger="keymasq.slurp")

    result = asyncio.run(capture.capture_point_async())
    assert result is None
    assert process.terminated is True
    assert process.killed is True
    assert process.wait_calls == 2
    assert "slurp process did not exit after kill" in caplog.text


def test_capture_point_async_cancels_process_on_external_cancel(monkeypatch) -> None:
    capture = SlurpCapture()
    capture._available = True
    capture._slurp_path = "/usr/bin/slurp"

    async def _communicate() -> tuple[bytes, bytes]:
        raise asyncio.CancelledError

    process = _FakeSlurpProcess(communicate=_communicate)

    _patch_slurp_process(monkeypatch, process)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(capture.capture_point_async())

    assert process.terminated is True


def test_capture_point_invokes_callback_with_result(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = SlurpCapture()
    capture._available = True
    values: list[SlurpResult | None] = []

    async def _capture_point_async(
        mode: SlurpMode = SlurpMode.POINT,
        on_ready: Any = None,
    ) -> SlurpResult:
        assert mode is SlurpMode.POINT
        assert on_ready is None
        return SlurpResult(x=7, y=8)

    monkeypatch.setattr(capture, "capture_point_async", _capture_point_async)

    capture.capture_point(values.append)

    assert values == [SlurpResult(x=7, y=8)]


def test_capture_point_async_logs_unexpected_terminate_failure(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = SlurpCapture()
    capture._available = True
    capture._slurp_path = "/usr/bin/slurp"

    async def _communicate() -> tuple[bytes, bytes]:
        raise asyncio.CancelledError

    class _BrokenProcess(_FakeSlurpProcess):
        def terminate(self) -> None:
            raise RuntimeError("terminate failed")

    process = _BrokenProcess(communicate=_communicate)
    _patch_slurp_process(monkeypatch, process)
    caplog.set_level(logging.ERROR, logger="keymasq.slurp")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(capture.capture_point_async())

    assert "Unexpected failure terminating slurp process" in caplog.text
    assert process.killed is False
