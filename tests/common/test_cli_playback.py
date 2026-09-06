import asyncio
import json
import signal
import sys
from pathlib import Path

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,exit_code", [("completed", 0), ("failed", 1), ("cancelled", 1)])
async def test_wait_cli_waits_for_its_terminal_event(
    tmp_path: Path, outcome: str, exit_code: int
) -> None:
    await run_cli(tmp_path, outcome, exit_code)


@pytest.mark.asyncio
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
async def test_interrupt_cancels_individual_playback(
    tmp_path: Path, signum: signal.Signals
) -> None:
    await run_cli(tmp_path, "cancelled", 128 + signum, signum)


async def run_cli(
    tmp_path: Path, outcome: str, exit_code: int, signum: signal.Signals | None = None
) -> None:
    socket_path = tmp_path / "session.sock"
    accepted = asyncio.Event()
    finish = asyncio.Event()
    disconnected = asyncio.Event()
    received: list[dict[str, object]] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            received.append(json.loads(await reader.readline()))
            writer.write(
                b'{"event":"unrelated"}\n{"status":"ok","playback_id":"owned","state":"queued"}\n'
            )
            await writer.drain()
            accepted.set()
            if signum is not None:
                received.append(json.loads(await reader.readline()))
            else:
                await finish.wait()
            writer.write(
                (
                    json.dumps(
                        {
                            "event": "macro_playback_finished",
                            "playback_id": "someone-else",
                            "state": "completed",
                            "status": "ok",
                        }
                    )
                    + "\n"
                ).encode()
            )
            writer.write(
                (
                    json.dumps(
                        {
                            "event": "macro_playback_finished",
                            "playback_id": "owned",
                            "state": outcome,
                            "status": "ok" if outcome == "completed" else "error",
                            "message": outcome,
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            disconnected.set()

    server = await asyncio.start_unix_server(handle, path=str(socket_path))
    script = (
        "import sys; from pathlib import Path; "
        "from keymasq.cli import playback, commands; "
        "playback.SESSION_SOCKET_PATH = Path(sys.argv[1]); "
        "commands.type_cli(['hello'], wait=True, json_output=True)"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(socket_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(accepted.wait(), 5)
        assert process.returncode is None
        # Give the CLI time to consume acceptance before sending an OS signal.
        if signum is not None:
            await asyncio.sleep(0.05)
            process.send_signal(signum)
        else:
            finish.set()
        stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == exit_code, stderr.decode()
        assert received[0]["track"] is True
        assert received[0]["cancel_on_disconnect"] is True
        if signum is not None:
            assert received[1] == {"command": "cancel_macro_request", "playback_id": "owned"}
        else:
            assert json.loads(stdout)["state"] == outcome
        await asyncio.wait_for(disconnected.wait(), 1)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        server.close()
        await server.wait_closed()
