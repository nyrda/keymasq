import asyncio
import threading
from pathlib import Path

import pytest

from keymasq.masking.backend import LinuxMaskBackend, finish_io
from keymasq.masking.coordinator import TRIAL_SECONDS, MaskReservation
from keymasq.masking.inventory import Attachment, HardwareInventory


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def deck_sysfs(tmp_path: Path) -> tuple[HardwareInventory, Path]:
    sys = tmp_path / "sys"
    usb = sys / "devices/pci/usb3/3-3"
    for name, value in {
        "idVendor": "28de",
        "idProduct": "1205",
        "serial": "deck-one",
        "product": "Steam Deck Controller",
        "devnum": "7",
        "busnum": "3",
    }.items():
        write(usb / name, value)
    for number in (0, 1, 2):
        write(usb / f"3-3:1.{number}/bInterfaceClass", "03")
    bus = sys / "bus/usb/devices"
    bus.mkdir(parents=True)
    (bus / "3-3").symlink_to(usb)
    main = usb / "3-3:1.2/0003:28DE:1205.0012"
    main.mkdir()
    driver = sys / "bus/hid/drivers/hid-steam"
    driver.mkdir(parents=True)
    (main / "driver").symlink_to(driver)
    write(main / "modalias", "hid:b0003g0001v000028DEp00001205")
    hid_bus = sys / "bus/hid/devices"
    hid_bus.mkdir(parents=True)
    proxy = main.parent / "0003:28DE:1205.0013"
    proxy.mkdir()
    write(proxy / "modalias", "hid:b0003g0103v000028DEp00001205")
    (proxy / "driver").symlink_to(driver)
    (hid_bus / proxy.name).symlink_to(proxy)
    (hid_bus / main.name).symlink_to(main)
    for target in (main, proxy):
        (driver / target.name).symlink_to(target)
    raw = sys / "class/hidraw/hidraw3"
    raw.mkdir(parents=True)
    (raw / "device").symlink_to(proxy)
    dev = tmp_path / "dev"
    write(dev / "hidraw3", "")
    write(dev / "bus/usb/003/007", "")
    return HardwareInventory(sys, dev), usb


def test_inventory_finds_deck_without_evdev_and_excludes_raw_proxy(tmp_path: Path) -> None:
    inventory, _usb = deck_sysfs(tmp_path)
    devices = inventory.scan()
    assert len(devices) == 1
    deck = devices[0]
    assert deck.supported
    assert deck.main_hid == "0003:28DE:1205.0012"
    assert inventory.event_nodes(deck) == []
    assert [node.name for node in inventory.nodes(deck)] == ["hidraw3", "007"]


def test_attachment_generation_prevents_reusing_a_disconnected_device(tmp_path: Path) -> None:
    inventory, usb = deck_sysfs(tmp_path)
    deck = inventory.scan()[0]
    (usb / "devnum").write_text("8")
    assert inventory.scan()[0].identity == deck.identity
    with pytest.raises(ValueError, match="disconnected or changed"):
        inventory.resolve(deck.identity, deck.generation)


def test_touchscreen_is_not_in_the_controller_scope(tmp_path: Path) -> None:
    inventory, _usb = deck_sysfs(tmp_path)
    touchscreen = inventory.sys_root / "devices/pci/usb1/1-1/input/input1"
    touchscreen.mkdir(parents=True)
    event = inventory.sys_root / "class/input/event0"
    event.mkdir(parents=True)
    (event / "device").symlink_to(touchscreen)
    write(inventory.dev_root / "input/event0", "")
    assert inventory.event_nodes(inventory.scan()[0]) == []


def test_rules_reserve_only_the_selected_connection(tmp_path: Path) -> None:
    inventory, _usb = deck_sysfs(tmp_path)
    backend = LinuxMaskBackend(inventory)
    matches = backend.rule_matches(inventory.scan()[0])
    assert all('idVendor}=="28de"' in match for match in matches)
    assert all('idProduct}=="1205"' in match for match in matches)
    assert not any("devnum" in match for match in matches)
    assert "ATTRS{serial}" in matches[0]
    assert "ATTR{serial}" in matches[1]


