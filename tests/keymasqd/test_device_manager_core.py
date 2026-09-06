import asyncio
import errno
import logging
import os
from collections import deque
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import evdev
import pytest

from keymasq.common.ipc import CommandType
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType, DeviceType
from keymasq.common.types import JsonObject
from keymasq.keymasqd import device_manager
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.runtime import (
    adapters,
    device_path_resolver,
    manager_cursor,
    outputs,
    source_hiding,
    topology,
)
from keymasq.keymasqd.runtime.combo import events, lifecycle
from keymasq.keymasqd.runtime.grab import acquisition, planning, release
from keymasq.keymasqd.runtime.grab.state import DesiredGrabConfig, GrabDeviceDeps, GrabRequest
from keymasq.keymasqd.runtime.grabbed_device.types import InputAccessMode
from keymasq.keymasqd.runtime.macro import controls, mouse
from tests.keymasqd.device_manager_support import FakeUInput, make_grabbed_device


@pytest.mark.asyncio
async def test_set_cursor_position_emits_absolute_mouse_move() -> None:
    manager = DeviceManager()
    mouse = FakeUInput()
    manager.output_state.mouse_uinput = mouse  # type: ignore[assignment]

    result = await manager.set_cursor_position(123, 456)

    assert result == {"status": "ok", "x": 123, "y": 456}
    assert mouse.writes == [
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, -2147483648),
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, -2147483648),
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 123),
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, 456),
    ]


@pytest.mark.asyncio
async def test_set_cursor_position_reports_missing_mouse_uinput() -> None:
    manager = DeviceManager()

    assert await manager.set_cursor_position(123, 456) == {
        "status": "error",
        "message": "No mouse uinput device available",
    }


@pytest.mark.asyncio
async def test_grab_device_permission_denied_mentions_input_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DeviceManager()

    def fake_input_device(_path: str):
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(device_manager.evdev, "InputDevice", fake_input_device)
    monkeypatch.setattr(device_manager, "resolve_stable_path", lambda path: path)

    with pytest.raises(PermissionError) as excinfo:
        await manager.grab_device(
            "1234:5678",
            ["/dev/input/event0"],
            {"a": "key_a"},
        )

    message = str(excinfo.value)
    assert "/dev/input/event0" in message
    assert "/dev/input/event*" in message


@pytest.mark.asyncio
async def test_get_cursor_position_uses_broadcast_request_response() -> None:
    broadcast = AsyncMock()
    manager = DeviceManager(broadcast_callback=broadcast)

    task = asyncio.create_task(manager.get_cursor_position(timeout_s=1.0))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    broadcast.assert_awaited_once()
    event_type, payload = broadcast.await_args.args
    assert event_type == CommandType.CURSOR_POSITION_REQUEST
    request_id = payload["request_id"]

    result = manager.handle_cursor_position_response(
        {"request_id": request_id, "status": "ok", "x": 42, "y": 84}
    )

    assert result == {"status": "ok", "matched": True}
    assert await task == (42, 84)


@pytest.mark.asyncio
async def test_get_cursor_position_sends_tracking_hint_when_requested() -> None:
    broadcast = AsyncMock()
    manager = DeviceManager(broadcast_callback=broadcast)

    task = asyncio.create_task(manager.get_cursor_position(timeout_s=1.0, tracking_hint_ms=123))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    broadcast.assert_awaited_once()
    event_type, payload = broadcast.await_args.args
    assert event_type == CommandType.CURSOR_POSITION_REQUEST
    assert payload["tracking_hint_ms"] == 123
    request_id = payload["request_id"]

    result = manager.handle_cursor_position_response(
        {"request_id": request_id, "status": "ok", "x": 10, "y": 20}
    )

    assert result == {"status": "ok", "matched": True}
    assert await task == (10, 20)


@pytest.mark.asyncio
async def test_get_cursor_position_bounds_timeout_by_tracking_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broadcast = AsyncMock()
    manager = DeviceManager(broadcast_callback=broadcast)
    observed_timeout: float | None = None

    async def wait_for_cursor_response(
        _future: asyncio.Future[JsonObject],
        *,
        timeout: float | None = None,
    ) -> JsonObject:
        nonlocal observed_timeout
        observed_timeout = timeout
        return {"status": "ok", "x": 10, "y": 20}

    monkeypatch.setattr(device_manager.asyncio, "wait_for", wait_for_cursor_response)

    assert await manager.get_cursor_position(timeout_s=1.0, tracking_hint_ms=25) == (10, 20)
    assert observed_timeout == pytest.approx(0.025)


@pytest.mark.asyncio
async def test_move_cursor_natural_stops_cursor_tracking_after_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broadcast = AsyncMock()
    manager = DeviceManager(broadcast_callback=broadcast)

    async def move_cursor_naturally(**_kwargs: object) -> JsonObject:
        return {"status": "ok"}

    monkeypatch.setattr(
        manager_cursor.natural_mouse,
        "move_cursor_naturally",
        move_cursor_naturally,
    )

    assert await manager.move_cursor_natural(10, 20, 5000, 0, "linear", 1, 500) == {"status": "ok"}
    broadcast.assert_awaited_once_with(CommandType.CURSOR_POSITION_TRACKING_STOP, {})


@pytest.mark.asyncio
async def test_move_cursor_natural_stops_cursor_tracking_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broadcast = AsyncMock()
    manager = DeviceManager(broadcast_callback=broadcast)

    async def move_cursor_naturally(**_kwargs: object) -> JsonObject:
        raise RuntimeError("move failed")

    monkeypatch.setattr(
        manager_cursor.natural_mouse,
        "move_cursor_naturally",
        move_cursor_naturally,
    )

    with pytest.raises(RuntimeError, match="move failed"):
        await manager.move_cursor_natural(10, 20, 5000, 0, "linear", 1, 500)
    broadcast.assert_awaited_once_with(CommandType.CURSOR_POSITION_TRACKING_STOP, {})


@pytest.mark.asyncio
async def test_cancel_cursor_move_waits_for_active_move_to_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DeviceManager()
    move_started = asyncio.Event()

    async def move_cursor_naturally(**kwargs: object) -> JsonObject:
        should_cancel = cast(Callable[[], bool], kwargs["should_cancel"])
        move_started.set()
        while not should_cancel():
            await asyncio.sleep(0)
        return {"status": "error", "message": "Cursor move cancelled"}

    monkeypatch.setattr(
        manager_cursor.natural_mouse,
        "move_cursor_naturally",
        move_cursor_naturally,
    )

    move_task = asyncio.create_task(manager.move_cursor_natural(10, 20, 5000, 0, "linear", 1, 500))
    await move_started.wait()

    await manager.cancel_cursor_move()

    assert (await move_task)["message"] == "Cursor move cancelled"


@pytest.mark.asyncio
async def test_cancel_cursor_move_cancels_a_move_waiting_on_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DeviceManager()
    move_started = asyncio.Event()
    move_calls: list[int] = []

    async def move_cursor_naturally(**kwargs: object) -> JsonObject:
        move_calls.append(cast(int, kwargs["target_x"]))
        should_cancel = cast(Callable[[], bool], kwargs["should_cancel"])
        move_started.set()
        while not should_cancel():
            await asyncio.sleep(0)
        return {"status": "error", "message": "Cursor move cancelled"}

    monkeypatch.setattr(
        manager_cursor.natural_mouse,
        "move_cursor_naturally",
        move_cursor_naturally,
    )

    active = asyncio.create_task(manager.move_cursor_natural(10, 20, 5000, 0, "linear", 1, 500))
    await move_started.wait()
    queued = asyncio.create_task(manager.move_cursor_natural(30, 40, 5000, 0, "linear", 1, 500))
    await asyncio.sleep(0)

    await manager.cancel_cursor_move()

    assert (await active)["message"] == "Cursor move cancelled"
    assert (await queued)["message"] == "Cursor move cancelled"
    assert move_calls == [10]


@pytest.mark.asyncio
async def test_neutralize_runtime_clears_active_runtime_and_releases_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DeviceManager()
    keyboard = FakeUInput()
    trigger_ends: list[str | None] = []
    device = make_grabbed_device(
        monkeypatch,
        keyboard_uinput=keyboard,
        profile_activation_trigger_end_observer=trigger_ends.append,
    )
    manager.grabbed_devices[device.hardware_id] = [device]
    manager.output_state.keyboard_uinput = keyboard  # type: ignore[assignment]

    device.state.held_source_keys.update({"key_a", "key_b"})
    device.state.held_source_actions["key_b"] = MappingAction(
        action_type=ActionType.KEYBOARD,
        target="key_f13",
    )
    device.state.held_profile_trigger_events.add("key_c")
    device.state.repeat_active_actions["key_r"] = MappingAction(
        action_type=ActionType.REPEAT,
    )
    device.state.motion_frame_values["imu"] = {"gyro": {"yaw": 1.0}}
    device.state.motion_last_frame_ns["motion:imu"] = 123
    device.state.passthrough_frame_output = object()
    device.state.held_output_keys["keyboard"].add(evdev.ecodes.KEY_F13)
    manager.combo_state.held_output_keys["keyboard"].add(evdev.ecodes.KEY_F14)
    manager.combo_state.superkey_output_refcounts["keyboard"][evdev.ecodes.KEY_F14] = 1

    cancel_macros = AsyncMock(return_value={"status": "ok"})
    clear_combos = AsyncMock()
    cancel_cursor = AsyncMock()
    reset_analog = AsyncMock()
    reset_superkeys = AsyncMock()
    monkeypatch.setattr(manager, "cancel_macro_playback", cancel_macros)
    monkeypatch.setattr(lifecycle, "clear_combo_runtime", clear_combos)
    monkeypatch.setattr(manager, "cancel_cursor_move", cancel_cursor)
    monkeypatch.setattr(device, "reset_analog_controls", reset_analog)
    monkeypatch.setattr(device, "reset_superkeys", reset_superkeys)
    manager.pause_runtime_input()

    result = await manager.neutralize_runtime()

    assert result == {"status": "ok", "neutralized": True}
    assert manager.runtime_input_paused() is True
    assert cancel_macros.await_count == 2
    clear_combos.assert_awaited_once()
    cancel_cursor.assert_awaited_once()
    reset_analog.assert_awaited_once()
    reset_superkeys.assert_awaited_once()
    assert trigger_ends == [
        f"{device.hardware_id}:key_a",
        f"{device.hardware_id}:key_b",
        f"{device.hardware_id}:key_c",
    ]
    assert device.state.quarantined_source_keys == {"key_a", "key_b"}
    assert device.state.repeat_active_actions == {}
    assert device.state.motion_frame_values == {}
    assert device.state.motion_last_frame_ns == {}
    assert device.state.passthrough_frame_output is None
    assert keyboard.writes == [
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 0),
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0),
    ]


@pytest.mark.asyncio
async def test_neutralize_runtime_continues_cleanup_after_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DeviceManager()
    device = make_grabbed_device(monkeypatch)
    manager.grabbed_devices[device.hardware_id] = [device]
    first_error = RuntimeError("macro cancellation failed")
    cancel_macros = AsyncMock(side_effect=[first_error, {"status": "ok"}])
    reset_analog = AsyncMock(side_effect=RuntimeError("analog reset failed"))
    reset_superkeys = AsyncMock()
    release_outputs = Mock()
    monkeypatch.setattr(manager, "cancel_macro_playback", cancel_macros)
    monkeypatch.setattr(
        lifecycle,
        "clear_combo_runtime",
        AsyncMock(side_effect=RuntimeError("combo reset failed")),
    )
    monkeypatch.setattr(manager, "cancel_cursor_move", AsyncMock())
    monkeypatch.setattr(device, "reset_analog_controls", reset_analog)
    monkeypatch.setattr(device, "reset_superkeys", reset_superkeys)
    monkeypatch.setattr(device, "release_tracked_outputs", release_outputs)

    with pytest.raises(RuntimeError, match="macro cancellation failed") as excinfo:
        await manager.neutralize_runtime()

    assert excinfo.value is first_error
    assert cancel_macros.await_count == 2
    reset_analog.assert_awaited_once()
    reset_superkeys.assert_awaited_once()
    release_outputs.assert_called_once()
    assert manager.runtime_input_paused() is False


@pytest.mark.asyncio
async def test_device_runtime_status_reports_live_and_grabbed_interfaces() -> None:
    manager = DeviceManager()
    manager.topology_state.live_snapshot = {
        "/dev/input/by-id/pad-event-joystick": topology.LiveInterfaceInfo(
            hardware_id="1234:5678",
            vendor_id="1234",
            product_id="5678",
            stable_path="/dev/input/by-id/pad-event-joystick",
            path="/dev/input/event10",
            interface_id="gamepad",
            phys="usb-test/input0",
            device_type="gamepad",
            capabilities=("btn_south",),
        )
    }
    manager.grabbed_devices["1234:5678"] = [
        SimpleNamespace(
            interface_id="gamepad",
            path="/dev/input/by-id/pad-event-joystick",
            resolved_event_path="/dev/input/event10",
            stable_path="/dev/input/by-id/pad-event-joystick",
            device_type=DeviceType.GAMEPAD,
        )
    ]

    result = await manager.device_runtime_status()

    assert result == {
        "status": "ok",
        "interfaces": [
            {
                "hardware_id": "1234:5678",
                "vendor_id": "1234",
                "product_id": "5678",
                "stable_path": "/dev/input/by-id/pad-event-joystick",
                "path": "/dev/input/event10",
                "interface_id": "gamepad",
                "phys": "usb-test/input0",
                "device_type": "gamepad",
                "capabilities": ["btn_south"],
            }
        ],
        "grabbed_interfaces": [
            {
                "hardware_id": "1234:5678",
                "interface_id": "gamepad",
                "path": "/dev/input/by-id/pad-event-joystick",
                "resolved_path": "/dev/input/event10",
                "stable_path": "/dev/input/by-id/pad-event-joystick",
                "device_type": "gamepad",
            }
        ],
    }


