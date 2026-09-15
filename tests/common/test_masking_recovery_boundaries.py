"""Failure injection at the helper's identity and hardware recovery boundaries."""

import asyncio
import json
import os
import shutil
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from keymasq.common.ipc import CommandType
from keymasq.keymasqd.hardware_masking import HardwareMasking
from keymasq.masking import backend as backend_module
from keymasq.masking import permissions, usb
from keymasq.masking.backend import LinuxMaskBackend, run_host, save_json
from keymasq.masking.inventory import HardwareInventory
from tests.common.test_hardware_masking import deck_sysfs, write


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [ValueError("repair failed"), OSError("disconnected"), asyncio.CancelledError()]
)
async def test_failed_owner_cleanup_cannot_reuse_old_helper_identity(monkeypatch, failure):
    masking = HardwareMasking(SimpleNamespace(masking_suspended=False))
    masking.initialized = True
    masking.session_uid = masking.coordinator.session_uid = 1000
    masking.needs_startup = False
    monkeypatch.setattr(masking, "release_runtime", AsyncMock())
    monkeypatch.setattr(masking, "request", AsyncMock(side_effect=failure))
    with pytest.raises(type(failure)):
        await masking.close()
    assert masking.session_uid is masking.coordinator.session_uid is None
    request = AsyncMock(return_value={})
    monkeypatch.setattr(masking, "request", request)
    monkeypatch.setattr(masking, "start_monitor", Mock())
    await masking.handle(CommandType.HARDWARE_INVENTORY, {}, uid=1001)
    assert [call.args for call in request.await_args_list] == [
        ("startup", {"uid": 1001}),
        ("inventory", {}),
    ]


@pytest.mark.asyncio
async def test_startup_checks_helper_identity_even_if_startup_flag_is_clear(monkeypatch):
    masking = HardwareMasking(SimpleNamespace(masking_suspended=False))
    masking.session_uid, masking.coordinator.session_uid, masking.needs_startup = 1001, 1000, False
    request = AsyncMock(return_value={})
    monkeypatch.setattr(masking, "request", request)
    await masking.startup()
    request.assert_awaited_once_with("startup", {"uid": 1001})


def test_composite_hub_is_rejected_and_discovery_cannot_cross_usb_devices(tmp_path):
    inventory = HardwareInventory(tmp_path / "sys", tmp_path / "dev")
    hub = inventory.sys_root / "devices/pci/usb3/3-1"
    child = hub / "3-1.2"
    for device, vendor, product in [(hub, "1234", "5678"), (child, "abcd", "9876")]:
        write(device / "idVendor", vendor)
        write(device / "idProduct", product)
        write(device / "devnum", "4")
        write(device / f"{device.name}:1.0/bInterfaceClass", "03")
        link = inventory.sys_root / "bus/usb/devices" / device.name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(device)
    write(hub / "3-1:1.1/bInterfaceClass", "09")
    hid = child / "3-1.2:1.0/0003:ABCD:9876.0001"
    driver = inventory.sys_root / "bus/hid/drivers/hid-generic"
    driver.mkdir(parents=True)
    hid.mkdir(parents=True)
    (hid / "driver").symlink_to(driver)
    link = inventory.sys_root / "bus/hid/devices" / hid.name
    link.parent.mkdir(parents=True)
    link.symlink_to(hid)
    raw = inventory.sys_root / "class/hidraw/hidraw3"
    raw.mkdir(parents=True)
    (raw / "device").symlink_to(hid)
    write(inventory.dev_root / "hidraw3", "")
    attachments = inventory.scan()
    assert [item.kernel_name for item in attachments] == ["3-1.2"]
    parent = replace(attachments[0], syspath=hub, kernel_name="3-1")
    backend = LinuxMaskBackend(inventory)
    with pytest.raises(ValueError, match="USB hubs"):
        backend.rule_matches(parent)
    assert inventory.nodes(parent) == []
    with pytest.raises(ValueError, match="No bound input interfaces"):
        inventory.bindings(parent)


