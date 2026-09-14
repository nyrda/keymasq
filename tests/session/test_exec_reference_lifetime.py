import asyncio
import gc
from dataclasses import replace
from unittest.mock import AsyncMock

import evdev
import pytest

from keymasq.common.ipc import Command, CommandType, Response
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.runtime.action import triggers
from keymasq.keymasqd.runtime.action_parser import parse_action
from keymasq.keymasqd.runtime.grabbed_device.device import GrabbedDevice, log
from keymasq.keymasqd.runtime.grabbed_device.event.pipeline import (
    build_event_processing_deps,
    process_event,
)
from keymasq.session.manager import events
from keymasq.session.manager.core import SessionManager
from keymasq.session.manager.payload import references
from keymasq.session.manager.profile import application, coordinator
from keymasq.session.profile.types import ResolvedDeviceProfile


@pytest.mark.asyncio
@pytest.mark.parametrize("deactivate", [False, True])
async def test_overload_release_exec_survives_profile_change(
    monkeypatch, deactivate, sample_hardware_config
):
    session = SessionManager()
    daemon = DeviceManager()
    hardware_id = "1234:5678"
    session.profile_state.grabbed_devices.add(hardware_id)
    monkeypatch.setattr(session.hardware, "get_hardware", lambda _id: sample_hardware_config)
    start_ref = references.allocate(
        session, "dictation start", owner="device", hardware_id=hardware_id
    )
    stop_ref = references.allocate(
        session, "dictation stop", owner="device", hardware_id=hardware_id
    )
    mapping = {
        "btn_side": parse_action(
            daemon,
            {
                "action": "superkey",
                "superkey": {
                    "name": "dictation",
                    "mode": "overload",
                    "overload_down_actions": [{"action": "exec", "exec_ref": start_ref}],
                    "overload_up_actions": [{"action": "exec", "exec_ref": stop_ref}],
                },
            },
        )
    }
    executed = AsyncMock()
    monkeypatch.setattr(events, "handle_exec_trigger", executed)

    async def broadcast(kind, data):
        await events.handle_event(session, kind, events.prepare_event(session, kind, data))

    device = GrabbedDevice(
        path="/dev/input/event-test",
        hardware_id=hardware_id,
        button_map={"btn_side": "btn_side"},
        mapping_getter=lambda: mapping,
        event_callback=AsyncMock(),
        broadcast_callback=broadcast,
        repeat_state=daemon.repeat_state,
    )
    device.running = True

    async def command(request: Command):
        if request.command == CommandType.UNUSED_EXEC_REFS:
            return Response(
                status="ok",
                data={"unused_exec_refs": daemon.exec_references.unused(request.data["exec_refs"])},
            )
        if request.command == CommandType.SET_MAPPING:
            previous = dict(mapping)
            mapping.clear()
            await device.reset_mapping_runtime_state(previous_mapping=previous)
        if request.command == CommandType.RELEASE_DEVICE:
            assert request.data["immediate"] is False
            mapping.clear()
        return Response(status="ok", data={"scheduled": True})

    session.client.send_command = AsyncMock(side_effect=command)
    deps = build_event_processing_deps(log=log)
    try:
        await process_event(
            device, evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 1), deps=deps
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        if deactivate:
            await coordinator.apply_resolved_device_profile(
                session, hardware_id, ResolvedDeviceProfile(hardware_id=hardware_id, mappings={})
            )
        else:
            for _ in range(3):
                await application.update_mapping(
                    session,
                    hardware_id,
                    ResolvedDeviceProfile(hardware_id=hardware_id, mappings={}),
                )
        await references.collect_retired(session)
        assert references.resolve(session, stop_ref) is not None
        await process_event(
            device, evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 0), deps=deps
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert [call.args[1]["cmd"] for call in executed.await_args_list] == [
            "dictation start",
            "dictation stop",
        ]
        mapping.clear()
        daemon.repeat_state.history.clear()
        await references.collect_retired(session)
        assert session.exec_state.retired_exec_refs == {}
    finally:
        await events.cancel_event_tasks(session)
        await device.reset_superkeys()


@pytest.mark.asyncio
async def test_queued_exec_payload_keeps_lease_until_broadcast_finishes():
    manager = DeviceManager()
    action = parse_action(manager, {"action": "exec", "exec_ref": 7})
    copied_action = replace(action)
    payload = triggers.build_action_trigger_payload(
        copied_action, source_device="kbd", source_button="key_a"
    )
    assert payload is not None
    copied_payload = payload.copy()
    del action, copied_action, payload
    gc.collect()
    assert manager.exec_references.unused([7, 8]) == [8]
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def broadcast(_kind, _data):
        entered.set()
        await finish.wait()

    task = asyncio.create_task(broadcast(CommandType.ACTION_TRIGGER, copied_payload))
    del copied_payload
    await entered.wait()
    assert manager.exec_references.unused([7]) == []
    finish.set()
    await task
    assert manager.exec_references.unused([7]) == [7]


@pytest.mark.asyncio
async def test_reference_collection_preserves_live_and_newly_retired_bindings():
    session = SessionManager()
    first = references.allocate(session, "first", owner="device", hardware_id="kbd")
    references.retire_device(session, "kbd")

    async def reply(_command):
        references.allocate(session, "second", owner="device", hardware_id="kbd")
        references.retire_device(session, "kbd")
        return Response(status="ok", data={"unused_exec_refs": [first, first + 1]})

    session.client.send_command = AsyncMock(side_effect=reply)
    await references.collect_retired(session)
    assert references.resolve(session, first) is None
    assert references.resolve(session, first + 1).cmd == "second"


@pytest.mark.asyncio
async def test_failed_reference_collection_does_not_discard_commands():
    session = SessionManager()
    ref = references.allocate(session, "stop", owner="combo")
    references.retire_combos(session)
    session.client.send_command = AsyncMock(return_value=Response(status="error"))
    await references.collect_retired(session)
    assert references.resolve(session, ref) is not None
    references.clear_all(session)
    assert references.resolve(session, ref) is None


@pytest.mark.asyncio
async def test_reference_collection_rotates_long_held_batch():
    session = SessionManager()
    for _ in range(1025):
        references.allocate(session, "command", owner="device", hardware_id="kbd")
    references.retire_device(session, "kbd")
    session.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"unused_exec_refs": []})
    )
    await references.collect_retired(session)
    first_batch = session.client.send_command.call_args.args[0].data["exec_refs"]
    assert len(first_batch) == 1024
    assert 1025 not in first_batch
    await references.collect_retired(session)
    assert 1025 in session.client.send_command.call_args.args[0].data["exec_refs"]


@pytest.mark.asyncio
async def test_retirement_worker_collects_and_disconnect_cancels_it():
    session = SessionManager()
    session.connected = True
    session.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"unused_exec_refs": [1]})
    )
    references.allocate(session, "command", owner="combo")
    references.retire_combos(session)
    try:
        assert session.exec_state.retirement_task is not None
        await asyncio.wait_for(asyncio.shield(session.exec_state.retirement_task), timeout=3)
        assert session.exec_state.retired_exec_refs == {}
        references.allocate(session, "next", owner="combo")
        references.retire_combos(session)
        task = session.exec_state.retirement_task
        assert task is not None and not task.done()
        references.clear_retired(session)
        await asyncio.sleep(0)
        assert task.cancelled()
        assert session.exec_state.retired_exec_refs == {}
    finally:
        await events.cancel_event_tasks(session)
