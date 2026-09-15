"""Multi-attachment behavior through the helper's public request interface."""

import asyncio
from pathlib import Path

import pytest

from keymasq.masking.backend import LinuxMaskBackend, save_json
from keymasq.masking.coordinator import MaskSupervisor
from keymasq.masking.inventory import Attachment, HardwareInventory


class Inventory(HardwareInventory):
    def __init__(self, root: Path, count: int = 3):
        super().__init__(root / "sys", root / "dev")
        self.attachments = [
            Attachment(
                f"{index:024x}",
                "1:100",
                f"Generic HID {index}",
                "abcd",
                "1234",
                "usb",
                root / "sys/devices" / f"1-{index}",
                f"1-{index}",
            )
            for index in range(1, count + 1)
        ]

    def scan(self):
        return list(self.attachments)

    def endpoint_roles(self, attachment):
        return [attachment.identity + ":hidraw"]


class Backend(LinuxMaskBackend):
    def for_attachment(self, identity):
        scoped = super().for_attachment(identity)
        return Backend(
            scoped.inventory,
            scoped.runtime_dir,
            scoped.rules_dir,
            scoped.state_dir,
            reservation_id=identity,
        )

    async def activate(self, attachment):
        save_json(self.journal, {"id": attachment.identity, "generation": attachment.generation})
        self.early.write_text(attachment.identity)
        self.late.write_text(attachment.identity)
        return []  # Raw-only devices must work without an evdev decoder.

    async def install_rules(self, attachment):
        self.early.write_text(attachment.identity)
        self.late.write_text(attachment.identity)

    async def refresh_interfaces(self, attachment):
        assert self.armed and self.journal.exists()
        return self.inventory.event_nodes(attachment)

    async def recover(self, *, keep_rules=False):
        if not keep_rules:
            self.early.unlink(missing_ok=True)
            self.late.unlink(missing_ok=True)
        self.journal.unlink(missing_ok=True)


@pytest.fixture
async def supervisor(tmp_path):
    backend = Backend(Inventory(tmp_path), tmp_path / "run", tmp_path / "rules", tmp_path / "state")
    supervisor = MaskSupervisor(backend)
    await supervisor.initialize()
    await supervisor.request({"command": "startup", "uid": 1000})
    yield supervisor
    await supervisor.restore("service_stopped")


def selected(result, identity):
    return next(item for item in result["masks"] if item["id"] == identity)


async def start(supervisor, attachment, *, keep=True, persist=True):
    data = {"id": attachment.identity, "generation": attachment.generation}
    result = await supervisor.request({"command": "mask", **data})
    token = selected(result, attachment.identity)["token"]
    data["token"] = token
    await supervisor.request({"command": "quiesced", **data})
    await supervisor.reservations[attachment.identity].apply_task
    await supervisor.request({"command": "ready", **data})
    if keep:
        await supervisor.request({"command": "keep", "persist": persist, **data})
    return data


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["acquiring", "disconnected", "no_masks"])
async def test_emergency_reset_restores_masks_without_runtime_readers(supervisor, phase):
    from keymasq.keymasqd.device_manager import DeviceManager
    from keymasq.keymasqd.hardware_masking import HardwareMasking

    attachment = supervisor.backend.inventory.scan()[0]
    if phase == "acquiring":
        data = {"id": attachment.identity, "generation": attachment.generation}
        result = await supervisor.request({"command": "mask", **data})
        token = selected(result, attachment.identity)["token"]
        await supervisor.request({"command": "quiesced", "token": token, **data})
        await supervisor.reservations[attachment.identity].apply_task
        assert selected(supervisor.status(), attachment.identity)["state"] == "acquiring"
    elif phase == "disconnected":
        await start(supervisor, attachment)
        supervisor.backend.inventory.attachments.remove(attachment)
        await supervisor.monitor_once()
        assert (
            selected(supervisor.status(), attachment.identity)["reason"] == "hardware_disconnected"
        )
    backend = supervisor.reservations[attachment.identity].backend if phase != "no_masks" else None
    if backend is not None:
        assert await asyncio.to_thread(backend.early.exists)
        assert await asyncio.to_thread(backend.late.exists)

    manager = DeviceManager()
    masking = HardwareMasking(manager)
    masking.coordinator = supervisor
    masking.initialized = True
    manager.masking_recovery = masking.restore
    assert not manager.masked_hardware_paths
    await manager.emergency_reset()
    assert manager.masking_suspended
    assert supervisor.status()["remapping_suspended"]
    if backend is not None:
        assert not await asyncio.to_thread(backend.early.exists)
        assert not await asyncio.to_thread(backend.late.exists)
        assert not await asyncio.to_thread(backend.journal.exists)
        assert selected(supervisor.status(), attachment.identity)["state"] == "restored"


