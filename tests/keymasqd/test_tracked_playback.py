import asyncio
from unittest.mock import MagicMock

import evdev
import pytest

from keymasq.common.ipc import CommandType
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.runtime.macro import controls


def setup_manager():
    manager = DeviceManager()
    manager.output_state.keyboard_uinput = MagicMock()
    events: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    async def broadcast(command, data):
        if command == CommandType.MACRO_PLAYBACK_FINISHED:
            events.put_nowait(data)

    manager.broadcast_callback = broadcast
    return manager, events


def key_events(code: int, duration: int) -> list[dict[str, object]]:
    return [
        {
            "type": evdev.ecodes.EV_KEY,
            "code": code,
            "value": 1,
            "t_us": 0,
            "device_type": "keyboard",
        },
        {
            "type": evdev.ecodes.EV_KEY,
            "code": code,
            "value": 0,
            "t_us": duration,
            "device_type": "keyboard",
        },
    ]


@pytest.mark.asyncio
async def test_completion_follows_key_release_and_task_cleanup() -> None:
    manager, events = setup_manager()
    await manager.play_macro(macro_events=key_events(evdev.ecodes.KEY_A, 1000), playback_id="first")
    result = await asyncio.wait_for(events.get(), 1)
    assert result == {"playback_id": "first", "state": "completed"}
    assert not manager.macro_state.tasks
    assert not manager.macro_state.held_refcount
    assert manager.output_state.keyboard_uinput.write.call_args_list[-1].args == (
        evdev.ecodes.EV_KEY,
        evdev.ecodes.KEY_A,
        0,
    )


@pytest.mark.asyncio
async def test_targeted_cancel_releases_only_target_and_acknowledges_once() -> None:
    manager, events = setup_manager()
    for playback_id, code in [("first", evdev.ecodes.KEY_A), ("second", evdev.ecodes.KEY_B)]:
        await manager.play_macro(macro_events=key_events(code, 10_000_000), playback_id=playback_id)
    async with asyncio.timeout(1):
        while len(manager.macro_state.held_refcount) < 2:
            await asyncio.sleep(0.001)
    await manager.cancel_macro_request("first")
    assert await asyncio.wait_for(events.get(), 1) == {"playback_id": "first", "state": "cancelled"}
    assert len(manager.macro_state.tasks) == 1
    assert ("keyboard", evdev.ecodes.KEY_B) in manager.macro_state.held_refcount
    assert ("keyboard", evdev.ecodes.KEY_A) not in manager.macro_state.held_refcount
    await manager.cancel_macro_request("second")
    assert (await asyncio.wait_for(events.get(), 1))["playback_id"] == "second"
    await manager.cancel_macro_request("first")
    assert events.empty()


@pytest.mark.asyncio
async def test_cancel_before_scheduler_starts_still_acknowledges() -> None:
    manager, events = setup_manager()
    await manager.play_macro(
        macro_events=key_events(evdev.ecodes.KEY_A, 10_000_000), playback_id="first"
    )
    await manager.cancel_macro_request("first")
    assert await asyncio.wait_for(events.get(), 1) == {"playback_id": "first", "state": "cancelled"}
    manager.output_state.keyboard_uinput.write.assert_not_called()
    assert not manager.macro_state.instance_meta


@pytest.mark.asyncio
async def test_runtime_failure_is_reported_after_cleanup(monkeypatch) -> None:
    manager, events = setup_manager()

    async def fail(*args, **kwargs):
        raise RuntimeError("control failed")

    monkeypatch.setattr(controls, "run_macro_control_action", fail)
    await manager.play_macro(
        macro_events=[{"macro_action": "wait", "t_us": 0, "duration_ms": 1}], playback_id="first"
    )
    result = await asyncio.wait_for(events.get(), 1)
    assert result["state"] == "failed"
    assert "control failed" in str(result["message"])
    assert not manager.macro_state.tasks


@pytest.mark.asyncio
async def test_tracked_toggle_requests_start_independent_instances() -> None:
    manager, events = setup_manager()
    for playback_id in ("first", "second"):
        await manager.play_macro(
            macro_events=key_events(evdev.ecodes.KEY_A, 10_000_000),
            playback_id=playback_id,
            loop_mode="toggle",
        )
    assert len(manager.macro_state.tasks) == 2
    await manager.cancel_macro_playback()
    results = [await asyncio.wait_for(events.get(), 1) for _ in range(2)]
    assert {result["playback_id"] for result in results} == {"first", "second"}
    assert all(result["state"] == "cancelled" for result in results)