@pytest.mark.asyncio
async def test_device_inspector_suppression_status_broadcasts() -> None:
    manager = DeviceManager()
    broadcasts: list[tuple[CommandType, dict[str, object]]] = []
    manager._broadcast_runtime_event = lambda event_type, data: broadcasts.append(  # type: ignore[method-assign]
        (event_type, data)
    )

    started = await manager.start_device_inspector("1234:5678")
    enabled = await manager.enable_device_inspector_suppression("1234:5678")
    disabled = await manager.disable_device_inspector_suppression("1234:5678", "key_esc")

    assert started["active"] is True
    assert started["suppressed"] is False
    assert enabled["suppressed"] is True
    assert disabled == {
        "status": "ok",
        "hardware_id": "1234:5678",
        "active": True,
        "suppressed": False,
        "reason": "key_esc",
    }
    assert broadcasts == [
        (
            CommandType.DEVICE_INSPECTOR_STATUS,
            {
                "hardware_id": "1234:5678",
                "active": True,
                "suppressed": False,
                "reason": "start",
            },
        ),
        (
            CommandType.DEVICE_INSPECTOR_STATUS,
            {
                "hardware_id": "1234:5678",
                "active": True,
                "suppressed": True,
                "reason": "enable_suppression",
            },
        ),
        (
            CommandType.DEVICE_INSPECTOR_STATUS,
            {
                "hardware_id": "1234:5678",
                "active": True,
                "suppressed": False,
                "reason": "key_esc",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_device_inspector_disable_does_not_activate_inactive_inspector() -> None:
    manager = DeviceManager()
    broadcasts: list[tuple[CommandType, dict[str, object]]] = []
    manager._broadcast_runtime_event = lambda event_type, data: broadcasts.append(  # type: ignore[method-assign]
        (event_type, data)
    )

    disabled = await manager.disable_device_inspector_suppression("1234:5678", "manual")

    assert disabled == {
        "status": "ok",
        "hardware_id": "1234:5678",
        "active": False,
        "suppressed": False,
        "reason": "manual",
    }
    assert "1234:5678" not in manager.device_inspector_state.active_hardware_ids
    assert broadcasts == [
        (
            CommandType.DEVICE_INSPECTOR_STATUS,
            {
                "hardware_id": "1234:5678",
                "active": False,
                "suppressed": False,
                "reason": "manual",
            },
        )
    ]


@pytest.mark.asyncio
async def test_release_all_devices_clears_device_inspector_state() -> None:
    manager = DeviceManager()
    manager.device_inspector_state.active_hardware_ids.add("1234:5678")
    manager.device_inspector_state.suppressed_hardware_ids.add("1234:5678")

    await manager.release_all_devices()

    assert manager.device_inspector_state.active_hardware_ids == set()
    assert manager.device_inspector_state.suppressed_hardware_ids == set()


@pytest.mark.asyncio
async def test_profile_activation_timeout_broadcasts_deactivate_requested() -> None:
    events: list[tuple[CommandType, dict[str, object]]] = []
    deactivate_event = asyncio.Event()
    expected_deactivate = (
        CommandType.PROFILE_DEACTIVATE_REQUESTED,
        {
            "profile_name": "Nav",
            "activation_id": "activation-1",
            "reason": "timeout",
        },
    )

    async def broadcast(event_type: CommandType, data: dict[str, object]) -> None:
        event = (event_type, data)
        events.append(event)
        if event == expected_deactivate:
            deactivate_event.set()

    manager = DeviceManager(broadcast_callback=broadcast)

    await manager.track_profile_activation(
        "Nav",
        "activation-1",
        "trigger-1",
        {"timeout_ms": 1},
    )
    await asyncio.wait_for(deactivate_event.wait(), timeout=1.0)

    assert events == [expected_deactivate]


@pytest.mark.asyncio
@pytest.mark.parametrize("source_profile_name", ["Other", "Nav", None])
async def test_profile_activation_action_count_consumes_any_recorded_action(
    source_profile_name: str | None,
) -> None:
    events: list[tuple[CommandType, dict[str, object]]] = []
    deactivate_event = asyncio.Event()
    expected_deactivate = (
        CommandType.PROFILE_DEACTIVATE_REQUESTED,
        {
            "profile_name": "Nav",
            "activation_id": "activation-1",
            "reason": "action_count",
        },
    )

    async def broadcast(event_type: CommandType, data: dict[str, object]) -> None:
        event = (event_type, data)
        events.append(event)
        if event == expected_deactivate:
            deactivate_event.set()

    manager = DeviceManager(broadcast_callback=broadcast)

    await manager.track_profile_activation(
        "Nav",
        "activation-1",
        "trigger-1",
        {"after_actions": 2},
    )
    manager.record_profile_action(source_profile_name)
    # Expiry schedules the manager callback, which schedules the broadcast task.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert events == []

    manager.record_profile_action(source_profile_name)
    await asyncio.wait_for(deactivate_event.wait(), timeout=1.0)

    assert events == [expected_deactivate]

    manager.record_profile_action(source_profile_name)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert events == [expected_deactivate]


@pytest.mark.asyncio
async def test_profile_activation_action_count_ignores_activation_trigger() -> None:
    events: list[tuple[CommandType, dict[str, object]]] = []
    deactivate_event = asyncio.Event()

    async def broadcast(event_type: CommandType, data: dict[str, object]) -> None:
        events.append((event_type, data))
        deactivate_event.set()

    manager = DeviceManager(broadcast_callback=broadcast)

    await manager.track_profile_activation(
        "Nav",
        "activation-1",
        "1234:5678:key_capslock",
        {"after_actions": 2},
    )
    for trigger_id in ("1234:5678:key_capslock", "1234:5678:key_a"):
        manager.record_profile_action(None, trigger_id)
        # Let both the expiry callback and its broadcast task run before asserting.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert events == []

    manager.record_profile_action(None, "1234:5678:key_b")
    await asyncio.wait_for(deactivate_event.wait(), timeout=1.0)
    assert events == [
        (
            CommandType.PROFILE_DEACTIVATE_REQUESTED,
            {
                "profile_name": "Nav",
                "activation_id": "activation-1",
                "reason": "action_count",
            },
        )
    ]


@pytest.mark.asyncio
async def test_profile_activation_trigger_end_broadcasts_deactivate_requested() -> None:
    events: list[tuple[CommandType, dict[str, object]]] = []
    deactivate_event = asyncio.Event()
    expected_deactivate = (
        CommandType.PROFILE_DEACTIVATE_REQUESTED,
        {
            "profile_name": "Nav",
            "activation_id": "activation-1",
            "reason": "trigger_end",
        },
    )

    async def broadcast(event_type: CommandType, data: dict[str, object]) -> None:
        event = (event_type, data)
        events.append(event)
        if event == expected_deactivate:
            deactivate_event.set()

    manager = DeviceManager(broadcast_callback=broadcast)
    manager.observe_profile_trigger_start("trigger-1")
    await manager.track_profile_activation(
        "Nav",
        "activation-1",
        "trigger-1",
        {"on_trigger_end": True},
    )
    manager.observe_profile_trigger_end("trigger-1")
    await asyncio.wait_for(deactivate_event.wait(), timeout=1.0)

    assert events == [expected_deactivate]


requires_uinput = pytest.mark.skipif(
    not os.access("/dev/uinput", os.W_OK),
    reason="No uinput access",
)


class TestDeviceManager:
    @pytest.fixture
    def manager(self):
        return DeviceManager()

    @pytest.mark.asyncio
    async def test_list_devices(self, manager):
        result = await manager.list_devices()

        assert "devices" in result
        assert isinstance(result["devices"], list)

    @pytest.mark.asyncio
    @requires_uinput
    async def test_grab_virtual_device(self, manager, virtual_mouse):
        device_path = virtual_mouse.device.path

        result = await manager.grab_device(
            hardware_id="1234:5678",
            evdev_paths=[device_path],
            button_map={"btn_left": "btn_left", "btn_right": "btn_right"},
        )

        assert result["grabbed"] is True
        assert result["hardware_id"] == "1234:5678"

        assert "1234:5678" in manager.grabbed_devices

        await manager.release_device("1234:5678")

    @pytest.mark.asyncio
    async def test_grab_device_keeps_desired_state_when_interface_is_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        create_global_uinputs = Mock()
        destroy_global_uinputs = Mock()

        def _missing_input_device(_path: str):
            raise FileNotFoundError(errno.ENOENT, "missing")

        monkeypatch.setattr(device_manager, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(device_manager.evdev, "InputDevice", _missing_input_device)
        monkeypatch.setattr(outputs, "create_global_uinputs", create_global_uinputs)
        monkeypatch.setattr(outputs, "destroy_global_uinputs", destroy_global_uinputs)

        result = await manager.grab_device(
            hardware_id="1234:5678",
            evdev_paths=["/dev/input/event404"],
            button_map={"btn_side": "btn_side"},
        )

        assert result == {
            "grabbed": True,
            "hardware_id": "1234:5678",
            "grabbed_count": 0,
            "skipped_count": 0,
            "waiting_for_device": True,
        }
        assert manager.grabbed_devices == {}
        assert manager.grab_state.desired_paths["1234:5678"] == {"/dev/input/event404"}
        assert manager.grab_state.desired_grabs["1234:5678"] == DesiredGrabConfig(
            paths={"/dev/input/event404"},
            button_map={"btn_side": "btn_side"},
            force_grab_unmapped=False,
        )
        create_global_uinputs.assert_not_called()

    @pytest.mark.asyncio
    async def test_grab_device_waits_when_keymasq_path_is_unresolved(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        enable_hotplug_hiding = AsyncMock()
        monkeypatch.setattr(device_manager.evdev, "list_devices", lambda: [])
        monkeypatch.setattr(
            source_hiding,
            "enable_hardware_hotplug_hiding",
            enable_hotplug_hiding,
        )
        evdev_interfaces = [{"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}]

        result = await manager.grab_device(
            hardware_id="2dc8:3106",
            evdev_paths=["keymasq:2dc8:3106"],
            evdev_interfaces=evdev_interfaces,
            button_map={"btn_south": "btn_south"},
        )

        assert result == {
            "grabbed": True,
            "hardware_id": "2dc8:3106",
            "grabbed_count": 0,
            "skipped_count": 0,
            "waiting_for_device": True,
        }
        assert manager.grab_state.desired_paths["2dc8:3106"] == {"keymasq:2dc8:3106"}
        assert manager.grab_state.desired_grabs["2dc8:3106"] == DesiredGrabConfig(
            paths={"keymasq:2dc8:3106"},
            button_map={"btn_south": "btn_south"},
            force_grab_unmapped=False,
            evdev_interfaces=evdev_interfaces,
        )
        enable_hotplug_hiding.assert_awaited_once_with("2dc8:3106")

    @pytest.mark.asyncio
    async def test_grab_device_logs_hotplug_hiding_enable_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        manager = DeviceManager()
        enable_hotplug_hiding = AsyncMock(side_effect=RuntimeError("udev failed"))
        monkeypatch.setattr(device_manager.evdev, "list_devices", lambda: [])
        monkeypatch.setattr(
            source_hiding,
            "enable_hardware_hotplug_hiding",
            enable_hotplug_hiding,
        )
        evdev_interfaces = [{"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}]

        with caplog.at_level(logging.ERROR, logger="keymasqd.devices"):
            result = await manager.grab_device(
                hardware_id="2dc8:3106",
                evdev_paths=["keymasq:2dc8:3106"],
                evdev_interfaces=evdev_interfaces,
                button_map={"btn_south": "btn_south"},
            )

        assert result["waiting_for_device"] is True
        assert manager.grab_state.desired_paths["2dc8:3106"] == {"keymasq:2dc8:3106"}
        enable_hotplug_hiding.assert_awaited_once_with("2dc8:3106")
        assert "Failed to enable source-hiding hotplug state" in caplog.text
        assert "hardware_id=2dc8:3106" in caplog.text

    @pytest.mark.asyncio
    async def test_grab_device_waits_for_explicit_gamepad_without_hotplug_hiding(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        explicit_path = "/dev/input/by-id/missing-pad-event-joystick"
        enable_hotplug_hiding = AsyncMock()

        def missing_device(_path: str):
            raise OSError(errno.ENOENT, "missing")

        monkeypatch.setattr(device_manager, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(
            source_hiding,
            "enable_hardware_hotplug_hiding",
            enable_hotplug_hiding,
        )
        manager._device_input = missing_device  # type: ignore[method-assign]
        evdev_interfaces = [{"id": "gamepad", "path": explicit_path, "type": "gamepad"}]

        result = await manager.grab_device(
            hardware_id="2dc8:3106",
            evdev_paths=[explicit_path],
            evdev_interfaces=evdev_interfaces,
            button_map={"btn_south": "btn_south"},
        )

        assert result["waiting_for_device"] is True
        assert manager.grab_state.desired_paths["2dc8:3106"] == {explicit_path}
        enable_hotplug_hiding.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_grab_device_waits_when_raw_interfaces_do_not_resolve(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        path = "/dev/input/event404"
        disable_hotplug_hiding = AsyncMock()
        monkeypatch.setattr(
            device_path_resolver,
            "resolve_evdev_interfaces",
            lambda *_args, **_kwargs: [],
        )
        monkeypatch.setattr(
            source_hiding,
            "disable_hardware_hotplug_hiding",
            disable_hotplug_hiding,
        )

        result = await manager.grab_device(
            hardware_id="1234:5678",
            evdev_paths=[],
            evdev_interfaces=[{"id": "kbd", "path": path, "type": "keyboard"}],
            button_map={"a": "key_a"},
        )

        assert result == {
            "grabbed": True,
            "hardware_id": "1234:5678",
            "grabbed_count": 0,
            "skipped_count": 0,
            "waiting_for_device": True,
        }
        assert manager.grab_state.desired_paths["1234:5678"] == {path}
        disable_hotplug_hiding.assert_awaited_once_with("1234:5678")

    @pytest.mark.asyncio
    async def test_release_device_disables_waiting_gamepad_hotplug_hiding(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        disable_hotplug_hiding = AsyncMock()
        evdev_interfaces = [{"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}]
        manager.grab_state.desired_paths["2dc8:3106"] = {"keymasq:2dc8:3106"}
        manager.grab_state.desired_grabs["2dc8:3106"] = DesiredGrabConfig(
            paths={"keymasq:2dc8:3106"},
            button_map={"btn_south": "btn_south"},
            evdev_interfaces=evdev_interfaces,
        )
        monkeypatch.setattr(
            source_hiding,
            "disable_hardware_hotplug_hiding",
            disable_hotplug_hiding,
        )

        result = await manager.release_device("2dc8:3106")

        assert result == {"released": True, "hardware_id": "2dc8:3106"}
        disable_hotplug_hiding.assert_awaited_once_with("2dc8:3106")

    @pytest.mark.asyncio
    async def test_release_waiting_device_does_not_destroy_global_outputs(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        destroy_global_uinputs = Mock()
        manager.output_state.device_count = 1
        manager.grab_state.desired_paths["045e:02a1@2"] = {
            "keymasq:045e:02a1@2",
        }
        manager.grab_state.desired_grabs["045e:02a1@2"] = DesiredGrabConfig(
            paths={"keymasq:045e:02a1@2"},
            button_map={"btn_south": "btn_south"},
            evdev_interfaces=[
                {
                    "id": "gamepad",
                    "path": "keymasq:045e:02a1@2",
                    "type": "gamepad",
                }
            ],
        )
        monkeypatch.setattr(outputs, "destroy_global_uinputs", destroy_global_uinputs)

        result = await manager.release_device("045e:02a1@2", immediate=True)

        assert result == {"released": True, "hardware_id": "045e:02a1@2"}
        destroy_global_uinputs.assert_not_called()
        assert manager.output_state.device_count == 1
        assert "045e:02a1@2" not in manager.grab_state.desired_paths
        assert "045e:02a1@2" not in manager.grab_state.desired_grabs

    @pytest.mark.asyncio
    async def test_release_device_logs_hotplug_hiding_disable_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        manager = DeviceManager()
        disable_hotplug_hiding = AsyncMock(side_effect=RuntimeError("udev failed"))
        evdev_interfaces = [{"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}]
        manager.grab_state.desired_paths["2dc8:3106"] = {"keymasq:2dc8:3106"}
        manager.grab_state.desired_grabs["2dc8:3106"] = DesiredGrabConfig(
            paths={"keymasq:2dc8:3106"},
            button_map={"btn_south": "btn_south"},
            evdev_interfaces=evdev_interfaces,
        )
        monkeypatch.setattr(
            source_hiding,
            "disable_hardware_hotplug_hiding",
            disable_hotplug_hiding,
        )

        with caplog.at_level(logging.ERROR, logger="keymasqd.devices"):
            result = await manager.release_device("2dc8:3106")

        assert result == {"released": True, "hardware_id": "2dc8:3106"}
        assert "2dc8:3106" not in manager.grab_state.desired_paths
        assert "2dc8:3106" not in manager.grab_state.desired_grabs
        disable_hotplug_hiding.assert_awaited_once_with("2dc8:3106")
        assert "Failed to disable source-hiding hotplug state" in caplog.text
        assert "hardware_id=2dc8:3106" in caplog.text

    @pytest.mark.asyncio
    async def test_release_device_keeps_hotplug_hiding_for_same_base_desired_gamepad(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        disable_hotplug_hiding = AsyncMock()
        evdev_interfaces = [{"id": "gamepad", "path": "keymasq:045e:02a1", "type": "gamepad"}]
        manager.grab_state.desired_paths["045e:02a1@0"] = {"keymasq:045e:02a1"}
        manager.grab_state.desired_grabs["045e:02a1@0"] = DesiredGrabConfig(
            paths={"keymasq:045e:02a1"},
            button_map={"btn_south": "btn_south"},
            evdev_interfaces=evdev_interfaces,
        )
        manager.grab_state.desired_paths["045e:02a1@1"] = {"keymasq:045e:02a1"}
        manager.grab_state.desired_grabs["045e:02a1@1"] = DesiredGrabConfig(
            paths={"keymasq:045e:02a1"},
            button_map={"btn_south": "btn_south"},
            evdev_interfaces=evdev_interfaces,
        )
        monkeypatch.setattr(
            source_hiding,
            "disable_hardware_hotplug_hiding",
            disable_hotplug_hiding,
        )

        result = await manager.release_device("045e:02a1@0")

        assert result == {"released": True, "hardware_id": "045e:02a1@0"}
        disable_hotplug_hiding.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_grab_device_excludes_paths_grabbed_by_other_hardware(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        manager.grabbed_devices["other"] = [
            SimpleNamespace(
                path="/dev/input/by-id/claimed-pad",
                stable_path="/dev/input/by-id/claimed-pad",
                resolved_event_path="/dev/input/event2",
            )
        ]
        enable_hotplug_hiding = AsyncMock()
        captured: dict[str, object] = {}

        def fake_resolve_evdev_interfaces(interfaces, **kwargs):
            captured["interfaces"] = interfaces
            captured["excluded_paths"] = kwargs.get("excluded_paths")
            captured["deps"] = kwargs.get("deps")
            captured["match_model_gamepads"] = kwargs.get("match_model_gamepads")
            return []

        monkeypatch.setattr(
            device_path_resolver,
            "resolve_evdev_interfaces",
            fake_resolve_evdev_interfaces,
        )
        monkeypatch.setattr(device_manager, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(
            source_hiding,
            "enable_hardware_hotplug_hiding",
            enable_hotplug_hiding,
        )
        evdev_interfaces = [{"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}]

        result = await manager.grab_device(
            hardware_id="2dc8:3106",
            evdev_paths=["keymasq:2dc8:3106"],
            evdev_interfaces=evdev_interfaces,
            button_map={"btn_south": "btn_south"},
        )

        assert result["waiting_for_device"] is True
        assert captured["interfaces"] == evdev_interfaces
        assert captured["excluded_paths"] == {
            "/dev/input/event2",
            "/dev/input/by-id/claimed-pad",
        }
        assert captured["match_model_gamepads"] is True
        deps = captured["deps"]
        assert isinstance(deps, device_path_resolver.DevicePathResolverDeps)
        assert callable(deps.resolve_stable_path_fn)
        enable_hotplug_hiding.assert_awaited_once_with("2dc8:3106")

    @pytest.mark.asyncio
    async def test_grab_device_excludes_hidden_grabbed_source_live_aliases(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        manager.grabbed_devices["other"] = [
            SimpleNamespace(
                path="/dev/input/by-id/pre-hide-pad",
                stable_path="/dev/input/by-id/pre-hide-pad",
                resolved_event_path="/dev/input/event22",
            )
        ]
        captured: dict[str, object] = {}

        def fake_resolve_stable_path(path: str) -> str:
            if path == "/dev/input/event22":
                return "/dev/input/by-id/current-pad"
            return path

        def fake_resolve_evdev_interfaces(_interfaces, **kwargs):
            captured["excluded_paths"] = kwargs.get("excluded_paths")
            return []

        monkeypatch.setattr(device_manager, "resolve_stable_path", fake_resolve_stable_path)
        monkeypatch.setattr(
            device_path_resolver,
            "resolve_evdev_interfaces",
            fake_resolve_evdev_interfaces,
        )
        monkeypatch.setattr(
            source_hiding,
            "enable_hardware_hotplug_hiding",
            AsyncMock(),
        )

        result = await manager.grab_device(
            hardware_id="2dc8:3106",
            evdev_paths=["keymasq:2dc8:3106"],
            evdev_interfaces=[{"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}],
            button_map={"btn_south": "btn_south"},
        )

        assert result["waiting_for_device"] is True
        assert captured["excluded_paths"] == {
            "/dev/input/by-id/pre-hide-pad",
            "/dev/input/event22",
            "/dev/input/by-id/current-pad",
        }

    def test_grabbed_paths_for_hardware_include_hidden_source_live_aliases(self) -> None:
        manager = SimpleNamespace(
            grabbed_devices={
                "2dc8:3106": [
                    SimpleNamespace(
                        path="/dev/input/by-id/pre-hide-pad",
                        stable_path="/dev/input/by-id/pre-hide-pad",
                        resolved_event_path="/dev/input/event22",
                    )
                ]
            }
        )

        paths = planning.grabbed_paths_for_hardware(
            manager,
            "2dc8:3106",
            resolve_stable_path_fn=lambda path: (
                "/dev/input/by-id/current-pad" if path == "/dev/input/event22" else path
            ),
        )

        assert paths == {
            "/dev/input/by-id/pre-hide-pad",
            "/dev/input/event22",
            "/dev/input/by-id/current-pad",
        }

    @pytest.mark.asyncio
    async def test_grab_device_logical_path_matches_same_vid_pid_gamepads_only(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        logical_path = "keymasq:2dc8:3106"
        paths = ["/dev/input/event2", "/dev/input/event3", "/dev/input/event4"]

        class _InputDevice:
            def __init__(self, path: str) -> None:
                self.path = path
                self.name = f"Pad {path.rsplit('event', 1)[-1]}"
                self.phys = f"usb-{path.rsplit('event', 1)[-1]}/input0"
                self.info = SimpleNamespace(vendor=0x2DC8, product=0x3106)

            def capabilities(self) -> dict[int, list[int]]:
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SOUTH],
                    evdev.ecodes.EV_ABS: [evdev.ecodes.ABS_X, evdev.ecodes.ABS_Y],
                }

            def input_props(self) -> list[int]:
                return []

            def close(self) -> None:
                return

        class _GrabbedDevice:
            def __init__(self, **kwargs) -> None:
                self.path = kwargs["path"]
                self.hardware_id = kwargs["hardware_id"]
                self.stable_path = self.path
                self.resolved_event_path = self.path
                self.interface_id = kwargs.get("interface_id", "")

            async def grab(self) -> None:
                return

            async def release(self) -> None:
                return

            def update_button_map(self, *args, **kwargs) -> None:
                return

            def update_analog_inputs(self, _inputs) -> None:
                return

        monkeypatch.setattr(device_manager.evdev, "list_devices", lambda: list(paths))
        monkeypatch.setattr(device_manager, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(device_path_resolver, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(device_manager, "GrabbedDevice", _GrabbedDevice)
        monkeypatch.setattr(device_manager, "_device_input", lambda path: _InputDevice(path))
        monkeypatch.setattr(outputs, "create_global_uinputs", Mock())
        enable_hotplug_hiding = AsyncMock()
        disable_hotplug_hiding = AsyncMock()
        monkeypatch.setattr(
            source_hiding,
            "enable_hardware_hotplug_hiding",
            enable_hotplug_hiding,
        )
        monkeypatch.setattr(
            source_hiding,
            "disable_hardware_hotplug_hiding",
            disable_hotplug_hiding,
        )
        manager._device_input = lambda path: _InputDevice(path)  # type: ignore[method-assign]

        evdev_interfaces = [
            {
                "id": "gamepad",
                "path": logical_path,
                "type": "gamepad",
                "phys": "usb-2/input0",
                "capabilities": ["btn_south", "abs_x", "abs_y"],
            }
        ]

        first = await manager.grab_device(
            hardware_id="2dc8:3106",
            evdev_paths=[logical_path],
            evdev_interfaces=[
                dict(evdev_interfaces[0]),
            ],
            button_map={"btn_south": "btn_south"},
            button_codes={"btn_south": evdev.ecodes.BTN_SOUTH},
        )
        second = await manager.grab_device(
            hardware_id="2dc8:3106@2",
            evdev_paths=[logical_path],
            evdev_interfaces=[
                dict(evdev_interfaces[0]),
            ],
            button_map={"btn_south": "btn_south"},
            button_codes={"btn_south": evdev.ecodes.BTN_SOUTH},
        )

        assert first["grabbed_count"] == 1
        assert second["grabbed_count"] == 1
        assert [device.path for device in manager.grabbed_devices["2dc8:3106"]] == [
            "/dev/input/event2"
        ]
        assert [device.path for device in manager.grabbed_devices["2dc8:3106@2"]] == [
            "/dev/input/event3"
        ]
        grabbed_paths = {
            device.path for devices in manager.grabbed_devices.values() for device in devices
        }
        assert "/dev/input/event4" not in grabbed_paths
        enable_hotplug_hiding.assert_not_awaited()
        assert [args.args[0] for args in disable_hotplug_hiding.await_args_list] == [
            "2dc8:3106",
            "2dc8:3106@2",
        ]

    @pytest.mark.asyncio
    async def test_grab_device_honors_explicit_gamepad_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        explicit_path = "/dev/input/by-id/test-pad-if02-event-joystick"
        paths = ["/dev/input/event2", "/dev/input/event3"]

        class _InputDevice:
            def __init__(self, path: str) -> None:
                self.path = path
                self.name = f"Pad {path.rsplit('/', 1)[-1]}"
                self.phys = "usb-explicit/input0"
                self.info = SimpleNamespace(vendor=0x2DC8, product=0x3106)

            def capabilities(self) -> dict[int, list[int]]:
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SOUTH],
                    evdev.ecodes.EV_ABS: [evdev.ecodes.ABS_X, evdev.ecodes.ABS_Y],
                }

            def input_props(self) -> list[int]:
                return []

            def close(self) -> None:
                return

        class _GrabbedDevice:
            def __init__(self, **kwargs) -> None:
                self.path = kwargs["path"]
                self.hardware_id = kwargs["hardware_id"]
                self.stable_path = self.path
                self.resolved_event_path = self.path
                self.interface_id = kwargs.get("interface_id", "")

            async def grab(self) -> None:
                return

            async def release(self) -> None:
                return

            def update_button_map(self, *args, **kwargs) -> None:
                return

            def update_analog_inputs(self, _inputs) -> None:
                return

        monkeypatch.setattr(device_manager.evdev, "list_devices", lambda: list(paths))
        monkeypatch.setattr(device_manager, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(device_path_resolver, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(device_manager, "GrabbedDevice", _GrabbedDevice)
        monkeypatch.setattr(device_manager, "_device_input", lambda path: _InputDevice(path))
        monkeypatch.setattr(outputs, "create_global_uinputs", Mock())
        disable_hotplug_hiding = AsyncMock()
        monkeypatch.setattr(
            source_hiding,
            "disable_hardware_hotplug_hiding",
            disable_hotplug_hiding,
        )
        manager._device_input = lambda path: _InputDevice(path)  # type: ignore[method-assign]

        result = await manager.grab_device(
            hardware_id="2dc8:3106",
            evdev_paths=[explicit_path],
            evdev_interfaces=[
                {
                    "id": "gamepad",
                    "path": explicit_path,
                    "type": "gamepad",
                    "phys": "usb-explicit/input0",
                    "capabilities": ["btn_south", "abs_x", "abs_y"],
                }
            ],
            button_map={"btn_south": "btn_south"},
            button_codes={"btn_south": evdev.ecodes.BTN_SOUTH},
        )

        assert result["grabbed_count"] == 1
        assert [device.path for device in manager.grabbed_devices["2dc8:3106"]] == [explicit_path]
        assert "/dev/input/event2" not in {
            device.path for devices in manager.grabbed_devices.values() for device in devices
        }
        disable_hotplug_hiding.assert_awaited_once_with("2dc8:3106")

    @pytest.mark.asyncio
    async def test_grab_device_registers_new_device_before_grab_retry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        path = "/dev/input/event2"
        observed_paths: list[list[str]] = []

        class _InputDevice:
            name = "Pad"
            phys = "usb-pad/input0"
            info = SimpleNamespace(vendor=0x2DC8, product=0x3106)

            def capabilities(self) -> dict[int, list[int]]:
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SOUTH],
                    evdev.ecodes.EV_ABS: [evdev.ecodes.ABS_X, evdev.ecodes.ABS_Y],
                }

            def input_props(self) -> list[int]:
                return []

            def close(self) -> None:
                return

        class _GrabbedDevice:
            def __init__(self, **kwargs) -> None:
                self.path = kwargs["path"]
                self.hardware_id = kwargs["hardware_id"]
                self.stable_path = self.path
                self.resolved_event_path = self.path
                self.interface_id = kwargs.get("interface_id", "")

            async def release(self) -> None:
                return

            def update_button_map(self, *args, **kwargs) -> None:
                return

            def update_analog_inputs(self, _inputs) -> None:
                return

        async def fake_grab_with_retry(device, *_args, **_kwargs) -> None:
            observed_paths.append(
                [grabbed.path for grabbed in manager.grabbed_devices["2dc8:3106"]]
            )
            assert device in manager.grabbed_devices["2dc8:3106"]

        monkeypatch.setattr(device_manager, "resolve_stable_path", lambda value: value)
        monkeypatch.setattr(device_manager, "GrabbedDevice", _GrabbedDevice)
        monkeypatch.setattr(acquisition, "grab_with_retry", fake_grab_with_retry)
        monkeypatch.setattr(outputs, "create_global_uinputs", Mock())
        monkeypatch.setattr(
            source_hiding,
            "disable_hardware_hotplug_hiding",
            AsyncMock(),
        )
        manager._device_input = lambda _path: _InputDevice()  # type: ignore[method-assign]

        result = await manager.grab_device(
            hardware_id="2dc8:3106",
            evdev_paths=[path],
            evdev_interfaces=[{"id": "gamepad", "path": path, "type": "gamepad"}],
            button_map={"btn_south": "btn_south"},
            button_codes={"btn_south": evdev.ecodes.BTN_SOUTH},
        )

        assert result["grabbed_count"] == 1
        assert observed_paths == [[path]]

    @pytest.mark.asyncio
    async def test_grab_device_creates_global_uinputs_once_for_multiple_interfaces(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        paths = ["/dev/input/event2", "/dev/input/event3"]
        create_global_uinputs = Mock()

        class _InputDevice:
            name = "Pad"
            phys = "usb-pad/input0"
            info = SimpleNamespace(vendor=0x2DC8, product=0x3106)

            def capabilities(self) -> dict[int, list[int]]:
                return {evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SOUTH]}

            def input_props(self) -> list[int]:
                return []

            def close(self) -> None:
                return

        class _GrabbedDevice:
            def __init__(self, **kwargs) -> None:
                self.path = kwargs["path"]
                self.hardware_id = kwargs["hardware_id"]
                self.stable_path = self.path
                self.resolved_event_path = self.path
                self.interface_id = kwargs.get("interface_id", "")

            async def grab(self) -> None:
                return

            async def release(self) -> None:
                return

            def update_button_map(self, *args, **kwargs) -> None:
                return

            def update_analog_inputs(self, _inputs) -> None:
                return

        monkeypatch.setattr(device_manager, "resolve_stable_path", lambda value: value)
        monkeypatch.setattr(device_manager, "GrabbedDevice", _GrabbedDevice)
        monkeypatch.setattr(outputs, "create_global_uinputs", create_global_uinputs)
        monkeypatch.setattr(
            source_hiding,
            "disable_hardware_hotplug_hiding",
            AsyncMock(),
        )
        manager._device_input = lambda _path: _InputDevice()  # type: ignore[method-assign]
        manager._detect_device_types = lambda _device: ["gamepad"]  # type: ignore[method-assign]

        result = await manager.grab_device(
            hardware_id="2dc8:3106",
            evdev_paths=paths,
            evdev_interfaces=[
                {"id": "gamepad", "path": paths[0], "type": "gamepad"},
                {"id": "gamepad", "path": paths[1], "type": "gamepad"},
            ],
            button_map={"btn_south": "btn_south"},
            button_codes={"btn_south": evdev.ecodes.BTN_SOUTH},
        )

        assert result["grabbed_count"] == 2
        assert [device.path for device in manager.grabbed_devices["2dc8:3106"]] == paths
        create_global_uinputs.assert_called_once()

    @pytest.mark.asyncio
    async def test_grab_device_reuses_combo_runtime_deps_for_callbacks(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = "/dev/input/event2"
        deps = object()
        factory_calls = 0
        observed_deps: list[object] = []

        class _InputDevice:
            name = "Pad"
            phys = "usb-pad/input0"
            info = SimpleNamespace(vendor=0x2DC8, product=0x3106)

            def capabilities(self) -> dict[int, list[int]]:
                return {evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SOUTH]}

            def input_props(self) -> list[int]:
                return []

            def close(self) -> None:
                return

        class _GrabbedDevice:
            def __init__(self, **kwargs) -> None:
                self.path = kwargs["path"]
                self.hardware_id = kwargs["hardware_id"]
                self.stable_path = self.path
                self.resolved_event_path = self.path
                self.interface_id = kwargs.get("interface_id", "")
                self.event_callback = kwargs["event_callback"]
                self.runtime_cleanup_callback = kwargs["runtime_cleanup_callback"]

            async def release(self) -> None:
                return

            def update_button_map(self, *args, **kwargs) -> None:
                return

            def update_analog_inputs(self, _inputs) -> None:
                return

        def build_deps(**_kwargs) -> object:
            nonlocal factory_calls
            factory_calls += 1
            return deps

        async def on_device_event(*_args, **kwargs) -> None:
            observed_deps.append(kwargs["deps"])

        async def clear_combo_runtime_for_binding_scope(*_args, **kwargs) -> None:
            observed_deps.append(kwargs["deps"])

        manager = SimpleNamespace(
            grabbed_devices={},
            grab_state=SimpleNamespace(
                desired_paths={},
                desired_grabs={},
                pending_hardware_release={},
                pending_interface_release={},
                release_grace_s=0.1,
            ),
            output_state=SimpleNamespace(
                keyboard_uinput=None,
                mouse_uinput=None,
                gamepad_uinput=None,
            ),
            active_mappings={},
            verbosity=0,
            broadcast_callback=AsyncMock(),
            set_cursor_position=AsyncMock(),
            recording_manager=None,
            play_macro=AsyncMock(),
            emergency_reset=AsyncMock(),
            macro_state=SimpleNamespace(mouse_rel_suppressed=False),
            repeat_state=object(),
            _device_input=lambda _path: _InputDevice(),
            _detect_device_types=lambda _device: ["gamepad"],
            _record_diagnostic=lambda *_args: None,
            resolve_gamepad_output=lambda *_args, **_kwargs: None,
            broadcast_device_inspector_event=lambda *_args: None,
            device_inspector_active=lambda: False,
            device_inspector_suppressed=lambda: False,
            device_inspector_suppressed_hardware_ids_snapshot=lambda: set(),
            disable_device_inspector_suppression=lambda: None,
            record_profile_action=lambda *_args: None,
            observe_profile_trigger_start=lambda *_args: None,
            observe_profile_trigger_end=lambda *_args: None,
        )

        monkeypatch.setattr(acquisition, "combo_runtime_deps", build_deps)
        monkeypatch.setattr(events, "on_device_event", on_device_event)
        monkeypatch.setattr(
            lifecycle,
            "clear_combo_runtime_for_binding_scope",
            clear_combo_runtime_for_binding_scope,
        )
        monkeypatch.setattr(acquisition, "grab_with_retry", AsyncMock())
        monkeypatch.setattr(outputs, "create_global_uinputs", Mock())

        await acquisition.grab_device_unlocked(
            manager,
            GrabRequest(
                hardware_id="2dc8:3106",
                evdev_paths=[path],
                button_map={"btn_south": "btn_south"},
                button_codes={"btn_south": evdev.ecodes.BTN_SOUTH},
                button_values=None,
                analog_inputs=None,
                force_grab_unmapped=False,
                evdev_interfaces=[{"id": "gamepad", "path": path, "type": "gamepad"}],
                update_desired=False,
            ),
            GrabDeviceDeps(
                desired_grab_config_cls=lambda **kwargs: kwargs,
                clear_device_path_cache_fn=lambda: None,
                resolve_stable_path_fn=lambda value: value,
                device_path_resolver_deps=device_path_resolver.DevicePathResolverDeps(
                    device_paths_fn=lambda: [],
                    device_input_fn=lambda _path: _InputDevice(),
                    detect_input_classes_fn=lambda _device: [],
                    primary_input_class_fn=lambda _types: DeviceType.GAMEPAD,
                ),
                grabbed_device_cls=_GrabbedDevice,
                get_interface_id_fn=lambda _path: "gamepad",
                str_value_fn=str,
                int_value_fn=int,
                fire_and_observe_fn=lambda coro, _label: asyncio.ensure_future(coro),
                errno_mod=errno,
            ),
        )

        device = manager.grabbed_devices["2dc8:3106"][0]
        await device.event_callback(
            "2dc8:3106",
            path,
            evdev.ecodes.EV_KEY,
            evdev.ecodes.BTN_SOUTH,
            1,
        )
        await device.event_callback(
            "2dc8:3106",
            path,
            evdev.ecodes.EV_KEY,
            evdev.ecodes.BTN_SOUTH,
            0,
        )
        await device.runtime_cleanup_callback("2dc8:3106", "gamepad")

        assert factory_calls == 1
        assert observed_deps == [deps, deps, deps]

    @pytest.mark.asyncio
    async def test_grab_device_rolls_back_pre_registered_device_on_grab_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        path = "/dev/input/event2"

        class _InputDevice:
            name = "Pad"
            phys = "usb-pad/input0"
            info = SimpleNamespace(vendor=0x2DC8, product=0x3106)

            def capabilities(self) -> dict[int, list[int]]:
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SOUTH],
                    evdev.ecodes.EV_ABS: [evdev.ecodes.ABS_X, evdev.ecodes.ABS_Y],
                }

            def input_props(self) -> list[int]:
                return []

            def close(self) -> None:
                return

        class _GrabbedDevice:
            def __init__(self, **kwargs) -> None:
                self.path = kwargs["path"]
                self.hardware_id = kwargs["hardware_id"]
                self.stable_path = self.path
                self.resolved_event_path = self.path
                self.interface_id = kwargs.get("interface_id", "")

            async def release(self) -> None:
                raise AssertionError("failed pre-registered device should not be released")

            def update_button_map(self, *args, **kwargs) -> None:
                return

            def update_analog_inputs(self, _inputs) -> None:
                return

        async def fake_grab_with_retry(*_args, **_kwargs) -> None:
            assert [device.path for device in manager.grabbed_devices["2dc8:3106"]] == [path]
            raise RuntimeError("grab failed")

        monkeypatch.setattr(device_manager, "resolve_stable_path", lambda value: value)
        monkeypatch.setattr(device_manager, "GrabbedDevice", _GrabbedDevice)
        monkeypatch.setattr(acquisition, "grab_with_retry", fake_grab_with_retry)
        monkeypatch.setattr(outputs, "create_global_uinputs", Mock())
        monkeypatch.setattr(outputs, "destroy_global_uinputs", Mock())
        manager._device_input = lambda _path: _InputDevice()  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="grab failed"):
            await manager.grab_device(
                hardware_id="2dc8:3106",
                evdev_paths=[path],
                evdev_interfaces=[{"id": "gamepad", "path": path, "type": "gamepad"}],
                button_map={"btn_south": "btn_south"},
                button_codes={"btn_south": evdev.ecodes.BTN_SOUTH},
            )

        assert manager.grabbed_devices == {}

    @pytest.mark.asyncio
    async def test_grab_device_reapply_prefers_existing_model_match(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        logical_path = "keymasq:2dc8:3106"
        paths = ["/dev/input/event2", "/dev/input/event3", "/dev/input/event4"]

        class _InputDevice:
            def __init__(self, path: str) -> None:
                self.path = path
                self.name = f"Pad {path.rsplit('event', 1)[-1]}"
                self.phys = f"usb-{path.rsplit('event', 1)[-1]}/input0"
                self.info = SimpleNamespace(vendor=0x2DC8, product=0x3106)

            def capabilities(self) -> dict[int, list[int]]:
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SOUTH],
                    evdev.ecodes.EV_ABS: [evdev.ecodes.ABS_X, evdev.ecodes.ABS_Y],
                }

            def input_props(self) -> list[int]:
                return []

            def close(self) -> None:
                return

        class _ExistingGrab:
            def __init__(self, hardware_id: str, path: str) -> None:
                self.hardware_id = hardware_id
                self.path = path
                self.stable_path = path
                self.interface_id = "gamepad"
                self.updated = 0

            def update_button_map(self, *args, **kwargs) -> None:
                self.updated += 1

            def update_analog_inputs(self, _inputs) -> None:
                return

        monkeypatch.setattr(device_manager.evdev, "list_devices", lambda: list(paths))
        monkeypatch.setattr(device_manager, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(device_path_resolver, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(device_manager, "_device_input", lambda path: _InputDevice(path))
        monkeypatch.setattr(
            source_hiding,
            "disable_hardware_hotplug_hiding",
            AsyncMock(),
        )
        manager._device_input = lambda path: _InputDevice(path)  # type: ignore[method-assign]

        current = _ExistingGrab("2dc8:3106", "/dev/input/event3")
        other = _ExistingGrab("2dc8:3106@3", "/dev/input/event4")
        manager.grabbed_devices["2dc8:3106"] = [current]
        manager.grabbed_devices["2dc8:3106@3"] = [other]

        result = await manager.grab_device(
            hardware_id="2dc8:3106",
            evdev_paths=[logical_path],
            evdev_interfaces=[
                {
                    "id": "gamepad",
                    "path": logical_path,
                    "type": "gamepad",
                    "capabilities": ["btn_south", "abs_x", "abs_y"],
                }
            ],
            button_map={"btn_south": "btn_south"},
            button_codes={"btn_south": evdev.ecodes.BTN_SOUTH},
        )

        assert result["grabbed_count"] == 1
        assert manager.grabbed_devices["2dc8:3106"] == [current]
        assert current.path == "/dev/input/event3"
        assert current.updated == 1
        assert manager.grab_state.pending_interface_release == {}

    @pytest.mark.asyncio
    async def test_grab_device_reapply_matches_hidden_source_event_alias(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        hardware_id = "2dc8:3106"

        class _ExistingGrab:
            path = "/dev/input/by-id/pre-hide-pad"
            stable_path = "/dev/input/by-id/pre-hide-pad"
            resolved_event_path = "/dev/input/event22"
            interface_id = "gamepad"

            def __init__(self) -> None:
                self.updated = 0

            def update_button_map(self, *args, **kwargs) -> None:
                self.updated += 1

            def update_analog_inputs(self, _inputs) -> None:
                return

        current = _ExistingGrab()
        manager.grabbed_devices[hardware_id] = [current]

        def fake_resolve_stable_path(path: str) -> str:
            if path == "/dev/input/event22":
                return "/dev/input/by-id/current-pad"
            return path

        monkeypatch.setattr(device_manager, "resolve_stable_path", fake_resolve_stable_path)
        monkeypatch.setattr(
            device_path_resolver,
            "resolve_evdev_interfaces",
            lambda *args, **kwargs: [
                device_path_resolver.ResolvedInterface(
                    path="/dev/input/event22",
                    configured_path="keymasq:2dc8:3106",
                    interface_id="gamepad",
                    device_type=DeviceType.GAMEPAD,
                    capabilities=["btn_south"],
                )
            ],
        )
        grab_with_retry = AsyncMock(side_effect=AssertionError)
        schedule_interface_release = Mock()
        device_input = Mock(side_effect=AssertionError)
        monkeypatch.setattr(acquisition, "grab_with_retry", grab_with_retry)
        monkeypatch.setattr(
            acquisition,
            "schedule_interface_release",
            schedule_interface_release,
        )
        monkeypatch.setattr(
            source_hiding,
            "disable_hardware_hotplug_hiding",
            AsyncMock(),
        )
        manager._device_input = device_input  # type: ignore[method-assign]

        result = await manager.grab_device(
            hardware_id=hardware_id,
            evdev_paths=["keymasq:2dc8:3106"],
            evdev_interfaces=[{"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}],
            button_map={"btn_south": "btn_south"},
            button_codes={"btn_south": evdev.ecodes.BTN_SOUTH},
        )

        assert result["grabbed_count"] == 1
        assert manager.grabbed_devices[hardware_id] == [current]
        assert current.updated == 1
        schedule_interface_release.assert_not_called()
        grab_with_retry.assert_not_awaited()
        device_input.assert_not_called()

    @pytest.mark.asyncio
    async def test_grab_device_failed_reassign_preserves_existing_grab_state(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        hardware_id = "2dc8:3106"
        old_config = DesiredGrabConfig(
            paths={"/dev/input/event3"},
            button_map={"btn_south": "btn_south"},
            evdev_interfaces=[{"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}],
        )

        class _InputDevice:
            path = "/dev/input/event2"
            name = "Busy Pad"
            phys = "usb-2/input0"
            info = SimpleNamespace(vendor=0x2DC8, product=0x3106)

            def capabilities(self) -> dict[int, list[int]]:
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SOUTH],
                    evdev.ecodes.EV_ABS: [evdev.ecodes.ABS_X, evdev.ecodes.ABS_Y],
                }

            def input_props(self) -> list[int]:
                return []

            def close(self) -> None:
                return

        class _ExistingGrab:
            path = "/dev/input/event3"
            stable_path = "/dev/input/event3"
            interface_id = "gamepad"

            def update_button_map(self, *args, **kwargs) -> None:
                return

            def update_analog_inputs(self, _inputs) -> None:
                return

        class _BusyGrab:
            def __init__(self, **kwargs) -> None:
                self.path = kwargs["path"]

            async def grab(self) -> None:
                raise OSError(errno.EBUSY, "Device or resource busy")

            async def release(self) -> None:
                return

        monkeypatch.setattr(device_manager, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(device_manager, "_device_input", lambda _path: _InputDevice())
        monkeypatch.setattr(device_manager, "GrabbedDevice", _BusyGrab)
        monkeypatch.setattr(outputs, "create_global_uinputs", Mock())
        monkeypatch.setattr(
            device_path_resolver,
            "resolve_evdev_interfaces",
            lambda *args, **kwargs: [
                device_path_resolver.ResolvedInterface(
                    path="/dev/input/event2",
                    configured_path="keymasq:2dc8:3106",
                    interface_id="gamepad",
                    device_type=DeviceType.GAMEPAD,
                    capabilities=["btn_south"],
                )
            ],
        )
        manager._device_input = lambda _path: _InputDevice()  # type: ignore[method-assign]
        manager.grabbed_devices[hardware_id] = [_ExistingGrab()]
        manager.grab_state.desired_paths[hardware_id] = {"/dev/input/event3"}
        manager.grab_state.desired_grabs[hardware_id] = old_config

        with pytest.raises(OSError):
            await manager.grab_device(
                hardware_id=hardware_id,
                evdev_paths=["keymasq:2dc8:3106"],
                evdev_interfaces=[
                    {"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}
                ],
                button_map={"btn_south": "btn_south"},
                button_codes={"btn_south": evdev.ecodes.BTN_SOUTH},
            )

        assert manager.grabbed_devices[hardware_id][0].path == "/dev/input/event3"
        assert manager.grab_state.desired_paths[hardware_id] == {"/dev/input/event3"}
        assert manager.grab_state.desired_grabs[hardware_id] is old_config
        assert manager.grab_state.pending_interface_release == {}

    @pytest.mark.asyncio
    @requires_uinput
    async def test_grab_device_resolves_keymasq_path_and_uses_configured_interface_id(
        self,
        manager,
        virtual_mouse,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device_path = virtual_mouse.device.path
        info = virtual_mouse.device.info
        logical_path = f"keymasq:{info.vendor:04x}:{info.product:04x}"
        monkeypatch.setattr(device_manager.evdev, "list_devices", lambda: [device_path])
        monkeypatch.setattr(
            device_path_resolver,
            "_is_keymasq_virtual_device",
            lambda _d: False,
        )
        device_path_resolver.refresh_cached_devices_sync(
            device_paths_fn=device_manager._device_paths,
            device_input_fn=device_manager._device_input,
            detect_input_classes_fn=device_manager.detect_input_classes,
            primary_input_class_fn=device_manager.primary_input_class,
        )

        result = await manager.grab_device(
            hardware_id=f"{info.vendor:04x}:{info.product:04x}",
            evdev_paths=[logical_path],
            evdev_interfaces=[
                {
                    "id": "mouse",
                    "path": logical_path,
                    "type": "mouse",
                    "capabilities": ["btn_left"],
                }
            ],
            button_map={"btn_left": "btn_left"},
        )

        assert result["grabbed_count"] == 1
        grabbed = manager.grabbed_devices[f"{info.vendor:04x}:{info.product:04x}"][0]
        assert grabbed.path == device_path
        assert grabbed.interface_id == "mouse"

        await manager.release_device(f"{info.vendor:04x}:{info.product:04x}")

    @pytest.mark.asyncio
    @requires_uinput
    async def test_grab_device_updates_existing_interface_id(
        self,
        manager,
        virtual_mouse,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device_path = virtual_mouse.device.path
        info = virtual_mouse.device.info
        hardware_id = f"{info.vendor:04x}:{info.product:04x}"
        logical_path = f"keymasq:{hardware_id}"
        monkeypatch.setattr(device_manager.evdev, "list_devices", lambda: [device_path])
        monkeypatch.setattr(
            device_path_resolver,
            "_is_keymasq_virtual_device",
            lambda _d: False,
        )
        device_path_resolver.refresh_cached_devices_sync(
            device_paths_fn=device_manager._device_paths,
            device_input_fn=device_manager._device_input,
            detect_input_classes_fn=device_manager.detect_input_classes,
            primary_input_class_fn=device_manager.primary_input_class,
        )

        await manager.grab_device(
            hardware_id=hardware_id,
            evdev_paths=[logical_path],
            evdev_interfaces=[
                {"id": "mouse", "path": logical_path, "type": "mouse"},
            ],
            button_map={"btn_left": "btn_left"},
        )
        grabbed = manager.grabbed_devices[hardware_id][0]
        assert grabbed.interface_id == "mouse"

        result = await manager.grab_device(
            hardware_id=hardware_id,
            evdev_paths=[logical_path],
            evdev_interfaces=[
                {"id": "pointer", "path": logical_path, "type": "mouse"},
            ],
            button_map={"btn_left": "btn_left"},
            analog_inputs={
                "stick": {
                    "source": "pointer",
                    "type": "stick",
                    "axes": [
                        {
                            "role": "x",
                            "evdev": "abs_x",
                            "evdev_code": evdev.ecodes.ABS_X,
                        }
                    ],
                }
            },
        )

        assert result["grabbed_count"] == 1
        assert manager.grabbed_devices[hardware_id][0] is grabbed
        assert grabbed.interface_id == "pointer"
        assert grabbed.analog_input_types["stick"] == "stick"

        await manager.release_device(hardware_id)

    @pytest.mark.asyncio
    async def test_grab_device_still_errors_when_present_interfaces_match_no_buttons(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()

        class _InputDevice:
            def __init__(self, path: str) -> None:
                self.path = path

            def capabilities(self) -> dict[int, list[int]]:
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_A],
                }

        monkeypatch.setattr(device_manager, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(device_manager.evdev, "InputDevice", _InputDevice)

        with pytest.raises(ValueError, match="matched mapped buttons"):
            await manager.grab_device(
                hardware_id="1234:5678",
                evdev_paths=["/dev/input/event10"],
                button_map={"btn_side": "btn_side"},
            )

    @pytest.mark.asyncio
    async def test_grab_device_force_grab_unmapped_grabs_without_matching_capabilities(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        path = "/dev/input/event10"

        class _InputDevice:
            name = "Keyboard"
            phys = "usb-keyboard/input0"
            info = SimpleNamespace(vendor=0x1234, product=0x5678)

            def capabilities(self) -> dict[int, list[int]]:
                return {evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_A]}

            def input_props(self) -> list[int]:
                return []

            def close(self) -> None:
                return

        class _GrabbedDevice:
            def __init__(self, **kwargs) -> None:
                self.path = kwargs["path"]
                self.hardware_id = kwargs["hardware_id"]
                self.stable_path = self.path
                self.resolved_event_path = self.path
                self.interface_id = kwargs.get("interface_id", "")

            async def grab(self) -> None:
                return

            async def release(self) -> None:
                return

            def update_button_map(self, *args, **kwargs) -> None:
                return

            def update_analog_inputs(self, _inputs) -> None:
                return

        monkeypatch.setattr(device_manager, "resolve_stable_path", lambda value: value)
        monkeypatch.setattr(device_manager, "GrabbedDevice", _GrabbedDevice)
        monkeypatch.setattr(outputs, "create_global_uinputs", Mock())
        monkeypatch.setattr(
            source_hiding,
            "disable_hardware_hotplug_hiding",
            AsyncMock(),
        )
        manager._device_input = lambda _path: _InputDevice()  # type: ignore[method-assign]
        manager._detect_device_types = lambda _device: ["keyboard"]  # type: ignore[method-assign]

        result = await manager.grab_device(
            hardware_id="1234:5678",
            evdev_paths=[path],
            button_map={"btn_side": "btn_side"},
            force_grab_unmapped=True,
        )

        assert result["grabbed_count"] == 1
        assert result["skipped_count"] == 0
        assert manager.grabbed_devices["1234:5678"][0].path == path

    @pytest.mark.asyncio
    async def test_grab_device_constructs_motion_interface_in_observe_mode(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        path = "/dev/input/event-motion"

        class _InputDevice:
            name = "Motion Sensor"
            phys = "usb-pad/input1"
            info = SimpleNamespace(vendor=0x1234, product=0x5678)

            def capabilities(self) -> dict[int, list[int]]:
                return {evdev.ecodes.EV_ABS: [evdev.ecodes.ABS_RX]}

            def input_props(self) -> list[int]:
                return [evdev.ecodes.INPUT_PROP_ACCELEROMETER]

            def close(self) -> None:
                return

        class _GrabbedDevice:
            def __init__(self, **kwargs) -> None:
                self.path = kwargs["path"]
                self.hardware_id = kwargs["hardware_id"]
                self.stable_path = self.path
                self.resolved_event_path = self.path
                self.interface_id = kwargs.get("interface_id", "")
                self.access_mode = kwargs["access_mode"]

            async def grab(self) -> None:
                return

            async def release(self) -> None:
                return

            def update_button_map(self, *args, **kwargs) -> None:
                return

            def update_analog_inputs(self, _inputs) -> None:
                return

            def update_motion_sensors(self, _sensors) -> None:
                return

        monkeypatch.setattr(device_manager, "resolve_stable_path", lambda value: value)
        monkeypatch.setattr(device_manager, "GrabbedDevice", _GrabbedDevice)
        monkeypatch.setattr(outputs, "create_global_uinputs", Mock())
        monkeypatch.setattr(
            source_hiding,
            "disable_hardware_hotplug_hiding",
            AsyncMock(),
        )
        manager._device_input = lambda _path: _InputDevice()  # type: ignore[method-assign]
        manager._detect_device_types = lambda _device: ["motion"]  # type: ignore[method-assign]

        result = await manager.grab_device(
            hardware_id="1234:5678",
            evdev_paths=[path],
            evdev_interfaces=[{"id": "imu", "path": path, "type": "motion"}],
            button_map={},
            motion_sensors={
                "imu": {
                    "source": "imu",
                    "gyro_axes": [
                        {
                            "role": "yaw",
                            "evdev": "abs_rx",
                            "evdev_code": evdev.ecodes.ABS_RX,
                        }
                    ],
                }
            },
        )

        assert result["grabbed_count"] == 1
        assert manager.grabbed_devices["1234:5678"][0].access_mode is InputAccessMode.OBSERVE

    @pytest.mark.asyncio
    @requires_uinput
    async def test_release_device(self, manager, virtual_mouse):
        device_path = virtual_mouse.device.path

        await manager.grab_device(
            hardware_id="1234:5678",
            evdev_paths=[device_path],
            button_map={},
        )

        result = await manager.release_device("1234:5678", immediate=True)

        assert result["released"] is True
        assert "1234:5678" not in manager.grabbed_devices

    @pytest.mark.asyncio
    async def test_release_nonexistent_device(self, manager):
        result = await manager.release_device("ffff:ffff")

        assert result["released"] is True

    @pytest.mark.asyncio
    @requires_uinput
    async def test_set_mapping(self, manager, virtual_mouse):
        device_path = virtual_mouse.device.path

        await manager.grab_device(
            hardware_id="1234:5678",
            evdev_paths=[device_path],
            button_map={"btn_side": "btn_side"},
        )

        mapping = {
            "btn_side": {"action": "keyboard", "target": "key_a"},
        }

        result = await manager.set_mapping("1234:5678", mapping)

        assert result["updated"] is True

        await manager.release_device("1234:5678")

    @pytest.mark.asyncio
    async def test_set_mapping_ungrabbed_device(self, manager):
        mapping = {"btn_side": {"action": "keyboard", "target": "key_a"}}

        with pytest.raises(ValueError, match="not grabbed"):
            await manager.set_mapping("ffff:ffff", mapping)

    @pytest.mark.asyncio
    @requires_uinput
    async def test_release_all_devices(self, manager, virtual_mouse, virtual_keyboard):
        mouse_path = virtual_mouse.device.path
        keyboard_path = virtual_keyboard.device.path

        await manager.grab_device(
            hardware_id="1234:5678",
            evdev_paths=[mouse_path],
            button_map={"btn_left": "btn_left"},
        )
        await manager.grab_device(
            hardware_id="abcd:ef01",
            evdev_paths=[keyboard_path],
            button_map={"key_a": "key_a"},
        )

        assert len(manager.grabbed_devices) == 2

        await manager.release_all_devices()

        assert len(manager.grabbed_devices) == 0

    @pytest.mark.asyncio
    async def test_release_interface_stops_event_loop_before_clearing_combo_runtime(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        pending = asyncio.Event()
        read_task = asyncio.create_task(pending.wait())

        class _Device:
            path = "/dev/input/event0"
            interface_id = "kbd"

            def __init__(self) -> None:
                self.task = read_task
                self.release_tracked_outputs = Mock()

            async def stop_event_loop(self) -> None:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)

            async def release(self) -> None:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)

        device = _Device()
        manager.grabbed_devices["hw"] = [device]
        manager.grab_state.desired_paths["hw"] = {device.path}

        async def clear_combo_runtime_for_binding_scope(*_args, **_kwargs) -> None:
            assert device.task.done()

        monkeypatch.setattr(
            lifecycle,
            "clear_combo_runtime_for_binding_scope",
            clear_combo_runtime_for_binding_scope,
        )
        monkeypatch.setattr(outputs, "destroy_global_uinputs", Mock())

        try:
            await release.release_interface_unlocked(manager, "hw", device.path)
        finally:
            if not read_task.done():
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)


class TestDeviceDetection:
    @pytest.mark.asyncio
    async def test_set_mapping_resets_existing_runtime_state(self) -> None:
        manager = DeviceManager()
        fake_device = SimpleNamespace(reset_mapping_runtime_state=AsyncMock())
        manager.grabbed_devices = {"1234:5678": [fake_device]}

        result = await manager.set_mapping(
            "1234:5678",
            {"btn_side": {"action": "suppress"}},
        )

        assert result == {"updated": True, "hardware_id": "1234:5678"}
        fake_device.reset_mapping_runtime_state.assert_awaited_once()

    def test_detect_device_type(self):
        manager = DeviceManager()

        class MockDevice:
            def capabilities(self):
                return {
                    evdev.ecodes.EV_REL: [evdev.ecodes.REL_X, evdev.ecodes.REL_Y],
                    evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_LEFT, evdev.ecodes.BTN_RIGHT],
                }

        result = manager._detect_device_type(MockDevice())
        assert result == DeviceType.MOUSE

        class MockKeyboard:
            def capabilities(self):
                return {
                    evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_A, evdev.ecodes.KEY_Q],
                }

        result = manager._detect_device_type(MockKeyboard())
        assert result == DeviceType.KEYBOARD

        class MockComboDevice:
            def capabilities(self):
                return {
                    evdev.ecodes.EV_REL: [evdev.ecodes.REL_X, evdev.ecodes.REL_Y],
                    evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_A, evdev.ecodes.BTN_LEFT],
                }

            def input_props(self):
                return [evdev.ecodes.INPUT_PROP_POINTING_STICK]

        combo_types = manager._detect_device_types(MockComboDevice())
        assert combo_types == ["mouse", "keyboard", "pointstick"]
        assert manager._detect_device_type(MockComboDevice()) == DeviceType.MOUSE


class TestListDevices:
    def test_list_devices_closes_devices_after_metadata_scan(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        closed_paths: list[str] = []

        class FakeDevice:
            name = "Raw Keyboard"
            phys = "usb-test"
            uniq = ""
            info = SimpleNamespace(vendor=0x1234, product=0x5678)

            def __init__(self, path: str) -> None:
                self.path = path

            def capabilities(self):
                return {evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_A]}

            def input_props(self):
                return []

            def close(self) -> None:
                closed_paths.append(self.path)

        monkeypatch.setattr(device_manager, "_device_paths", lambda: ["/dev/input/event0"])
        monkeypatch.setattr(device_manager.evdev, "InputDevice", FakeDevice)
        monkeypatch.setattr(
            device_manager, "resolve_stable_path", lambda _path: "/dev/input/by-id/raw-kbd"
        )
        monkeypatch.setattr(device_manager, "get_interface_id", lambda _path: "kbd")

        result = manager._list_devices_sync()

        assert len(cast(list[dict[str, object]], result["devices"])) == 1
        assert closed_paths == ["/dev/input/event0"]

    def test_list_devices_closes_devices_when_metadata_scan_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        closed_paths: list[str] = []

        class FakeDevice:
            name = "Broken Keyboard"
            phys = "usb-test"
            uniq = ""
            info = SimpleNamespace(vendor=0x1234, product=0x5678)

            def __init__(self, path: str) -> None:
                self.path = path

            def capabilities(self):
                raise RuntimeError("unreadable capabilities")

            def close(self) -> None:
                closed_paths.append(self.path)

        monkeypatch.setattr(device_manager, "_device_paths", lambda: ["/dev/input/event0"])
        monkeypatch.setattr(device_manager.evdev, "InputDevice", FakeDevice)

        result = manager._list_devices_sync()

        assert result == {"devices": []}
        assert closed_paths == ["/dev/input/event0"]

    def test_list_devices_treats_oserror_as_expected_scan_miss(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        closed_paths: list[str] = []

        class FakeDevice:
            name = "Disconnected Keyboard"
            phys = "usb-test"
            uniq = ""
            info = SimpleNamespace(vendor=0x1234, product=0x5678)

            def __init__(self, path: str) -> None:
                self.path = path

            def capabilities(self):
                raise OSError("device disconnected")

            def close(self) -> None:
                closed_paths.append(self.path)

        monkeypatch.setattr(device_manager, "_device_paths", lambda: ["/dev/input/event0"])
        monkeypatch.setattr(device_manager.evdev, "InputDevice", FakeDevice)
        caplog.set_level(logging.DEBUG, logger="keymasqd.devices")

        result = manager._list_devices_sync()

        assert result == {"devices": []}
        assert closed_paths == ["/dev/input/event0"]
        assert "Skipping unreadable device /dev/input/event0" in caplog.text
        assert "Could not read device /dev/input/event0" not in caplog.text

    def test_list_devices_permission_error_logs_hint(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()

        def fake_input_device(_path: str):
            raise PermissionError(errno.EACCES, "denied")

        monkeypatch.setattr(device_manager, "_device_paths", lambda: ["/dev/input/event0"])
        monkeypatch.setattr(device_manager.evdev, "InputDevice", fake_input_device)
        caplog.set_level(logging.WARNING, logger="keymasqd.devices")

        result = manager._list_devices_sync()

        assert result == {"devices": []}
        assert "Skipping unreadable device /dev/input/event0" in caplog.text
        assert "/dev/input/event*" in caplog.text

    def test_list_devices_marks_physical_recording_identity(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()

        class FakeDevice:
            path = "/dev/input/event0"
            name = "Raw Keyboard"
            phys = "usb-test"
            uniq = ""
            info = SimpleNamespace(vendor=0x1234, product=0x5678)

            def capabilities(self):
                return {evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_A]}

            def input_props(self):
                return []

        monkeypatch.setattr(device_manager, "_device_paths", lambda: ["/dev/input/event0"])
        monkeypatch.setattr(device_manager.evdev, "InputDevice", lambda _path: FakeDevice())
        monkeypatch.setattr(
            device_manager, "resolve_stable_path", lambda _path: "/dev/input/by-id/raw-kbd"
        )
        monkeypatch.setattr(device_manager, "get_interface_id", lambda _path: "kbd")

        result = manager._list_devices_sync()
        result_devices = cast(list[dict[str, object]], result["devices"])
        device = result_devices[0]

        assert device["path"] == "/dev/input/event0"
        assert device["open_path"] == "/dev/input/event0"
        assert device["stable_path"] == "/dev/input/by-id/raw-kbd"
        assert device["recording_id"] == "physical:/dev/input/by-id/raw-kbd"
        assert device["recording_kind"] == "physical"
        assert device["grabbed_by_keymasq"] is False

    def test_list_devices_marks_keymasq_outputs_and_passthrough_sources(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = SimpleNamespace(
            device=SimpleNamespace(path="/dev/input/event10")
        )  # type: ignore[assignment]
        manager.grabbed_devices = {
            "1234:5678": [
                SimpleNamespace(
                    path="/dev/input/event0",
                    stable_path="/dev/input/by-id/raw-kbd",
                    hardware_id="1234:5678",
                    interface_id="kbd",
                    uinput=SimpleNamespace(device=SimpleNamespace(path="/dev/input/event20")),
                )
            ]
        }

        class FakeDevice:
            def __init__(self, path: str) -> None:
                self.path = path
                self.name = {
                    "/dev/input/event0": "Raw Keyboard",
                    "/dev/input/event10": "keymasq-keyboard",
                    "/dev/input/event20": "keymasq-1234:5678",
                }[path]
                self.phys = "py-evdev-uinput" if path != "/dev/input/event0" else "usb-test"
                self.uniq = ""
                self.info = SimpleNamespace(vendor=0x1234, product=0x5678)

            def capabilities(self):
                return {evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_A]}

            def input_props(self):
                return []

        stable_paths = {
            "/dev/input/event0": "/dev/input/by-id/raw-kbd",
            "/dev/input/event10": "/dev/input/event10",
            "/dev/input/event20": "/dev/input/event20",
        }
        monkeypatch.setattr(
            device_manager,
            "_device_paths",
            lambda: ["/dev/input/event0", "/dev/input/event10", "/dev/input/event20"],
        )
        monkeypatch.setattr(device_manager.evdev, "InputDevice", FakeDevice)
        monkeypatch.setattr(device_manager, "resolve_stable_path", lambda path: stable_paths[path])
        monkeypatch.setattr(
            device_manager, "get_interface_id", lambda path: "kbd" if "raw" in path else path
        )

        result = manager._list_devices_sync()
        result_devices = cast(list[dict[str, object]], result["devices"])
        devices = {device["path"]: device for device in result_devices}

        raw = devices["/dev/input/event0"]
        assert raw["recording_id"] == "physical:/dev/input/by-id/raw-kbd"
        assert raw["recording_kind"] == "physical"
        assert raw["grabbed_by_keymasq"] is True
        assert raw["source_hardware_id"] == "1234:5678"
        assert raw["source_interface_id"] == "kbd"

        output = devices["/dev/input/event10"]
        assert output["recording_id"] == "keymasq:output:keyboard"
        assert output["recording_kind"] == "keymasq_output"
        assert output["keymasq_output"] == "keyboard"

        passthrough = devices["/dev/input/event20"]
        assert passthrough["recording_id"] == "keymasq:passthrough:1234:5678:kbd"
        assert passthrough["recording_kind"] == "keymasq_passthrough"
        assert passthrough["source_stable_path"] == "/dev/input/by-id/raw-kbd"
        assert passthrough["source_path"] == "/dev/input/event0"

    @pytest.mark.asyncio
    async def test_list_devices_offloads_scan_to_thread(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        expected = {"devices": [{"path": "/dev/input/event0"}]}
        calls: list[object] = []

        def fake_scan() -> dict:
            calls.append("scan")
            return expected

        async def fake_to_thread(func, /, *args, **kwargs):
            calls.append(func)
            assert args == ()
            assert kwargs == {}
            return func()

        monkeypatch.setattr(manager, "_list_devices_sync", fake_scan)
        monkeypatch.setattr(device_manager.asyncio, "to_thread", fake_to_thread)

        result = await manager.list_devices()

        assert result == expected
        assert calls == [fake_scan, "scan"]

    @pytest.mark.asyncio
    async def test_diagnostics_loop_offloads_snapshot_to_thread(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        manager.diagnostics_state.enabled = True
        manager.broadcast_callback = AsyncMock()
        manager.diagnostics_state.samples = {"passthrough_mapped": deque([1.0, 3.0])}
        summaries: list[dict[str, dict[str, object]]] = []
        broadcasts: list[tuple[CommandType, dict[str, object]]] = []
        calls: list[tuple[object, tuple[object, ...]]] = []

        async def fake_sleep(_delay: float) -> None:
            manager.diagnostics_state.enabled = False

        async def fake_to_thread(func, /, *args, **kwargs):
            assert kwargs == {}
            calls.append((func, args))
            return func(*args)

        monkeypatch.setattr(device_manager.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(device_manager.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(manager, "_log_diagnostics_summary", summaries.append)
        monkeypatch.setattr(
            manager,
            "_broadcast_runtime_event",
            lambda *args: broadcasts.append(args),
        )

        await manager._diagnostics_loop()

        expected_summary = {
            "passthrough_mapped": {"n": 2, "p50": 1.0, "p95": 1.0, "p99": 1.0, "max": 3.0}
        }
        assert summaries == [expected_summary]
        assert calls == [
            (
                manager._summarize_diagnostics_snapshot,
                ({"passthrough_mapped": [1.0, 3.0]},),
            ),
            (summaries.append, (expected_summary,)),
        ]
        assert broadcasts == [
            (
                CommandType.DIAGNOSTICS_SNAPSHOT,
                {
                    "enabled": True,
                    "interval": 5.0,
                    "categories": ["mainline"],
                    "samples": expected_summary,
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_diagnostics_filters_to_mainline_by_default(self) -> None:
        manager = DeviceManager()
        manager.diagnostics_state.enabled = True

        manager._record_diagnostic("passthrough_mapped", 10.0)
        manager._record_diagnostic("passthrough_syn", 15.0)
        manager._record_diagnostic("action_key", 20.0)
        manager._record_diagnostic("combo_passthrough", 30.0)
        manager._record_diagnostic("syn", 40.0)
        manager._record_diagnostic("macro_load", 50.0)
        manager._record_diagnostic("macro_iteration", 60.0)

        assert set(manager.diagnostics_state.samples) == {
            "passthrough_mapped",
            "passthrough_syn",
            "action_key",
        }

    @pytest.mark.asyncio
    async def test_diagnostics_can_include_non_default_categories(self) -> None:
        manager = DeviceManager()
        manager.diagnostics_state.enabled = True
        manager.diagnostics_state.categories = {"combo", "macro", "internal"}

        manager._record_diagnostic("passthrough_mapped", 10.0)
        manager._record_diagnostic("combo_passthrough", 20.0)
        manager._record_diagnostic("combo_passthrough_held", 30.0)
        manager._record_diagnostic("syn", 40.0)
        manager._record_diagnostic("combo_recalled_release_suppressed", 50.0)
        manager._record_diagnostic("macro_load", 60.0)
        manager._record_diagnostic("macro_iteration", 70.0)

        assert set(manager.diagnostics_state.samples) == {
            "combo_passthrough",
            "combo_passthrough_held",
            "syn",
            "combo_recalled_release_suppressed",
            "macro_load",
            "macro_iteration",
        }

    @pytest.mark.asyncio
    async def test_topology_watch_loop_retries_when_live_and_reconciled_snapshots_differ(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager(topology_poll_s=0.01)
        snapshot = {
            "/dev/input/by-id/test-mouse": topology.LiveInterfaceInfo(
                hardware_id="1234:5678",
                vendor_id="1234",
                product_id="5678",
                stable_path="/dev/input/by-id/test-mouse",
                path="/dev/input/event10",
                interface_id="mouse",
            )
        }
        manager.topology_state.live_snapshot = dict(snapshot)
        manager.topology_state.reconciled_snapshot = {}
        schedule_topology_reconcile = Mock()
        sleep_calls = 0

        async def fake_sleep(_delay: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                raise asyncio.CancelledError()

        async def fake_to_thread(func, /, *args, **kwargs):
            return snapshot

        monkeypatch.setattr(device_manager.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(device_manager.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(topology, "schedule_topology_reconcile", schedule_topology_reconcile)

        with pytest.raises(asyncio.CancelledError):
            await topology.topology_watch_loop(
                manager, log=device_manager.log, deps=device_manager._topology_runtime_deps()
            )

        schedule_topology_reconcile.assert_called_once_with(
            manager,
            snapshot,
            log=device_manager.log,
            deps=device_manager._topology_runtime_deps(),
        )

    @pytest.mark.asyncio
    async def test_topology_watch_loop_logs_scan_failures_and_keeps_running(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        manager = DeviceManager(topology_poll_s=0.01)
        snapshot = {
            "/dev/input/by-id/test-mouse": topology.LiveInterfaceInfo(
                hardware_id="1234:5678",
                vendor_id="1234",
                product_id="5678",
                stable_path="/dev/input/by-id/test-mouse",
                path="/dev/input/event10",
                interface_id="mouse",
            )
        }
        schedule_topology_reconcile = Mock()
        sleep_calls = 0
        scan_calls = 0

        async def fake_sleep(_delay: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 3:
                raise asyncio.CancelledError()

        async def fake_to_thread(func, /, *args, **kwargs):
            nonlocal scan_calls
            scan_calls += 1
            if scan_calls == 1:
                raise RuntimeError("scan boom")
            return snapshot

        monkeypatch.setattr(device_manager.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(device_manager.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(topology, "schedule_topology_reconcile", schedule_topology_reconcile)

        with caplog.at_level(logging.WARNING, logger="keymasqd.devices"):
            with pytest.raises(asyncio.CancelledError):
                await topology.topology_watch_loop(
                    manager, log=device_manager.log, deps=device_manager._topology_runtime_deps()
                )

        assert "Topology scan failed: scan boom" in caplog.text
        schedule_topology_reconcile.assert_called_once_with(
            manager,
            snapshot,
            log=device_manager.log,
            deps=device_manager._topology_runtime_deps(),
        )

    def test_scan_live_interfaces_logs_snapshot_device_failures(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        class _FakeDevice:
            path = ""
            name = "Test Device"
            phys = ""
            info = SimpleNamespace(vendor=0x1234, product=0x5678)

            def capabilities(self) -> dict[int, list[int]]:
                return {}

        devices = {
            "/dev/input/event0": _FakeDevice(),
            "/dev/input/event1": _FakeDevice(),
            "/dev/input/event2": _FakeDevice(),
        }

        def device_input(path: str) -> _FakeDevice:
            return devices[path]

        def resolve_stable_path(path: str) -> str:
            if path.endswith("event0"):
                raise OSError("stable path disappeared")
            if path.endswith("event1"):
                raise RuntimeError("stable resolver invalid")
            return f"/dev/input/by-id/{path.rsplit('/', 1)[-1]}"

        with caplog.at_level(logging.DEBUG, logger="keymasqd.devices"):
            snapshot = topology.scan_live_interfaces_sync(
                clear_device_path_cache_fn=lambda: None,
                device_paths_fn=lambda: list(devices),
                device_input_fn=device_input,
                detect_input_classes_fn=lambda _device: ["keyboard"],
                primary_input_class_fn=lambda _classes: DeviceType.KEYBOARD,
                resolve_stable_path_fn=resolve_stable_path,
                get_interface_id_fn=lambda _stable_path: "kbd",
                log=device_manager.log,
            )

        assert list(snapshot) == ["/dev/input/by-id/event2"]
        assert snapshot["/dev/input/by-id/event2"].hardware_id == "1234:5678"
        assert "Could not read live topology device /dev/input/event0" in caplog.text
        assert "stable path disappeared" in caplog.text
        assert "Unexpected failure reading live topology device /dev/input/event1" in caplog.text
        assert "RuntimeError: stable resolver invalid" in caplog.text

    def test_scan_live_interfaces_skips_keymasq_virtual_outputs(self) -> None:
        class _FakeDevice:
            path = ""
            info = SimpleNamespace(vendor=0x1234, product=0x5678)

            def __init__(self, *, name: str, phys: str) -> None:
                self.name = name
                self.phys = phys

            def capabilities(self) -> dict[int, list[int]]:
                return {}

        devices = {
            "/dev/input/event0": _FakeDevice(name="Physical Pad", phys="usb-1"),
            "/dev/input/event1": _FakeDevice(
                name="Physical Pad",
                phys="py-evdev-uinput",
            ),
        }
        resolved_paths: list[str] = []

        def resolve_stable_path(path: str) -> str:
            resolved_paths.append(path)
            return f"/dev/input/by-id/{path.rsplit('/', 1)[-1]}"

        snapshot = topology.scan_live_interfaces_sync(
            clear_device_path_cache_fn=lambda: None,
            device_paths_fn=lambda: list(devices),
            device_input_fn=lambda path: devices[path],
            detect_input_classes_fn=lambda _device: ["gamepad"],
            primary_input_class_fn=lambda _classes: DeviceType.GAMEPAD,
            resolve_stable_path_fn=resolve_stable_path,
            get_interface_id_fn=lambda _stable_path: "joystick",
            log=device_manager.log,
        )

        assert list(snapshot) == ["/dev/input/by-id/event0"]
        assert resolved_paths == ["/dev/input/event0"]

    @pytest.mark.asyncio
    async def test_start_topology_watcher_reconciles_initial_snapshot_with_existing_grabs(
        self,
    ) -> None:
        stable_path = "/dev/input/by-id/stale-kbd"
        manager = DeviceManager(topology_poll_s=1.0, topology_debounce_s=1.0)
        manager.grabbed_devices["1234:5678"] = [
            SimpleNamespace(
                path="/dev/input/event5",
                stable_path=stable_path,
                resolved_event_path="/dev/input/event5",
                interface_id="kbd",
            )
        ]
        released: list[tuple[str, str]] = []

        async def release_interface(
            manager_arg: DeviceManager,
            hardware_id: str,
            path: str,
        ) -> None:
            released.append((hardware_id, path))
            devices = manager_arg.grabbed_devices.get(hardware_id, [])
            manager_arg.grabbed_devices[hardware_id] = [
                device for device in devices if device.path != path
            ]
            if not manager_arg.grabbed_devices[hardware_id]:
                del manager_arg.grabbed_devices[hardware_id]

        deps = topology.TopologyRuntimeDeps(
            asyncio_mod=adapters.ASYNCIO_RUNTIME,
            clear_device_path_cache_fn=lambda: None,
            device_paths_fn=lambda: [],
            device_input_fn=lambda path: None,
            detect_input_classes_fn=lambda device: [],
            primary_input_class_fn=lambda device: None,
            resolve_stable_path_fn=lambda path: path,
            get_interface_id_fn=lambda stable_path: None,
            release_interface_fn=release_interface,
        )

        await topology.start_topology_watcher(manager, log=device_manager.log, deps=deps)
        try:
            assert released == [("1234:5678", "/dev/input/event5")]
            assert manager.grabbed_devices == {}
            assert manager.topology_state.live_snapshot == {}
            assert manager.topology_state.reconciled_snapshot == {}
        finally:
            await topology.stop_topology_watcher(manager, deps=deps)

    def test_topology_events_match_numbered_desired_hardware_id(self) -> None:
        manager = SimpleNamespace(_command_type=CommandType)
        snapshot = {
            "/dev/input/by-id/test-pad": topology.LiveInterfaceInfo(
                hardware_id="1234:5678",
                vendor_id="1234",
                product_id="5678",
                stable_path="/dev/input/by-id/test-pad",
                path="/dev/input/event10",
                interface_id="gamepad",
            )
        }

        events = topology.build_topology_events(
            manager,
            {},
            snapshot,
            {"1234:5678@2"},
        )

        assert events == [
            (
                CommandType.DEVICE_CONNECTED,
                {
                    "hardware_id": "1234:5678",
                    "vendor_id": "1234",
                    "product_id": "5678",
                    "path": "/dev/input/event10",
                    "stable_path": "/dev/input/by-id/test-pad",
                    "interface_id": "gamepad",
                },
            )
        ]

    def test_topology_events_match_interface_qualified_desired_hardware_id(
        self,
    ) -> None:
        manager = SimpleNamespace(_command_type=CommandType)
        snapshot = {
            "/dev/input/by-id/test-kbd": topology.LiveInterfaceInfo(
                hardware_id="1234:5678",
                vendor_id="1234",
                product_id="5678",
                stable_path="/dev/input/by-id/test-kbd",
                path="/dev/input/event10",
                interface_id="kbd",
            ),
            "/dev/input/by-id/test-mouse": topology.LiveInterfaceInfo(
                hardware_id="1234:5678",
                vendor_id="1234",
                product_id="5678",
                stable_path="/dev/input/by-id/test-mouse",
                path="/dev/input/event11",
                interface_id="mouse",
            ),
        }

        events = topology.build_topology_events(
            manager,
            {},
            snapshot,
            {"1234:5678@kbd"},
        )

        assert events == [
            (
                CommandType.DEVICE_CONNECTED,
                {
                    "hardware_id": "1234:5678",
                    "vendor_id": "1234",
                    "product_id": "5678",
                    "path": "/dev/input/event10",
                    "stable_path": "/dev/input/by-id/test-kbd",
                    "interface_id": "kbd",
                },
            )
        ]

    def test_hardware_id_matches_desired_normalizes_desired_ids(self) -> None:
        assert topology.hardware_id_matches_desired("abcd:1234", {"ABCD:1234"})
        assert topology.hardware_id_matches_desired(
            "abcd:1234",
            {"ABCD:1234@interface"},
            interface_id="INTERFACE",
        )
        assert not topology.hardware_id_matches_desired(
            "abcd:1234",
            {"ABCD:1234@interface"},
            interface_id="other",
        )

    def test_hardware_id_matches_desired_numeric_instance_wildcards_interface(
        self,
    ) -> None:
        desired = {topology.normalize_hardware_id("046D:C08B@2")}
        hardware_id = topology.normalize_hardware_id("046d:c08b")

        assert topology.hardware_id_matches_desired(
            hardware_id,
            desired,
            interface_id=topology.normalize_hardware_id("kbd"),
        )
        assert topology.hardware_id_matches_desired(
            hardware_id,
            desired,
            interface_id=topology.normalize_hardware_id("mouse"),
        )

    def test_hardware_id_matches_desired_named_interface_requires_exact_match(
        self,
    ) -> None:
        desired = {topology.normalize_hardware_id("046D:C08B@eth0")}
        hardware_id = topology.normalize_hardware_id("046d:c08b")

        assert topology.hardware_id_matches_desired(
            hardware_id,
            desired,
            interface_id=topology.normalize_hardware_id("ETH0"),
        )
        assert not topology.hardware_id_matches_desired(
            hardware_id,
            desired,
            interface_id=topology.normalize_hardware_id("wlan0"),
        )

    def test_topology_events_report_reconnect_for_same_stable_path(self) -> None:
        manager = SimpleNamespace(_command_type=CommandType)
        stable_path = "/dev/input/by-id/test-pad"
        previous = {
            stable_path: topology.LiveInterfaceInfo(
                hardware_id="1234:5678",
                vendor_id="1234",
                product_id="5678",
                stable_path=stable_path,
                path="/dev/input/event10",
                interface_id="gamepad",
            )
        }
        current = {
            stable_path: topology.LiveInterfaceInfo(
                hardware_id="1234:5678",
                vendor_id="1234",
                product_id="5678",
                stable_path=stable_path,
                path="/dev/input/event11",
                interface_id="gamepad",
            )
        }

        events = topology.build_topology_events(
            manager,
            previous,
            current,
            {"1234:5678"},
        )

        assert events == [
            (
                CommandType.DEVICE_DISCONNECTED,
                {
                    "hardware_id": "1234:5678",
                    "vendor_id": "1234",
                    "product_id": "5678",
                    "path": "/dev/input/event10",
                    "stable_path": stable_path,
                    "interface_id": "gamepad",
                },
            ),
            (
                CommandType.DEVICE_CONNECTED,
                {
                    "hardware_id": "1234:5678",
                    "vendor_id": "1234",
                    "product_id": "5678",
                    "path": "/dev/input/event11",
                    "stable_path": stable_path,
                    "interface_id": "gamepad",
                },
            ),
        ]

    def test_topology_events_report_disconnect_when_stable_path_hardware_changes(
        self,
    ) -> None:
        manager = SimpleNamespace(_command_type=CommandType)
        stable_path = "/dev/input/by-id/test-pad"
        previous = {
            stable_path: topology.LiveInterfaceInfo(
                hardware_id="1234:5678",
                vendor_id="1234",
                product_id="5678",
                stable_path=stable_path,
                path="/dev/input/event10",
                interface_id="gamepad",
            )
        }
        current = {
            stable_path: topology.LiveInterfaceInfo(
                hardware_id="8765:4321",
                vendor_id="8765",
                product_id="4321",
                stable_path=stable_path,
                path="/dev/input/event10",
                interface_id="gamepad",
            )
        }

        events = topology.build_topology_events(
            manager,
            previous,
            current,
            {"1234:5678"},
        )

        assert events == [
            (
                CommandType.DEVICE_DISCONNECTED,
                {
                    "hardware_id": "1234:5678",
                    "vendor_id": "1234",
                    "product_id": "5678",
                    "path": "/dev/input/event10",
                    "stable_path": stable_path,
                    "interface_id": "gamepad",
                },
            )
        ]

    def test_topology_events_report_hidden_source_when_stable_path_changes(self) -> None:
        manager = SimpleNamespace(_command_type=CommandType)
        previous = {
            "/dev/input/by-id/test-pad-event-joystick": topology.LiveInterfaceInfo(
                hardware_id="1234:5678",
                vendor_id="1234",
                product_id="5678",
                stable_path="/dev/input/by-id/test-pad-event-joystick",
                path="/dev/input/event10",
                interface_id="joystick",
            )
        }
        current = {
            "/dev/input/by-id/test-pad-event-if00": topology.LiveInterfaceInfo(
                hardware_id="1234:5678",
                vendor_id="1234",
                product_id="5678",
                stable_path="/dev/input/by-id/test-pad-event-if00",
                path="/dev/input/event10",
                interface_id="event-if00",
            ),
            "/dev/input/by-id/test-mouse": topology.LiveInterfaceInfo(
                hardware_id="8765:4321",
                vendor_id="8765",
                product_id="4321",
                stable_path="/dev/input/by-id/test-mouse",
                path="/dev/input/event12",
                interface_id="mouse",
            ),
        }

        events = topology.build_topology_events(
            manager,
            previous,
            current,
            {"1234:5678", "8765:4321"},
            hidden_source_paths={"/dev/input/event10"},
        )

        assert events == [
            (
                CommandType.DEVICE_CONNECTED,
                {
                    "hardware_id": "8765:4321",
                    "vendor_id": "8765",
                    "product_id": "4321",
                    "path": "/dev/input/event12",
                    "stable_path": "/dev/input/by-id/test-mouse",
                    "interface_id": "mouse",
                },
            ),
            (
                CommandType.DEVICE_CONNECTED,
                {
                    "hardware_id": "1234:5678",
                    "vendor_id": "1234",
                    "product_id": "5678",
                    "path": "/dev/input/event10",
                    "stable_path": "/dev/input/by-id/test-pad-event-if00",
                    "interface_id": "event-if00",
                },
            ),
        ]

    def test_topology_events_report_hidden_source_without_previous_membership(self) -> None:
        manager = SimpleNamespace(
            _command_type=CommandType,
            grabbed_devices={
                "1234:5678": [
                    SimpleNamespace(
                        path="/dev/input/event10",
                        resolved_event_path="/dev/input/event10",
                        source_hidden_kernel_names=[],
                        source_pending_hidden_kernel_names=["event10"],
                    )
                ]
            },
        )
        current = {
            "/dev/input/by-id/test-pad-event-joystick": topology.LiveInterfaceInfo(
                hardware_id="1234:5678",
                vendor_id="1234",
                product_id="5678",
                stable_path="/dev/input/by-id/test-pad-event-joystick",
                path="/dev/input/event10",
                interface_id="joystick",
            )
        }

        hidden_paths = topology.hidden_grabbed_source_paths(manager)
        events = topology.build_topology_events(
            manager,
            {},
            current,
            {"1234:5678"},
            hidden_source_paths=hidden_paths,
        )

        assert hidden_paths == {"/dev/input/event10"}
        assert events == [
            (
                CommandType.DEVICE_CONNECTED,
                {
                    "hardware_id": "1234:5678",
                    "vendor_id": "1234",
                    "product_id": "5678",
                    "path": "/dev/input/event10",
                    "stable_path": "/dev/input/by-id/test-pad-event-joystick",
                    "interface_id": "joystick",
                },
            )
        ]

    def test_topology_events_suppress_same_stable_hidden_source_churn(self) -> None:
        manager = SimpleNamespace(_command_type=CommandType)
        stable_path = "/dev/input/by-id/test-pad-event-joystick"
        previous = {
            stable_path: topology.LiveInterfaceInfo(
                hardware_id="1234:5678",
                vendor_id="1234",
                product_id="5678",
                stable_path=stable_path,
                path="/dev/input/event10",
                interface_id="joystick",
            )
        }
        current = {
            stable_path: topology.LiveInterfaceInfo(
                hardware_id="1234:5678",
                vendor_id="1234",
                product_id="5678",
                stable_path=stable_path,
                path="/dev/input/event10",
                interface_id="event-if00",
            )
        }

        events = topology.build_topology_events(
            manager,
            previous,
            current,
            {"1234:5678"},
            hidden_source_paths={"/dev/input/event10"},
        )

        assert events == []

    def test_topology_events_report_hidden_source_when_event_path_is_gone(
        self,
    ) -> None:
        manager = SimpleNamespace(_command_type=CommandType)
        previous = {
            "/dev/input/by-id/test-pad-event-joystick": topology.LiveInterfaceInfo(
                hardware_id="1234:5678",
                vendor_id="1234",
                product_id="5678",
                stable_path="/dev/input/by-id/test-pad-event-joystick",
                path="/dev/input/event10",
                interface_id="joystick",
            )
        }
        current: dict[str, topology.LiveInterfaceInfo] = {}

        events = topology.build_topology_events(
            manager,
            previous,
            current,
            {"1234:5678"},
            hidden_source_paths={"/dev/input/event10"},
        )

        assert events == [
            (
                CommandType.DEVICE_DISCONNECTED,
                {
                    "hardware_id": "1234:5678",
                    "vendor_id": "1234",
                    "product_id": "5678",
                    "path": "/dev/input/event10",
                    "stable_path": "/dev/input/by-id/test-pad-event-joystick",
                    "interface_id": "joystick",
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_reconcile_topology_releases_stale_grab_when_live_event_path_changes(
        self,
    ) -> None:
        stable_path = "/dev/input/by-id/test-kbd"
        manager = SimpleNamespace(
            grabbed_devices={
                "1234:5678": [
                    SimpleNamespace(
                        path="/dev/input/event5",
                        stable_path=stable_path,
                        interface_id="kbd",
                    )
                ]
            }
        )
        snapshot = {
            stable_path: topology.LiveInterfaceInfo(
                hardware_id="1234:5678",
                vendor_id="1234",
                product_id="5678",
                stable_path=stable_path,
                path="/dev/input/event9",
                interface_id="kbd",
            )
        }
        release_interface = AsyncMock()
        deps = SimpleNamespace(release_interface_fn=release_interface)

        await topology.reconcile_topology_unlocked(manager, snapshot, deps=deps)

        release_interface.assert_awaited_once_with(
            manager,
            "1234:5678",
            "/dev/input/event5",
        )

    @pytest.mark.asyncio
    async def test_reconcile_topology_releases_stale_grab_when_live_hardware_changes(
        self,
    ) -> None:
        stable_path = "/dev/input/by-id/test-kbd"
        manager = SimpleNamespace(
            grabbed_devices={
                "1234:5678": [
                    SimpleNamespace(
                        path="/dev/input/event5",
                        stable_path=stable_path,
                        resolved_event_path="/dev/input/event5",
                        interface_id="kbd",
                    )
                ]
            }
        )
        snapshot = {
            stable_path: topology.LiveInterfaceInfo(
                hardware_id="8765:4321",
                vendor_id="8765",
                product_id="4321",
                stable_path=stable_path,
                path="/dev/input/event5",
                interface_id="kbd",
            )
        }
        release_interface = AsyncMock()
        deps = SimpleNamespace(release_interface_fn=release_interface)

        await topology.reconcile_topology_unlocked(manager, snapshot, deps=deps)

        release_interface.assert_awaited_once_with(
            manager,
            "1234:5678",
            "/dev/input/event5",
        )

    @pytest.mark.asyncio
    async def test_reconcile_topology_releases_interface_qualified_grab_on_mismatch(
        self,
    ) -> None:
        stable_path = "/dev/input/by-id/test-kbd"
        manager = SimpleNamespace(
            grabbed_devices={
                "1234:5678@kbd": [
                    SimpleNamespace(
                        path="/dev/input/event5",
                        stable_path=stable_path,
                        resolved_event_path="/dev/input/event5",
                        interface_id="kbd",
                    )
                ]
            }
        )
        snapshot = {
            stable_path: topology.LiveInterfaceInfo(
                hardware_id="1234:5678",
                vendor_id="1234",
                product_id="5678",
                stable_path=stable_path,
                path="/dev/input/event5",
                interface_id="mouse",
            )
        }
        release_interface = AsyncMock()
        deps = SimpleNamespace(release_interface_fn=release_interface)

        await topology.reconcile_topology_unlocked(manager, snapshot, deps=deps)

        release_interface.assert_awaited_once_with(
            manager,
            "1234:5678@kbd",
            "/dev/input/event5",
        )

    @pytest.mark.asyncio
    async def test_reconcile_topology_keeps_grab_when_configured_source_id_differs(
        self,
    ) -> None:
        stable_path = "/dev/input/event5"
        manager = SimpleNamespace(
            grabbed_devices={
                "1234:5678": [
                    SimpleNamespace(
                        path="/dev/input/event5",
                        stable_path=stable_path,
                        interface_id="kbd",
                    )
                ]
            }
        )
        snapshot = {
            stable_path: topology.LiveInterfaceInfo(
                hardware_id="1234:5678",
                vendor_id="1234",
                product_id="5678",
                stable_path=stable_path,
                path="/dev/input/event5",
                interface_id="event5",
            )
        }
        release_interface = AsyncMock()
        deps = SimpleNamespace(release_interface_fn=release_interface)

        await topology.reconcile_topology_unlocked(manager, snapshot, deps=deps)

        release_interface.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reconcile_topology_keeps_hidden_source_when_stable_path_changes(
        self,
    ) -> None:
        stable_path = "/dev/input/by-id/test-pad-event-joystick"
        manager = SimpleNamespace(
            grabbed_devices={
                "1234:5678@joystick": [
                    SimpleNamespace(
                        path=stable_path,
                        stable_path=stable_path,
                        resolved_event_path="/dev/input/event5",
                        interface_id="joystick",
                        source_hidden_kernel_names=["event5", "js0"],
                    )
                ]
            }
        )
        snapshot = {
            "/dev/input/by-id/test-pad-event-if00": topology.LiveInterfaceInfo(
                hardware_id="1234:5678",
                vendor_id="1234",
                product_id="5678",
                stable_path="/dev/input/by-id/test-pad-event-if00",
                path="/dev/input/event5",
                interface_id="event-if00",
            )
        }
        release_interface = AsyncMock()
        deps = SimpleNamespace(release_interface_fn=release_interface)

        await topology.reconcile_topology_unlocked(manager, snapshot, deps=deps)

        release_interface.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reconcile_topology_releases_hidden_source_when_event_path_is_gone(
        self,
    ) -> None:
        stable_path = "/dev/input/by-id/test-pad-event-joystick"
        manager = SimpleNamespace(
            grabbed_devices={
                "1234:5678": [
                    SimpleNamespace(
                        path=stable_path,
                        stable_path=stable_path,
                        resolved_event_path="/dev/input/event5",
                        interface_id="joystick",
                        source_hidden_kernel_names=["event5", "js0"],
                    )
                ]
            }
        )
        snapshot = {
            "/dev/input/by-id/test-pad-event-if00": topology.LiveInterfaceInfo(
                hardware_id="1234:5678",
                vendor_id="1234",
                product_id="5678",
                stable_path="/dev/input/by-id/test-pad-event-if00",
                path="/dev/input/event9",
                interface_id="event-if00",
            )
        }
        release_interface = AsyncMock()
        deps = SimpleNamespace(release_interface_fn=release_interface)

        await topology.reconcile_topology_unlocked(manager, snapshot, deps=deps)

        release_interface.assert_awaited_once_with(
            manager,
            "1234:5678",
            stable_path,
        )

    @pytest.mark.asyncio
    async def test_reconcile_topology_keeps_by_id_grab_for_matching_event_path(
        self,
    ) -> None:
        stable_path = "/dev/input/by-id/test-kbd"
        manager = SimpleNamespace(
            grabbed_devices={
                "1234:5678": [
                    SimpleNamespace(
                        path=stable_path,
                        stable_path=stable_path,
                        resolved_event_path="/dev/input/event5",
                        interface_id="kbd",
                    )
                ]
            }
        )
        snapshot = {
            stable_path: topology.LiveInterfaceInfo(
                hardware_id="1234:5678",
                vendor_id="1234",
                product_id="5678",
                stable_path=stable_path,
                path="/dev/input/event5",
                interface_id="kbd",
            )
        }
        release_interface = AsyncMock()
        deps = SimpleNamespace(release_interface_fn=release_interface)

        await topology.reconcile_topology_unlocked(manager, snapshot, deps=deps)

        release_interface.assert_not_awaited()


class TestMacroControlActions:
    @pytest.mark.asyncio
    async def test_run_macro_control_action_wait_uses_wall_clock_duration(self, monkeypatch):
        manager = DeviceManager()
        clock = {"now": 10.0}
        sleep_calls: list[float] = []

        class _FakeLoop:
            def time(self) -> float:
                return clock["now"]

        async def fake_sleep(duration: float) -> None:
            sleep_calls.append(duration)
            clock["now"] += duration

        monkeypatch.setattr(device_manager.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(device_manager.asyncio, "get_running_loop", lambda: _FakeLoop())

        result = await controls.run_macro_control_action(
            manager,
            {"macro_action": "wait", "duration_us": 20_000},
            deps=device_manager._macro_runtime_deps(),
        )

        assert sleep_calls == [0.02]
        assert result == pytest.approx(0.02)

    @pytest.mark.asyncio
    async def test_run_macro_control_action_wait_renews_mouse_suppression(self, monkeypatch):
        manager = DeviceManager()
        clock = {"now": 10.0}
        begin_mouse_rel_suppression = Mock()

        class _FakeLoop:
            def time(self) -> float:
                return clock["now"]

        async def fake_sleep(duration: float) -> None:
            clock["now"] += duration

        monkeypatch.setattr(device_manager.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(device_manager.asyncio, "get_running_loop", lambda: _FakeLoop())
        monkeypatch.setattr(mouse, "renew_macro_mouse_suppression", begin_mouse_rel_suppression)

        result = await controls.run_macro_control_action(
            manager,
            {"macro_action": "wait", "duration_us": 10_000_000},
            renew_mouse_suppression=True,
            deps=device_manager._macro_runtime_deps(),
        )

        begin_mouse_rel_suppression.assert_called_once()
        assert begin_mouse_rel_suppression.call_args.kwargs["timeout_s"] == pytest.approx(11.0)
        assert result == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_mouse_suppression_watchdog_keeps_active_inhibit_count(self, monkeypatch):
        manager = DeviceManager()

        async def fake_sleep(_duration: float) -> None:
            return None

        monkeypatch.setattr(device_manager.asyncio, "sleep", fake_sleep)

        manager.macro_state.mouse_rel_suppressed = True
        manager.macro_state.mouse_inhibit_count = 1
        await mouse.mouse_rel_suppression_watchdog(
            manager,
            1.0,
            deps=device_manager._macro_runtime_deps(),
        )

        assert manager.macro_state.mouse_rel_suppressed is True

        manager.macro_state.mouse_inhibit_count = 0
        await mouse.mouse_rel_suppression_watchdog(
            manager,
            1.0,
            deps=device_manager._macro_runtime_deps(),
        )

        assert manager.macro_state.mouse_rel_suppressed is False

    @pytest.mark.asyncio
    async def test_run_macro_control_action_wait_random_uses_random_range(self, monkeypatch):
        manager = DeviceManager()
        clock = {"now": 20.0}
        sleep_calls: list[float] = []

        class _FakeLoop:
            def time(self) -> float:
                return clock["now"]

        async def fake_sleep(duration: float) -> None:
            sleep_calls.append(duration)
            clock["now"] += duration

        monkeypatch.setattr(device_manager.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(device_manager.asyncio, "get_running_loop", lambda: _FakeLoop())
        monkeypatch.setattr(controls.random, "randint", lambda _minimum, _maximum: 50_000)

        result = await controls.run_macro_control_action(
            manager,
            {"macro_action": "wait_random", "min_us": 10_000, "max_us": 80_000},
            deps=device_manager._macro_runtime_deps(),
        )

        assert sleep_calls == [0.05]
        assert result == pytest.approx(0.05)

    @pytest.mark.asyncio
    async def test_run_macro_control_action_exec_async_broadcasts(self):
        manager = DeviceManager()

        callback = AsyncMock()

        async def cb(command, data):
            await callback(command, data)

        manager.broadcast_callback = cb

        result = await controls.run_macro_control_action(
            manager,
            {
                "macro_action": "exec_async",
                "command": "echo hi",
            },
            deps=device_manager._macro_runtime_deps(),
        )

        callback.assert_awaited_once()
        called_command, called_data = callback.await_args.args
        assert called_command == CommandType.ACTION_TRIGGER
        assert called_data["action_type"] == "exec"
        assert called_data["macro_exec_async"] is True
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_run_macro_control_action_compositor_dispatch_broadcasts(self):
        manager = DeviceManager()

        callback = AsyncMock()

        async def cb(command, data):
            await callback(command, data)

        manager.broadcast_callback = cb

        result = await controls.run_macro_control_action(
            manager,
            {
                "macro_action": "compositor_dispatch",
                "compositor": "hyprland",
                "dispatcher": "workspace",
                "args": "e+1",
            },
            deps=device_manager._macro_runtime_deps(),
        )

        callback.assert_awaited_once()
        called_command, called_data = callback.await_args.args
        assert called_command == CommandType.ACTION_TRIGGER
        assert called_data == {
            "action_type": "compositor_dispatch",
            "compositor": "hyprland",
            "dispatcher": "workspace",
            "args": "e+1",
        }
        assert result == 0.0

    @pytest.mark.parametrize("action_type", ["exec_sync", "exec_parallel"])
    @pytest.mark.asyncio
    async def test_run_macro_control_action_exec_wait_id_timeout_and_cleanup(
        self,
        monkeypatch,
        action_type,
    ):
        manager = DeviceManager()
        clock = {"now": 30.0}
        callback = AsyncMock()

        async def cb(command, data):
            await callback(command, data)

        manager.broadcast_callback = cb
        begin_mouse_rel_suppression = Mock()
        end_mouse_rel_suppression = Mock()
        real_loop = asyncio.get_running_loop()

        class _FakeLoop:
            def create_future(self) -> asyncio.Future[int]:
                return real_loop.create_future()

            def time(self) -> float:
                return clock["now"]

        async def fake_sleep(duration: float) -> None:
            return None

        async def fake_wait_for(awaitable, timeout):
            clock["now"] += 0.025
            raise TimeoutError

        monkeypatch.setattr(device_manager.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(device_manager.asyncio, "get_running_loop", lambda: _FakeLoop())
        monkeypatch.setattr(device_manager.asyncio, "wait_for", fake_wait_for)
        monkeypatch.setattr(mouse, "acquire_macro_mouse_inhibit", begin_mouse_rel_suppression)
        monkeypatch.setattr(mouse, "release_macro_mouse_inhibit", end_mouse_rel_suppression)

        result = await controls.run_macro_control_action(
            manager,
            {
                "macro_action": action_type,
                "command": "echo hi",
                "inhibit_mouse": True,
                "timeout_ms": 100,
            },
            deps=device_manager._macro_runtime_deps(),
        )

        assert begin_mouse_rel_suppression.called is True
        assert end_mouse_rel_suppression.called is True
        assert manager.macro_state.exec_waiters == {}
        callback.assert_awaited_once()
        assert callback.await_args.args[0] == CommandType.ACTION_TRIGGER
        called_data = callback.await_args.args[1]
        assert called_data["action_type"] == "exec"
        assert called_data["cmd"] == "echo hi"
        assert called_data["macro_exec_timeout_ms"] == 100
        assert called_data["macro_exec_wait_id"]
        assert result == pytest.approx(0.025)

    @pytest.mark.asyncio
    async def test_run_macro_control_action_cancellation_requests_process_cancel(self):
        manager = DeviceManager()
        started = asyncio.Event()
        broadcasts: list[tuple[CommandType, dict[str, object]]] = []

        async def cb(command: CommandType, data: dict[str, object]) -> None:
            broadcasts.append((command, data))
            if data.get("macro_exec_wait_id"):
                started.set()

        manager.broadcast_callback = cb
        task = asyncio.create_task(
            controls.run_macro_control_action(
                manager,
                {
                    "macro_action": "exec_parallel",
                    "command": "sleep 30",
                    "timeout_ms": 30_000,
                },
                deps=device_manager._macro_runtime_deps(),
            )
        )

        await asyncio.wait_for(started.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(broadcasts) == 2
        start_data = broadcasts[0][1]
        cancel_data = broadcasts[1][1]
        assert cancel_data == {
            "action_type": "exec",
            "macro_exec_cancel_id": start_data["macro_exec_wait_id"],
        }
        assert manager.macro_state.exec_waiters == {}


class TestReleaseScheduling:
    @pytest.mark.asyncio
    async def test_release_interface_reenables_gamepad_hotplug_hiding_when_still_desired(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        device = SimpleNamespace(
            path="/dev/input/event2",
            interface_id="gamepad",
            stop_event_loop=AsyncMock(),
            release=AsyncMock(),
            release_tracked_outputs=Mock(),
        )
        manager.grabbed_devices["2dc8:3106"] = [device]
        evdev_interfaces = [{"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}]
        manager.grab_state.desired_paths["2dc8:3106"] = {"keymasq:2dc8:3106"}
        manager.grab_state.desired_grabs["2dc8:3106"] = DesiredGrabConfig(
            paths={"keymasq:2dc8:3106"},
            button_map={"btn_south": "btn_south"},
            evdev_interfaces=evdev_interfaces,
        )
        enable_hotplug_hiding = AsyncMock()

        async def clear_combo_runtime(*_args, **_kwargs) -> None:
            return None

        monkeypatch.setattr(
            lifecycle,
            "clear_combo_runtime_for_binding_scope",
            clear_combo_runtime,
        )
        monkeypatch.setattr(
            source_hiding,
            "enable_hardware_hotplug_hiding",
            enable_hotplug_hiding,
        )
        monkeypatch.setattr(outputs, "destroy_global_uinputs", Mock())

        await release.release_interface_unlocked(
            manager,
            "2dc8:3106",
            "/dev/input/event2",
        )

        enable_hotplug_hiding.assert_awaited_once_with("2dc8:3106")
        assert "2dc8:3106" not in manager.grabbed_devices
        assert manager.grab_state.desired_grabs["2dc8:3106"].evdev_interfaces == (evdev_interfaces)

    @pytest.mark.asyncio
    async def test_release_on_hold_state_is_retried_then_released(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        manager = DeviceManager(held_release_retry_s=0.001)
        fake_device = type("Device", (), {})()
        fake_device.release = AsyncMock()

        holds = {"count": 0}

        def has_held() -> bool:
            holds["count"] += 1
            return holds["count"] == 1

        manager.grabbed_devices = {"hw": [fake_device]}
        fake_device.has_held_source_inputs = has_held

        async def release_device(_manager, _hardware_id: str, *, log) -> None:
            await fake_device.release()

        monkeypatch.setattr(release, "release_device_unlocked", release_device)

        await release.schedule_hardware_release_unlocked(
            manager,
            "hw",
            0.001,
            asyncio_mod=adapters.ASYNCIO_RUNTIME,
            log=device_manager.log,
        )
        task = manager.grab_state.pending_hardware_release["hw"]
        await task

        assert fake_device.release.await_count == 1
        assert holds["count"] >= 2
