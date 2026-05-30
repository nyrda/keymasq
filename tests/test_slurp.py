import asyncio
from collections.abc import Awaitable
from typing import Any

import pytest

from keymasq.common.slurp import SlurpCapture, SlurpMode, SlurpResult


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

    class _FakeProcess:
        returncode = 1

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"boom"

    async def _create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess_exec)

    result = asyncio.run(capture.capture_point_async())
    assert result is None


def test_capture_point_async_returns_parsed_result_and_calls_on_ready(monkeypatch) -> None:
    capture = SlurpCapture()
    capture._available = True
    capture._slurp_path = "/usr/bin/slurp"
    events: list[str] = []

    class _FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            events.append("communicate")
            return b"50,60\n", b""

    async def _create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        assert args[0] == "/usr/bin/slurp"
        events.append("spawn")
        return _FakeProcess()

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

    class _FakeProcess:
        returncode = 0

        def __init__(self) -> None:
            self.terminated = False
            self.communicated = False

        async def communicate(self) -> tuple[bytes, bytes]:
            self.communicated = True
            return b"50,60\n", b""

        def terminate(self) -> None:
            self.terminated = True

        async def wait(self) -> None:
            return None

    process = _FakeProcess()

    async def _create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return process

    async def _on_ready() -> None:
        raise RuntimeError("ready failed")

    async def _sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess_exec)
    monkeypatch.setattr(asyncio, "sleep", _sleep)

    result = asyncio.run(
        capture.capture_point_async(mode=SlurpMode.POINT_IMMEDIATE, on_ready=_on_ready)
    )

    assert result is None
    assert process.terminated is True
    assert process.communicated is False
    assert capture._process is None


def test_capture_point_async_returns_none_for_empty_output(monkeypatch) -> None:
    capture = SlurpCapture()
    capture._available = True
    capture._slurp_path = "/usr/bin/slurp"

    class _FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def _create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess_exec)

    result = asyncio.run(capture.capture_point_async())
    assert result is None


def test_capture_point_async_returns_none_and_kills_on_communicate_timeout(monkeypatch) -> None:
    capture = SlurpCapture()
    capture._available = True
    capture._slurp_path = "/usr/bin/slurp"

    class _FakeProcess:
        returncode = 0

        def __init__(self) -> None:
            self.terminated = False
            self.killed = False
            self.wait_calls = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

        def wait(self) -> Awaitable[None]:
            self.wait_calls += 1

            async def _wait() -> None:
                return None

            return _wait()

    process = _FakeProcess()

    async def _create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return process

    async def _wait_for(awaitable: Awaitable[object], timeout: float) -> object:
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess_exec)
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

        class _FakeProcess:
            returncode = 0

            def __init__(self, name: str) -> None:
                self.name = name
                self.terminated = False
                self.waited = False

            async def communicate(self) -> tuple[bytes, bytes]:
                if self.name == "second":
                    second_started.set()
                    await second_release.wait()
                    return b"70,80\n", b""
                return b"", b""

            def terminate(self) -> None:
                self.terminated = True

            async def wait(self) -> None:
                self.waited = True

        processes: list[_FakeProcess] = []

        async def _create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
            process = _FakeProcess("first" if not processes else "second")
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

    class _FakeProcess:
        returncode = 0

        def __init__(self) -> None:
            self.terminated = False

        async def communicate(self) -> tuple[bytes, bytes]:
            raise asyncio.CancelledError

        def terminate(self) -> None:
            self.terminated = True

        async def wait(self) -> None:
            return None

    process = _FakeProcess()

    async def _create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess_exec)

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

    class _FakeProcess:
        def __init__(self) -> None:
            self.terminated = False
            self.waited = False

        def terminate(self) -> None:
            self.terminated = True

        async def wait(self) -> None:
            self.waited = True

    process = _FakeProcess()
    capture._process = process  # type: ignore[assignment]

    asyncio.run(capture.cancel_async())

    assert process.terminated is True
    assert process.waited is True
    assert capture._process is None


def test_cancel_async_kills_and_waits_again_when_terminate_wait_times_out(
    monkeypatch,
) -> None:
    capture = SlurpCapture()

    class _FakeProcess:
        def __init__(self) -> None:
            self.terminated = False
            self.killed = False
            self.wait_calls = 0

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

        def wait(self) -> Awaitable[None]:
            self.wait_calls += 1

            async def _wait() -> None:
                return None

            return _wait()

    process = _FakeProcess()
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