def test_scope_check_ignores_the_same_controllers_proxy_and_auxiliary_interfaces(
    tmp_path: Path,
) -> None:
    inventory, usb = deck_sysfs(tmp_path)
    backend = LinuxMaskBackend(inventory)
    main = inventory.scan()[0].main_hid
    driver = inventory.sys_root / "bus/hid/drivers/hid-steam"
    auxiliary = usb / "3-3:1.0/0003:28DE:1205.0010"
    auxiliary.mkdir()
    (driver / auxiliary.name).symlink_to(auxiliary)
    assert backend.inventory.other_steam_controllers(main) == []
    other = usb.parent / "3-4/3-4:1.2/0003:28DE:1205.0014"
    other.mkdir(parents=True)
    (driver / other.name).symlink_to(other)
    assert backend.inventory.other_steam_controllers(main) == [other.name]


class FakeBackend(LinuxMaskBackend):
    def __init__(self, tmp_path: Path) -> None:
        inventory, _usb = deck_sysfs(tmp_path)
        super().__init__(inventory, tmp_path / "run", tmp_path / "rules", tmp_path / "state")
        self.prepare_directories()
        self.recoveries = 0
        self.fail_activation = False
        self.fail_recovery = False

    async def activate(self, attachment: Attachment) -> list[str]:
        self.journal.write_text("pending")
        if self.fail_activation:
            raise OSError("udev failed")
        return ["/dev/input/event5"]

    async def install_rules(self, attachment: Attachment) -> None:
        self.early.write_text("\n".join(self.rule_matches(attachment)))
        self.late.write_text("reserved")

    async def recover(self, *, keep_rules: bool = False) -> None:
        self.recoveries += 1
        if self.fail_recovery:
            self.fail_recovery = False
            raise OSError("temporary restoration failure")
        self.journal.unlink(missing_ok=True)
        if not keep_rules:
            self.early.unlink(missing_ok=True)
            self.late.unlink(missing_ok=True)


async def begin(supervisor: MaskReservation) -> dict[str, object]:
    await supervisor.request({"command": "startup", "uid": 1000})
    deck = supervisor.backend.inventory.scan()[0]
    result = await supervisor.request(
        {"command": "mask", "id": deck.identity, "generation": deck.generation}
    )
    assert result["state"] == "applying"
    assert supervisor.apply_task is not None
    await supervisor.request({"command": "quiesced", "token": result["token"]})
    await supervisor.apply_task
    return result


@pytest.mark.asyncio
async def test_keep_requires_ready_output_and_explicit_live_trial(tmp_path: Path) -> None:
    clock = [100.0]
    supervisor = MaskReservation(FakeBackend(tmp_path), lambda: clock[0])
    result = await begin(supervisor)
    token = result["token"]
    with pytest.raises(ValueError, match="not ready"):
        await supervisor.request({"command": "keep", "token": token})
    await supervisor.request({"command": "ready", "token": token})
    clock[0] += 2
    kept = await supervisor.request({"command": "keep", "token": token})
    assert kept["state"] == "masked"
    # Confirmation removes the trial deadline, but does not remove the lease.
    clock[0] += TRIAL_SECONDS
    await supervisor.request({"command": "poll"})
    await supervisor.monitor_once()
    assert supervisor.state["state"] == "masked"


@pytest.mark.asyncio
async def test_expired_confirmation_restores_and_latches_suspension(tmp_path: Path) -> None:
    clock = [100.0]
    backend = FakeBackend(tmp_path)
    supervisor = MaskReservation(backend, lambda: clock[0])
    result = await begin(supervisor)
    await supervisor.request({"command": "ready", "token": result["token"]})
    clock[0] += TRIAL_SECONDS
    await supervisor.request({"command": "poll"})
    with pytest.raises(ValueError, match="expired"):
        await supervisor.request({"command": "keep", "token": result["token"]})
    await supervisor.monitor_once()
    assert backend.recoveries == 1
    assert supervisor.status()["remapping_suspended"] is True
    assert supervisor.state["reason"] == "trial_expired"
    await supervisor.monitor_once()
    assert backend.recoveries == 1
    await supervisor.request({"command": "resume"})
    assert supervisor.status()["remapping_suspended"] is False