@pytest.mark.asyncio
async def test_same_model_masks_restore_independently(supervisor):
    first, second, third = supervisor.backend.inventory.scan()
    data = await start(supervisor, first)
    await start(supervisor, second)
    await start(supervisor, third)
    backends = [item.backend for item in supervisor.reservations.values()]
    assert len({item.early for item in backends}) == 3
    assert len({item.journal for item in backends}) == 3
    result = await supervisor.request({"command": "restore", **data})
    assert selected(result, first.identity)["state"] == "restoring"
    await supervisor.reservations[first.identity].recovery_task
    result = await supervisor.request({"command": "inventory"})
    assert selected(result, first.identity)["state"] == "restored"
    assert selected(result, second.identity)["state"] == "masked"
    assert selected(result, third.identity)["state"] == "masked"
    assert not result["remapping_suspended"]
    assert not backends[0].journal.exists()
    assert all(
        item.journal.exists() and item.early.exists() and item.late.exists()
        for item in backends[1:]
    )


@pytest.mark.asyncio
async def test_wrong_device_token_cannot_confirm_or_restore_another_mask(supervisor):
    first, second, _ = supervisor.backend.inventory.scan()
    first_data = await start(supervisor, first, keep=False)
    second_data = await start(supervisor, second, keep=False)
    for command in ("keep", "restore", "quiesced", "ready"):
        with pytest.raises(ValueError):
            await supervisor.request(
                {"command": command, **second_data, "token": first_data["token"]}
            )
    assert all(item.state["state"] == "trial" for item in supervisor.reservations.values())


@pytest.mark.asyncio
async def test_disconnect_and_trial_expiry_leave_other_masks_running(supervisor):
    clock = [100.0]
    supervisor.clock = lambda: clock[0]
    # Renew the lease in the fake clock's time domain, independent of host uptime.
    await supervisor.request({"command": "poll"})
    first, second, third = supervisor.backend.inventory.scan()
    await start(supervisor, first)
    await start(supervisor, second)
    await start(supervisor, third, keep=False)
    supervisor.backend.inventory.attachments.remove(first)
    await supervisor.monitor_once()
    assert selected(supervisor.status(), first.identity)["reason"] == "hardware_disconnected"
    assert selected(supervisor.status(), second.identity)["state"] == "masked"
    clock[0] += 31
    await supervisor.request({"command": "poll"})
    await supervisor.monitor_once()
    assert selected(supervisor.status(), third.identity)["reason"] == "trial_expired"
    assert selected(supervisor.status(), second.identity)["state"] == "masked"
    supervisor.backend.inventory.attachments.append(first)
    await supervisor.request({"command": "resume", "id": first.identity})
    await supervisor.request({"command": "poll"})
    result = supervisor.status()
    assert selected(result, first.identity)["state"] == "applying"
    assert selected(result, second.identity)["state"] == "masked"
    assert selected(result, third.identity)["state"] == "restored"


