"""Saved masks converge on connected hardware without hammering privileged jobs."""

import asyncio
import json
import math

import pytest

from keymasq.masking.backend import MaskOperationError, save_json
from keymasq.masking.coordinator import (
    INITIAL_RETRY_DELAY_S,
    MAX_RETRY_DELAY_S,
    MaskCoordinator,
)
from tests.common.test_multiple_hardware_masks import Backend, Inventory, selected, start


@pytest.fixture
async def clocked(tmp_path):
    clock = [1000.0]
    backend = Backend(Inventory(tmp_path), tmp_path / "run", tmp_path / "rules", tmp_path / "state")
    supervisor = MaskCoordinator(backend, lambda: clock[0])
    await supervisor.initialize()
    await supervisor.request({"command": "startup", "uid": 1000})
    yield supervisor, clock
    await supervisor.restore("service_stopped")


async def confirmed_and_offline(supervisor, attachment):
    """A confirmed USB mask whose device is unplugged and whose rules are gone."""
    data = await start(supervisor, attachment)
    supervisor.backend.inventory.attachments.remove(attachment)
    await supervisor.monitor_once()
    await supervisor.request({"command": "restore", **data, "persist": False})
    await supervisor.reservations[attachment.identity].recovery_task
    item = supervisor.reservations[attachment.identity]
    assert not item.backend.armed
    return item


@pytest.mark.asyncio
async def test_missing_root_selector_stops_offline_arming_until_reconnected(clocked, monkeypatch):
    supervisor, clock = clocked
    first, *_ = supervisor.backend.inventory.scan()
    item = await confirmed_and_offline(supervisor, first)
    calls = []

    async def install_rules(self, attachment):
        calls.append(attachment.identity)

    monkeypatch.setattr(Backend, "install_rules", install_rules)
    result = await supervisor.request({"command": "mask", "id": first.identity, "persist": True})
    mask = selected(result, first.identity)
    assert mask["enabled"] and mask["state"] == "restored"
    assert mask["lifecycle"] == "saved_incomplete"
    assert mask["attention_code"] == "selector_missing"
    assert mask["next_retry_seconds"] == 0
    assert calls == []
    for _ in range(5):
        clock[0] += 60
        await supervisor.request({"command": "poll"})
    assert calls == []
    assert math.isinf(item.next_attempt)
    # Reconnecting is new information: activation records the selector itself.
    supervisor.backend.inventory.attachments.append(first)
    result = await supervisor.request({"command": "poll"})
    mask = selected(result, first.identity)
    assert mask["state"] == "applying" and mask["lifecycle"] == "activating"
    assert mask["attention_code"] == ""
    assert "error" not in mask
    assert calls == []


@pytest.mark.asyncio
async def test_invalid_root_selector_is_terminal_offline(clocked, monkeypatch):
    supervisor, clock = clocked
    first, *_ = supervisor.backend.inventory.scan()
    item = await confirmed_and_offline(supervisor, first)
    (item.backend.state_dir / "selector.json").write_text("{}")
    monkeypatch.setattr(item.backend.inventory, "from_selector", lambda selector: 1 / 0)
    install = []
    monkeypatch.setattr(Backend, "install_rules", lambda self, a: install.append(a))

    def broken(selector):
        raise ValueError("Invalid saved attachment path")

    monkeypatch.setattr(item.backend.inventory, "from_selector", broken)
    result = await supervisor.request({"command": "mask", "id": first.identity, "persist": True})
    assert selected(result, first.identity)["attention_code"] == "selector_invalid"
    assert selected(result, first.identity)["lifecycle"] == "saved_incomplete"
    clock[0] += MAX_RETRY_DELAY_S * 2
    await supervisor.request({"command": "poll"})
    assert install == []