@pytest.mark.asyncio
async def test_reconnect_ends_the_reservation_without_remasking(tmp_path: Path) -> None:
    backend = FakeBackend(tmp_path)
    supervisor = MaskReservation(backend)
    await begin(supervisor)
    attachment = backend.inventory.scan()[0]
    (attachment.syspath / "devnum").write_text("8")
    await supervisor.monitor_once()
    assert backend.recoveries == 1
    assert supervisor.state["reason"] == "hardware_disconnected"
    assert supervisor.status()["remapping_suspended"] is True


@pytest.mark.asyncio
async def test_old_keep_request_cannot_confirm_a_new_trial(tmp_path: Path) -> None:
    supervisor = MaskReservation(FakeBackend(tmp_path))
    old = await begin(supervisor)
    await supervisor.request({"command": "restore"})
    new = await begin(supervisor)
    assert old["token"] != new["token"]
    await supervisor.request({"command": "ready", "token": new["token"]})
    with pytest.raises(ValueError, match="ended"):
        await supervisor.request({"command": "keep", "token": old["token"]})


@pytest.mark.asyncio
async def test_failed_activation_and_recovery_remain_recoverable(tmp_path: Path) -> None:
    backend = FakeBackend(tmp_path)
    backend.fail_activation = True
    backend.fail_recovery = True
    supervisor = MaskReservation(backend)
    await begin(supervisor)
    with pytest.raises(OSError, match="restoration failure"):
        await supervisor.monitor_once()
    assert supervisor.state["state"] == "recovery_failed"
    assert backend.journal.exists()
    await supervisor.monitor_once()
    assert supervisor.state["state"] == "restored"
    assert not backend.journal.exists()


@pytest.mark.asyncio
async def test_cancellation_waits_for_pending_mutation_before_recovery(tmp_path: Path) -> None:
    entered, finish = threading.Event(), threading.Event()
    journal: list[str] = []

    def mutate() -> None:
        entered.set()
        finish.wait(timeout=5)
        journal.append("mutation finished")

    async def activate_then_restore() -> None:
        try:
            await finish_io(mutate)
        finally:
            journal.append("restored")

    task = asyncio.create_task(activate_then_restore())
    await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert journal == []
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert journal == ["mutation finished", "restored"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["disconnect", "endpoint", "scope", "trial_expired"])
async def test_normal_hardware_changes_do_not_kill_daemon(tmp_path, monkeypatch, cause):
    clock = [100.0]
    backend = FakeBackend(tmp_path)
    supervisor = MaskReservation(backend, lambda: clock[0])
    await begin(supervisor)
    if cause == "disconnect":
        (backend.inventory.scan()[0].syspath / "devnum").write_text("8")
    elif cause == "endpoint":
        (backend.inventory.dev_root / "hidraw3").unlink()
    elif cause == "scope":
        monkeypatch.setattr(backend.inventory, "other_steam_controllers", lambda _: ["other"])
    elif cause == "trial_expired":
        clock[0] += TRIAL_SECONDS
        await supervisor.request({"command": "poll"})
    await supervisor.monitor_once()
    assert backend.recoveries == 1
    assert supervisor.state["state"] == "restored"


@pytest.mark.asyncio
async def test_repair_retry_preserves_reason_and_does_not_kill_daemon(tmp_path, monkeypatch):
    backend = FakeBackend(tmp_path)
    supervisor = MaskReservation(backend)
    await begin(supervisor)
    backend.fail_recovery = True
    with pytest.raises(OSError, match="restoration failure"):
        await supervisor.request({"command": "restore", "reason": "replacement_unavailable"})
    await supervisor.monitor_once()
    assert supervisor.state["state"] == "restored"
    assert supervisor.state["reason"] == "replacement_unavailable"
