import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from keymasq.masking import client, operations
from keymasq.masking.backend import DeviceInUseError, LinuxMaskBackend
from tests.common.test_generic_hardware_masking import generic_usb

TOKEN = "a" * 32


@pytest.mark.asyncio
async def test_service_cleanup_stops_jobs_before_restoring_permissions(monkeypatch):
    calls = []

    async def stop(*args, **kwargs):
        assert args == ("systemctl", "stop", "keymasq-hardware@*.service")
        calls.append("stopped")

    async def restore():
        assert calls == ["stopped"]
        calls.append("restored")

    monkeypatch.setattr(operations, "run_host", stop)
    monkeypatch.setattr(operations, "recover_all", restore)
    await operations.recover_hardware()
    assert calls == ["stopped", "restored"]


@pytest.mark.asyncio
async def test_privileged_job_preserves_the_blocking_application(tmp_path, monkeypatch):
    directory = tmp_path / "requests"
    monkeypatch.setattr(client, "REQUESTS", directory)
    monkeypatch.setattr(operations, "REQUESTS", directory)
    monkeypatch.setattr(operations.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=os.getuid()))
    root = LinuxMaskBackend(runtime_dir=tmp_path / "run", state_dir=tmp_path / "state")

    async def execute(*_):
        raise DeviceInUseError("123", "Wine")

    async def systemctl(*args, **kwargs):
        await operations.run_request(next(directory.iterdir()).name, root)
        return ""

    monkeypatch.setattr(operations, "execute", execute)
    monkeypatch.setattr(client, "run_host", systemctl)
    with pytest.raises(DeviceInUseError) as error:
        await client.request("activate", "b" * 24)
    assert error.value.application == "Wine"
    assert error.value.pid == "123"
    assert not list(directory.iterdir())


def test_recording_polkit_authorization_cannot_invoke_hardware_operations(monkeypatch):
    monkeypatch.setattr(operations.os, "geteuid", lambda: 0)
    monkeypatch.setenv("PKEXEC_UID", "1000")
    with pytest.raises(PermissionError, match="Recording authorization"):
        operations.main("recover-hardware")


@pytest.fixture
def request_file(tmp_path, monkeypatch):
    directory = tmp_path / "requests"
    directory.mkdir(mode=0o700)
    path = directory / TOKEN
    path.write_text('{"operation":"recover","id":"' + "b" * 24 + '"}')
    path.chmod(0o600)
    monkeypatch.setattr(operations, "REQUESTS", directory)
    monkeypatch.setattr(operations.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=os.getuid()))
    return path


@pytest.mark.parametrize(
    "attack", ["symlink", "hardlink", "writable", "directory", "large", "owner"]
)
def test_privileged_request_rejects_unsafe_inode(request_file, monkeypatch, attack):
    if attack == "symlink":
        target = request_file.with_name("target")
        request_file.rename(target)
        request_file.symlink_to(target)
    elif attack == "hardlink":
        os.link(request_file, request_file.with_name("alias"))
    elif attack == "writable":
        request_file.chmod(0o666)
    elif attack == "directory":
        request_file.parent.chmod(0o777)
    elif attack == "owner":
        monkeypatch.setattr(
            operations.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=os.getuid() + 1)
        )
    else:
        request_file.write_bytes(b"x" * (operations.MAX_REQUEST + 1))
    with pytest.raises(OSError):
        operations.open_request(TOKEN)


@pytest.mark.parametrize("token", ["../escape", "a" * 33, "A" * 32, TOKEN + "\n"])
def test_privileged_request_identity_is_not_a_path(token):
    with pytest.raises(ValueError):
        operations.open_request(token)


@pytest.mark.asyncio
async def test_response_cannot_follow_a_replaced_request_path(request_file, tmp_path, monkeypatch):
    root = LinuxMaskBackend(
        runtime_dir=tmp_path / "run", state_dir=tmp_path / "state", rules_dir=tmp_path / "rules"
    )
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("unchanged")
    original = request_file.with_name("original")

    async def execute(*_):
        request_file.rename(original)
        request_file.symlink_to(unrelated)
        return {"finished": True}

    monkeypatch.setattr(operations, "execute", execute)
    await operations.run_request(TOKEN, root)
    assert unrelated.read_text() == "unchanged"
    assert json.loads(original.read_text()) == {"status": "ok", "finished": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        {"operation": "write", "id": "a" * 24, "path": "/etc/passwd"},
        {"operation": "activate", "id": "../../etc"},
        {"operation": "activate", "id": "a" * 24, "generation": "unknown", "path": "/etc/passwd"},
    ],
)
async def test_helper_only_operates_on_root_discovered_attachments(tmp_path, message):
    inventory, _, _, _ = generic_usb(tmp_path)
    root = LinuxMaskBackend(inventory, tmp_path / "run", tmp_path / "rules", tmp_path / "state")
    root.prepare_directories()
    with pytest.raises(ValueError):
        await operations.execute(message, root)
    assert not list((tmp_path / "rules").glob("*.rules"))


@pytest.mark.asyncio
async def test_cancellation_waits_for_the_systemd_job_before_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "REQUESTS", tmp_path / "requests")
    started, finish = asyncio.Event(), asyncio.Event()

    async def systemctl(*args, **kwargs):
        assert args[:3] == ("systemctl", "--no-ask-password", "start")
        assert "--job-mode=fail" in args
        started.set()
        await finish.wait()
        path = next((tmp_path / "requests").iterdir())
        path.write_text('{"status":"ok"}')
        return ""

    monkeypatch.setattr(client, "run_host", systemctl)
    task = asyncio.create_task(client.request("activate", "b" * 24, generation="7:8"))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert list((tmp_path / "requests").iterdir())
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list((tmp_path / "requests").iterdir()) == []


