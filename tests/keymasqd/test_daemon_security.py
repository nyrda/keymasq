import tempfile
import uuid
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest

from keymasq.common.ipc import CommandType
from keymasq.common.security import SecurityPolicy
from keymasq.keymasqd import daemon as daemon_module


@pytest.mark.asyncio
async def test_refresh_and_lock_commands_require_client_context(daemon_testbed):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed

    with pytest.raises(PermissionError, match="missing client context"):
        await daemon._handle_command(CommandType.REFRESH_RECORDING_UNLOCK, {"uid": 10})

    with pytest.raises(PermissionError, match="missing client context"):
        await daemon._handle_command(CommandType.LOCK_RECORDING_UNLOCK, {"uid": 10})


def test_signal_handler_only_sets_shutdown_event(daemon_testbed):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    daemon.running = True

    daemon._signal_handler()

    assert daemon.running is True
    assert daemon._shutdown_event.is_set()


@pytest.mark.asyncio
async def test_unknown_command_raises_value_error(daemon_testbed):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed

    class FakeCommand(Enum):
        UNKNOWN = "unknown"

    with pytest.raises(ValueError, match="Unknown command"):
        await daemon._handle_command(FakeCommand.UNKNOWN, {})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("expected_allowed",),
    [
        (True,),
        (False,),
    ],
)
def test_validate_peer_behavior(daemon_testbed, expected_allowed: bool):
    daemon, *_rest = daemon_testbed
    daemon.security_policy = SecurityPolicy(
        daemon_allowed_uids=[1111],
        recording_unlock_required=True,
    )

    peer = SimpleNamespace(uid=1111 if expected_allowed else 2222, pid=1, gid=1)
    allowed, reason = daemon._validate_peer(peer)

    if expected_allowed:
        assert allowed is True
        assert reason == "peer uid allowed"
    else:
        assert allowed is False
        assert "not allowed" in reason


class _BadRunDir:
    def __init__(self, path: str) -> None:
        self.path = path

    def __fspath__(self) -> str:
        return self.path

    def __str__(self) -> str:
        return self.path

    def chmod(self, _mode: int) -> None:
        return None

    def stat(self):
        return SimpleNamespace(st_mode=0o40777)


def test_secure_run_dir_rejects_insecure_permissions(
    daemon_testbed,
    monkeypatch,
    tmp_path: Path,
):
    daemon, *_rest = daemon_testbed
    bad_dir = tmp_path / "bad-run"
    bad_dir.mkdir()

    monkeypatch.setattr(daemon_module.os, "chmod", lambda _path, _mode: None)
    monkeypatch.setattr(daemon_module, "RUN_DIR", _BadRunDir(str(bad_dir)))

    with pytest.raises(RuntimeError, match="Insecure run directory permissions"):
        daemon._secure_run_dir()


def test_cleanup_socket_path_unlinks_existing_path(daemon_testbed, monkeypatch, tmp_path: Path):
    daemon, *_rest = daemon_testbed
    socket_path = tmp_path / "daemon.sock"
    socket_path.write_text("not a socket")
    monkeypatch.setattr(daemon_module, "SOCKET_PATH", socket_path)

    daemon._cleanup_socket_path()

    assert socket_path.exists() is False


def test_sd_notify_sends_to_pathname_socket(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="kmq-", dir="/tmp") as temp_dir:
        notify_path = Path(temp_dir) / "notify.sock"
        with daemon_module.socket.socket(
            daemon_module.socket.AF_UNIX,
            daemon_module.socket.SOCK_DGRAM,
        ) as listener:
            listener.bind(str(notify_path))
            listener.settimeout(1.0)
            monkeypatch.setenv("NOTIFY_SOCKET", str(notify_path))

            daemon_module.sd_notify("READY=1")

            assert listener.recv(1024) == b"READY=1\n"


def test_sd_notify_sends_to_abstract_socket(monkeypatch):
    notify_name = f"keymasq-notify-{uuid.uuid4().hex}"
    with daemon_module.socket.socket(
        daemon_module.socket.AF_UNIX,
        daemon_module.socket.SOCK_DGRAM,
    ) as listener:
        listener.bind(f"\0{notify_name}")
        listener.settimeout(1.0)
        monkeypatch.setenv("NOTIFY_SOCKET", f"@{notify_name}")

        daemon_module.sd_notify("READY=1")

        assert listener.recv(1024) == b"READY=1\n"


@pytest.mark.parametrize("fail_at", ["connect", "sendall"])
def test_sd_notify_closes_socket_when_notify_fails(monkeypatch, fail_at: str):
    class FakeSocket:
        def __init__(self) -> None:
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_exc_info) -> None:
            self.closed = True

        def connect(self, _path: str) -> None:
            if fail_at == "connect":
                raise OSError("connect failed")

        def sendall(self, _payload: bytes) -> None:
            if fail_at == "sendall":
                raise OSError("send failed")

    fake_socket = FakeSocket()

    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/keymasq-notify.sock")
    monkeypatch.setattr(daemon_module.socket, "socket", lambda *_args: fake_socket)

    daemon_module.sd_notify("READY=1")

    assert fake_socket.closed is True
