"""Connection-owned playback requests and the FIFO text playback queue."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from keymasq.common.ipc import Command, CommandType
from keymasq.common.types import JsonObject

if TYPE_CHECKING:
    from .core import SessionManager

log = logging.getLogger("keymasq-session.playback")

TERMINAL_STATES = {"completed", "cancelled", "failed"}
MAX_ACTIVE_REQUESTS = 128
MAX_FINISHED_REQUESTS = 256


@dataclass
class PlaybackRequest:
    playback_id: str
    owner: asyncio.StreamWriter | None
    cancel_on_disconnect: bool
    notify: bool
    result: JsonObject
    completion: asyncio.Future[JsonObject]
    task: asyncio.Task[None] | None = None
    submitted: bool = False
    cancel_requested: bool = False


class PlaybackRequests:
    def __init__(self, manager: SessionManager) -> None:
        self.manager = manager
        self.requests: dict[str, PlaybackRequest] = {}
        self.text_lock = asyncio.Lock()

    def submit(self, request: JsonObject, writer: asyncio.StreamWriter) -> JsonObject:
        active = sum(job.result["state"] not in TERMINAL_STATES for job in self.requests.values())
        if active >= MAX_ACTIVE_REQUESTS:
            return {"status": "error", "message": "Playback queue is full"}
        playback_id = uuid4().hex
        tracked = request.get("track") is True
        job = PlaybackRequest(
            playback_id,
            writer,
            request.get("cancel_on_disconnect", tracked) is True,
            tracked,
            {"status": "ok", "playback_id": playback_id, "state": "queued"},
            asyncio.get_running_loop().create_future(),
        )
        self.requests[playback_id] = job
        job.task = asyncio.create_task(self._run(job, dict(request)))
        return dict(job.result)

    def status(self, playback_id: str, writer: asyncio.StreamWriter) -> JsonObject:
        job = self.requests.get(playback_id)
        if job is None or job.owner is not writer:
            return {"status": "error", "message": "Unknown playback_id for this connection"}
        return dict(job.result)

    async def cancel(self, playback_id: str, writer: asyncio.StreamWriter) -> JsonObject:
        result = self.status(playback_id, writer)
        if result.get("status") != "ok":
            return result
        job = self.requests[playback_id]
        if job.result["state"] not in TERMINAL_STATES:
            job.cancel_requested = True
            if not job.submitted:
                if job.task is not None:
                    job.task.cancel()
                self._finish(job, {"state": "cancelled"})
            elif job.result["state"] == "running":
                await self._cancel_daemon(job)
            # A request being submitted is cancelled by _play once acceptance arrives.
        return dict(job.result)

    async def _cancel_daemon(self, job: PlaybackRequest) -> None:
        response = await self.manager.client.send_command(
            Command(
                command=CommandType.CANCEL_MACRO_PLAYBACK,
                data={"playback_id": job.playback_id},
            ),
            timeout=None,
        )
        if response.status != "ok":
            raise RuntimeError(response.error or "Cancellation failed")

    async def _run(self, job: PlaybackRequest, request: JsonObject) -> None:
        try:
            if request.get("command") == "type_text" or request.get("ordered") is True:
                async with self.text_lock:
                    await self._play(job, request)
            else:
                await self._play(job, request)
        except asyncio.CancelledError:
            self._finish(job, {"state": "cancelled"})
            raise
        except Exception as exc:
            log.exception("Playback request failed")
            self._finish(job, {"state": "failed", "message": str(exc)})

    async def _play(self, job: PlaybackRequest, request: JsonObject) -> None:
        from .command.macro import handle_macro_commands

        request["playback_id"] = job.playback_id
        # Mark submission before awaiting compilation/IPC so disconnect cannot lose
        # a daemon request whose acknowledgement has not arrived yet.
        job.submitted = True
        result = await handle_macro_commands(self.manager, str(request["command"]), request)
        if result is None or result.get("status") != "ok":
            self._finish(
                job,
                {
                    "state": "cancelled" if job.cancel_requested else "failed",
                    "message": (result or {}).get("message", "Playback failed"),
                },
            )
            return
        if job.result["state"] in TERMINAL_STATES:
            return
        job.result.update(result)
        job.result["state"] = "running"
        if job.cancel_requested:
            await self._cancel_daemon(job)
        terminal = await job.completion
        self._finish(job, terminal)

    def finished(self, data: JsonObject) -> None:
        job = self.requests.get(str(data.get("playback_id", "")))
        if job is not None and not job.completion.done():
            job.completion.set_result(data)

    def _finish(self, job: PlaybackRequest, result: JsonObject) -> None:
        if job.result["state"] in TERMINAL_STATES:
            return
        job.result.update(result)
        job.result["status"] = "ok" if job.result["state"] == "completed" else "error"
        if job.result["state"] == "cancelled":
            job.result["message"] = "Playback cancelled"
        if job.notify and job.owner is not None:
            self.manager.broadcast_to_session_client_ids(
                {**job.result, "event": "macro_playback_finished"},
                {id(job.owner)},
            )
        if job.owner is None:
            self.requests.pop(job.playback_id, None)
        finished = [
            key for key, value in self.requests.items() if value.result["state"] in TERMINAL_STATES
        ]
        for key in finished[:-MAX_FINISHED_REQUESTS]:
            self.requests.pop(key, None)

    async def disconnect(self, writer: asyncio.StreamWriter) -> None:
        for job in list(self.requests.values()):
            if job.owner is not writer:
                continue
            if job.cancel_on_disconnect:
                try:
                    await self.cancel(job.playback_id, writer)
                except (OSError, RuntimeError):
                    pass
            job.owner = None
            if job.result["state"] in TERMINAL_STATES:
                self.requests.pop(job.playback_id, None)

    async def daemon_disconnected(self) -> None:
        tasks: list[asyncio.Task[None]] = []
        for job in list(self.requests.values()):
            self._finish(
                job, {"state": "failed", "message": "Daemon disconnected; playback outcome unknown"}
            )
            if job.task is not None and not job.task.done():
                job.task.cancel()
                tasks.append(job.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def cancel_pending(self) -> None:
        """Stop queued work when the global macro stop or runtime reset fires."""
        for job in list(self.requests.values()):
            if job.result["state"] in TERMINAL_STATES:
                continue
            job.cancel_requested = True
            if not job.submitted:
                if job.task is not None:
                    job.task.cancel()
                self._finish(job, {"state": "cancelled"})

    async def shutdown(self) -> None:
        self.cancel_pending()
        for job in list(self.requests.values()):
            if job.submitted and job.result["state"] not in TERMINAL_STATES:
                try:
                    await self._cancel_daemon(job)
                except (OSError, RuntimeError):
                    pass
        await self.daemon_disconnected()
