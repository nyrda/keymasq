from types import SimpleNamespace

import pytest

from keymasq.common.security import PeerCredentials, SecurityPolicy
from keymasq.session.manager import commands as session_commands
from keymasq.session.manager.events import handle_mpris_trigger
from keymasq.session.mpris import MprisDBusError


class _FakeMprisController:
    def __init__(self, error: MprisDBusError | None = None) -> None:
        self.error = error
        self.commands: list[object] = []

    async def handle_command(self, command: object, *, raise_on_error: bool = False) -> bool:
        self.commands.append(command)
        if self.error is not None and raise_on_error:
            raise self.error
        return self.error is None

    def status_snapshot(self) -> dict[str, object]:
        return {"started": True, "players": []}


@pytest.mark.asyncio
async def test_handle_mpris_trigger_uses_session_controller() -> None:
    controller = _FakeMprisController()
    manager = SimpleNamespace(mpris_controller=controller)

    await handle_mpris_trigger(manager, {"command": "play_pause"})  # type: ignore[arg-type]

    assert controller.commands == ["play_pause"]


@pytest.mark.asyncio
async def test_session_mpris_command_uses_session_controller() -> None:
    controller = _FakeMprisController()
    manager = SimpleNamespace(
        security_policy=SecurityPolicy(session_command_acl={"client": []}),
        mpris_controller=controller,
    )

    result = await session_commands.handle_session_request(
        manager,  # type: ignore[arg-type]
        {"command": "mpris", "mpris_command": "play-pause"},
        "client",
        PeerCredentials(pid=1, uid=1000, gid=1000),
        None,  # type: ignore[arg-type]
    )

    assert result == {
        "status": "ok",
        "command": "play_pause",
        "mpris": {"started": True, "players": []},
    }
    assert controller.commands == ["play_pause"]


@pytest.mark.asyncio
async def test_session_mpris_command_reports_controller_failure() -> None:
    controller = _FakeMprisController(
        MprisDBusError("", "session D-Bus transport failed: no bus")
    )
    manager = SimpleNamespace(
        security_policy=SecurityPolicy(session_command_acl={"client": []}),
        mpris_controller=controller,
    )

    result = await session_commands.handle_session_request(
        manager,  # type: ignore[arg-type]
        {"command": "mpris", "mpris_command": "play"},
        "client",
        PeerCredentials(pid=1, uid=1000, gid=1000),
        None,  # type: ignore[arg-type]
    )

    assert result == {
        "status": "error",
        "command": "play",
        "message": "session D-Bus transport failed: no bus",
        "mpris": {"started": True, "players": []},
    }
    assert controller.commands == ["play"]


@pytest.mark.asyncio
async def test_session_mpris_status_uses_mpris_acl_path() -> None:
    controller = _FakeMprisController()
    manager = SimpleNamespace(
        security_policy=SecurityPolicy(session_command_acl={"client": []}),
        mpris_controller=controller,
    )

    result = await session_commands.handle_session_request(
        manager,  # type: ignore[arg-type]
        {"command": "mpris", "mpris_command": "status"},
        "client",
        PeerCredentials(pid=1, uid=1000, gid=1000),
        None,  # type: ignore[arg-type]
    )

    assert result == {
        "status": "ok",
        "command": "status",
        "mpris": {"started": True, "players": []},
    }
    assert controller.commands == []


@pytest.mark.asyncio
async def test_session_mpris_command_rejects_unknown_command() -> None:
    controller = _FakeMprisController()
    manager = SimpleNamespace(
        security_policy=SecurityPolicy(session_command_acl={"client": []}),
        mpris_controller=controller,
    )

    result = await session_commands.handle_session_request(
        manager,  # type: ignore[arg-type]
        {"command": "mpris", "mpris_command": "shuffle"},
        "client",
        PeerCredentials(pid=1, uid=1000, gid=1000),
        None,  # type: ignore[arg-type]
    )

    assert result == {"status": "error", "message": "unknown MPRIS command: shuffle"}
    assert controller.commands == []
