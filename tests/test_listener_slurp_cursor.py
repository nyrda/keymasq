import logging
from collections.abc import Awaitable, Callable

import pytest

from keymasq.common.ipc import Command, Response
from keymasq.common.slurp import SlurpMode, SlurpResult
from keymasq.session.slurp import capture_slurp_cursor_position


class _FakeClient:
    def __init__(self) -> None:
        self.commands: list[Command] = []

    async def send_command(self, command: Command) -> Response:
        self.commands.append(command)
        return Response(status="ok")


class _FakeSlurp:
    def __init__(
        self,
        *,
        available: bool = True,
        result: SlurpResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self._available = available
        self.result = result
        self.error = error
        self.capture_calls = 0
        self.mode: SlurpMode | None = None

    @property
    def available(self) -> bool:
        return self._available

    async def capture_point_async(
        self,
        mode: SlurpMode = SlurpMode.POINT,
        on_ready: Callable[[], Awaitable[None]] | None = None,
        timeout: float = 5.0,
    ) -> SlurpResult | None:
        _ = timeout
        self.capture_calls += 1
        self.mode = mode
        if on_ready is not None:
            await on_ready()
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_capture_slurp_cursor_position_returns_none_when_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    slurp = _FakeSlurp(available=False)
    client = _FakeClient()

    with caplog.at_level(logging.DEBUG):
        result = await capture_slurp_cursor_position(
            slurp,
            client,  # type: ignore[arg-type]
            logging.getLogger("keymasq-test"),
        )

    assert result is None
    assert slurp.capture_calls == 0
    assert "Slurp cursor capture not available" in caplog.text


@pytest.mark.asyncio
async def test_capture_slurp_cursor_position_returns_none_without_client(
    caplog: pytest.LogCaptureFixture,
) -> None:
    slurp = _FakeSlurp(result=SlurpResult(x=10, y=20))

    with caplog.at_level(logging.DEBUG):
        result = await capture_slurp_cursor_position(
            slurp,
            None,
            logging.getLogger("keymasq-test"),
        )

    assert result is None
    assert slurp.capture_calls == 0
    assert "Slurp cursor capture requires client connection" in caplog.text


@pytest.mark.asyncio
async def test_capture_slurp_cursor_position_returns_point_and_triggers_macro() -> None:
    slurp = _FakeSlurp(result=SlurpResult(x=10, y=20))
    client = _FakeClient()

    result = await capture_slurp_cursor_position(
        slurp,
        client,  # type: ignore[arg-type]
    )

    assert result == (10, 20)
    assert slurp.capture_calls == 1
    assert slurp.mode is SlurpMode.POINT_IMMEDIATE
    assert len(client.commands) == 1


@pytest.mark.asyncio
async def test_capture_slurp_cursor_position_returns_none_on_capture_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    slurp = _FakeSlurp(error=RuntimeError("boom"))
    client = _FakeClient()

    with caplog.at_level(logging.DEBUG):
        result = await capture_slurp_cursor_position(
            slurp,
            client,  # type: ignore[arg-type]
            logging.getLogger("keymasq-test"),
        )

    assert result is None
    assert "Slurp cursor capture failed: boom" in caplog.text
