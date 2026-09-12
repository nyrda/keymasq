import asyncio
import json
from pathlib import Path

import pytest

from keymasq.masking.service import TRIAL_SECONDS, MaskSupervisor
from tests.common.test_hardware_masking import FakeBackend, begin


async def confirm(supervisor: MaskSupervisor, *, persist: bool = True) -> None:
    trial = await begin(supervisor)
    await supervisor.request({"command": "ready", "token": trial["token"]})
    await supervisor.request({"command": "keep", "token": trial["token"], "persist": persist})


async def restart(supervisor: MaskSupervisor, *, uid: int = 1000) -> MaskSupervisor:
    fresh = MaskSupervisor(supervisor.backend, supervisor.clock)
    await fresh.load_policy()
    await fresh.request({"command": "startup", "uid": uid})
    if fresh.apply_task:
        await fresh.apply_task
    return fresh


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["lifecycle_stop", "service_stopped"])
async def test_confirmed_mask_restarts_without_gui_and_uses_current_generation(
    tmp_path: Path, reason: str
):
    supervisor = MaskSupervisor(FakeBackend(tmp_path))
    trial = await begin(supervisor)
    await supervisor.request({"command": "ready", "token": trial["token"]})
    # Omitting the new option must default to enabled.
    await supervisor.request({"command": "keep", "token": trial["token"]})
    await supervisor.restore(reason, stop_owner=False)
    attachment = supervisor.backend.inventory.scan()[0]
    (attachment.syspath / "devnum").write_text("8")
    fresh = await restart(supervisor)
    assert fresh.state["generation"] != trial["generation"]
    assert fresh.state["automatic"] is True
    assert fresh.state["state"] == "acquiring"
    result = await fresh.request({"command": "ready", "token": fresh.state["token"]})
    assert result["state"] == "masked"
    assert result["remapping_suspended"] is False


@pytest.mark.asyncio
async def test_unconfirmed_trial_never_creates_startup_intent(tmp_path: Path):
    supervisor = MaskSupervisor(FakeBackend(tmp_path))
    await begin(supervisor)
    await supervisor.restore("lifecycle_stop", stop_owner=False)
    fresh = await restart(supervisor)
    assert not fresh.active
    assert not fresh.policy
    assert fresh.status()["remapping_suspended"] is True


@pytest.mark.asyncio
async def test_disabling_persistence_keeps_live_output_but_does_not_restart(tmp_path: Path):
    supervisor = MaskSupervisor(FakeBackend(tmp_path))
    await confirm(supervisor)
    result = await supervisor.request(
        {
            "command": "persistence",
            "token": supervisor.state["token"],
            "persist": False,
        }
    )
    assert result["state"] == "masked"
    await supervisor.restore("lifecycle_stop", stop_owner=False)
    fresh = await restart(supervisor)
    assert not fresh.active
    assert fresh.status()["persist"] is False


@pytest.mark.asyncio
async def test_opt_out_at_confirmation_is_saved(tmp_path: Path):
    supervisor = MaskSupervisor(FakeBackend(tmp_path))
    await confirm(supervisor, persist=False)
    await supervisor.restore("lifecycle_stop", stop_owner=False)
    assert not (await restart(supervisor)).active


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["user_restore", "admin_restore", "daemon_unresponsive", "daemon_disconnected"]
)
async def test_escape_hatch_survives_restarts_with_persistence_enabled(tmp_path: Path, reason: str):
    supervisor = MaskSupervisor(FakeBackend(tmp_path))
    await confirm(supervisor)
    await supervisor.restore(reason, stop_owner=False)
    fresh = await restart(supervisor)
    await fresh.request({"command": "heartbeat"})
    assert not fresh.active
    assert fresh.status()["persist"] is True
    assert fresh.status()["remapping_suspended"] is True
    # A normal service stop must not clear the emergency latch.
    await fresh.restore("service_stopped", stop_owner=False)
    assert not (await restart(fresh)).active
    await fresh.request({"command": "resume"})
    await fresh.request({"command": "heartbeat"})
    assert fresh.apply_task
    await fresh.apply_task
    assert fresh.state["state"] == "acquiring"


