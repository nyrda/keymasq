"""Playback contracts for pause on release, including structured child calls."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import evdev
import pytest

from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.macro_store import MacroStore
from keymasq.keymasqd.runtime.macro.timing import MacroPauseState, MacroPlaybackTimeline


def key(code: int, value: int, t_us: int = 0) -> dict[str, object]:
    return dict(device_type="keyboard", type=evdev.ecodes.EV_KEY, code=code, value=value, t_us=t_us)


def writes(manager: DeviceManager) -> list[tuple[int, int, int]]:
    return [call.args for call in manager.output_state.keyboard_uinput.write.call_args_list]


async def until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.fixture
def manager(tmp_path: Path) -> DeviceManager:
    result = DeviceManager()
    result.output_state.keyboard_uinput = MagicMock()
    result.macro_store = MacroStore(tmp_path / "macros")
    return result


async def trigger(manager: DeviceManager, value: int, **kwargs: object) -> dict[str, object]:
    return await manager.play_macro(
        macro_name="parent",
        load_stored_macro=False,
        source_device="kbd",
        source_button="key_f13",
        trigger_value=value,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_pause_cleans_outputs_preserves_gap_and_skips_old_release(manager) -> None:
    events = [key(30, 1), key(30, 0, 150_000), key(48, 1, 150_000), key(48, 0, 150_000)]
    await trigger(manager, 1, macro_events=events, loop_stop_behavior="pause_run")
    await until(lambda: len(writes(manager)) == 1)
    await trigger(manager, 0)
    assert writes(manager) == [(1, 30, 1), (1, 30, 0)]
    await asyncio.sleep(0.2)
    assert len(writes(manager)) == 2
    assert (await trigger(manager, 1, macro_events=events))["resumed"] is True
    await asyncio.sleep(0.04)
    assert len(writes(manager)) == 2
    await until(lambda: not manager.macro_state.tasks)
    assert writes(manager) == [(1, 30, 1), (1, 30, 0), (1, 48, 1), (1, 48, 0)]
    assert not manager.macro_state.pauses
    assert not manager.macro_state.pause_released


@pytest.mark.asyncio
async def test_fresh_press_after_cleanup_has_its_own_release(manager) -> None:
    await trigger(
        manager,
        1,
        loop_stop_behavior="pause_run",
        macro_events=[
            key(30, 1),
            key(30, 2, 50_000),
            key(30, 1, 50_000),
            key(30, 0, 50_000),
        ],
    )
    await until(lambda: len(writes(manager)) == 1)
    await trigger(manager, 0)
    await trigger(manager, 1)
    await until(lambda: not manager.macro_state.tasks)
    assert writes(manager) == [(1, 30, 1), (1, 30, 0), (1, 30, 1), (1, 30, 0)]


@pytest.mark.asyncio
@pytest.mark.parametrize("pause_seconds", [0.04, 0.3])
async def test_explicit_wait_counts_time_paused(manager, pause_seconds) -> None:
    await trigger(
        manager,
        1,
        loop_stop_behavior="pause_run",
        macro_events=[
            key(30, 1),
            {"t_us": 0, "macro_action": "wait", "duration_us": 200_000},
            key(30, 0),
            key(48, 1),
            key(48, 0),
        ],
    )
    await until(lambda: len(writes(manager)) == 1)
    await asyncio.sleep(0.03)
    await trigger(manager, 0)
    await asyncio.sleep(pause_seconds)
    assert len(writes(manager)) == 2
    await trigger(manager, 1)
    if pause_seconds < 0.2:
        await asyncio.sleep(0.04)
        assert len(writes(manager)) == 2
    else:
        async with asyncio.timeout(0.1):
            await until(lambda: not manager.macro_state.tasks)
    await until(lambda: not manager.macro_state.tasks)
    assert writes(manager)[-2:] == [(1, 48, 1), (1, 48, 0)]


@pytest.mark.asyncio
@pytest.mark.parametrize("complete_while_paused", [True, False])
async def test_sync_exec_keeps_running_and_parent_stays_paused(
    manager, complete_while_paused
) -> None:
    manager.broadcast_callback = AsyncMock()
    await trigger(
        manager,
        1,
        loop_stop_behavior="pause_run",
        macro_events=[
            {"t_us": 0, "macro_action": "exec_sync", "command": "example", "timeout_ms": 1000},
            key(30, 1),
            key(30, 0),
        ],
    )
    await until(lambda: bool(manager.macro_state.exec_waiters))
    waiter = next(iter(manager.macro_state.exec_waiters.values()))
    await trigger(manager, 0)
    if complete_while_paused:
        waiter.set_result(0)
        await asyncio.sleep(0.02)
        assert not writes(manager)
    await trigger(manager, 1)
    if not complete_while_paused:
        await asyncio.sleep(0.02)
        assert not writes(manager)
        waiter.set_result(0)
    await until(lambda: not manager.macro_state.tasks)
    assert len(manager.broadcast_callback.call_args_list) == 1
    assert writes(manager) == [(1, 30, 1), (1, 30, 0)]


@pytest.mark.asyncio
async def test_natural_move_finishes_while_paused(manager, monkeypatch) -> None:
    started, finished = asyncio.Event(), asyncio.Event()

    async def move(*_args):
        started.set()
        await finished.wait()
        return {"status": "ok"}

    monkeypatch.setattr(manager, "move_cursor_natural", move)
    await trigger(
        manager,
        1,
        loop_stop_behavior="pause_run",
        macro_events=[
            {"t_us": 0, "macro_action": "mouse_move_natural_abs", "x": 100, "y": 200},
            key(30, 1),
            key(30, 0),
        ],
    )
    await started.wait()
    await trigger(manager, 0)
    finished.set()
    await asyncio.sleep(0.02)
    assert not writes(manager)
    await trigger(manager, 1)
    await until(lambda: not manager.macro_state.tasks)
    assert len(writes(manager)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("call_mode", ["macro_sync", "macro_parallel"])
async def test_only_child_pauses_and_repress_resumes_without_new_parent(manager, call_mode) -> None:
    manager.macro_store.create(
        {
            "name": "child",
            "events": [
                key(30, 1),
                key(30, 0, 70_000),
                key(48, 1, 70_000),
                key(48, 0, 70_000),
            ],
        }
    )
    await trigger(
        manager,
        1,
        macro_events=[
            {
                "t_us": 0,
                "macro_action": call_mode,
                "macro_name": "child",
                "loop_stop_behavior": "pause_run",
            },
            key(46, 1),
            key(46, 0),
        ],
    )
    await until(lambda: (1, 30, 1) in writes(manager))
    await trigger(manager, 0)
    await asyncio.sleep(0.1)
    assert (1, 48, 1) not in writes(manager)
    assert ((1, 46, 1) in writes(manager)) == (call_mode == "macro_parallel")
    assert len(manager.macro_state.tasks) == 2
    sequence = manager.macro_state.instance_seq
    assert (await trigger(manager, 1))["resumed"] is True
    assert manager.macro_state.instance_seq == sequence
    await until(lambda: not manager.macro_state.tasks)
    assert writes(manager).count((1, 30, 1)) == 1
    assert writes(manager).count((1, 46, 1)) == 1


@pytest.mark.asyncio
async def test_parent_pause_leaves_ordinary_child_and_its_outputs_running(manager) -> None:
    manager.macro_store.create({"name": "child", "events": [key(30, 1), key(30, 0, 80_000)]})
    await trigger(
        manager,
        1,
        loop_stop_behavior="pause_run",
        macro_events=[
            {"t_us": 0, "macro_action": "macro_sync", "macro_name": "child"},
            key(48, 1),
            key(48, 0),
        ],
    )
    await until(lambda: bool(writes(manager)))
    await trigger(manager, 0)
    assert writes(manager) == [(1, 30, 1)]
    await until(lambda: (1, 30, 0) in writes(manager))
    assert writes(manager) == [(1, 30, 1), (1, 30, 0)]
    await trigger(manager, 1)
    await until(lambda: not manager.macro_state.tasks)
    assert writes(manager)[-2:] == [(1, 48, 1), (1, 48, 0)]


@pytest.mark.asyncio
async def test_child_reached_after_release_starts_paused(manager) -> None:
    manager.macro_store.create({"name": "child", "events": [key(30, 1), key(30, 0)]})
    await trigger(
        manager,
        1,
        macro_events=[
            {
                "t_us": 60_000,
                "macro_action": "macro_sync",
                "macro_name": "child",
                "loop_stop_behavior": "pause_run",
            },
        ],
    )
    await trigger(manager, 0)
    await until(lambda: bool(manager.macro_state.pauses))
    assert not writes(manager)
    await trigger(manager, 1)
    await until(lambda: not manager.macro_state.tasks)
    assert writes(manager) == [(1, 30, 1), (1, 30, 0)]


@pytest.mark.asyncio
async def test_cancel_reaps_paused_subtree(manager) -> None:
    manager.macro_store.create({"name": "child", "events": [key(30, 1), key(30, 0, 50_000)]})
    await trigger(
        manager,
        1,
        macro_events=[
            {
                "t_us": 0,
                "macro_action": "macro_parallel",
                "macro_name": "child",
                "loop_stop_behavior": "pause_run",
            },
        ],
    )
    await until(lambda: bool(writes(manager)))
    await trigger(manager, 0)
    await manager.cancel_macro_playback()
    assert not manager.macro_state.tasks
    assert not manager.macro_state.pauses
    assert not manager.macro_state.instance_meta
    assert not manager.macro_state.pause_released


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["count", "hold"])
async def test_pause_preserves_repetition_state(manager, mode) -> None:
    await trigger(
        manager,
        1,
        loop_mode=mode,
        loop_count=2,
        loop_stop_behavior="pause_run",
        macro_events=[key(30, 1), key(30, 0, 50_000)],
    )
    await until(lambda: bool(writes(manager)))
    await trigger(manager, 0)
    await asyncio.sleep(0.07)
    assert writes(manager).count((1, 30, 1)) == 1
    await trigger(manager, 1)
    if mode == "count":
        await until(lambda: not manager.macro_state.tasks)
        assert writes(manager).count((1, 30, 1)) == 2
    else:
        await until(lambda: writes(manager).count((1, 30, 1)) >= 3)
        await manager.cancel_macro_playback()


def test_pause_and_blocking_overlap_is_counted_once() -> None:
    pause = MacroPauseState()
    timeline = MacroPlaybackTimeline(0, pause=pause)
    pause.pause(2)
    pause.resume(7)
    # A ten-second wait includes the five paused seconds.
    timeline.extend_for_blocking_action(10 - pause.total(10))
    assert timeline.event_delay(0, now_s=10) == 0
    assert timeline.event_delay(2_000_000, now_s=10) == 2
    # The minimum timeline duration also excludes user pause time.
    assert timeline.nominal_end_delay(20_000_000, now_s=10) == 15


@pytest.mark.asyncio
async def test_repress_resumes_all_overlapping_invocations(manager) -> None:
    manager.macro_store.create({"name": "child", "events": [key(30, 1), key(30, 0, 80_000)]})
    events = [
        {
            "t_us": 0,
            "macro_action": "macro_sync",
            "macro_name": "child",
            "loop_stop_behavior": "pause_run",
        }
    ]
    await trigger(manager, 1, macro_events=events)
    await trigger(manager, 1, macro_events=events)
    await until(lambda: writes(manager).count((1, 30, 1)) == 2)
    await trigger(manager, 0)
    assert len(manager.macro_state.tasks) == 4
    sequence = manager.macro_state.instance_seq
    await trigger(manager, 1, macro_events=events)
    assert manager.macro_state.instance_seq == sequence
    await until(lambda: not manager.macro_state.tasks)
    assert writes(manager) == [(1, 30, 1), (1, 30, 1), (1, 30, 0)]


@pytest.mark.asyncio
async def test_resume_does_not_reload_macro_file(manager, monkeypatch) -> None:
    await trigger(
        manager, 1, loop_stop_behavior="pause_run", macro_events=[key(30, 1), key(30, 0, 50_000)]
    )
    await until(lambda: bool(writes(manager)))
    await trigger(manager, 0)
    loader = AsyncMock(side_effect=AssertionError("resume must retain its event source"))
    monkeypatch.setattr(manager, "_stored_macro_event_source", loader)
    result = await manager.play_macro(
        macro_name="parent",
        source_device="kbd",
        source_button="key_f13",
    )
    assert result["resumed"] is True
    loader.assert_not_called()
    await until(lambda: not manager.macro_state.tasks)


@pytest.mark.asyncio
async def test_release_during_initial_load_is_not_lost(manager, monkeypatch) -> None:
    manager.macro_store.create({"name": "parent", "events": [key(30, 1), key(30, 0, 50_000)]})
    original_loader = manager._stored_macro_event_source
    loading, finish_loading = asyncio.Event(), asyncio.Event()

    async def load(*args, **kwargs):
        loading.set()
        await finish_loading.wait()
        return await original_loader(*args, **kwargs)

    monkeypatch.setattr(manager, "_stored_macro_event_source", load)
    press = asyncio.create_task(
        manager.play_macro(
            macro_name="parent",
            source_device="kbd",
            source_button="key_f13",
            loop_stop_behavior="pause_run",
        )
    )
    await loading.wait()
    release = asyncio.create_task(trigger(manager, 0))
    await asyncio.sleep(0)
    finish_loading.set()
    await asyncio.gather(press, release)
    assert len(manager.macro_state.pauses) == 1
    assert all(pause.paused for pause in manager.macro_state.pauses.values())
    await manager.cancel_macro_playback()


@pytest.mark.asyncio
async def test_pause_releases_mouse_inhibition_until_resume(manager) -> None:
    await trigger(
        manager,
        1,
        block_mouse_movement=True,
        loop_stop_behavior="pause_run",
        macro_events=[key(30, 1), key(30, 0, 80_000)],
    )
    await until(lambda: bool(writes(manager)))
    assert manager.macro_state.mouse_inhibit_count == 1
    await trigger(manager, 0)
    assert manager.macro_state.mouse_inhibit_count == 0
    assert not manager.macro_state.mouse_rel_suppressed
    await trigger(manager, 1)
    assert manager.macro_state.mouse_inhibit_count == 1
    await until(lambda: not manager.macro_state.tasks)
    assert manager.macro_state.mouse_inhibit_count == 0


@pytest.mark.asyncio
async def test_pause_timeout_discards_progress_and_next_press_starts_fresh(manager) -> None:
    events = [key(30, 1), key(30, 0, 500_000)]
    await trigger(
        manager, 1, macro_events=events, loop_stop_behavior="pause_run", pause_timeout_s=0.03
    )
    await until(lambda: bool(writes(manager)))
    await trigger(manager, 0)
    await until(lambda: not manager.macro_state.tasks)
    assert not manager.macro_state.pauses
    assert not manager.macro_state.pause_released
    await trigger(manager, 1, macro_events=events, loop_stop_behavior="pause_run")
    await until(lambda: writes(manager).count((1, 30, 1)) == 2)
    await manager.cancel_macro_playback()


@pytest.mark.asyncio
async def test_resume_cancels_timeout_and_next_release_starts_new_timer(manager) -> None:
    await trigger(
        manager,
        1,
        loop_stop_behavior="pause_run",
        pause_timeout_s=0.08,
        macro_events=[key(30, 1), key(30, 0, 1_000_000)],
    )
    await until(lambda: bool(writes(manager)))
    pause = next(iter(manager.macro_state.pauses.values()))
    await trigger(manager, 0)
    old_handle = pause.timeout_handle
    await asyncio.sleep(0.03)
    await trigger(manager, 1)
    assert old_handle.cancelled()
    await asyncio.sleep(0.09)
    assert manager.macro_state.tasks
    await trigger(manager, 0)
    await asyncio.sleep(0.02)
    assert manager.macro_state.tasks
    await until(lambda: not manager.macro_state.tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("call_mode", ["macro_sync", "macro_parallel"])
@pytest.mark.parametrize("parent_pauses", [True, False])
async def test_child_timeout_preserves_parent_and_siblings(
    manager, call_mode, parent_pauses
) -> None:
    manager.macro_store.create({"name": "child", "events": [key(30, 1), key(30, 0, 500_000)]})
    await trigger(
        manager,
        1,
        loop_stop_behavior="pause_run" if parent_pauses else "finish_run",
        pause_timeout_s=0,
        macro_events=[
            {"t_us": 0, "macro_action": "macro_parallel", "macro_name": "child"},
            {
                "t_us": 0,
                "macro_action": call_mode,
                "macro_name": "child",
                "loop_stop_behavior": "pause_run",
                "pause_timeout_s": 0.03,
            },
            key(48, 1, 500_000),
            key(48, 0, 500_000),
        ],
    )
    await until(lambda: len(manager.macro_state.tasks) == 3)
    root_id = next(
        i
        for i, meta in manager.macro_state.instance_meta.items()
        if meta["parent_instance_id"] is None
    )
    child_id = next(
        i
        for i, meta in manager.macro_state.instance_meta.items()
        if meta["loop_stop_behavior"] == "pause_run" and i != root_id
    )
    sibling_id = next(i for i in manager.macro_state.tasks if i not in {root_id, child_id})
    await manager.play_macro(
        macro_name="unrelated",
        load_stored_macro=False,
        macro_events=[key(46, 1, 400_000), key(46, 0, 400_000)],
    )
    await trigger(manager, 0)
    await until(lambda: child_id not in manager.macro_state.tasks)
    assert root_id in manager.macro_state.tasks
    assert sibling_id in manager.macro_state.tasks
    assert len(manager.macro_state.tasks) == 3
    assert (1, 48, 1) not in writes(manager)
    if parent_pauses:
        assert manager.macro_state.pauses[root_id].paused
        sequence = manager.macro_state.instance_seq
        assert (await trigger(manager, 1))["resumed"] is True
        assert manager.macro_state.instance_seq == sequence
    await until(lambda: not manager.macro_state.tasks)
    assert writes(manager).count((1, 48, 1)) == 1
    assert writes(manager).count((1, 46, 1)) == 1


@pytest.mark.asyncio
async def test_pause_timeout_cancels_active_sync_exec(manager) -> None:
    manager.broadcast_callback = AsyncMock()
    await trigger(
        manager,
        1,
        loop_stop_behavior="pause_run",
        pause_timeout_s=0.03,
        macro_events=[
            {"t_us": 0, "macro_action": "exec_sync", "command": "example", "timeout_ms": 5000},
            key(30, 1),
            key(30, 0),
        ],
    )
    await until(lambda: bool(manager.macro_state.exec_waiters))
    wait_id = next(iter(manager.macro_state.exec_waiters))
    await trigger(manager, 0)
    await until(lambda: not manager.macro_state.tasks)
    assert not writes(manager)
    assert not manager.macro_state.exec_waiters
    assert any(
        call.args[1].get("macro_exec_cancel_id") == wait_id
        for call in manager.broadcast_callback.call_args_list
    )


@pytest.mark.asyncio
async def test_pause_timeout_cancels_active_natural_move(manager, monkeypatch) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def move(*_args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(manager, "move_cursor_natural", move)
    await trigger(
        manager,
        1,
        loop_stop_behavior="pause_run",
        pause_timeout_s=0.03,
        macro_events=[
            {"t_us": 0, "macro_action": "mouse_move_natural_abs", "x": 100, "y": 200},
            key(30, 1),
            key(30, 0),
        ],
    )
    await until(started.is_set)
    await trigger(manager, 0)
    await until(lambda: not manager.macro_state.tasks)
    assert cancelled.is_set()
    assert not writes(manager)


@pytest.mark.asyncio
async def test_expired_deadline_cannot_be_resumed_before_timer_callback(manager) -> None:
    await trigger(
        manager,
        1,
        loop_stop_behavior="pause_run",
        pause_timeout_s=10,
        macro_events=[key(30, 1), key(30, 0, 500_000)],
    )
    await until(lambda: bool(writes(manager)))
    await trigger(manager, 0)
    pause = next(iter(manager.macro_state.pauses.values()))
    pause.paused_at_s = asyncio.get_running_loop().time() - 11
    result = await trigger(manager, 1)
    assert not result.get("resumed")
    await until(lambda: not manager.macro_state.tasks)


@pytest.mark.asyncio
async def test_cancel_all_removes_pause_timeout_handle(manager) -> None:
    await trigger(
        manager,
        1,
        loop_stop_behavior="pause_run",
        pause_timeout_s=30,
        macro_events=[key(30, 1), key(30, 0, 500_000)],
    )
    await until(lambda: bool(writes(manager)))
    await trigger(manager, 0)
    pause = next(iter(manager.macro_state.pauses.values()))
    handle = pause.timeout_handle
    await manager.cancel_macro_playback()
    assert handle.cancelled()
    assert pause.timeout_handle is None


def test_pause_timeout_survives_macro_storage(manager) -> None:
    manager.macro_store.create(
        {
            "name": "timeout",
            "events": [key(30, 1), key(30, 0)],
            "loop_stop_behavior": "pause_run",
            "pause_timeout_s": 120,
        }
    )
    assert manager.macro_store.get_meta("timeout")["pause_timeout_s"] == 120


@pytest.mark.asyncio
async def test_child_timeout_includes_release_time_before_call(manager) -> None:
    manager.macro_store.create({"name": "child", "events": [key(30, 1), key(30, 0)]})
    await trigger(
        manager,
        1,
        macro_events=[
            {
                "t_us": 150_000,
                "macro_action": "macro_sync",
                "macro_name": "child",
                "loop_stop_behavior": "pause_run",
                "pause_timeout_s": 0.1,
            },
            key(48, 1),
            key(48, 0),
        ],
    )
    await trigger(manager, 0)
    async with asyncio.timeout(0.23):
        await until(lambda: not manager.macro_state.tasks)
    assert writes(manager) == [(1, 48, 1), (1, 48, 0)]


@pytest.mark.asyncio
@pytest.mark.parametrize("expires", ["parent", "child"])
async def test_timeout_cancels_descendants_even_with_never(manager, expires) -> None:
    manager.macro_store.create(
        {
            "name": "grandchild",
            "events": [
                key(30, 1),
                key(30, 0, 500_000),
            ],
        }
    )
    manager.macro_store.create(
        {
            "name": "child",
            "events": [
                {
                    "t_us": 0,
                    "macro_action": "macro_sync",
                    "macro_name": "grandchild",
                    "loop_stop_behavior": "pause_run",
                    "pause_timeout_s": 0,
                },
            ],
        }
    )
    await trigger(
        manager,
        1,
        loop_stop_behavior="pause_run",
        pause_timeout_s=0.03 if expires == "parent" else 0,
        macro_events=[
            {
                "t_us": 0,
                "macro_action": "macro_sync",
                "macro_name": "child",
                "loop_stop_behavior": "pause_run",
                "pause_timeout_s": 0.03 if expires == "child" else 0,
            },
            key(48, 1),
            key(48, 0),
        ],
    )
    await until(lambda: bool(writes(manager)))
    await trigger(manager, 0)
    remaining = 0 if expires == "parent" else 1
    await until(lambda: len(manager.macro_state.tasks) == remaining)
    assert (1, 48, 1) not in writes(manager)
    if expires == "child":
        assert next(iter(manager.macro_state.pauses.values())).paused
        await trigger(manager, 1)
        await until(lambda: not manager.macro_state.tasks)
        assert writes(manager)[-2:] == [(1, 48, 1), (1, 48, 0)]


@pytest.mark.asyncio
async def test_resume_does_not_revive_child_expired_before_timer_callback(manager) -> None:
    manager.macro_store.create({"name": "child", "events": [key(30, 1), key(30, 0, 500_000)]})
    await trigger(
        manager,
        1,
        loop_stop_behavior="pause_run",
        macro_events=[
            {
                "t_us": 0,
                "macro_action": "macro_sync",
                "macro_name": "child",
                "loop_stop_behavior": "pause_run",
                "pause_timeout_s": 10,
            },
            key(48, 1),
            key(48, 0),
        ],
    )
    await until(lambda: bool(writes(manager)))
    await trigger(manager, 0)
    child_pause = next(pause for pause in manager.macro_state.pauses.values() if pause.timeout_s)
    child_pause.paused_at_s = asyncio.get_running_loop().time() - 11
    on_resume = MagicMock()
    child_pause.on_resume = on_resume
    assert (await trigger(manager, 1))["resumed"] is True
    await until(lambda: not manager.macro_state.tasks)
    on_resume.assert_not_called()
    assert writes(manager).count((1, 30, 1)) == 1
    assert writes(manager)[-2:] == [(1, 48, 1), (1, 48, 0)]