@pytest.mark.asyncio
@pytest.mark.parametrize("module_initially_present", [True, False])
async def test_detached_deck_records_mode_before_mutation_and_rolls_back(
    tmp_path, monkeypatch, module_initially_present
):
    inventory, _ = deck_sysfs(tmp_path)
    bound = inventory.scan()[0]
    detached = replace(bound, main_hid="")
    backend = LinuxMaskBackend(inventory, tmp_path / "run", tmp_path / "rules", tmp_path / "state")
    backend.prepare_directories()
    if module_initially_present:
        write(backend.mode_path, "Y\n")
    monkeypatch.setattr(inventory, "nodes", lambda _: [])
    monkeypatch.setattr(
        inventory,
        "bindings",
        Mock(side_effect=ValueError("No bound input interfaces are available to reconnect")),
    )
    monkeypatch.setattr(backend, "install_rules", AsyncMock())
    monkeypatch.setattr(backend, "trigger", AsyncMock())
    monkeypatch.setattr(backend, "reject_unrevoked_handles", lambda _: None)
    monkeypatch.setattr(backend, "remove_rules", AsyncMock())
    monkeypatch.setattr(
        backend, "verify_access", AsyncMock(side_effect=OSError("activation fails"))
    )

    async def reconnect(_backend, _attachment, _snapshot):
        write(backend.mode_path, "Y\n")
        return bound

    monkeypatch.setattr(usb, "reconnect", reconnect)
    with pytest.raises(OSError, match="activation fails"):
        await backend.activate(detached)
    assert json.loads(backend.journal.read_text())["mode"] == "Y"
    assert backend.mode_path.read_text().strip() == "N"
    await backend.recover()
    assert backend.mode_path.read_text().strip() == "Y"


@pytest.mark.asyncio
async def test_detached_deck_still_rejects_other_steam_controllers(tmp_path):
    inventory, _ = deck_sysfs(tmp_path)
    detached = replace(inventory.scan()[0], main_hid="")
    backend = LinuxMaskBackend(inventory, tmp_path / "run", tmp_path / "rules", tmp_path / "state")
    backend.prepare_directories()
    other = inventory.sys_root / "devices/other/0003:28DE:1142.0002"
    other.mkdir(parents=True)
    (inventory.sys_root / "bus/hid/drivers/hid-steam" / other.name).symlink_to(other)
    with pytest.raises(ValueError, match="Disconnect other"):
        await backend.snapshot(detached)
    assert not backend.journal.exists()


@pytest.fixture
async def permission_backend(tmp_path, monkeypatch):
    if any(shutil.which(tool) is None for tool in ("setfacl", "getfacl")):
        pytest.skip("POSIX ACL tools are not installed")
    probe = tmp_path / "acl-probe"
    probe.touch()
    try:
        await run_host("setfacl", "-m", "u:32109:r--", str(probe))
        acl = await run_host("getfacl", "-cn", str(probe))
        if "user:32109:r--" not in acl:
            pytest.skip("The temporary filesystem does not preserve extended ACLs")
    except OSError as exc:
        pytest.skip(f"POSIX ACL operations are unavailable: {exc}")
    finally:
        probe.unlink()
    inventory, _ = deck_sysfs(tmp_path)
    attachment = inventory.scan()[0]
    node = inventory.dev_root / "hidraw3"
    node.chmod(0o640)
    backend = LinuxMaskBackend(inventory, tmp_path / "run", tmp_path / "rules", tmp_path / "state")
    backend.prepare_directories()
    monkeypatch.setattr(inventory, "nodes", lambda _: [node])
    # Use real ACLs and ownership on a temporary file, without touching /dev.
    monkeypatch.setattr(permissions.stat, "S_ISCHR", lambda _: True)
    monkeypatch.setattr(backend_module, "run_host", AsyncMock(return_value=""))
    return backend, attachment, node


@pytest.mark.asyncio
@pytest.mark.parametrize("prearmed,no_journal", [(False, False), (True, False), (True, True)])
async def test_recovery_preserves_static_acl_and_current_seat_grants(
    permission_backend, monkeypatch, prearmed, no_journal
):
    backend, attachment, node = permission_backend
    original_gid = node.stat().st_gid
    old_user, new_user, static_group = 32101, 32102, 32103
    await run_host("setfacl", "-m", f"u:{old_user}:rw-,g:{static_group}:r--", str(node))

    async def host(*args, **kwargs):
        if args[0] == "udevadm":
            return "TAGS=:uaccess:seat:\n"
        return await run_host(*args, **kwargs)

    monkeypatch.setattr(permissions, "run_host", host)
    await backend.install_rules(attachment)
    assert backend.permissions.exists()
    if not no_journal:
        save_json(
            backend.journal,
            {
                "id": attachment.identity,
                "generation": attachment.generation,
                "selector": backend.selector(attachment),
                "nodes": {},
                "bindings": {},
                "mode": "",
                "prearmed": prearmed,
            },
        )
    await run_host("setfacl", "-b", str(node))
    masked_gid = next((gid for gid in os.getgroups() if gid != original_gid), original_gid)
    os.chown(node, -1, masked_gid)
    node.chmod(0o660)

    async def current_policy(*_):
        acl = await run_host("getfacl", "-cn", str(node))
        assert f"group:{static_group}:r--" in acl
        assert f"user:{old_user}:" not in acl
        assert node.stat().st_gid == original_gid
        await run_host("setfacl", "-m", f"u:{new_user}:rw-", str(node))

    monkeypatch.setattr(backend, "trigger", current_policy)
    await backend.recover()
    acl = await run_host("getfacl", "-cn", str(node))
    assert f"user:{old_user}:" not in acl
    assert f"user:{new_user}:rw-" in acl
    assert f"group:{static_group}:r--" in acl
    assert not backend.journal.exists() and not backend.permissions.exists()


