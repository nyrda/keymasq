import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from keymasq.common.ipc import Command, CommandType, Response
from keymasq.common.types import JsonObject
from keymasq.session.manager import playback as playback_module
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
        assert (
            timeout is None
            if command.command != CommandType.CANCEL_MACRO_PLAYBACK
            else timeout is not None
        )
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
    first = requests.submit(
        {"command": "type_text", "text": "a", "track": True, "ordered": True}, first_owner
    )
    second = requests.submit(
        {"command": "type_text", "text": "b", "track": True, "ordered": True}, second_owner
    )
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
    first = requests.submit(
        {"command": "type_text", "text": "a", "track": True, "ordered": True}, writer
    )
    second = requests.submit(
        {"command": "type_text", "text": "b", "track": True, "ordered": True}, writer
    )
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
        assert (
            timeout is None
            if command.command != CommandType.CANCEL_MACRO_PLAYBACK
            else timeout is not None
        )
        result = await original_send(command, timeout=timeout)
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
        assert (
            timeout is None
            if command.command != CommandType.CANCEL_MACRO_PLAYBACK
            else timeout is not None
        )
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
    first = requests.submit(
        {"command": "type_text", "text": "a", "track": True, "ordered": True}, writer
    )
    second = requests.submit(
        {"command": "type_text", "text": "b", "track": True, "ordered": True}, writer
    )
    for result in (first, second):
        terminal = await join(requests, result)
        assert terminal["state"] == "failed"
        assert terminal["message"] == "output failed"


@pytest.mark.asyncio
async def test_daemon_loss_finishes_active_and_queued_requests() -> None:
    _, requests, sent = setup_requests()
    writer = owner()
    results = [
        requests.submit(
            {"command": "type_text", "text": "a", "track": True, "ordered": True}, writer
        )
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
        assert (
            timeout is None
            if command.command != CommandType.CANCEL_MACRO_PLAYBACK
            else timeout is not None
        )
        result = await original_send(command, timeout=timeout)
        if command.command == CommandType.MACRO_PLAY_BY_NAME:
            await release.wait()
        return result

    manager.client.send_command = AsyncMock(side_effect=send)
    writer = owner()
    first = requests.submit(
        {"command": "play_macro", "name": "a", "track": True, "ordered": True}, writer
    )
    second = requests.submit(
        {"command": "type_text", "text": "b", "track": True, "ordered": True}, writer
    )
    await sent.get()
    requests.cancel_pending()
    release.set()
    assert (await join(requests, first))["state"] == "cancelled"
    assert requests.status(str(second["playback_id"]), writer)["state"] == "cancelled"
    assert sent.get_nowait().command == CommandType.CANCEL_MACRO_PLAYBACK
    assert sent.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["type_text", "play_macro"])
async def test_default_requests_run_concurrently_even_with_an_ordered_request(command: str) -> None:
    _, requests, sent = setup_requests()
    writer = owner()
    results = [
        requests.submit(
            {"command": command, "text": "a", "name": "a", "track": True, "ordered": ordered},
            writer,
        )
        for ordered in (True, False, False)
    ]
    submitted = [await asyncio.wait_for(sent.get(), 1) for _ in results]
    assert {item.data["playback_id"] for item in submitted} == {
        result["playback_id"] for result in results
    }
    # All three starts were accepted before any completion was delivered.
    for result in results:
        requests.finished({"playback_id": result["playback_id"], "state": "completed"})
    for result in results:
        assert (await join(requests, result))["state"] == "completed"


@pytest.mark.asyncio
async def test_cancel_timeout_keeps_request_tracked_and_ordered_queue_blocked(monkeypatch) -> None:
    manager, requests, sent = setup_requests()
    monkeypatch.setattr(playback_module, "CANCEL_ACK_TIMEOUT_S", 0.01)
    original_send = manager.client.send_command

    async def send(command: Command, timeout: float | None = None) -> Response:
        if command.command == CommandType.CANCEL_MACRO_PLAYBACK:
            # Also model a blocked write/drain before the client reply timeout.
            await asyncio.Event().wait()
        return await original_send(command, timeout=timeout)

    manager.client.send_command = AsyncMock(side_effect=send)
    writer = owner()
    first = requests.submit(
        {"command": "play_macro", "name": "a", "track": True, "ordered": True}, writer
    )
    second = requests.submit(
        {"command": "play_macro", "name": "b", "track": True, "ordered": True}, writer
    )
    await asyncio.wait_for(sent.get(), 1)
    result = await asyncio.wait_for(requests.cancel(str(first["playback_id"]), writer), 0.5)
    assert result["status"] == "error"
    assert "outcome unknown" in str(result["message"])
    assert requests.status(str(first["playback_id"]), writer)["state"] == "running"
    assert sent.empty()
    requests.finished({"playback_id": first["playback_id"], "state": "cancelled"})
    assert (await join(requests, first))["state"] == "cancelled"
    assert (await asyncio.wait_for(sent.get(), 1)).data["playback_id"] == second["playback_id"]
    requests.finished({"playback_id": second["playback_id"], "state": "completed"})
    await join(requests, second)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["disconnect", "shutdown"])
async def test_teardown_returns_when_daemon_does_not_acknowledge_cancel(
    monkeypatch, operation
) -> None:
    manager, requests, sent = setup_requests()
    monkeypatch.setattr(playback_module, "CANCEL_ACK_TIMEOUT_S", 0.1)
    cancellations = 0
    original_send = manager.client.send_command

    async def send(command: Command, timeout: float | None = None) -> Response:
        nonlocal cancellations
        if command.command == CommandType.CANCEL_MACRO_PLAYBACK:
            cancellations += 1
            # Also model a blocked write/drain before the client reply timeout.
            await asyncio.Event().wait()
        return await original_send(command, timeout=timeout)

    manager.client.send_command = AsyncMock(side_effect=send)
    writer = owner()
    for _ in range(8):
        requests.submit({"command": "play_macro", "name": "a", "track": True}, writer)
    for _ in range(8):
        await asyncio.wait_for(sent.get(), 1)
    if operation == "disconnect":
        await asyncio.wait_for(requests.disconnect(writer), 0.5)
        assert all(job.owner is None for job in requests.requests.values())
    else:
        await asyncio.wait_for(requests.shutdown(), 0.5)
        assert all(job.result["state"] == "failed" for job in requests.requests.values())
    assert cancellations == 8
    await requests.daemon_disconnected()


@pytest.mark.asyncio
async def test_terminal_retention_uses_completion_order_and_preserves_active_requests(
    monkeypatch,
) -> None:
    _, requests, sent = setup_requests()
    monkeypatch.setattr(playback_module, "MAX_FINISHED_REQUESTS", 2)
    writer = owner()
    results = [
        requests.submit({"command": "play_macro", "name": "a", "track": True}, writer)
        for _ in range(4)
    ]
    for _ in results:
        await asyncio.wait_for(sent.get(), 1)
    first, second, third, active = results
    for result in (second, third, first):
        requests.finished({"playback_id": result["playback_id"], "state": "completed"})
        await join(requests, result)
    assert requests.status(str(first["playback_id"]), writer)["state"] == "completed"
    assert requests.status(str(third["playback_id"]), writer)["state"] == "completed"
    assert requests.status(str(second["playback_id"]), writer)["status"] == "error"
    assert requests.status(str(active["playback_id"]), writer)["state"] == "running"
    await requests.daemon_disconnected()
