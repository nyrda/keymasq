import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from keymasq.common.ipc import Command, CommandType, Response
from keymasq.common.types import JsonObject
from keymasq.session.manager.command.macro import handle_macro_commands
from keymasq.session.manager.core import SessionManager
from keymasq.session.manager.playback import PlaybackRequests


def setup_requests():
    manager = cast(
        SessionManager,
        SimpleNamespace(
            client=SimpleNamespace(send_command=AsyncMock()),
            security_policy=SimpleNamespace(macro_exec_timeout_max_ms=1000),
            broadcast_to_session_client_ids=Mock(),
        ),
    )
    requests = PlaybackRequests(manager)
    manager.playback_requests = requests
    sent: asyncio.Queue[Command] = asyncio.Queue()

    async def send(command: Command, timeout: float | None = None) -> Response:
        assert timeout is None
        sent.put_nowait(command)
        if command.command == CommandType.CANCEL_MACRO_PLAYBACK:
            requests.finished({"playback_id": command.data["playback_id"], "state": "cancelled"})
        return Response(status="ok", data={"status": "ok"})

    manager.client.send_command = AsyncMock(side_effect=send)
    return manager, requests, sent


def owner() -> asyncio.StreamWriter:
    return cast(asyncio.StreamWriter, object())


async def join(requests: PlaybackRequests, result: JsonObject) -> JsonObject:
    job = requests.requests[str(result["playback_id"])]
    assert job.task is not None
    await asyncio.wait_for(asyncio.shield(job.task), 1)
    return job.result


@pytest.mark.asyncio
async def test_text_fifo_across_clients_and_owner_only_completion() -> None:
    manager, requests, sent = setup_requests()
    first_owner, second_owner = owner(), owner()
    first = requests.submit({"command": "type_text", "text": "a", "track": True}, first_owner)
    second = requests.submit({"command": "type_text", "text": "b", "track": True}, second_owner)
    command = await asyncio.wait_for(sent.get(), 1)
    assert command.data["playback_id"] == first["playback_id"]
    assert sent.empty()
    requests.finished({"playback_id": first["playback_id"], "state": "completed"})
    assert (await join(requests, first))["state"] == "completed"
    command = await asyncio.wait_for(sent.get(), 1)
    assert command.data["playback_id"] == second["playback_id"]
    requests.finished({"playback_id": second["playback_id"], "state": "completed"})
    await join(requests, second)
    notifications = manager.broadcast_to_session_client_ids.call_args_list
    assert [call.args[1] for call in notifications] == [{id(first_owner)}, {id(second_owner)}]


@pytest.mark.asyncio
async def test_cancel_is_scoped_and_queued_cancellation_never_starts() -> None:
    _, requests, sent = setup_requests()
    writer, stranger = owner(), owner()
    first = requests.submit({"command": "type_text", "text": "a", "track": True}, writer)
    second = requests.submit({"command": "type_text", "text": "b", "track": True}, writer)
    await asyncio.wait_for(sent.get(), 1)
    first_id, second_id = str(first["playback_id"]), str(second["playback_id"])
    assert (await requests.cancel(first_id, stranger))["status"] == "error"
    assert requests.status(first_id, stranger)["status"] == "error"
    assert (await requests.cancel(second_id, writer))["state"] == "cancelled"
    await requests.cancel(first_id, writer)
    assert (await join(requests, first))["state"] == "cancelled"
    command = sent.get_nowait()
    assert command.command == CommandType.CANCEL_MACRO_PLAYBACK
    assert command.data == {"playback_id": first_id}
    assert sent.empty()


@pytest.mark.asyncio
async def test_disconnect_during_acceptance_cancels_after_ack() -> None:
    manager, requests, sent = setup_requests()
    accepted = asyncio.Event()
    original_send = manager.client.send_command

    async def send(command: Command, timeout: float | None = None) -> Response:
        assert timeout is None
        result = await original_send(command)
        if command.command == CommandType.MACRO_PLAY_BY_NAME:
            await accepted.wait()
        return result

    manager.client.send_command = AsyncMock(side_effect=send)
    writer = owner()
    result = requests.submit({"command": "play_macro", "name": "slow", "track": True}, writer)
    job = requests.requests[str(result["playback_id"])]
    await asyncio.wait_for(sent.get(), 1)
    await requests.disconnect(writer)
    assert sent.empty()
    accepted.set()
    assert job.task is not None
    await asyncio.wait_for(job.task, 1)
    assert sent.get_nowait().command == CommandType.CANCEL_MACRO_PLAYBACK
    assert not requests.requests