@pytest.mark.asyncio
async def test_transient_arm_failure_backs_off_and_resets_on_success(clocked, monkeypatch):
    supervisor, clock = clocked
    first, *_ = supervisor.backend.inventory.scan()
    item = await confirmed_and_offline(supervisor, first)
    save_json(item.backend.state_dir / "selector.json", item.backend.inventory.selector(first))
    attempts = []
    failing = [True]

    async def install_rules(self, attachment):
        attempts.append(clock[0])
        if failing[0]:
            raise MaskOperationError("job_failed", "systemctl failed: unit not found")
        self.early.write_text(attachment.identity)
        self.late.write_text(attachment.identity)

    monkeypatch.setattr(Backend, "install_rules", install_rules)
    result = await supervisor.request({"command": "mask", "id": first.identity, "persist": True})
    mask = selected(result, first.identity)
    assert mask["lifecycle"] == "attention"
    assert mask["attention_code"] == "job_failed"
    assert mask["next_retry_seconds"] == INITIAL_RETRY_DELAY_S
    assert "unit not found" in mask["error"]
    assert len(attempts) == 1
    # Heartbeats inside the delay do not start privileged jobs.
    clock[0] += 1
    await supervisor.request({"command": "poll"})
    assert len(attempts) == 1
    clock[0] += 1
    await supervisor.request({"command": "poll"})
    assert len(attempts) == 2
    assert item.next_attempt == clock[0] + INITIAL_RETRY_DELAY_S * 2
    for _ in range(8):
        clock[0] = item.next_attempt
        await supervisor.request({"command": "poll"})
    assert item.retry_delay == MAX_RETRY_DELAY_S
    failing[0] = False
    clock[0] = item.next_attempt
    result = await supervisor.request({"command": "poll"})
    mask = selected(result, first.identity)
    assert item.backend.armed
    assert mask["lifecycle"] == "waiting_for_device"
    assert mask["attention_code"] == "" and "error" not in mask
    assert item.retry_delay == INITIAL_RETRY_DELAY_S


@pytest.mark.asyncio
async def test_failed_mask_request_retries_with_backoff_instead_of_pausing(clocked, monkeypatch):
    supervisor, clock = clocked
    first, *_ = supervisor.backend.inventory.scan()
    data = await start(supervisor, first)
    await supervisor.request({"command": "restore", **data, "persist": False})
    await supervisor.reservations[first.identity].recovery_task
    item = supervisor.reservations[first.identity]
    original = item.backend.inventory.validate
    rejected = [True]

    def validate(attachment):
        if rejected[0]:
            raise ValueError("Invalid physical attachment")
        original(attachment)

    monkeypatch.setattr(item.backend.inventory, "validate", validate)
    result = await supervisor.request({"command": "mask", "id": first.identity, "persist": True})
    mask = selected(result, first.identity)
    assert mask["lifecycle"] == "attention"
    assert mask["attention_code"] == "automatic_start_failed"
    assert not mask["remapping_suspended"]
    assert not (item.backend.state_dir / "suspended").exists()
    rejected[0] = False
    await supervisor.request({"command": "poll"})
    assert selected((await supervisor.status()), first.identity)["state"] == "restored"
    clock[0] += INITIAL_RETRY_DELAY_S
    result = await supervisor.request({"command": "poll"})
    assert selected(result, first.identity)["state"] == "applying"