@pytest.mark.asyncio
async def test_saved_masks_restart_independently_and_belong_to_user(supervisor):
    first, second, third = supervisor.backend.inventory.scan()
    await start(supervisor, first)
    await start(supervisor, second)
    await start(supervisor, third, persist=False)
    await supervisor.restore("lifecycle_stop")
    fresh = MaskSupervisor(supervisor.backend)
    await fresh.initialize()
    result = await fresh.request({"command": "startup", "uid": 1001})
    assert not any(item["state"] == "applying" for item in result["masks"])
    result = await fresh.request({"command": "startup", "uid": 1000})
    assert selected(result, first.identity)["state"] == "applying"
    assert selected(result, second.identity)["state"] == "applying"
    assert selected(result, third.identity)["state"] == "restored"
    await fresh.restore("service_stopped")


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["lifecycle_stop", "service_stopped", "user_restore"])
async def test_restart_uses_original_reason_after_failed_recovery(supervisor, reason):
    first = supervisor.backend.inventory.scan()[0]
    await start(supervisor, first)
    await supervisor.reservations[first.identity].restore(reason)
    suspended = supervisor.reservations[first.identity].backend.state_dir / "suspended"
    suspended.write_text("recovery_failed\n")
    fresh = MaskSupervisor(supervisor.backend)
    await fresh.initialize()
    result = selected(await fresh.request({"command": "startup", "uid": 1000}), first.identity)
    if reason == "user_restore":
        assert result["state"] == "restored"
        assert result["reason"] == "user_restore"
        assert result["remapping_suspended"]
    else:
        assert result["state"] == "applying"
        assert not result["remapping_suspended"]
        item = fresh.reservations[first.identity]
        await item.request({"command": "quiesced", "token": result["token"]})
        assert item.apply_task is not None
        await item.apply_task
        result = await item.request({"command": "ready", "token": result["token"]})
        assert result["state"] == "masked"
    await fresh.restore("service_stopped")


@pytest.mark.asyncio
async def test_failed_user_restore_keeps_its_reason_through_shutdown(supervisor, monkeypatch):
    first = supervisor.backend.inventory.scan()[0]
    await start(supervisor, first)
    item = supervisor.reservations[first.identity]
    original = item.backend.recover

    async def fail():
        raise OSError("udev recovery failed")

    monkeypatch.setattr(item.backend, "recover", fail)
    with pytest.raises(OSError, match="udev recovery failed"):
        await item.restore("user_restore")
    assert (item.backend.state_dir / "suspended").read_text().strip() == "user_restore"
    monkeypatch.setattr(item.backend, "recover", original)
    await item.restore("lifecycle_stop")
    fresh = MaskSupervisor(supervisor.backend)
    await fresh.initialize()
    result = selected(await fresh.request({"command": "startup", "uid": 1000}), first.identity)
    assert result["state"] == "restored"
    assert result["reason"] == "user_restore"
    assert result["remapping_suspended"]
    await fresh.restore("service_stopped")


@pytest.mark.asyncio
async def test_a_slow_restore_does_not_block_other_trials_or_heartbeats(supervisor, monkeypatch):
    first, second, _ = supervisor.backend.inventory.scan()
    data = await start(supervisor, first)
    entered, finish = asyncio.Event(), asyncio.Event()
    item = supervisor.reservations[first.identity]
    recover = item.backend.recover

    async def delayed():
        entered.set()
        await finish.wait()
        await recover()

    monkeypatch.setattr(item.backend, "recover", delayed)
    await supervisor.request({"command": "restore", **data})
    await entered.wait()
    try:
        result = await asyncio.wait_for(supervisor.request({"command": "poll"}), 1)
        assert selected(result, first.identity)["state"] == "restoring"
        await asyncio.wait_for(start(supervisor, second), 1)
        assert selected(supervisor.status(), second.identity)["state"] == "masked"
    finally:
        finish.set()
        await item.recovery_task


@pytest.mark.asyncio
async def test_no_fixed_device_count_limit(tmp_path):
    backend = Backend(
        Inventory(tmp_path, 80), tmp_path / "run", tmp_path / "rules", tmp_path / "state"
    )
    supervisor = MaskSupervisor(backend)
    await supervisor.initialize()
    await supervisor.request({"command": "startup", "uid": 1000})
    try:
        for attachment in backend.inventory.scan():
            await start(supervisor, attachment)
        result = await supervisor.request({"command": "inventory"})
        assert len(result["devices"]) == len(result["masks"]) == 80
        assert all(item["state"] == "masked" for item in result["masks"])
    finally:
        await supervisor.restore("service_stopped")