@pytest.mark.asyncio
async def test_saved_mask_only_starts_for_its_authenticated_user(tmp_path: Path):
    supervisor = MaskSupervisor(FakeBackend(tmp_path))
    await confirm(supervisor)
    await supervisor.restore("lifecycle_stop", stop_owner=False)
    fresh = await restart(supervisor, uid=1001)
    assert not fresh.active
    with pytest.raises(ValueError, match="another user"):
        await fresh.request({"command": "persistence", "id": fresh.policy["id"], "persist": False})
    await fresh.request({"command": "startup", "uid": 1000})
    assert fresh.apply_task
    await fresh.apply_task
    assert fresh.active


@pytest.mark.asyncio
async def test_automatic_acquisition_timeout_pauses_until_explicit_resume(tmp_path: Path):
    clock = [100.0]
    supervisor = MaskSupervisor(FakeBackend(tmp_path), lambda: clock[0])
    await confirm(supervisor)
    await supervisor.restore("lifecycle_stop", stop_owner=False)
    fresh = await restart(supervisor)
    clock[0] += TRIAL_SECONDS
    await fresh.request({"command": "heartbeat"})
    await fresh.monitor_once()
    assert fresh.status()["remapping_suspended"] is True
    assert not (await restart(fresh)).active


@pytest.mark.asyncio
async def test_failed_automatic_activation_does_not_retry_on_next_restart(tmp_path: Path):
    supervisor = MaskSupervisor(FakeBackend(tmp_path))
    await confirm(supervisor)
    await supervisor.restore("lifecycle_stop", stop_owner=False)
    supervisor.backend.fail_activation = True
    fresh = await restart(supervisor)
    await fresh.monitor_once()
    assert not (await restart(fresh)).active


@pytest.mark.asyncio
async def test_malformed_confirmation_cannot_bypass_trial(tmp_path: Path):
    supervisor = MaskSupervisor(FakeBackend(tmp_path))
    trial = await begin(supervisor)
    await supervisor.request({"command": "ready", "token": trial["token"]})
    with pytest.raises(ValueError, match="true or false"):
        await supervisor.request({"command": "keep", "token": trial["token"], "persist": "false"})
    assert supervisor.state["state"] == "trial"
    assert not supervisor.policy


@pytest.mark.asyncio
async def test_remembered_display_state_is_not_startup_authorization(tmp_path: Path):
    supervisor = MaskSupervisor(FakeBackend(tmp_path))
    (supervisor.backend.state_dir / "selection.json").write_text(
        json.dumps(
            {
                "id": supervisor.backend.inventory.scan()[0].identity,
                "state": "masked",
            }
        )
    )
    fresh = await restart(supervisor)
    assert not fresh.active
    await fresh.restore("admin_restore", stop_owner=False)
    assert fresh.status()["remapping_suspended"] is True


@pytest.mark.asyncio
async def test_recovery_waiting_on_startup_cannot_be_undone_by_its_heartbeat(tmp_path: Path):
    supervisor = MaskSupervisor(FakeBackend(tmp_path))
    await confirm(supervisor)
    await supervisor.restore("lifecycle_stop", stop_owner=False)
    async with supervisor.operation_lock:
        heartbeat = asyncio.create_task(supervisor.request({"command": "heartbeat"}))
        await asyncio.sleep(0)
        recovery = asyncio.create_task(supervisor.restore("admin_restore", stop_owner=False))
        await asyncio.sleep(0)
    await asyncio.gather(heartbeat, recovery)
    assert not supervisor.active
    await supervisor.request({"command": "heartbeat"})
    assert not supervisor.active
    assert not (await restart(supervisor)).active


@pytest.mark.asyncio
async def test_supervisor_stop_does_not_mistake_its_own_daemon_termination_for_failure(
    tmp_path: Path,
):
    supervisor = MaskSupervisor(FakeBackend(tmp_path))
    await confirm(supervisor)
    async with supervisor.operation_lock:
        cleanup = asyncio.create_task(supervisor.restore("service_stopped", stop_owner=False))
        await asyncio.sleep(0)
        disconnected = asyncio.create_task(supervisor.owner_disconnected())
        await asyncio.sleep(0)
    await asyncio.gather(cleanup, disconnected)
    assert supervisor.status()["remapping_suspended"] is False
    assert (await restart(supervisor)).active