@pytest.mark.asyncio
async def test_user_retry_resets_the_backoff(clocked, monkeypatch):
    supervisor, clock = clocked
    first, *_ = supervisor.backend.inventory.scan()
    item = await confirmed_and_offline(supervisor, first)
    save_json(item.backend.state_dir / "selector.json", item.backend.inventory.selector(first))
    attempts = []

    async def install_rules(self, attachment):
        attempts.append(clock[0])
        raise OSError("systemctl failed")

    monkeypatch.setattr(Backend, "install_rules", install_rules)
    await supervisor.request({"command": "mask", "id": first.identity, "persist": True})
    await supervisor.request({"command": "poll"})
    assert len(attempts) == 1
    await supervisor.request({"command": "mask", "id": first.identity, "persist": True})
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_one_scan_serves_every_reservation_and_hotplug_gates_rescans(clocked, monkeypatch):
    supervisor, clock = clocked
    scans = []
    original = supervisor.backend.inventory.scan
    monkeypatch.setattr(
        supervisor.backend.inventory, "scan", lambda: scans.append(clock[0]) or original()
    )
    for attachment in supervisor.backend.inventory.attachments[:]:
        await start(supervisor, attachment)
    for attachment in supervisor.backend.inventory.attachments[:]:
        supervisor.backend.inventory.attachments.remove(attachment)
    await supervisor.monitor_once()
    scans.clear()
    await supervisor.request({"command": "poll"})
    assert len(scans) == 1
    supervisor.scan_interval = 5.0
    scans.clear()
    await supervisor.request({"command": "poll"})
    clock[0] += 1
    await supervisor.request({"command": "poll"})
    assert scans == []
    supervisor.mark_hardware_changed()
    await supervisor.request({"command": "poll"})
    assert len(scans) == 1
    clock[0] += 5
    await supervisor.request({"command": "poll"})
    assert len(scans) == 2
    await supervisor.request({"command": "startup", "uid": 1000})
    assert len(scans) == 3


@pytest.mark.asyncio
async def test_presence_is_remembered_in_the_saved_policy(clocked):
    supervisor, _clock = clocked
    first, *_ = supervisor.backend.inventory.scan()
    item = await supervisor.reservation(first.identity)
    wall = [1_700_000_000.0]
    item.wall_clock = lambda: wall[0]
    data = await start(supervisor, first)
    policy = json.loads((item.backend.state_dir / "policy.json").read_text())
    assert policy["last_seen"] == wall[0]
    result = await supervisor.status()
    assert selected(result, first.identity)["last_seen"] == wall[0]
    supervisor.backend.inventory.attachments.remove(first)
    await supervisor.monitor_once()
    await supervisor.request({"command": "poll"})
    mask = selected((await supervisor.status()), first.identity)
    assert mask["lifecycle"] == "waiting_for_device"
    assert mask["last_seen"] == wall[0]
    wall[0] += 3600
    supervisor.backend.inventory.attachments.append(first)
    await supervisor.request({"command": "poll"})
    policy = json.loads((item.backend.state_dir / "policy.json").read_text())
    assert policy["last_seen"] == wall[0]
    data["token"] = selected((await supervisor.status()), first.identity)["token"]
    await supervisor.request({"command": "restore", **data, "persist": False})
    await supervisor.reservations[first.identity].recovery_task
    policy = json.loads((item.backend.state_dir / "policy.json").read_text())
    assert policy["persist"] is False and policy["last_seen"] == wall[0]


@pytest.mark.asyncio
async def test_lifecycle_reports_off_for_other_users_and_disabled_choices(clocked):
    supervisor, _clock = clocked
    first, *_ = supervisor.backend.inventory.scan()
    data = await start(supervisor, first)
    assert selected((await supervisor.status()), first.identity)["lifecycle"] == "masked"
    await supervisor.request({"command": "restore", **data, "persist": False})
    await supervisor.reservations[first.identity].recovery_task
    assert selected((await supervisor.status()), first.identity)["lifecycle"] == "off"
    await supervisor.request({"command": "persistence", "id": first.identity, "persist": True})
    await supervisor.request({"command": "startup", "uid": 1001})
    assert selected((await supervisor.status()), first.identity)["lifecycle"] == "off"


@pytest.mark.asyncio
async def test_invalid_last_seen_is_rejected_like_other_policy_damage(tmp_path):
    backend = Backend(Inventory(tmp_path), tmp_path / "run", tmp_path / "rules", tmp_path / "state")
    supervisor = MaskCoordinator(backend)
    first = backend.inventory.attachments[0]
    item = await supervisor.reservation(first.identity)
    save_json(
        item.backend.state_dir / "policy.json",
        {"id": first.identity, "owner_uid": 1000, "persist": True, "last_seen": "yesterday"},
    )
    fresh = MaskCoordinator(backend)
    await fresh.initialize()
    assert not fresh.reservations[first.identity].policy
    assert fresh.reservations[first.identity].state["reason"] == "invalid_saved_state"
    await asyncio.sleep(0)
