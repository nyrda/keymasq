import asyncio
import logging
from pathlib import Path
from unittest.mock import MagicMock

import evdev
import pytest

from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.macro_store import MacroStore
from keymasq.keymasqd.runtime.macro import loops


def _key_event(code: int, value: int, t_us: int) -> dict[str, object]:
    return {
        "t_us": t_us,
        "device_type": "keyboard",
        "type": evdev.ecodes.EV_KEY,
        "code": code,
        "value": value,
    }


async def _wait_for_no_running_macros(
    manager: DeviceManager,
    timeout_s: float = 5.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while loops.running_macro_instance_ids(manager.macro_state):
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                "macro instances still running: "
                f"{loops.running_macro_instance_ids(manager.macro_state)}"
            )
        await asyncio.sleep(0.005)


def _manager_with_store(tmp_path: Path) -> tuple[DeviceManager, MacroStore]:
    manager = DeviceManager()
    manager.output_state.keyboard_uinput = MagicMock()
    store = MacroStore(tmp_path / "macros")
    manager.macro_store = store
    return manager, store


@pytest.mark.asyncio
async def test_sync_child_blocks_later_parent_events(tmp_path: Path) -> None:
    manager, store = _manager_with_store(tmp_path)
    store.create(
        {
            "name": "child",
            "events": [
                _key_event(evdev.ecodes.KEY_B, 1, 0),
                _key_event(evdev.ecodes.KEY_B, 0, 20_000),
            ],
        }
    )

    await manager.play_macro(
        macro_name="parent",
        load_stored_macro=False,
        macro_events=[
            {"t_us": 0, "macro_action": "macro_sync", "macro_name": "child"},
            _key_event(evdev.ecodes.KEY_A, 1, 0),
        ],
    )
    await _wait_for_no_running_macros(manager)

    writes = [call.args for call in manager.output_state.keyboard_uinput.write.call_args_list]
    assert writes.index((evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0)) < writes.index(
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)
    )


@pytest.mark.asyncio
async def test_parallel_children_join_each_parent_count_iteration(tmp_path: Path) -> None:
    manager, store = _manager_with_store(tmp_path)
    store.create(
        {
            "name": "child",
            "events": [
                _key_event(evdev.ecodes.KEY_B, 1, 0),
                _key_event(evdev.ecodes.KEY_B, 0, 20_000),
            ],
        }
    )

    await manager.play_macro(
        macro_name="parent",
        load_stored_macro=False,
        loop_mode="count",
        loop_count=2,
        macro_events=[
            {"t_us": 0, "macro_action": "macro_parallel", "macro_name": "child"},
            _key_event(evdev.ecodes.KEY_A, 1, 1_000),
        ],
    )
    await _wait_for_no_running_macros(manager)

    writes = [call.args for call in manager.output_state.keyboard_uinput.write.call_args_list]
    a_presses = [
        index
        for index, event in enumerate(writes)
        if event == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)
    ]
    b_releases = [
        index
        for index, event in enumerate(writes)
        if event == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0)
    ]
    assert len(a_presses) == 2
    assert len(b_releases) == 2
    assert b_releases[0] < a_presses[1]


@pytest.mark.asyncio
async def test_hold_child_is_skipped_if_source_released_before_call(tmp_path: Path) -> None:
    manager, store = _manager_with_store(tmp_path)
    store.create(
        {
            "name": "held child",
            "events": [_key_event(evdev.ecodes.KEY_C, 1, 0)],
        }
    )

    await manager.play_macro(
        macro_name="parent",
        load_stored_macro=False,
        source_device="kbd",
        source_button="key_f13",
        macro_events=[
            {"t_us": 0, "macro_action": "wait", "duration_us": 30_000},
            {
                "t_us": 0,
                "macro_action": "macro_sync",
                "macro_name": "held child",
                "loop_mode": "hold",
            },
        ],
    )
    await asyncio.sleep(0.005)
    await manager.play_macro(
        macro_name="parent",
        load_stored_macro=False,
        source_device="kbd",
        source_button="key_f13",
        trigger_value=0,
    )
    await _wait_for_no_running_macros(manager)

    manager.output_state.keyboard_uinput.write.assert_not_called()


