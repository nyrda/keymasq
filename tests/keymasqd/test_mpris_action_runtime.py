import asyncio
import logging

import evdev
import pytest

from keymasq.common.ipc import CommandType
from keymasq.common.models import ActionType, MappingAction
from keymasq.common.types import SyntheticInputEvent
from keymasq.keymasqd.runtime.action_runner import (
    ActionExecutionHandle,
    ActionRuntimeContext,
    build_action_trigger_payload,
    drain_action_tasks,
    execute_action,
)
from keymasq.keymasqd.runtime.adapters import identity_uinput_writer
from keymasq.keymasqd.runtime.grabbed_device_types import ActionExecutionDeps


def _deps() -> ActionExecutionDeps:
    return ActionExecutionDeps(
        asyncio_mod=asyncio,
        fire_and_observe_fn=lambda coro, _label: asyncio.create_task(coro),
        evdev_mod=evdev,
        uinput_writer=identity_uinput_writer,
    )


def test_mpris_action_trigger_payload_includes_command() -> None:
    payload = build_action_trigger_payload(
        MappingAction(action_type=ActionType.MPRIS, mpris_command="play-pause"),
        source_device="kbd",
        source_button="key_playpause",
        trigger_id="kbd:key_playpause",
    )

    assert payload == {
        "action_type": "mpris",
        "command": "play_pause",
        "source_device": "kbd",
        "source_button": "key_playpause",
        "trigger_id": "kbd:key_playpause",
    }


@pytest.mark.asyncio
async def test_mpris_action_press_dispatches_to_session() -> None:
    events: list[tuple[CommandType, dict[str, object]]] = []

    async def broadcast(event_type: CommandType, data: dict[str, object]) -> None:
        events.append((event_type, data))

    runtime = ActionRuntimeContext(
        path="/dev/input/event0",
        hardware_id="1532:00b4",
        broadcast_callback=broadcast,
    )
    handle = ActionExecutionHandle()

    await execute_action(
        runtime,
        MappingAction(action_type=ActionType.MPRIS, mpris_command="stop"),
        SyntheticInputEvent(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_STOPCD, 1),
        "extra_14",
        deps=_deps(),
        execution_handle=handle,
    )
    await drain_action_tasks(handle)

    assert events == [
        (
            CommandType.ACTION_TRIGGER,
            {
                "action_type": "mpris",
                "command": "stop",
                "source_device": "1532:00b4",
                "source_button": "extra_14",
                "trigger_id": "1532:00b4:extra_14",
            },
        )
    ]


@pytest.mark.asyncio
async def test_mpris_action_without_session_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="keymasqd.runtime.action_runner")
    runtime = ActionRuntimeContext(
        path="/dev/input/event0",
        hardware_id="1532:00b4",
        broadcast_callback=None,
    )

    await execute_action(
        runtime,
        MappingAction(action_type=ActionType.MPRIS, mpris_command="play_pause"),
        SyntheticInputEvent(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_PLAYPAUSE, 1),
        "extra_13",
        deps=_deps(),
    )

    assert "Cannot dispatch mpris action extra_13: no session connection" in caplog.messages
