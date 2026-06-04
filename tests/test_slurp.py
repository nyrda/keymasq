import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from keymasq.common.slurp import SlurpCapture, SlurpMode, SlurpResult


class _FakeSlurpProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        communicate: Callable[[], Awaitable[tuple[bytes, bytes]]] | None = None,
        wait: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.communicate_calls = 0
        self.wait_calls = 0
        self.terminated = False
        self.killed = False
        self.waited = False
        self._communicate = communicate
        self._wait = wait

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicate_calls += 1
        if self._communicate:
            return await self._communicate()
        return self.stdout, self.stderr

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self) -> Awaitable[None]:
        self.wait_calls += 1
        self.waited = True
        if self._wait:
            return self._wait()

        async def _wait() -> None:
            return None

        return _wait()


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


@pytest.mark.parametrize(
    ("slurp_path", "compositor_id"),
    [
        (None, "wayland-wlr"),
        ("/usr/bin/slurp", None),
        ("/usr/bin/slurp", "x11"),
        ("/usr/bin/slurp", "wayland-wlr"),
    ],
)
def test_slurp_available_matches_unavailable_reason(
    slurp_path: str | None,
    compositor_id: str | None,
) -> None:
    capture = SlurpCapture()
    capture._slurp_path = slurp_path
    capture._available = None
    capture._compositor_id = compositor_id

    assert capture.available is (capture.get_unavailable_reason() is None)


def test_slurp_get_unavailable_reason_without_binary() -> None:
    capture = SlurpCapture()
    capture._slurp_path = None
    capture._compositor_id = "wayland-wlr"
    assert capture.get_unavailable_reason() == "slurp is not installed"


def test_slurp_get_unavailable_reason_with_bad_compositor() -> None:
    capture = SlurpCapture()
    capture._slurp_path = "/usr/bin/slurp"
    capture._compositor_id = "x11"
    reason = capture.get_unavailable_reason()
    assert reason is not None
    assert "does not support slurp" in reason


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
    assert capture._process is None


def test_capture_point_async_returns_none_for_empty_output(monkeypatch) -> None:
    capture = SlurpCapture()
    capture._available = True
    capture._slurp_path = "/usr/bin/slurp"
    process = _FakeSlurpProcess()

    _patch_slurp_process(monkeypatch, process)

    result = asyncio.run(capture.capture_point_async())
    assert result is None


def test_capture_point_async_returns_none_and_kills_on_communicate_timeout(monkeypatch) -> None:
    capture = SlurpCapture()
    capture._available = True
    capture._slurp_path = "/usr/bin/slurp"
    process = _FakeSlurpProcess()

    async def _wait_for(awaitable: Awaitable[object], timeout: float) -> object:
        awaitable.close()
        raise TimeoutError

    _patch_slurp_process(monkeypatch, process)
    monkeypatch.setattr(asyncio, "wait_for", _wait_for)

    result = asyncio.run(capture.capture_point_async())
    assert result is None
    assert process.terminated is True
    assert process.killed is True
    assert process.wait_calls == 2
    assert capture._process is None


def test_capture_point_async_timeout_does_not_clear_overlapping_capture(monkeypatch) -> None:
    capture = SlurpCapture()
    capture._available = True
    capture._slurp_path = "/usr/bin/slurp"
    capture._process = None

    async def _run() -> None:
        first_waiting = asyncio.Event()
        allow_first_timeout = asyncio.Event()
        second_started = asyncio.Event()
        second_release = asyncio.Event()

        def _communicate_for(name: str) -> Callable[[], Awaitable[tuple[bytes, bytes]]]:
            async def _communicate() -> tuple[bytes, bytes]:
                if name == "second":
                    second_started.set()
                    await second_release.wait()
                    return b"70,80\n", b""
                return b"", b""

            return _communicate

        processes: list[_FakeSlurpProcess] = []

        async def _create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeSlurpProcess:
            name = "first" if not processes else "second"
            process = _FakeSlurpProcess(communicate=_communicate_for(name))
            processes.append(process)
            return process

        async def _wait_for(awaitable: Awaitable[object], timeout: float) -> object:
            if timeout == 0.01:
                first_waiting.set()
                await allow_first_timeout.wait()
                awaitable.close()
                raise TimeoutError
            return await awaitable

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess_exec)
        monkeypatch.setattr(asyncio, "wait_for", _wait_for)

        first_task = asyncio.create_task(capture.capture_point_async(timeout=0.01))
        await first_waiting.wait()
        second_task = asyncio.create_task(capture.capture_point_async(timeout=5.0))
        await second_started.wait()

        allow_first_timeout.set()
        assert await first_task is None
        first_process, second_process = processes
        assert first_process.terminated is True
        assert first_process.waited is True
        assert second_process.terminated is False
        assert capture._process is second_process

        second_release.set()
        assert await second_task == SlurpResult(x=70, y=80)
        assert capture._process is None

    asyncio.run(_run())


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
    assert capture._process is None


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


def test_cancel_async_terminates_process() -> None:
    capture = SlurpCapture()
    process = _FakeSlurpProcess()
    capture._process = process  # type: ignore[assignment]

    asyncio.run(capture.cancel_async())

    assert process.terminated is True
    assert process.waited is True
    assert capture._process is None


def test_cancel_async_kills_and_waits_again_when_terminate_wait_times_out(
    monkeypatch,
) -> None:
    capture = SlurpCapture()
    process = _FakeSlurpProcess()
    capture._process = process  # type: ignore[assignment]
    wait_for_calls = 0

    async def _wait_for(awaitable: Awaitable[object], timeout: float) -> object:
        nonlocal wait_for_calls
        wait_for_calls += 1
        awaitable.close()
        if wait_for_calls == 1:
            raise TimeoutError
        return None

    monkeypatch.setattr(asyncio, "wait_for", _wait_for)

    asyncio.run(capture.cancel_async())

    assert process.terminated is True
    assert process.killed is True
    assert process.wait_calls == 2
    assert capture._process is None


def test_cancel_async_warns_when_process_survives_kill(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = SlurpCapture()
    process = _FakeSlurpProcess()
    capture._process = process  # type: ignore[assignment]

    async def _wait_for(awaitable: Awaitable[object], timeout: float) -> object:
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", _wait_for)
    caplog.set_level(logging.WARNING, logger="keymasq.slurp")

    asyncio.run(capture.cancel_async())

    assert process.terminated is True
    assert process.killed is True
    assert process.wait_calls == 2
    assert "slurp process did not exit after kill" in caplog.text
    assert capture._process is None


def test_cancel_async_logs_unexpected_terminate_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    capture = SlurpCapture()

    class _BrokenProcess(_FakeSlurpProcess):
        def terminate(self) -> None:
            raise RuntimeError("terminate failed")

    process = _BrokenProcess()
    capture._process = process  # type: ignore[assignment]
    caplog.set_level(logging.ERROR, logger="keymasq.slurp")

    asyncio.run(capture.cancel_async())

    assert "Unexpected failure terminating slurp process" in caplog.text
    assert process.killed is False
    assert capture._process is None