@pytest.mark.asyncio
async def test_completion_before_acceptance_and_failure_release_queue() -> None:
    manager, requests, _ = setup_requests()

    async def send(command: Command, timeout: float | None = None) -> Response:
        assert timeout is None
        requests.finished(
            {
                "playback_id": command.data["playback_id"],
                "state": "failed",
                "message": "output failed",
            }
        )
        return Response(status="ok", data={"status": "ok"})

    manager.client.send_command = AsyncMock(side_effect=send)
    writer = owner()
    first = requests.submit({"command": "type_text", "text": "a", "track": True}, writer)
    second = requests.submit({"command": "type_text", "text": "b", "track": True}, writer)
    for result in (first, second):
        terminal = await join(requests, result)
        assert terminal["state"] == "failed"
        assert terminal["message"] == "output failed"


@pytest.mark.asyncio
async def test_daemon_loss_finishes_active_and_queued_requests() -> None:
    _, requests, sent = setup_requests()
    writer = owner()
    results = [
        requests.submit({"command": "type_text", "text": "a", "track": True}, writer)
        for _ in range(2)
    ]
    await asyncio.wait_for(sent.get(), 1)
    await requests.daemon_disconnected()
    for result in results:
        assert requests.status(str(result["playback_id"]), writer)["state"] == "failed"
    assert sent.empty()


@pytest.mark.asyncio
async def test_detached_text_survives_disconnect_and_caller_cannot_set_id() -> None:
    manager, requests, sent = setup_requests()
    writer = owner()
    result = await handle_macro_commands(
        manager, "type_text", {"command": "type_text", "text": "a", "playback_id": "forged"}, writer
    )
    assert result is not None
    job = requests.requests[str(result["playback_id"])]
    await requests.disconnect(writer)
    command = await asyncio.wait_for(sent.get(), 1)
    assert command.data["playback_id"] != "forged"
    requests.finished({"playback_id": result["playback_id"], "state": "completed"})
    assert job.task is not None
    await asyncio.wait_for(job.task, 1)
    assert not requests.requests


@pytest.mark.asyncio
async def test_cancel_during_compilation_prevents_submission(monkeypatch) -> None:
    from keymasq.session.manager.command import macro

    _, requests, sent = setup_requests()
    compiling = asyncio.Event()
    release = asyncio.Event()

    async def compile_later(func, /, *args, **kwargs):
        compiling.set()
        await release.wait()
        return func(*args, **kwargs)

    monkeypatch.setattr(macro.asyncio, "to_thread", compile_later)
    writer = owner()
    result = requests.submit({"command": "type_text", "text": "a", "track": True}, writer)
    await compiling.wait()
    await requests.cancel(str(result["playback_id"]), writer)
    release.set()
    assert (await join(requests, result))["state"] == "cancelled"
    assert sent.empty()


@pytest.mark.asyncio
async def test_global_stop_cancels_queue_and_submission_in_progress() -> None:
    manager, requests, sent = setup_requests()
    release = asyncio.Event()
    original_send = manager.client.send_command

    async def send(command: Command, timeout: float | None = None) -> Response:
        assert timeout is None
        result = await original_send(command)
        if command.command == CommandType.MACRO_PLAY_BY_NAME:
            await release.wait()
        return result

    manager.client.send_command = AsyncMock(side_effect=send)
    writer = owner()
    first = requests.submit(
        {"command": "play_macro", "name": "a", "track": True, "ordered": True}, writer
    )
    second = requests.submit({"command": "type_text", "text": "b", "track": True}, writer)
    await sent.get()
    requests.cancel_pending()
    release.set()
    assert (await join(requests, first))["state"] == "cancelled"
    assert requests.status(str(second["playback_id"]), writer)["state"] == "cancelled"
    assert sent.get_nowait().command == CommandType.CANCEL_MACRO_PLAYBACK
    assert sent.empty()