@pytest.mark.asyncio
async def test_source_less_hold_child_runs_once(tmp_path: Path) -> None:
    manager, store = _manager_with_store(tmp_path)
    store.create(
        {
            "name": "held child",
            "events": [
                _key_event(evdev.ecodes.KEY_D, 1, 0),
                _key_event(evdev.ecodes.KEY_D, 0, 1_000),
            ],
        }
    )

    await manager.play_macro(
        macro_name="parent",
        load_stored_macro=False,
        macro_events=[
            {
                "t_us": 0,
                "macro_action": "macro_sync",
                "macro_name": "held child",
                "loop_mode": "hold",
            }
        ],
    )
    await _wait_for_no_running_macros(manager)

    writes = [call.args for call in manager.output_state.keyboard_uinput.write.call_args_list]
    assert writes.count((evdev.ecodes.EV_KEY, evdev.ecodes.KEY_D, 1)) == 1


@pytest.mark.parametrize(
    ("loop_mode", "loop_count", "expected_presses"),
    [("count", 3, 3), ("toggle", 99, 1)],
)
@pytest.mark.asyncio
async def test_child_count_is_explicit_and_nested_toggle_runs_once(
    tmp_path: Path,
    loop_mode: str,
    loop_count: int,
    expected_presses: int,
) -> None:
    manager, store = _manager_with_store(tmp_path)
    store.create(
        {
            "name": "child",
            "events": [
                _key_event(evdev.ecodes.KEY_G, 1, 0),
                _key_event(evdev.ecodes.KEY_G, 0, 1_000),
            ],
        }
    )

    await manager.play_macro(
        macro_name="parent",
        load_stored_macro=False,
        macro_events=[
            {
                "t_us": 0,
                "macro_action": "macro_sync",
                "macro_name": "child",
                "loop_mode": loop_mode,
                "loop_count": loop_count,
            }
        ],
    )
    await _wait_for_no_running_macros(manager)

    writes = [call.args for call in manager.output_state.keyboard_uinput.write.call_args_list]
    assert writes.count((evdev.ecodes.EV_KEY, evdev.ecodes.KEY_G, 1)) == expected_presses


@pytest.mark.asyncio
async def test_source_less_top_level_hold_runs_once() -> None:
    manager = DeviceManager()
    manager.output_state.keyboard_uinput = MagicMock()

    await manager.play_macro(
        macro_name="source-less hold",
        load_stored_macro=False,
        loop_mode="hold",
        macro_events=[
            _key_event(evdev.ecodes.KEY_H, 1, 0),
            _key_event(evdev.ecodes.KEY_H, 0, 1_000),
        ],
    )
    await _wait_for_no_running_macros(manager)

    writes = [call.args for call in manager.output_state.keyboard_uinput.write.call_args_list]
    assert writes.count((evdev.ecodes.EV_KEY, evdev.ecodes.KEY_H, 1)) == 1