@pytest.mark.asyncio
async def test_global_recovery_cannot_race_an_active_operation(tmp_path, monkeypatch):
    inventory, attachment, _, _ = generic_usb(tmp_path)
    root = LinuxMaskBackend(inventory, tmp_path / "run", tmp_path / "rules", tmp_path / "state")
    root.prepare_directories()
    started, finish = asyncio.Event(), asyncio.Event()

    async def activate(self, item):
        started.set()
        await finish.wait()
        self.active_attachment = item
        return []

    monkeypatch.setattr(LinuxMaskBackend, "activate", activate)
    task = asyncio.create_task(
        operations.execute(
            {
                "operation": "activate",
                "id": attachment.identity,
                "generation": attachment.generation,
            },
            root,
        )
    )
    await started.wait()
    with pytest.raises(BlockingIOError):
        await operations.recover_all(root)
    finish.set()
    await task
    monkeypatch.setattr(LinuxMaskBackend, "recover", AsyncMock())
    await operations.recover_all(root)


@pytest.mark.asyncio
async def test_listing_hardware_does_not_start_privileged_jobs(tmp_path, monkeypatch):
    from keymasq.common.ipc import CommandType
    from keymasq.keymasqd.hardware_masking import HardwareMasking
    from keymasq.masking.coordinator import MaskCoordinator

    backend = client.SystemdMaskBackend()
    backend.state_dir = tmp_path / "policy"
    monkeypatch.setattr(
        backend, "prepare_directories", lambda: backend.state_dir.mkdir(exist_ok=True)
    )
    monkeypatch.setattr(backend, "reservation_ids", lambda: [])
    monkeypatch.setattr(backend.inventory, "scan", lambda: [])
    operation = AsyncMock()
    monkeypatch.setattr(client, "request", operation)
    masking = HardwareMasking(SimpleNamespace(masking_suspended=False))
    masking.coordinator = MaskCoordinator(backend)
    monkeypatch.setattr(masking, "start_monitor", Mock())
    result = await masking.handle(CommandType.HARDWARE_INVENTORY, {}, uid=os.getuid())
    assert result["available"] and result["devices"] == []
    operation.assert_not_awaited()


@pytest.mark.asyncio
async def test_systemd_backend_keeps_existing_policy_and_transaction_paths(tmp_path, monkeypatch):
    identity = "b" * 24
    run, policy, legacy = (tmp_path / name for name in ("run", "policy", "legacy"))
    monkeypatch.setattr(client, "RUNTIME_DIR", run)
    monkeypatch.setattr(client, "POLICY_DIR", policy)
    monkeypatch.setattr(client, "STATE_DIR", legacy)
    backend = client.SystemdMaskBackend()
    scoped = backend.for_attachment(identity)
    assert scoped.journal == run / "reservations" / identity / "journal.json"
    assert scoped.permissions == run / "reservations" / identity / "permissions.json"
    assert scoped.armed_record == run / "reservations" / identity / "armed.json"
    assert scoped.early.name == f"72-keymasq-masking-{identity}.rules"
    assert scoped.late.name == f"99-zz-keymasq-masking-{identity}.rules"
    assert scoped.inventory is backend.inventory
    for invalid in ("../escape", "A" * 24, "b" * 23):
        with pytest.raises(ValueError, match="Invalid attachment identity"):
            backend.for_attachment(invalid)

    def prepare_legacy():
        directory = legacy / "reservations" / identity
        directory.mkdir(parents=True)
        (directory / "policy.json").write_bytes(b'{"persist":true,"owner_uid":1000}\n')
        (directory / "selection.json").write_bytes(b'{"state":"restored"}\n')

    await asyncio.to_thread(prepare_legacy)
    assert await asyncio.to_thread(backend.reservation_ids) == [identity]
    await asyncio.to_thread(scoped.prepare_directories)
    for name in ("policy.json", "selection.json"):
        assert await asyncio.to_thread(
            (scoped.state_dir / name).read_bytes
        ) == await asyncio.to_thread((legacy / "reservations" / identity / name).read_bytes)
    await asyncio.to_thread((scoped.state_dir / "policy.json").write_bytes, b'{"persist":false}\n')
    await asyncio.to_thread(scoped.prepare_directories)
    assert (
        await asyncio.to_thread((scoped.state_dir / "policy.json").read_bytes)
        == b'{"persist":false}\n'
    )


@pytest.mark.asyncio
async def test_daemon_watchdog_runs_on_the_input_event_loop(monkeypatch):
    from keymasq.keymasqd import daemon as module

    daemon = module.Daemon()
    daemon.running = True
    monkeypatch.setenv("WATCHDOG_USEC", "30000")
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    messages = []

    def notify(message):
        messages.append(message)
        daemon.running = False

    monkeypatch.setattr(module, "sd_notify", notify)
    await asyncio.wait_for(daemon._watchdog(), 1)
    assert messages == ["WATCHDOG=1"]


@pytest.mark.asyncio
async def test_watchdog_does_not_hide_a_stalled_mask_coordinator(monkeypatch):
    from keymasq.keymasqd import daemon as module

    daemon = module.Daemon()
    daemon.running = True
    daemon.hardware_masking.task = Mock(done=lambda: False)
    daemon.hardware_masking.last_progress = 0
    monkeypatch.setenv("WATCHDOG_USEC", "30000")
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    notify = Mock()
    monkeypatch.setattr(module, "sd_notify", notify)
    task = asyncio.create_task(daemon._watchdog())
    await asyncio.sleep(0.04)
    daemon.running = False
    await task
    notify.assert_not_called()
