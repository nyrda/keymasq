"""Generic physical HID policy and rollback, independent of controller models."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from keymasq.masking import backend as backend_module
from keymasq.masking.backend import LinuxMaskBackend, save_json
from keymasq.masking.coordinator import MaskReservation
from keymasq.masking.inventory import NoBoundInterfacesError
from tests.common.test_hardware_masking import FakeBackend, deck_sysfs, write


@pytest.fixture(autouse=True)
def simulated_node_permissions(monkeypatch):
    # These sysfs fixtures use regular files and test driver/rule sequencing.
    # Permission recovery is exercised with real ACLs in the boundary tests.
    from keymasq.masking import permissions

    monkeypatch.setattr(permissions, "capture", AsyncMock())
    monkeypatch.setattr(permissions, "restore", AsyncMock())


def generic_usb(tmp_path: Path):
    inventory, usb = deck_sysfs(tmp_path)
    write(usb / "idVendor", "abcd")
    write(usb / "idProduct", "9876")
    # HID identities need not equal USB identities. Discover the actual driver.
    driver = inventory.sys_root / "bus/hid/drivers/hid-example"
    driver.mkdir()
    hid = next(
        path
        for path in (inventory.sys_root / "bus/hid/devices").iterdir()
        if path.name.endswith("0012")
    )
    (hid / "driver").unlink()
    (hid / "driver").symlink_to(driver)
    return inventory, inventory.scan()[0], hid, driver


def test_unknown_usb_vendor_and_driver_need_no_masking_registration(tmp_path):
    inventory, attachment, hid, _driver = generic_usb(tmp_path)
    assert attachment.supported
    assert not attachment.is_deck
    assert inventory.bindings(attachment) == {hid.name: "hid-example"}
    matches = LinuxMaskBackend(inventory).rule_matches(attachment)
    assert all('idVendor}=="abcd"' in match for match in matches)
    assert all('idProduct}=="9876"' in match for match in matches)
    assert not any("devnum" in match for match in matches)


@pytest.mark.asyncio
async def test_snapshot_does_not_ignore_binding_errors_with_the_same_message(tmp_path, monkeypatch):
    inventory, attachment, _hid, _driver = generic_usb(tmp_path)
    failure = ValueError("No bound input interfaces are available to reconnect")
    monkeypatch.setattr(inventory, "bindings", Mock(side_effect=failure))
    with pytest.raises(ValueError) as error:
        await LinuxMaskBackend(inventory).snapshot(attachment)
    assert error.value is failure


@pytest.mark.parametrize("escape", ["parent", "symlink"])
def test_saved_selector_rejects_escape_before_hardware_validation(tmp_path, monkeypatch, escape):
    inventory, attachment, _hid, _driver = generic_usb(tmp_path)
    devices = inventory.sys_root / "devices"
    outside = tmp_path / "outside"
    outside.mkdir()
    if escape == "parent":
        path = devices / ".." / ".." / "outside" / attachment.kernel_name
    else:
        (devices / "escape").symlink_to(outside, target_is_directory=True)
        path = devices / "escape" / attachment.kernel_name
    selector = inventory.selector(attachment)
    selector["path"] = str(path)
    validate = Mock()
    monkeypatch.setattr(inventory, "validate", validate)

    with pytest.raises(ValueError, match="Invalid saved attachment path"):
        inventory.from_selector(selector)
    validate.assert_not_called()


@pytest.mark.parametrize("offline", [False, True])
def test_saved_selector_resolves_inside_symlinked_sysfs_root(tmp_path, offline):
    inventory, attachment, _hid, _driver = generic_usb(tmp_path)
    sys_alias = tmp_path / "sys-alias"
    sys_alias.symlink_to(inventory.sys_root, target_is_directory=True)
    inventory.sys_root = sys_alias
    path = sys_alias / "devices" / "offline" if offline else attachment.syspath
    selector = inventory.selector(attachment)
    selector["path"] = str(path)

    restored = inventory.from_selector(selector)

    assert restored.syspath == path.resolve()
    assert restored.identity == attachment.identity
    assert restored.syspath.exists() is not offline


def test_hid_descendants_do_not_make_a_shared_usb_hub_a_masking_candidate(tmp_path):
    inventory, attachment, _hid, _driver = generic_usb(tmp_path)
    hub = attachment.syspath.parent
    write(hub / "idVendor", "1234")
    write(hub / "idProduct", "5678")
    (inventory.sys_root / "bus/usb/devices/3-1").symlink_to(hub)
    assert [item.identity for item in inventory.scan()] == [attachment.identity]


def bluetooth_hid(tmp_path: Path):
    inventory, _usb = deck_sysfs(tmp_path)
    hid = inventory.sys_root / "devices/virtual/misc/uhid/0005:ABCD:9876.0020"
    write(
        hid / "uevent",
        "HID_NAME=Unknown Bluetooth HID\nHID_UNIQ=remote-one\nHID_PHYS=adapter-one\n",
    )
    driver = inventory.sys_root / "bus/hid/drivers/hid-generic"
    driver.mkdir()
    (hid / "driver").symlink_to(driver)
    (inventory.sys_root / "bus/hid/devices" / hid.name).symlink_to(hid)
    raw = inventory.sys_root / "class/hidraw/hidraw8"
    write(raw / "placeholder", "")
    (raw / "device").symlink_to(hid)
    write(inventory.dev_root / "hidraw8", "")
    attachment = next(item for item in inventory.scan() if item.transport == "bluetooth")
    return inventory, attachment, hid


def test_unknown_bluetooth_uhid_without_evdev_has_device_local_rules(tmp_path):
    inventory, attachment, hid = bluetooth_hid(tmp_path)
    assert attachment.supported
    assert inventory.event_nodes(attachment) == []
    assert inventory.bindings(attachment) == {hid.name: "hid-generic"}
    assert inventory.node_role(attachment, inventory.dev_root / "hidraw8") == "hid:hidraw"
    rules = LinuxMaskBackend(inventory).rule_matches(attachment)
    assert len(rules) == 2
    assert all(f'KERNELS=="{hid.name}"' in rule for rule in rules)
    assert not any('SUBSYSTEM=="usb"' in rule for rule in rules)


def test_bluetooth_reconnect_preserves_selection_but_invalidates_generation(tmp_path):
    inventory, attachment, hid = bluetooth_hid(tmp_path)
    link = inventory.sys_root / "bus/hid/devices" / hid.name
    link.unlink()
    replacement = hid.with_name("0005:ABCD:9876.0021")
    hid.rename(replacement)
    (link.parent / replacement.name).symlink_to(replacement)
    new = next(item for item in inventory.scan() if item.transport == "bluetooth")
    assert new.identity == attachment.identity
    assert new.generation != attachment.generation
    with pytest.raises(ValueError, match="disconnected or changed"):
        inventory.resolve(attachment.identity, attachment.generation)


def test_disappearing_hid_does_not_abort_inventory_scan(tmp_path, monkeypatch):
    inventory, attachment, _hid = bluetooth_hid(tmp_path)
    original = Path.stat

    def stat(path, *args, **kwargs):
        if path == attachment.syspath:
            raise FileNotFoundError("disconnected during scan")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    assert all(item.identity != attachment.identity for item in inventory.scan())


@pytest.mark.parametrize("field,value", [("vendor", 'abcd", RUN+="bad'), ("kernel_name", "../bad")])
def test_rule_generation_rejects_unsafe_values(tmp_path, field, value):
    inventory, attachment, _hid, _driver = generic_usb(tmp_path)
    with pytest.raises(ValueError, match="Invalid"):
        LinuxMaskBackend(inventory).rule_matches(replace(attachment, **{field: value}))


@pytest.mark.asyncio
async def test_rebind_uses_recorded_driver_and_rejects_a_changed_binding(tmp_path, monkeypatch):
    inventory, attachment, hid, driver = generic_usb(tmp_path)
    backend = LinuxMaskBackend(inventory)
    monkeypatch.setattr(backend_module, "run_host", AsyncMock(return_value=""))
    await backend.rebind_hid(attachment, hid.name, driver.name)
    assert (driver / "unbind").read_text() == hid.name
    assert (driver / "bind").read_text() == hid.name
    other = driver.with_name("hid-other")
    other.mkdir()
    (hid / "driver").unlink()
    (hid / "driver").symlink_to(other)
    with pytest.raises(ValueError, match="different driver"):
        await backend.rebind_hid(attachment, hid.name, driver.name)
    assert not (other / "unbind").exists()


@pytest.mark.asyncio
async def test_rebind_rejects_hardware_outside_selected_attachment(tmp_path, monkeypatch):
    inventory, attachment, hid, driver = generic_usb(tmp_path)
    backend = LinuxMaskBackend(inventory)
    monkeypatch.setattr(backend_module, "run_host", AsyncMock(return_value=""))
    other = replace(attachment, syspath=attachment.syspath.parent / "other")
    monkeypatch.setattr(inventory, "resolve", lambda *_: other)
    with pytest.raises(ValueError, match="interface changed"):
        await backend.rebind_hid(attachment, hid.name, driver.name)
    assert not (driver / "unbind").exists()


@pytest.mark.asyncio
async def test_recovery_removes_policy_even_if_driver_repair_fails(tmp_path, monkeypatch):
    inventory, attachment, hid, driver = generic_usb(tmp_path)
    backend = LinuxMaskBackend(inventory, tmp_path / "run", tmp_path / "rules", tmp_path / "state")
    backend.prepare_directories()
    save_json(
        backend.journal,
        {
            "id": attachment.identity,
            "generation": attachment.generation,
            "bindings": {hid.name: driver.name},
            "mode": "",
            "nodes": {},
        },
    )
    backend.early.write_text("deny")
    backend.late.write_text("deny")
    monkeypatch.setattr(backend_module, "run_host", AsyncMock(return_value=""))
    monkeypatch.setattr(backend, "rebind_hid", AsyncMock(side_effect=OSError("bind failed")))
    with pytest.raises(OSError, match="bind failed"):
        await backend.recover()
    assert not backend.early.exists()
    assert not backend.late.exists()
    assert backend.journal.exists()
    assert not backend.mode_path.exists()
    monkeypatch.setattr(backend, "rebind_hid", AsyncMock())
    await backend.recover()
    assert not backend.journal.exists()


@pytest.mark.asyncio
async def test_takeover_waits_for_daemon_quiescence_and_rejects_stale_ack(tmp_path):
    backend = FakeBackend(tmp_path)
    supervisor = MaskReservation(backend)
    attachment = backend.inventory.scan()[0]
    result = await supervisor.request(
        {"command": "mask", "id": attachment.identity, "generation": attachment.generation}
    )
    await asyncio.sleep(0)
    assert not backend.journal.exists()
    with pytest.raises(ValueError, match="trial has ended"):
        await supervisor.request({"command": "quiesced", "token": "stale"})
    await supervisor.request({"command": "quiesced", "token": result["token"]})
    assert supervisor.apply_task
    await supervisor.apply_task
    assert supervisor.state["state"] == "acquiring"


@pytest.mark.asyncio
async def test_generic_activation_applies_policy_before_rebinding_without_deck_mode(
    tmp_path, monkeypatch
):
    inventory, attachment, hid, driver = generic_usb(tmp_path)
    backend = LinuxMaskBackend(inventory, tmp_path / "run", tmp_path / "rules", tmp_path / "state")
    backend.prepare_directories()
    snapshot = {"nodes": {}, "bindings": {hid.name: driver.name}}
    monkeypatch.setattr(backend, "snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(backend, "reject_unrevoked_handles", lambda *_: None)
    monkeypatch.setattr(backend, "verify_access", AsyncMock())
    commands = []

    async def host(*args, **_kwargs):
        commands.append(args)
        return ""

    async def rebind(*_args, **_kwargs):
        assert any(command[:2] == ("udevadm", "trigger") for command in commands)
        assert 'ATTRS{idVendor}=="abcd"' in backend.early.read_text()
        assert 'GROUP:="keymasq"' in backend.late.read_text()
        assert "setfacl" in backend.late.read_text()

    monkeypatch.setattr(backend_module, "run_host", host)
    monkeypatch.setattr(backend, "rebind_hid", rebind)
    assert await backend.activate(attachment) == []
    assert not backend.mode_path.exists()


@pytest.mark.asyncio
async def test_activation_waits_for_udev_to_apply_rules_to_rebound_nodes(tmp_path, monkeypatch):
    """A bare settle after bind can return before udevd queues the new nodes."""
    inventory, attachment, hid, driver = generic_usb(tmp_path)
    backend = LinuxMaskBackend(inventory, tmp_path / "run", tmp_path / "rules", tmp_path / "state")
    backend.prepare_directories()
    snapshot = {"nodes": {}, "bindings": {hid.name: driver.name}}
    monkeypatch.setattr(backend, "snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(backend, "reject_unrevoked_handles", lambda *_: None)
    events: list[tuple[str, ...]] = []

    async def host(*args, **_kwargs):
        if args[:2] == ("udevadm", "trigger"):
            events.append(args)
        return ""

    async def rebind(*_args, **_kwargs):
        events.append(("rebind",))

    async def verify_access(current):
        # udev must have processed the replacement nodes under the armed
        # rules before their ownership is checked.
        assert events[-1][:2] == ("udevadm", "trigger")
        assert "--action=change" in events[-1]
        assert f"--parent-match={current.syspath}" in events[-1]
        assert "--settle" in events[-1]
        assert ("rebind",) in events[:-1]
        events.append(("verify",))

    monkeypatch.setattr(backend_module, "run_host", host)
    monkeypatch.setattr(backend, "rebind_hid", rebind)
    monkeypatch.setattr(backend, "verify_access", verify_access)
    assert await backend.activate(attachment) == []
    assert events[-1] == ("verify",)
    triggers = [event for event in events if event[:2] == ("udevadm", "trigger")]
    assert len(triggers) == 2  # once before rebind for live nodes, once after


@pytest.mark.asyncio
async def test_takeover_rejects_a_raw_endpoint_that_survives_the_rebind(tmp_path, monkeypatch):
    inventory, attachment, hid, driver = generic_usb(tmp_path)
    backend = LinuxMaskBackend(inventory, tmp_path / "run", tmp_path / "rules", tmp_path / "state")
    backend.prepare_directories()
    node = inventory.dev_root / "hidraw3"
    info = node.stat()
    snapshot = {
        "bindings": {hid.name: driver.name},
        "nodes": {
            inventory.node_role(attachment, node): {"filesystem": info.st_dev, "inode": info.st_ino}
        },
    }
    monkeypatch.setattr(backend, "snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(backend, "reject_unrevoked_handles", lambda *_: None)
    monkeypatch.setattr(backend, "verify_access", AsyncMock())
    monkeypatch.setattr(backend, "rebind_hid", AsyncMock())
    monkeypatch.setattr(backend_module, "run_host", AsyncMock(return_value=""))
    with pytest.raises(OSError, match="was not replaced"):
        await backend.activate(attachment)


@pytest.mark.asyncio
async def test_generic_supervisor_does_not_apply_deck_global_mode_conflict(tmp_path):
    backend = FakeBackend(tmp_path)
    attachment = backend.inventory.scan()[0]
    write(attachment.syspath / "idVendor", "abcd")
    attachment = backend.inventory.scan()[0]
    supervisor = MaskReservation(backend)
    result = await supervisor.request(
        {"command": "mask", "id": attachment.identity, "generation": attachment.generation}
    )
    assert result["main_hid"] == ""
    await supervisor.request({"command": "restore"})
    assert json.loads((backend.state_dir / "selection.json").read_text())["state"] == "restored"


def test_direct_usb_holder_is_detected_through_an_alias(tmp_path, monkeypatch):
    inventory, attachment, _hid, _driver = generic_usb(tmp_path)
    usb_node = inventory.dev_root / "bus/usb/003/007"
    usb_node.unlink()
    usb_node.symlink_to("/dev/null")
    process = tmp_path / "proc/1234"
    (process / "fd").mkdir(parents=True)
    (process / "fd/7").symlink_to("/dev/null")
    original_iterdir = Path.iterdir
    monkeypatch.setattr(
        Path,
        "iterdir",
        lambda path: iter([process]) if path == Path("/proc") else original_iterdir(path),
    )
    with pytest.raises(ValueError, match="Process 1234.*direct USB"):
        LinuxMaskBackend(inventory).reject_unrevoked_handles(attachment)


@pytest.mark.parametrize("denied_at", ["directory", "descriptor"])
def test_denied_fd_inspection_refuses_masking_with_clear_error(tmp_path, monkeypatch, denied_at):
    inventory, attachment, _hid, _driver = generic_usb(tmp_path)
    process = tmp_path / "proc/1234"
    descriptors = process / "fd"
    descriptors.mkdir(parents=True)
    descriptor = descriptors / "7"
    descriptor.touch()
    original_iterdir = Path.iterdir
    original_stat = Path.stat

    def iterdir(path):
        if path == Path("/proc"):
            return iter([process])
        if path == descriptors and denied_at == "directory":
            raise PermissionError("FD directory denied")
        return original_iterdir(path)

    def stat(path, *args, **kwargs):
        if path == descriptor and denied_at == "descriptor":
            raise PermissionError("FD target denied")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(Path, "stat", stat)

    with pytest.raises(
        PermissionError,
        match="Cannot inspect existing device handles for process 1234; "
        "masking cannot safely continue",
    ) as failure:
        LinuxMaskBackend(inventory).reject_unrevoked_handles(attachment)
    assert isinstance(failure.value.__cause__, PermissionError)


PORTAL_MOUNT = (
    "53 300 0:72 / /run/user/1000/doc rw,nosuid,nodev,relatime shared:349 - fuse.portal portal rw"
)


@pytest.mark.parametrize(
    ("mountinfo", "fdinfo", "refused"),
    [
        pytest.param(
            PORTAL_MOUNT, "pos:\t0\nflags:\t0100000\nmnt_id:\t53\n", False, id="nodev-fuse"
        ),
        pytest.param(
            PORTAL_MOUNT.replace("fuse.portal", "fuse"), "mnt_id: 53", False, id="plain-fuse"
        ),
        pytest.param(
            PORTAL_MOUNT.replace("fuse.portal", "fuseblk"), "mnt_id: 53", False, id="fuseblk"
        ),
        pytest.param(PORTAL_MOUNT.replace(",nodev", ""), "mnt_id: 53", True, id="fuse-with-dev"),
        pytest.param(
            PORTAL_MOUNT.replace("fuse.portal", "ext4"), "mnt_id: 53", True, id="not-fuse"
        ),
        pytest.param(
            PORTAL_MOUNT.replace("fuse.portal", "fusectl"), "mnt_id: 53", True, id="fuse-control"
        ),
        pytest.param(
            PORTAL_MOUNT.replace("nodev", "nodevice"), "mnt_id: 53", True, id="option-prefix"
        ),
        pytest.param(
            PORTAL_MOUNT.replace(",nodev", "") + ",nodev",
            "mnt_id: 53",
            True,
            id="superblock-only-nodev",
        ),
        pytest.param(PORTAL_MOUNT, "mnt_id: 54", True, id="foreign-or-detached-mount"),
        pytest.param(PORTAL_MOUNT, None, True, id="fdinfo-unreadable"),
        pytest.param(None, "mnt_id: 53", True, id="mountinfo-unreadable"),
        pytest.param(PORTAL_MOUNT, "pos: 0", True, id="missing-mount-id"),
        pytest.param(PORTAL_MOUNT, "mnt_id:", True, id="empty-mount-id"),
        pytest.param(PORTAL_MOUNT, "mnt_id: 53 extra", True, id="malformed-mount-id"),
        pytest.param(PORTAL_MOUNT, "mnt_id: 53\nmnt_id: 54", True, id="duplicate-mount-id"),
        pytest.param(
            PORTAL_MOUNT.replace("53 ", "invalid ", 1), "mnt_id: invalid", True, id="nonnumeric-id"
        ),
        pytest.param(
            PORTAL_MOUNT.partition(" - ")[0] + " - ", "mnt_id: 53", True, id="missing-filesystem"
        ),
        pytest.param(
            "53 300 0:72 / - fuse.portal portal rw", "mnt_id: 53", True, id="truncated-mount"
        ),
        pytest.param(PORTAL_MOUNT + "\n" + PORTAL_MOUNT, "mnt_id: 53", True, id="ambiguous-mount"),
        pytest.param(PORTAL_MOUNT.replace(" - ", " "), "mnt_id: 53", True, id="missing-separator"),
    ],
)
def test_denied_descriptor_is_only_skipped_on_a_nodev_fuse_mount(
    tmp_path, monkeypatch, mountinfo, fdinfo, refused
):
    inventory, attachment, _hid, _driver = generic_usb(tmp_path)
    process = tmp_path / "proc/1234"
    descriptor = process / "fd/7"
    descriptor.parent.mkdir(parents=True)
    descriptor.touch()
    if mountinfo is not None:
        (process / "mountinfo").write_text(mountinfo + "\n")
    if fdinfo is not None:
        (process / "fdinfo").mkdir()
        (process / "fdinfo/7").write_text(fdinfo)
    original_iterdir = Path.iterdir
    original_stat = Path.stat

    def stat(path, *args, **kwargs):
        if path == descriptor:
            raise PermissionError("FD target denied")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(
        Path,
        "iterdir",
        lambda path: iter([process]) if path == Path("/proc") else original_iterdir(path),
    )
    monkeypatch.setattr(Path, "stat", stat)

    backend = LinuxMaskBackend(inventory)
    if refused:
        with pytest.raises(PermissionError, match="Cannot inspect existing device handles"):
            backend.reject_unrevoked_handles(attachment)
    else:
        backend.reject_unrevoked_handles(attachment)


@pytest.mark.parametrize("metadata", ["fdinfo/7", "mountinfo"])
@pytest.mark.parametrize("error", [PermissionError, OSError, UnicodeDecodeError])
def test_unreadable_fuse_metadata_refuses_the_exception(tmp_path, monkeypatch, metadata, error):
    process = tmp_path / "1234"

    def read_text(path, *args, **kwargs):
        if path == process / metadata:
            if error is UnicodeDecodeError:
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
            raise error("metadata unavailable")
        return "mnt_id: 53" if path == process / "fdinfo/7" else PORTAL_MOUNT

    monkeypatch.setattr(Path, "read_text", read_text)
    assert not LinuxMaskBackend._descriptor_cannot_be_a_device(process, "7")


def test_fuse_exception_does_not_skip_a_later_direct_usb_handle(tmp_path, monkeypatch):
    inventory, attachment, _hid, _driver = generic_usb(tmp_path)
    usb_node = inventory.dev_root / "bus/usb/003/007"
    usb_node.unlink()
    usb_node.symlink_to("/dev/null")
    process = tmp_path / "proc/1234"
    (process / "fd").mkdir(parents=True)
    portal = process / "fd/7"
    portal.touch()
    usb = process / "fd/8"
    usb.symlink_to("/dev/null")
    (process / "fdinfo").mkdir()
    (process / "fdinfo/7").write_text("mnt_id: 53\n")
    (process / "mountinfo").write_text(PORTAL_MOUNT)
    original_iterdir, original_stat = Path.iterdir, Path.stat

    def iterdir(path):
        if path == Path("/proc"):
            return iter([process])
        if path == process / "fd":
            return iter([portal, usb])
        return original_iterdir(path)

    def stat(path, *args, **kwargs):
        if path == portal:
            raise PermissionError("portal denied")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(Path, "stat", stat)
    with pytest.raises(ValueError, match="Process 1234.*direct USB"):
        LinuxMaskBackend(inventory).reject_unrevoked_handles(attachment)


@pytest.mark.asyncio
async def test_missing_raw_endpoint_restores_even_when_attachment_remains(tmp_path):
    from tests.common.test_hardware_masking import begin

    backend = FakeBackend(tmp_path)
    supervisor = MaskReservation(backend)
    result = await begin(supervisor)
    await supervisor.request({"command": "ready", "token": result["token"]})
    (backend.inventory.dev_root / "hidraw3").unlink()
    await supervisor.monitor_once()
    assert supervisor.state["reason"] == "hardware_interfaces_changed"
    assert backend.recoveries == 1


def usb_input_without_hid(tmp_path):
    from keymasq.masking.inventory import HardwareInventory

    sys = tmp_path / "sys"
    usb = sys / "devices/pci/usb1/1-2/1-2.3"
    for name, value in {
        "idVendor": "abcd",
        "idProduct": "5678",
        "devnum": "7",
        "busnum": "1",
    }.items():
        write(usb / name, value)
    interface = usb / "1-2.3:1.0"
    write(interface / "bInterfaceClass", "ff")
    driver = sys / "bus/usb/drivers/example-input"
    write(driver / "bind", "")
    write(driver / "unbind", "")
    (interface / "driver").symlink_to(driver)
    bus = sys / "bus/usb/devices"
    bus.mkdir(parents=True)
    (bus / usb.name).symlink_to(usb)
    (bus / interface.name).symlink_to(interface)
    input_parent = interface / "input/input1"
    write(input_parent / "name", "Vendor USB gamepad")
    event = sys / "class/input/event4"
    event.mkdir(parents=True)
    (event / "device").symlink_to(input_parent)
    write(tmp_path / "dev/input/event4", "")
    inventory = HardwareInventory(sys, tmp_path / "dev")
    return inventory, inventory.scan()[0], interface, driver


def test_usb_input_driver_without_hid_is_discovered_without_vendor_allowlist(tmp_path):
    inventory, attachment, interface, driver = usb_input_without_hid(tmp_path)
    assert inventory.bindings(attachment) == {f"usb:{interface.name}": driver.name}
    assert inventory.event_nodes(attachment) == [str(inventory.dev_root / "input/event4")]
    # The receiver's hub must not be selected, even though it has input descendants.
    hub = attachment.syspath.parent
    write(hub / "idVendor", "abcd")
    write(hub / "idProduct", "1111")
    (inventory.sys_root / "bus/usb/devices" / hub.name).symlink_to(hub)
    assert inventory.scan() == [attachment]


@pytest.mark.asyncio
async def test_usb_input_rebind_checks_scope_and_removes_existing_endpoints(tmp_path, monkeypatch):
    inventory, attachment, interface, driver = usb_input_without_hid(tmp_path)
    backend = LinuxMaskBackend(inventory)
    monkeypatch.setattr(backend_module, "run_host", AsyncMock(return_value=""))
    with pytest.raises(OSError, match="did not remove its endpoints"):
        await backend.rebind_binding(attachment, f"usb:{interface.name}", driver.name)
    assert (driver / "unbind").read_text() == interface.name
    assert (driver / "bind").read_text() == ""
    (interface / "input/input1/name").unlink()
    (interface / "input/input1").rmdir()
    await backend.rebind_binding(attachment, f"usb:{interface.name}", driver.name)
    assert (driver / "bind").read_text() == interface.name
    with pytest.raises(ValueError, match="Invalid recorded"):
        await backend.rebind_binding(attachment, "usb:../../other", driver.name)
    with pytest.raises(ValueError, match="changed during takeover"):
        await backend.rebind_binding(attachment, "usb:1-9:1.0", driver.name)


@pytest.mark.asyncio
async def test_missing_bindings_after_usb_reconnect_still_restores_access(tmp_path, monkeypatch):
    from keymasq.masking import permissions

    inventory, attachment, hid, driver = generic_usb(tmp_path)
    backend = LinuxMaskBackend(inventory, tmp_path / "run", tmp_path / "rules", tmp_path / "state")
    backend.prepare_directories()
    save_json(
        backend.journal,
        {
            "id": attachment.identity,
            "generation": attachment.generation,
            "selector": backend.inventory.selector(attachment),
            "usb_reconnect": True,
            "bindings": {hid.name: driver.name},
            "mode": "",
            "nodes": {},
        },
    )
    backend.early.write_text("deny")
    backend.late.write_text("deny")
    (attachment.syspath / "devnum").write_text("99")
    monkeypatch.setattr(
        inventory,
        "bindings",
        Mock(side_effect=NoBoundInterfacesError()),
    )
    monkeypatch.setattr(backend_module, "run_host", AsyncMock(return_value=""))
    rebind, restored, triggered = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(backend, "rebind_binding", rebind)
    monkeypatch.setattr(permissions, "restore", restored)
    monkeypatch.setattr(backend, "trigger", triggered)
    with pytest.raises(ValueError, match="No bound input interfaces"):
        await backend.recover()
    assert not backend.early.exists() and not backend.late.exists()
    restored.assert_awaited_once()
    triggered.assert_awaited_once()
    rebind.assert_not_awaited()
    assert backend.journal.exists()
    monkeypatch.setattr(inventory, "bindings", Mock(return_value={hid.name: driver.name}))
    await backend.recover()
    assert not backend.journal.exists()
    rebind.assert_awaited_once()


def i2c_hid(tmp_path: Path):
    from keymasq.masking.inventory import HardwareInventory

    sys = tmp_path / "sys"
    hid = sys / "devices/pci/i2c_designware.0/i2c-1/i2c-ABCD0001:00/0018:ABCD:9876.0003"
    write(hid / "uevent", "HID_NAME=I2C touchpad\n")
    driver = sys / "bus/hid/drivers/hid-multitouch"
    write(driver / "bind", "")
    write(driver / "unbind", "")
    (hid / "driver").symlink_to(driver)
    bus = sys / "bus/hid/devices"
    bus.mkdir(parents=True)
    (bus / hid.name).symlink_to(hid)
    write(hid / "hidraw/hidraw2/dev", "")
    raw = sys / "class/hidraw/hidraw2"
    raw.mkdir(parents=True)
    (raw / "device").symlink_to(hid)
    write(tmp_path / "dev/hidraw2", "")
    inventory = HardwareInventory(sys, tmp_path / "dev")
    return inventory, inventory.scan()[0], hid, driver


@pytest.mark.asyncio
async def test_recovery_rebinds_an_i2c_hid_left_without_driver_or_raw_endpoint(
    tmp_path, monkeypatch
):
    inventory, attachment, hid, driver = i2c_hid(tmp_path)
    assert attachment.transport == "hid"
    backend = LinuxMaskBackend(inventory, tmp_path / "run", tmp_path / "rules", tmp_path / "state")
    backend.prepare_directories()
    save_json(
        backend.journal,
        {
            "id": attachment.identity,
            "generation": attachment.generation,
            "selector": inventory.selector(attachment),
            "bindings": {hid.name: driver.name},
            "mode": "",
            "nodes": {},
        },
    )
    # The unbind succeeded and the bind failed. The HID device is still attached.
    (hid / "driver").unlink()
    (hid / "hidraw/hidraw2/dev").unlink()
    (hid / "hidraw/hidraw2").rmdir()
    (inventory.sys_root / "class/hidraw/hidraw2/device").unlink()
    (inventory.dev_root / "hidraw2").unlink()
    assert inventory.scan() == []
    monkeypatch.setattr(backend_module, "run_host", AsyncMock(return_value=""))

    await backend.recover()

    assert (driver / "bind").read_text() == hid.name
    assert (driver / "unbind").read_text() == ""
    assert not backend.journal.exists()
