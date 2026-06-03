from unittest.mock import AsyncMock

import pytest

import keymasq.session.manager.recording as session_recording_module
from keymasq.common.security import PeerCredentials
from keymasq.session.manager import SessionManager


def grant_recording_refresh_owner(
    manager: SessionManager,
    peer: PeerCredentials,
    writer: object,
    monkeypatch: pytest.MonkeyPatch,
    *,
    lease_id: str = "lease-test",
) -> AsyncMock:
    manager.unlock_state.refresh_owner = {
        "uid": peer.uid,
        "pid": peer.pid,
        "writer_id": id(writer),
        "lease_id": lease_id,
        "source": "runtime",
    }
    resolve_unlock_status_async = AsyncMock(
        return_value={"unlocked": True, "source": "runtime", "expires_at": 9999999999}
    )
    monkeypatch.setattr(
        session_recording_module,
        "resolve_unlock_status_async",
        resolve_unlock_status_async,
    )
    return resolve_unlock_status_async
