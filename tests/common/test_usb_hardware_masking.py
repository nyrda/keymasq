import asyncio
import ctypes
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from keymasq.masking import backend as backend_module
from keymasq.masking import usb as usb_module
from keymasq.masking.backend import DeviceInUseError, LinuxMaskBackend, save_json
from keymasq.masking.usb import check_hub_power
from tests.common.test_generic_hardware_masking import generic_usb
from tests.common.test_hardware_masking import write


@pytest.fixture(autouse=True)
def simulated_node_permissions(monkeypatch):
    # These sysfs fixtures use regular files and test driver/rule sequencing.
    # Permission recovery is exercised with real ACLs in the boundary tests.
    from keymasq.masking import permissions

    monkeypatch.setattr(permissions, "capture", AsyncMock())
    monkeypatch.setattr(permissions, "restore", AsyncMock())


@pytest.fixture
def usb_port(tmp_path, monkeypatch):
    inventory, attachment, hid, driver = generic_usb(tmp_path)
    port = attachment.syspath.parent / "3-0:1.0/usb3-port3"
    write(port / "disable", "0")
    (attachment.syspath / "port").symlink_to(port)
    backend = LinuxMaskBackend(inventory, tmp_path / "run", tmp_path / "rules", tmp_path / "state")
    backend.prepare_directories()
    monkeypatch.setattr(usb_module, "check_hub_power", lambda *_: None)
    monkeypatch.setattr(usb_module, "run_host", AsyncMock(return_value=""))
    snapshot = {
        "id": attachment.identity,
        "generation": attachment.generation,
        "nodes": {},
        "bindings": {hid.name: driver.name},
        "mode": "",
        "selector": backend.inventory.selector(attachment),
    }
    return backend, attachment, port, snapshot


def test_usb_rules_match_reconnect_but_keep_serial_scope(tmp_path):
    inventory, attachment, _hid, _driver = generic_usb(tmp_path)
    backend = LinuxMaskBackend(inventory)
    before = backend.rule_matches(attachment)
    (attachment.syspath / "devnum").write_text("8")
    assert backend.rule_matches(inventory.scan()[0]) == before
    (attachment.syspath / "serial").write_text("different-controller")
    assert backend.rule_matches(inventory.scan()[0]) != before
    assert (
        backend.inventory.from_selector(backend.inventory.selector(attachment)).serial
        == attachment.serial
    )


def test_usb_without_serial_tests_absence_on_usb_parent_for_every_endpoint(tmp_path):
    inventory, attachment, _hid, _driver = generic_usb(tmp_path)
    (attachment.syspath / "serial").unlink()
    attachment = inventory.scan()[0]
    assert attachment.serial == ""
    backend = LinuxMaskBackend(inventory)
    matches = backend.rule_matches(attachment)
    absence = f"TEST!={backend.rule_literal(str(attachment.syspath / 'serial'))}"
    assert len(matches) == 3
    assert all(absence in match for match in matches)
    assert all("ATTR{serial}" not in match and "ATTRS{serial}" not in match for match in matches)
    (attachment.syspath / "serial").write_text("replacement")
    replacement = backend.rule_matches(inventory.scan()[0])
    assert all(absence not in match for match in replacement)


@pytest.mark.asyncio
async def test_usb_reconnect_journals_port_before_disconnect_and_tracks_new_generation(
    usb_port, monkeypatch
):
    backend, attachment, port, snapshot = usb_port
    original = Path.write_text
    writes = []

    def write_port(path, text, *args, **kwargs):
        if path == port / "disable":
            writes.append(text.strip())
            record = json.loads(backend.journal.read_text())
            assert record["usb_port"]["disabled"]
            assert record["usb_port"]["path"] == str(port)
            if text.strip() == "0":
                original(attachment.syspath / "devnum", "8")
        return original(path, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_port)
    current = await usb_module.reconnect(backend, attachment, snapshot)
    assert writes == ["1", "0"]
    assert current.identity == attachment.identity
    assert current.generation != attachment.generation
    record = json.loads(backend.journal.read_text())
    assert record["generation"] == current.generation
    assert not record["usb_port"]["disabled"]
    assert isinstance(usb_module.run_host, AsyncMock)
    usb_module.run_host.assert_awaited_once_with("udevadm", "settle", "--timeout=8", timeout=10.0)


@pytest.mark.asyncio
async def test_usb_reconnect_timeout_leaves_port_enabled(usb_port, monkeypatch):
    backend, attachment, port, snapshot = usb_port
    monkeypatch.setattr(usb_module, "RECONNECT_TIMEOUT_S", 0)
    with pytest.raises(OSError, match="did not return"):
        await usb_module.reconnect(backend, attachment, snapshot)
    assert (port / "disable").read_text().strip() == "0"


@pytest.mark.asyncio
async def test_cancel_during_port_disable_waits_for_write_and_reenables(usb_port, monkeypatch):
    backend, attachment, port, snapshot = usb_port
    entered, finish = threading.Event(), threading.Event()
    original = Path.write_text

    def write_port(path, text, *args, **kwargs):
        result = original(path, text, *args, **kwargs)
        if path == port / "disable" and text.strip() == "1":
            entered.set()
            assert finish.wait(5)
        return result

    monkeypatch.setattr(Path, "write_text", write_port)
    task = asyncio.create_task(usb_module.reconnect(backend, attachment, snapshot))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (port / "disable").read_text().strip() == "0"
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_crash_recovery_reenables_port_before_removing_policy(usb_port, monkeypatch):
    backend, attachment, port, snapshot = usb_port
    snapshot["usb_port"] = usb_module.port_record(backend, attachment)
    snapshot["bindings"] = {}
    save_json(backend.journal, snapshot)
    (port / "disable").write_text("1")
    backend.early.write_text("deny")
    backend.late.write_text("deny")

    async def host(*args, **_kwargs):
        assert (port / "disable").read_text().strip() == "0"
        return ""

    monkeypatch.setattr(backend_module, "run_host", host)
    await backend.recover()
    assert not backend.journal.exists()
    assert not backend.armed


