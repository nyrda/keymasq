from unittest.mock import AsyncMock

import pytest

from keymasq.common.ipc import Response
from keymasq.session.manager.command import hardware_masking
from keymasq.session.manager.core import SessionManager
from keymasq.session.manager.profile import coordinator


@pytest.mark.asyncio
@pytest.mark.parametrize("command", hardware_masking.COMMANDS)
async def test_slow_hardware_recovery_finishes_before_session_deadline(command, monkeypatch):
    manager = SessionManager()
    manager.connected = True
    state = {"masks": [{"id": "controller", "state": "restored"}]}

    async def slow_daemon_response(request, *, timeout):
        assert request.command == hardware_masking.COMMANDS[command]
        # Model the full 75-second in-flight job followed by a 75-second
        # recovery job without sleeping in the test.
        if timeout <= 150:
            raise TimeoutError("Session stopped waiting before hardware recovery completed")
        return Response(status="ok", data=state)

    monkeypatch.setattr(manager.client, "send_command", slow_daemon_response)
    reapply = AsyncMock()
    monkeypatch.setattr(coordinator, "reevaluate_profiles", reapply)
    result = await hardware_masking.handle_hardware_masking_commands(manager, command, {})
    assert result == {"status": "ok", **state}
    assert reapply.await_count == (1 if command == "resume_hardware" else 0)
