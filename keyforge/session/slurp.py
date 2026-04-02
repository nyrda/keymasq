import logging

from keyforge.common.ipc import Command, CommandType
from keyforge.session.client import KeyforgedClient

log = logging.getLogger("keyforge-session.slurp")

SLURP_MACRO_NAME = "__slurp_trigger"


async def trigger_slurp_macro(daemon_client: KeyforgedClient) -> None:
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