@pytest.mark.asyncio
async def test_clean_owner_close_preserves_saved_and_explicitly_stopped_masks(supervisor):
    first, second, _ = supervisor.backend.inventory.scan()
    data = await start(supervisor, first)
    await start(supervisor, second)
    await supervisor.request({"command": "restore", **data})
    await supervisor.reservations[first.identity].recovery_task
    async with supervisor.reservations[second.identity].operation_lock:
        cleanup = asyncio.create_task(supervisor.restore("service_stopped"))
        await asyncio.sleep(0)
        disconnected = asyncio.create_task(supervisor.owner_disconnected())
        await asyncio.sleep(0)
    await asyncio.gather(cleanup, disconnected)
    assert selected(supervisor.status(), first.identity)["reason"] == "user_restore"
    assert selected(supervisor.status(), first.identity)["remapping_suspended"]
    assert not selected(supervisor.status(), second.identity)["remapping_suspended"]
    fresh = MaskSupervisor(supervisor.backend)
    await fresh.initialize()
    result = await fresh.request({"command": "startup", "uid": 1000})
    assert selected(result, first.identity)["state"] == "restored"
    assert selected(result, second.identity)["state"] == "applying"
    await fresh.restore("service_stopped")


@pytest.mark.asyncio
async def test_switch_off_disables_saved_choice_and_switch_on_reuses_confirmation(supervisor):
    first, _, _ = supervisor.backend.inventory.scan()
    data = await start(supervisor, first)
    await supervisor.request({"command": "restore", **data, "persist": False})
    await supervisor.reservations[first.identity].recovery_task
    assert not selected(supervisor.status(), first.identity)["enabled"]
    assert not selected(supervisor.status(), first.identity)["persist"]
    await supervisor.request({"command": "poll"})
    assert selected(supervisor.status(), first.identity)["state"] == "restored"
    result = await supervisor.request(
        {"command": "mask", "id": first.identity, "generation": first.generation, "persist": True}
    )
    current = selected(result, first.identity)
    assert current["automatic"]
    assert current["enabled"]
    await supervisor.request(
        {"command": "quiesced", "id": first.identity, "token": current["token"]}
    )
    await supervisor.reservations[first.identity].apply_task
    result = await supervisor.request(
        {"command": "ready", "id": first.identity, "token": current["token"]}
    )
    assert selected(result, first.identity)["state"] == "masked"


@pytest.mark.asyncio
async def test_saved_switch_can_be_enabled_and_disabled_while_disconnected(supervisor):
    first, _, _ = supervisor.backend.inventory.scan()
    data = await start(supervisor, first)
    supervisor.backend.inventory.attachments.remove(first)
    await supervisor.monitor_once()
    assert selected(supervisor.status(), first.identity)["enabled"]
    await supervisor.request({"command": "restore", **data, "persist": False})
    await supervisor.reservations[first.identity].recovery_task
    assert not selected(supervisor.status(), first.identity)["enabled"]
    result = await supervisor.request({"command": "mask", "id": first.identity, "persist": True})
    assert selected(result, first.identity)["enabled"]
    assert selected(result, first.identity)["state"] == "restored"
    supervisor.backend.inventory.attachments.append(first)
    await supervisor.request({"command": "poll"})
    assert selected(supervisor.status(), first.identity)["state"] == "applying"


@pytest.mark.asyncio
async def test_unmask_all_disables_saved_masks_without_pausing_remapping(supervisor):
    for attachment in supervisor.backend.inventory.scan():
        await start(supervisor, attachment)
    result = await supervisor.request({"command": "restore", "persist": False})
    assert not result["remapping_suspended"]
    assert all(not mask["enabled"] and not mask["persist"] for mask in result["masks"])
    await supervisor.request({"command": "poll"})
    assert all(mask["state"] == "restored" for mask in supervisor.status()["masks"])


@pytest.mark.asyncio
async def test_offline_enable_requires_prior_confirmation_for_the_authenticated_user(supervisor):
    first, second, _ = supervisor.backend.inventory.scan()
    with pytest.raises(ValueError, match="first time"):
        await supervisor.request({"command": "mask", "id": second.identity, "persist": True})
    data = await start(supervisor, first)
    await supervisor.request({"command": "restore", **data, "persist": False})
    await supervisor.reservations[first.identity].recovery_task
    await supervisor.request({"command": "startup", "uid": 1001})
    with pytest.raises(ValueError, match="first time"):
        await supervisor.request({"command": "mask", "id": first.identity, "persist": True})