def test_port_recovery_does_not_enable_a_replacement_hub(usb_port):
    backend, attachment, port, snapshot = usb_port
    snapshot["usb_port"] = usb_module.port_record(backend, attachment)
    port.rename(port.with_name("old-port"))
    write(port / "disable", "1")
    usb_module.enable_recorded_port(backend, snapshot)
    assert (port / "disable").read_text() == "1"


def test_port_validation_rejects_parent_hubs_and_wrong_ports(usb_port):
    backend, attachment, port, _snapshot = usb_port
    (attachment.syspath / "port").unlink()
    (attachment.syspath / "port").symlink_to(port.parent)
    with pytest.raises(ValueError, match="individual USB port"):
        usb_module.port_record(backend, attachment)
    (attachment.syspath / "port").unlink()
    (attachment.syspath / "port").symlink_to(port)
    write(attachment.syspath / "bDeviceClass", "09")
    with pytest.raises(ValueError, match="disconnected or changed"):
        usb_module.port_record(backend, attachment)


@pytest.mark.parametrize("switching", [0, 1, 2])
def test_hub_power_descriptor_limits_reconnect_to_independent_ports(
    usb_port, monkeypatch, switching
):
    backend, attachment, _port, _snapshot = usb_port
    hub = attachment.syspath.parent
    write(hub / "busnum", "3")
    write(hub / "devnum", "1")
    write(hub / "version", "2.00")
    write(backend.inventory.dev_root / "bus/usb/003/001", "")

    def ioctl(_fd, command, data):
        request = usb_module._ControlTransfer.from_buffer(data)
        assert command == 0xC0005500 | (ctypes.sizeof(request) << 16)
        assert request.request_type == 0xA0 and request.value == 0x2900
        ctypes.memmove(request.data, bytes([9, 0x29, 4, switching, 0, 0, 0, 0, 0]), 9)
        return 9

    monkeypatch.setattr(usb_module.fcntl, "ioctl", ioctl)
    if switching == 0:
        with pytest.raises(ValueError, match="multiple ports"):
            check_hub_power(backend, hub)
    else:
        check_hub_power(backend, hub)


@pytest.mark.asyncio
async def test_refresh_keeps_receiver_drivers_nodes_and_recovery_record(usb_port, monkeypatch):
    backend, attachment, port, snapshot = usb_port
    save_json(backend.journal, snapshot)
    backend.early.write_text("armed")
    backend.late.write_text("armed")
    before = backend.journal.read_bytes()
    nodes = {str(p): p.stat().st_ino for p in backend.inventory.nodes(attachment)}
    monkeypatch.setattr(backend_module, "run_host", AsyncMock(return_value=""))
    monkeypatch.setattr(backend, "verify_access", AsyncMock())
    monkeypatch.setattr(backend, "reject_unrevoked_handles", lambda *_: None)
    await backend.refresh_interfaces(attachment)
    assert backend.journal.read_bytes() == before
    assert backend.early.read_text() == backend.late.read_text() == "armed"
    assert (port / "disable").read_text() == "0"
    assert nodes == {str(p): p.stat().st_ino for p in backend.inventory.nodes(attachment)}


@pytest.mark.asyncio
async def test_direct_usb_takeover_installs_rules_before_reconnect(usb_port, monkeypatch):
    backend, attachment, _port, snapshot = usb_port
    monkeypatch.setattr(backend, "snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(backend, "verify_access", AsyncMock())
    monkeypatch.setattr(backend_module, "run_host", AsyncMock(return_value=""))
    reject = iter([DeviceInUseError("123", "wine"), None])

    def handles(_attachment):
        failure = next(reject)
        if failure:
            raise failure

    async def reconnect(_backend, old, _snapshot):
        assert backend.early.exists() and backend.late.exists()
        assert "devnum" not in backend.early.read_text()
        for rules in (backend.early, backend.late):
            assert all(
                line.startswith('ACTION!="remove",') for line in rules.read_text().splitlines()
            )
        (old.syspath / "devnum").write_text("8")
        return backend.inventory.scan()[0]

    monkeypatch.setattr(backend, "reject_unrevoked_handles", handles)
    monkeypatch.setattr(usb_module, "reconnect", reconnect)
    assert await backend.activate(attachment) == []
    assert backend.active_attachment.generation != attachment.generation


@pytest.mark.asyncio
async def test_disconnect_keeps_rules_and_unmask_restores_a_new_generation(usb_port, monkeypatch):
    backend, attachment, _port, snapshot = usb_port
    snapshot["bindings"] = {}
    save_json(backend.journal, snapshot)
    monkeypatch.setattr(backend_module, "run_host", AsyncMock(return_value=""))
    await backend.install_rules(attachment)
    (attachment.syspath / "devnum").write_text("8")
    before = backend.early.read_text()
    await backend.recover(keep_rules=True)
    assert backend.early.read_text() == before
    assert not backend.journal.exists()
    trigger = AsyncMock()
    monkeypatch.setattr(backend, "trigger", trigger)
    await backend.recover()
    assert not backend.armed
    assert trigger.await_args.args[0].generation != attachment.generation
    assert trigger.await_args.args[1] == "add"
