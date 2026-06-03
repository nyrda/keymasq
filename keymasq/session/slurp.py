import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from keymasq.common.ipc import Command, CommandType
from keymasq.common.slurp import SlurpMode, SlurpResult
from keymasq.session.client import KeymasqdClient

log = logging.getLogger("keymasq-session.slurp")

SLURP_MACRO_NAME = "__slurp_trigger"


class SlurpCursorCapture(Protocol):
    @property
    def available(self) -> bool: ...

    async def capture_point_async(
        self,
        mode: SlurpMode = SlurpMode.POINT,
        on_ready: Callable[[], Awaitable[None]] | None = None,
        timeout: float = 5.0,
    ) -> SlurpResult | None: ...


async def trigger_slurp_macro(daemon_client: KeymasqdClient) -> None:
    log.debug("trigger_slurp_macro: sending MACRO_PLAY_BY_NAME for __slurp_trigger")
    try:
        result = await daemon_client.send_command(
            Command(
                command=CommandType.MACRO_PLAY_BY_NAME,
                data={"name": SLURP_MACRO_NAME, "speed": 1.0},
            )
        )
        log.debug(f"trigger_slurp_macro: result status={result.status}")
    except Exception:
        log.exception("failed to trigger slurp macro")


async def capture_slurp_cursor_position(
    slurp: SlurpCursorCapture,
    client: KeymasqdClient | None,
    logger: logging.Logger | None = None,
) -> tuple[int, int] | None:
    if not slurp.available:
        if logger is not None:
            logger.debug("Slurp cursor capture not available")
        return None

    if client is None:
        if logger is not None:
            logger.debug("Slurp cursor capture requires client connection")
        return None

    try:
        result = await slurp.capture_point_async(
            mode=SlurpMode.POINT_IMMEDIATE,
            on_ready=lambda: trigger_slurp_macro(client),
        )
        if result:
            return (result.x, result.y)
    except OSError as exc:
        if logger is not None:
            logger.debug("Slurp cursor capture failed: %s", exc)
    except Exception:
        if logger is not None:
            logger.exception("Unexpected slurp cursor capture failure")
        else:
            log.exception("Unexpected slurp cursor capture failure")
    return None