@pytest.mark.asyncio
async def test_usb_disconnect_keeps_access_rules_until_automatic_reacquisition(supervisor):
    first, second, _ = supervisor.backend.inventory.scan()
    await start(supervisor, first)
    await start(supervisor, second)
    backend = supervisor.reservations[first.identity].backend
    rules = backend.early.read_text()
    supervisor.backend.inventory.attachments.remove(first)
    await supervisor.monitor_once()
    state = selected(supervisor.status(), first.identity)
    assert not state["remapping_suspended"]
    assert backend.early.read_text() == rules
    assert backend.late.exists()
    supervisor.backend.inventory.attachments.append(first)
    await supervisor.request({"command": "poll"})
    assert selected(supervisor.status(), first.identity)["state"] == "applying"
    assert selected(supervisor.status(), second.identity)["state"] == "masked"
    assert backend.early.read_text() == rules


@pytest.mark.asyncio
@pytest.mark.parametrize("reader_first", [False, True])
async def test_receiver_controller_connection_keeps_rules_during_reacquisition(
    supervisor, monkeypatch, reader_first
):
    first, second, _ = supervisor.backend.inventory.scan()
    data = await start(supervisor, first)
    await start(supervisor, second)
    backend = supervisor.reservations[first.identity].backend
    rules = backend.early.read_text()
    original = supervisor.backend.inventory.endpoint_roles
    monkeypatch.setattr(
        supervisor.backend.inventory,
        "endpoint_roles",
        lambda attachment: (
            original(attachment)
            + (["controller:event"] if attachment.identity == first.identity else [])
        ),
    )
    if reader_first:
        await supervisor.request(
            {"command": "restore", **data, "reason": "replacement_unavailable"}
        )
    else:
        await supervisor.monitor_once()
    state = selected(supervisor.status(), first.identity)
    assert state["state"] == "applying"
    assert not state["remapping_suspended"]
    assert backend.early.read_text() == rules
    assert backend.late.exists()
    await supervisor.request({"command": "poll"})
    assert selected(supervisor.status(), first.identity)["state"] == "applying"
    assert selected(supervisor.status(), second.identity)["state"] == "masked"
    token = state["token"]
    assert token != data["token"]
    await supervisor.request({"command": "quiesced", "id": first.identity, "token": token})
    await supervisor.reservations[first.identity].apply_task
    await supervisor.request({"command": "ready", "id": first.identity, "token": token})
    assert selected(supervisor.status(), first.identity)["state"] == "masked"
    assert "controller:event" in selected(supervisor.status(), first.identity)["endpoint_roles"]
    assert backend.early.read_text() == rules


@pytest.mark.asyncio
async def test_owner_loss_disarms_offline_usb_devices(supervisor):
    first, _, _ = supervisor.backend.inventory.scan()
    await start(supervisor, first)
    supervisor.backend.inventory.attachments.remove(first)
    await supervisor.monitor_once()
    backend = supervisor.reservations[first.identity].backend
    assert backend.armed
    await supervisor.owner_disconnected()
    assert not backend.armed


@pytest.mark.asyncio
async def test_usb_generation_change_during_activation_is_not_a_disconnect(supervisor, monkeypatch):
    from dataclasses import replace

    first, _, _ = supervisor.backend.inventory.scan()
    item = await supervisor.reservation(first.identity)
    entered, finish = asyncio.Event(), asyncio.Event()
    original = item.backend.activate

    async def cycle(attachment):
        current = replace(attachment, generation="2:200")
        supervisor.backend.inventory.attachments[0] = current
        entered.set()
        await finish.wait()
        item.backend.active_attachment = current
        return await original(current)

    monkeypatch.setattr(item.backend, "activate", cycle)
    result = await supervisor.request(
        {"command": "mask", "id": first.identity, "generation": first.generation}
    )
    token = selected(result, first.identity)["token"]
    await supervisor.request({"command": "quiesced", "id": first.identity, "token": token})
    await entered.wait()
    try:
        await supervisor.monitor_once()
        assert item.state["state"] == "applying"
    finally:
        finish.set()
        await item.apply_task
    assert item.state["generation"] == "2:200"
    assert item.state["state"] == "acquiring"
    await supervisor.monitor_once()
    assert item.state["state"] == "acquiring"