@pytest.mark.asyncio
async def test_static_user_acl_on_non_seat_node_survives_prearmed_recovery(
    permission_backend, monkeypatch
):
    backend, attachment, node = permission_backend
    await run_host("setfacl", "-m", "u:32104:r--", str(node))

    async def host(*args, **kwargs):
        return "" if args[0] == "udevadm" else await run_host(*args, **kwargs)

    monkeypatch.setattr(permissions, "run_host", host)
    before = await run_host("getfacl", "-cn", str(node))
    await backend.install_rules(attachment)
    await run_host("setfacl", "-b", str(node))
    node.chmod(0o660)
    monkeypatch.setattr(backend, "trigger", AsyncMock())
    await backend.recover()
    assert await run_host("getfacl", "-cn", str(node)) == before
    assert stat.S_IMODE(node.stat().st_mode) == 0o640


@pytest.mark.asyncio
async def test_permission_verification_failure_keeps_recovery_journal(
    permission_backend, monkeypatch
):
    backend, attachment, node = permission_backend

    async def capture_host(*args, **kwargs):
        return "" if args[0] == "udevadm" else await run_host(*args, **kwargs)

    monkeypatch.setattr(permissions, "run_host", capture_host)
    await backend.install_rules(attachment)
    node.chmod(0o660)

    async def broken_restore(*args, **kwargs):
        if args[0] == "setfacl":
            return ""  # A successful command alone is not proof of restoration.
        return await run_host(*args, **kwargs)

    monkeypatch.setattr(permissions, "run_host", broken_restore)
    with pytest.raises(OSError, match="baseline permissions"):
        await backend.recover()
    assert backend.journal.exists() and backend.permissions.exists()
    monkeypatch.setattr(permissions, "run_host", capture_host)
    monkeypatch.setattr(backend, "trigger", AsyncMock())
    await backend.recover()
    assert not backend.journal.exists()


@pytest.mark.asyncio
async def test_node_first_seen_while_armed_gets_clean_baseline_before_current_policy(
    permission_backend, monkeypatch
):
    backend, attachment, node = permission_backend
    scan = backend.inventory.scan
    monkeypatch.setattr(backend.inventory, "scan", lambda: [])
    await backend.install_rules(attachment)
    assert not backend.permissions.exists()
    monkeypatch.setattr(backend.inventory, "scan", scan)
    node.chmod(0o660)
    await run_host("setfacl", "-m", "u:32101:rw-", str(node))
    chown = Mock()
    monkeypatch.setattr(permissions.os, "chown", chown)
    original_stat = Path.stat

    def root_stat(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        if path != node:
            return info
        values = list(info)
        values[4:6] = [0, 0]
        return os.stat_result(values)

    # Ownership changes require root; ACL and mode operations are real.
    monkeypatch.setattr(Path, "stat", root_stat)

    async def current_policy(*_):
        assert stat.S_IMODE(node.stat().st_mode) == 0o600
        assert permissions.acl_entries(await run_host("getfacl", "-cn", str(node))) == {
            "user::rw-",
            "group::---",
            "other::---",
        }
        await run_host("setfacl", "-m", "u:32102:rw-", str(node))

    monkeypatch.setattr(backend, "trigger", current_policy)
    await backend.recover()
    chown.assert_called_once_with(node, 0, 0)
    acl = await run_host("getfacl", "-cn", str(node))
    assert "user:32102:rw-" in acl and "user:32101:" not in acl
    assert not backend.armed and not backend.journal.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mask,expected", [("r--", 0o040), ("---", 0)])
async def test_static_acl_preserves_effective_owning_group_permissions(
    permission_backend, mask, expected
):
    _backend, _attachment, node = permission_backend
    original = f"user::rw-,user:32109:rw-,group::rw-,mask::{mask},other::---"
    await run_host("setfacl", f"--set={original}", str(node))
    assert node.stat().st_mode & 0o070 == expected
    acl = await run_host("getfacl", "-cn", str(node))
    baseline = permissions.static_acl(acl, uaccess=True)
    await run_host("setfacl", "--set=" + ",".join(baseline.splitlines()), str(node))
    assert node.stat().st_mode & 0o070 == expected
    assert "user:32109:" not in await run_host("getfacl", "-cn", str(node))
