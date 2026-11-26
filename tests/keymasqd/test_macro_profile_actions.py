import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from keymasq.common.ipc import CommandType
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.macro_store import MacroStore
from keymasq.keymasqd.runtime.macro import loops

WHILE_HELD = {"on_trigger_end": True}


def _profile_event(action: str, profile: str, t_us: int = 0, **extra: object) -> dict[str, object]:
    return {
        "device_type": "macro",
        "type": 0,
        "code": 0,
        "value": 0,
        "t_us": t_us,
        "macro_action": action,
        "profile_name": profile,
        **extra,
    }


def _exec_async(t_us: int = 0) -> dict[str, object]:
    return {"t_us": t_us, "macro_action": "exec_async", "command": "true"}


def _manager(tmp_path: Path) -> tuple[DeviceManager, AsyncMock]:
    broadcast = AsyncMock()
    manager = DeviceManager(broadcast_callback=broadcast)
    manager.output_state.keyboard_uinput = MagicMock()
    manager.macro_store = MacroStore(tmp_path / "macros")
    return manager, broadcast


async def _wait_for_no_running_macros(manager: DeviceManager) -> None:
    deadline = asyncio.get_running_loop().time() + 5.0
    while loops.running_macro_instance_ids(manager.macro_state):
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.005)


def _action_triggers(broadcast: AsyncMock) -> list[dict[str, object]]:
    return [
        call.args[1]
        for call in broadcast.await_args_list
        if call.args[0] == CommandType.ACTION_TRIGGER
    ]


@pytest.mark.asyncio
async def test_child_profile_action_targets_root_trigger(tmp_path: Path) -> None:
    manager, broadcast = _manager(tmp_path)
    assert manager.macro_store is not None
    manager.macro_store.create(
        {
            "name": "child",
            "events": [_profile_event("profile_enable", "Nav", deactivation=WHILE_HELD)],
        }
    )
    manager.observe_profile_trigger_start("1234:5678:key_a")

    await manager.play_macro(
        macro_name="parent",
        load_stored_macro=False,
        macro_events=[{"t_us": 0, "macro_action": "macro_sync", "macro_name": "child"}],
        source_device="1234:5678",
        source_button="key_a",
    )
    await _wait_for_no_running_macros(manager)

    assert _action_triggers(broadcast) == [
        {
            "action_type": "profile_enable",
            "profile_name": "Nav",
            "deactivation": {"on_trigger_end": True},
            "source_device": "1234:5678",
            "source_button": "key_a",
            "trigger_id": "1234:5678:key_a",
        }
    ]


@pytest.mark.asyncio
async def test_source_less_macro_sends_persistent_and_one_shot_profile_actions(
    tmp_path: Path,
) -> None:
    manager, broadcast = _manager(tmp_path)

    await manager.play_macro(
        macro_name="cli",
        load_stored_macro=False,
        macro_events=[
            _profile_event("profile_disable", "Desktop"),
            _profile_event("profile_toggle", "Nav", deactivation={"after_actions": 1}),
        ],
    )
    await _wait_for_no_running_macros(manager)

    assert [
        (data["action_type"], data["profile_name"], data.get("deactivation"))
        for data in _action_triggers(broadcast)
    ] == [
        ("profile_disable", "Desktop", None),
        ("profile_toggle", "Nav", {"after_actions": 1}),
    ]


@pytest.mark.parametrize(
    ("source", "release_before_event", "allow_profile_actions", "deactivation"),
    [
        pytest.param(("", ""), False, True, WHILE_HELD, id="while-held-without-trigger"),
        pytest.param(
            ("1234:5678", "key_a"), True, True, WHILE_HELD, id="while-held-after-release"
        ),
        pytest.param(("1234:5678", "key_a"), False, False, None, id="profile-lifecycle-macro"),
    ],
)
@pytest.mark.asyncio
async def test_profile_action_is_skipped(
    tmp_path: Path,
    source: tuple[str, str],
    release_before_event: bool,
    allow_profile_actions: bool,
    deactivation: dict[str, object] | None,
) -> None:
    manager, broadcast = _manager(tmp_path)
    extra = {"deactivation": deactivation} if deactivation is not None else {}

    await manager.play_macro(
        macro_name="macro",
        load_stored_macro=False,
        macro_events=[
            _profile_event("profile_enable", "Nav", t_us=30_000, **extra),
            _exec_async(t_us=30_000),
        ],
        source_device=source[0],
        source_button=source[1],
        allow_profile_actions=allow_profile_actions,
    )
    if release_before_event:
        await manager.play_macro(
            macro_name="macro",
            load_stored_macro=False,
            source_device=source[0],
            source_button=source[1],
            trigger_value=0,
        )
    await _wait_for_no_running_macros(manager)

    assert [data["action_type"] for data in _action_triggers(broadcast)] == ["exec"]


@pytest.mark.asyncio
async def test_combo_macro_trigger_keeps_while_held_activation_until_release(
    tmp_path: Path,
) -> None:
    manager, broadcast = _manager(tmp_path)
    source = {"source_device": "combo", "source_button": "combo:c1"}

    await manager.play_macro(macro_name="macro", load_stored_macro=False, **source)
    await manager.track_profile_activation("Nav", "activation-1", "combo:combo:c1", WHILE_HELD)
    await asyncio.sleep(0)
    held_events = [call.args[0] for call in broadcast.call_args_list]

    await manager.play_macro(
        macro_name="macro", load_stored_macro=False, trigger_value=0, **source
    )
    await asyncio.sleep(0)

    assert CommandType.PROFILE_DEACTIVATE_REQUESTED not in held_events
    assert [
        call.args[1]["activation_id"]
        for call in broadcast.call_args_list
        if call.args[0] == CommandType.PROFILE_DEACTIVATE_REQUESTED
    ] == ["activation-1"]
