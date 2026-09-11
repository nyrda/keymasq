import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from keymasq.keymasqd import device_manager
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.macro_file import macro_payload_from_events
from keymasq.keymasqd.macro_store import MacroStore
from keymasq.keymasqd.runtime.macro import scheduler
from keymasq.keymasqd.runtime.macro.events import expand_macro_rapidfire


def block(**fields: object) -> dict[str, object]:
    return {
        "macro_action": "macro_rapidfire",
        "device_type": "keyboard",
        "type": 1,
        "code": 30,
        "value": 0,
        "t_us": 0,
        "duration_us": 100_000,
        "rapidfire_hold_ms": 10,
        "rapidfire_wait_ms": 10,
        **fields,
    }


async def source(events: list[dict[str, object]]) -> AsyncIterator[dict[str, object]]:
    for event in events:
        yield event


@pytest.mark.asyncio
async def test_merges_blocks_with_other_inputs_and_observes_only_stored_events() -> None:
    control_down = dict(device_type="keyboard", type=1, code=29, value=1, t_us=0)
    control_up = {**control_down, "value": 0, "t_us": 100_000}
    raw = [control_down, block(), block(code=48, t_us=18_000, duration_us=20_000), control_up]
    observed = []
    expanded = [
        event async for event in expand_macro_rapidfire(source(raw), observe=observed.append)
    ]
    assert observed == raw
    assert [event["t_us"] for event in expanded] == sorted(event["t_us"] for event in expanded)
    assert expanded[0] == control_down
    assert expanded[-1] == control_up
    assert [(e["code"], e["value"]) for e in expanded if e["t_us"] == 18_000] == [(30, 1), (48, 1)]
    assert sum(e["code"] == 30 for e in expanded) == 12
    assert sum(e["code"] == 48 for e in expanded) == 2


@pytest.mark.asyncio
async def test_large_block_expands_lazily_and_retains_gamepad_output() -> None:
    stream = expand_macro_rapidfire(
        source(
            [
                block(
                    device_type="gamepad",
                    output_id="virtual-gamepad-3",
                    duration_us=10**15,
                    rapidfire_hold_ms=0,
                    rapidfire_wait_ms=1,
                )
            ]
        )
    )
    try:
        first = await anext(stream)
        second = await anext(stream)
        assert first["t_us"] == second["t_us"] == 0
        assert first["output_id"] == second["output_id"] == "virtual-gamepad-3"
        assert [first["value"], second["value"]] == [1, 0]
    finally:
        await stream.aclose()


def test_storage_duration_includes_block_end_without_expansion() -> None:
    event = block(t_us=50_000)
    payload = macro_payload_from_events({}, [event])
    assert payload["duration_us"] == 150_000
    assert payload["event_count"] == 1
    assert payload["events"] == [event]
    assert payload["device_types"] == ["keyboard"]


@pytest.mark.asyncio
@pytest.mark.parametrize("speed", [0.5, 1.0, 2.0])
async def test_scheduler_scales_pulses_and_shifts_them_for_explicit_wait(speed: float) -> None:
    manager = DeviceManager()
    now = 0.0
    emitted: list[tuple[float, int, int]] = []

    async def sleep(delay: float) -> None:
        nonlocal now
        now += delay

    manager.output_state.keyboard_uinput = MagicMock()
    manager.output_state.keyboard_uinput.write.side_effect = lambda _type, code, value: (
        emitted.append((now, code, value))
    )
    deps = replace(
        device_manager._macro_runtime_deps(),
        asyncio_mod=SimpleNamespace(
            sleep=sleep,
            get_running_loop=lambda: SimpleNamespace(time=lambda: now),
            to_thread=asyncio.to_thread,
            CancelledError=asyncio.CancelledError,
        ),
    )
    await scheduler.play_macro_task(
        manager,
        instance_id=1,
        macro_events=[block(), {"macro_action": "wait", "t_us": 50_000, "duration_us": 20_000}],
        macro_name="pulse",
        replay_mouse_movement=True,
        replay_mouse_clicks=True,
        speed=speed,
        loop_mode="none",
        loop_count=1,
        move_to_start=False,
        start_x=0,
        start_y=0,
        block_mouse_movement=False,
        deps=deps,
    )
    expected = [0, 10, 18, 28, 36, 46, 54, 64, 72, 82, 90, 100]
    assert [time for time, _, _ in emitted] == pytest.approx(
        [
            milliseconds / 1000 / speed + (0.020 if milliseconds > 50 else 0)
            for milliseconds in expected
        ]
    )
    assert [(code, value) for _, code, value in emitted] == [(30, 1), (30, 0)] * 6


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_pause_or_cancel_releases_active_pulse_without_leaking_outputs(cancel: bool) -> None:
    manager = DeviceManager()
    pressed = asyncio.Event()
    output = MagicMock()
    output.write.side_effect = lambda *_args: pressed.set()
    manager.output_state.keyboard_uinput = output
    trigger = dict(
        macro_name="pulse",
        load_stored_macro=False,
        source_device="kbd",
        source_button="key_f13",
    )
    try:
        await manager.play_macro(
            **trigger,
            trigger_value=1,
            loop_stop_behavior="pause_run",
            macro_events=[block(duration_us=200_000, rapidfire_hold_ms=50, rapidfire_wait_ms=50)],
        )
        await asyncio.wait_for(pressed.wait(), timeout=2)
        if cancel:
            await manager.cancel_macro_playback()
            assert not manager.macro_state.tasks
        else:
            await manager.play_macro(**trigger, trigger_value=0)
            assert all(pause.paused for pause in manager.macro_state.pauses.values())
        assert [call.args for call in output.write.call_args_list] == [(1, 30, 1), (1, 30, 0)]
        if not cancel:
            await manager.play_macro(**trigger, trigger_value=1)
            await asyncio.wait_for(asyncio.gather(*manager.macro_state.tasks.values()), timeout=2)
            assert [call.args for call in output.write.call_args_list] == [
                (1, 30, 1),
                (1, 30, 0),
            ] * 3
        assert not manager.macro_state.instance_held
    finally:
        await manager.cancel_macro_playback()


@pytest.mark.asyncio
async def test_stored_block_loops_from_cache_without_saving_generated_pulses(
    tmp_path: Path,
) -> None:
    manager = DeviceManager()
    output = MagicMock()
    manager.output_state.keyboard_uinput = output
    store = MacroStore(tmp_path / "macros")
    event = block(duration_us=10_000, rapidfire_hold_ms=1, rapidfire_wait_ms=1)
    store.create({"name": "pulse", "events": [event]})
    manager.macro_store = store
    try:
        await manager.play_macro(macro_name="pulse", loop_mode="count", loop_count=3)
        await asyncio.wait_for(asyncio.gather(*manager.macro_state.tasks.values()), timeout=2)
        assert [call.args for call in output.write.call_args_list] == [(1, 30, 1), (1, 30, 0)] * 15
        revision = store.probe_revision("pulse")
        assert revision is not None
        cached = manager.macro_state.replay_cache.get(revision)
        assert cached is not None
        assert cached.events == (event,)
        assert cached.duration_us == 10_000
    finally:
        await manager.cancel_macro_playback()
