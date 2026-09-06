"""Wait for a connection-owned playback request without blocking the event loop."""

import asyncio
import json
import signal
from typing import cast

from keymasq.common.paths import SESSION_SOCKET_PATH
from keymasq.common.types import JsonObject


async def wait_for_playback(payload: JsonObject) -> JsonObject:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    interrupted = 0

    def interrupt(signum: int) -> None:
        nonlocal interrupted
        interrupted = signum
        if task is not None:
            task.cancel()

    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, interrupt, signum)
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    playback_id = ""
    try:
        reader, writer = await asyncio.open_unix_connection(str(SESSION_SOCKET_PATH))
        writer.write(
            (json.dumps({**payload, "track": True, "cancel_on_disconnect": True}) + "\n").encode()
        )
        await writer.drain()

        while True:
            result = await _receive(reader)
            if result.get("event") == "macro_playback_finished":
                if result.get("playback_id") == playback_id:
                    return result
            elif "event" not in result:
                if result.get("status") != "ok":
                    return result
                playback_id = str(result.get("playback_id", ""))
                if not playback_id:
                    return {
                        "status": "error",
                        "message": "Session does not support tracked playback",
                    }
    except asyncio.CancelledError:
        if writer is not None and reader is not None and playback_id:
            try:
                writer.write(
                    (
                        json.dumps({"command": "cancel_macro_request", "playback_id": playback_id})
                        + "\n"
                    ).encode()
                )
                await writer.drain()
                async with asyncio.timeout(3):
                    while True:
                        result = await _receive(reader)
                        if (
                            result.get("event") == "macro_playback_finished"
                            and result.get("playback_id") == playback_id
                        ):
                            break
            except (OSError, TimeoutError, asyncio.CancelledError):
                pass
        raise SystemExit(128 + (interrupted or signal.SIGINT)) from None
    except (OSError, ValueError) as exc:
        return {"status": "error", "message": str(exc)}
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)


async def _receive(reader: asyncio.StreamReader) -> JsonObject:
    while True:
        line = await reader.readline()
        if not line:
            raise ConnectionError("Session disconnected before playback completed")
        value = json.loads(line)
        if isinstance(value, dict):
            return cast(JsonObject, value)
