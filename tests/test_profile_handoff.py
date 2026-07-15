import asyncio
from collections.abc import Callable

import evdev
import pytest

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.runtime.grabbed_device.device import GrabbedDevice, log
from keymasq.keymasqd.runtime.grabbed_device.event.pipeline import (
    build_event_processing_deps,
    process_event,
)


class _FakeUInput:
    def __init__(self) -> None:
        self.events: list[tuple[int, int, int]] = []

    def write(self, event_type: int, code: int, value: int) -> None:
        self.events.append((int(event_type), int(code), int(value)))

    def syn(self) -> None:
        return


class _DummyGrabbedDevice:
    def __init__(self, path: str) -> None:
        self.path = path
        self.cleaned = False
        self.released = False
        self.held = False
        self.held_checks = 0

    def release_tracked_outputs(self) -> None:
        self.cleaned = True

    async def stop_event_loop(self) -> None:
        return

    async def release(self) -> None:
        self.released = True

    def has_held_source_inputs(self) -> bool:
        self.held_checks += 1
        return self.held


async def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout_s: float = 1.0,
    interval_s: float = 0.005,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval_s)
    assert predicate()


def _has_key_event(keyboard: _FakeUInput, code: int, value: int) -> bool:
    return (evdev.ecodes.EV_KEY, code, value) in keyboard.events


async def _noop_event_callback(*_args, **_kwargs) -> None:
    return


async def _process_event(device: GrabbedDevice, event: evdev.InputEvent) -> None:
    await process_event(
        device,
        event,
        deps=build_event_processing_deps(log=log),
    )


def _build_grabbed_device(mapping_ref: dict) -> tuple[GrabbedDevice, _FakeUInput]:
    keyboard = _FakeUInput()
    passthrough = _FakeUInput()
    device = GrabbedDevice(
        path="/dev/input/event-test",
        hardware_id="1234:5678",
        button_map={"btn_side": "btn_side"},
        mapping_getter=lambda: mapping_ref["value"],
        event_callback=_noop_event_callback,
        keyboard_uinput=keyboard,
        mouse_uinput=_FakeUInput(),
        gamepad_uinput=_FakeUInput(),
    )
    device.uinput = passthrough
    device.running = True
    return device, keyboard


@pytest.mark.asyncio
async def test_profile_switch_defers_rebind_until_release() -> None:
    mapping_ref = {
        "value": {
            "btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
        }
    }
    device, keyboard = _build_grabbed_device(mapping_ref)

    down = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 1)
    up = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 0)

    await _process_event(device, down)
    mapping_ref["value"] = {
        "btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_b"),
    }
    await _process_event(device, up)

    key_a = evdev.ecodes.KEY_A
    key_b = evdev.ecodes.KEY_B
    key_events = [e for e in keyboard.events if e[0] == evdev.ecodes.EV_KEY]
    assert (evdev.ecodes.EV_KEY, key_a, 1) in key_events
    assert (evdev.ecodes.EV_KEY, key_a, 0) in key_events
    assert all(code != key_b for _, code, _ in key_events)


@pytest.mark.asyncio
async def test_rapidfire_release_uses_original_action_after_switch() -> None:
    mapping_ref = {
        "value": {
            "btn_side": MappingAction(
                action_type=ActionType.KEYBOARD,
                target="key_a",
                rapidfire_enabled=True,
                rapidfire_hold_ms=10,
                rapidfire_wait_ms=10,
            ),
        }
    }
    device, keyboard = _build_grabbed_device(mapping_ref)

    down = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 1)
    up = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 0)

    await _process_event(device, down)
    key_a = evdev.ecodes.KEY_A
    await _wait_until(lambda: _has_key_event(keyboard, key_a, 1))
    await _wait_until(lambda: _has_key_event(keyboard, key_a, 0))

    mapping_ref["value"] = {
        "btn_side": MappingAction(
            action_type=ActionType.KEYBOARD,
            target="key_b",
            rapidfire_enabled=True,
            rapidfire_hold_ms=10,
            rapidfire_wait_ms=10,
        ),
    }
    await _process_event(device, up)
    await _wait_until(lambda: device.state.rapidfire_tasks == {})

    key_b = evdev.ecodes.KEY_B
    key_events = [e for e in keyboard.events if e[0] == evdev.ecodes.EV_KEY]
    assert any(code == key_a and value == 1 for _, code, value in key_events)
    assert any(code == key_a and value == 0 for _, code, value in key_events)
    assert all(code != key_b for _, code, _ in key_events)


@pytest.mark.asyncio
async def test_multiple_switches_while_held_keep_original_release_and_clear_state() -> None:
    mapping_ref = {
        "value": {
            "btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
        }
    }
    device, keyboard = _build_grabbed_device(mapping_ref)

    down = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 1)
    up = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 0)

    await _process_event(device, down)
    mapping_ref["value"] = {
        "btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_b"),
    }
    mapping_ref["value"] = {
        "btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_c"),
    }
    await _process_event(device, up)

    key_events = [e for e in keyboard.events if e[0] == evdev.ecodes.EV_KEY]
    assert (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1) in key_events
    assert (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0) in key_events
    assert all(code not in (evdev.ecodes.KEY_B, evdev.ecodes.KEY_C) for _, code, _ in key_events)
    assert device.state.held_source_actions == {}
    assert device.state.held_output_keys["keyboard"] == set()


@pytest.mark.asyncio
async def test_release_device_uses_grace_period_and_cleans_outputs() -> None:
    manager = DeviceManager(release_grace_s=0.05)
    dummy = _DummyGrabbedDevice("/dev/input/event-dummy")

    manager.grabbed_devices["1234:5678"] = [dummy]
    manager.active_mappings["1234:5678"] = {
        "btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_a")
    }
    manager.grab_state.desired_paths["1234:5678"] = {dummy.path}

    result = await manager.release_device("1234:5678", immediate=False, grace_s=0.05)
    assert result["scheduled"] is True
    assert dummy.cleaned is False

    await _wait_until(lambda: dummy.released is True and "1234:5678" not in manager.grabbed_devices)

    assert dummy.released is True
    assert "1234:5678" not in manager.grabbed_devices


@pytest.mark.asyncio
async def test_release_device_retries_when_source_button_is_held() -> None:
    manager = DeviceManager(release_grace_s=0.03, held_release_retry_s=0.03)
    dummy = _DummyGrabbedDevice("/dev/input/event-dummy")
    dummy.held = True

    manager.grabbed_devices["1234:5678"] = [dummy]
    manager.active_mappings["1234:5678"] = {
        "btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_a")
    }
    manager.grab_state.desired_paths["1234:5678"] = {dummy.path}

    result = await manager.release_device("1234:5678", immediate=False, grace_s=0.03)
    assert result["scheduled"] is True

    await _wait_until(lambda: dummy.held_checks >= 1)
    assert "1234:5678" in manager.grabbed_devices
    assert dummy.released is False

    dummy.held = False
    await _wait_until(lambda: dummy.released is True and "1234:5678" not in manager.grabbed_devices)

    assert dummy.released is True
    assert "1234:5678" not in manager.grabbed_devices
