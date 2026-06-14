import logging

from keymasq.common.ipc import Command, CommandType
from keymasq.session.client import KeymasqdClient

log = logging.getLogger("keymasq-session.cursor_position")

CURSOR_POSITION_TRIGGER_MACRO_NAME = "__cursor_position_trigger"


async def trigger_cursor_position_sample(daemon_client: KeymasqdClient) -> None:
    log.debug(
        "trigger_cursor_position_sample: sending MACRO_PLAY_BY_NAME for %s",
        CURSOR_POSITION_TRIGGER_MACRO_NAME,
    )
    try:
        result = await daemon_client.send_command(
            Command(
                command=CommandType.MACRO_PLAY_BY_NAME,
                data={"name": CURSOR_POSITION_TRIGGER_MACRO_NAME, "speed": 1.0},
            )
        )
        log.debug("trigger_cursor_position_sample: result status=%s", result.status)
    except Exception:
        log.exception("failed to trigger cursor position sample")