@pytest.mark.asyncio
async def test_missing_child_aborts_parent_and_logs_call_chain(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager, _store = _manager_with_store(tmp_path)
    caplog.set_level(logging.ERROR, logger="keymasqd.devices")

    await manager.play_macro(
        macro_name="parent",
        load_stored_macro=False,
        macro_events=[
            {"t_us": 0, "macro_action": "macro_sync", "macro_name": "missing"},
            _key_event(evdev.ecodes.KEY_E, 1, 1_000),
        ],
    )
    await _wait_for_no_running_macros(manager)

    manager.output_state.keyboard_uinput.write.assert_not_called()
    assert "parent -> missing" in caplog.text
    assert "Traceback" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_parallel_child_failure_stops_later_parent_event(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager, store = _manager_with_store(tmp_path)
    store.create(
        {
            "name": "failing child",
            "events": [{"t_us": 0, "macro_action": "macro_sync", "macro_name": "missing"}],
        }
    )
    caplog.set_level(logging.ERROR, logger="keymasqd.devices")

    await manager.play_macro(
        macro_name="parent",
        load_stored_macro=False,
        macro_events=[
            {
                "t_us": 0,
                "macro_action": "macro_parallel",
                "macro_name": "failing child",
            },
            _key_event(evdev.ecodes.KEY_F, 1, 30_000),
        ],
    )
    await _wait_for_no_running_macros(manager)

    manager.output_state.keyboard_uinput.write.assert_not_called()
    assert "parent -> failing child -> missing" in caplog.text


@pytest.mark.parametrize("macro_action", ["macro_sync", "macro_parallel"])
@pytest.mark.asyncio
async def test_direct_recursive_call_is_blocked(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    macro_action: str,
) -> None:
    manager, store = _manager_with_store(tmp_path)
    store.create(
        {
            "name": "A",
            "events": [
                {"t_us": 0, "macro_action": macro_action, "macro_name": "A"},
                _key_event(evdev.ecodes.KEY_A, 1, 1_000),
            ],
        }
    )
    caplog.set_level(logging.ERROR, logger="keymasqd.devices")

    await manager.play_macro(macro_name="A")
    await _wait_for_no_running_macros(manager)

    manager.output_state.keyboard_uinput.write.assert_not_called()
    assert "A -> A: recursive macro call blocked" in caplog.text
    assert "Traceback" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_indirect_recursive_call_logs_complete_cycle(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager, store = _manager_with_store(tmp_path)
    store.create(
        {
            "name": "A",
            "events": [{"t_us": 0, "macro_action": "macro_sync", "macro_name": "B"}],
        }
    )
    store.create(
        {
            "name": "B",
            "events": [{"t_us": 0, "macro_action": "macro_parallel", "macro_name": "C"}],
        }
    )
    store.create(
        {
            "name": "C",
            "events": [{"t_us": 0, "macro_action": "macro_sync", "macro_name": "A"}],
        }
    )
    caplog.set_level(logging.ERROR, logger="keymasqd.devices")

    await manager.play_macro(macro_name="A")
    await _wait_for_no_running_macros(manager)

    assert "A -> B -> C -> A: recursive macro call blocked" in caplog.text
    assert "Traceback" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_unexpected_child_start_failure_keeps_traceback(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _store = _manager_with_store(tmp_path)

    async def fail_to_start_child(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("unexpected child failure")

    monkeypatch.setattr(manager, "start_macro_child", fail_to_start_child)
    caplog.set_level(logging.ERROR, logger="keymasqd.devices")

    await manager.play_macro(
        macro_name="parent",
        load_stored_macro=False,
        macro_events=[{"t_us": 0, "macro_action": "macro_sync", "macro_name": "child"}],
    )
    await _wait_for_no_running_macros(manager)

    record = next(record for record in caplog.records if "Macro playback aborted" in record.message)
    assert "parent -> child: unexpected child failure" in record.message
    assert record.exc_info is not None


@pytest.mark.asyncio
async def test_repeated_non_recursive_calls_remain_allowed(tmp_path: Path) -> None:
    manager, store = _manager_with_store(tmp_path)
    store.create(
        {
            "name": "B",
            "events": [
                _key_event(evdev.ecodes.KEY_B, 1, 0),
                _key_event(evdev.ecodes.KEY_B, 0, 1_000),
            ],
        }
    )

    await manager.play_macro(
        macro_name="A",
        load_stored_macro=False,
        macro_events=[
            {"t_us": 0, "macro_action": "macro_sync", "macro_name": "B"},
            {"t_us": 1_000, "macro_action": "macro_sync", "macro_name": "B"},
        ],
    )
    await _wait_for_no_running_macros(manager)

    writes = [call.args for call in manager.output_state.keyboard_uinput.write.call_args_list]
    assert writes.count((evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1)) == 2
