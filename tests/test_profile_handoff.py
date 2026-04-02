import asyncio

import evdev
import pytest

from keyforge.common.models import ActionType, MappingAction
from keyforge.keyforged.device_manager import DeviceManager, GrabbedDevice


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

    def release_tracked_outputs(self) -> None:
        self.cleaned = True

    async def release(self) -> None:
        self.released = True

    def has_held_source_inputs(self) -> bool:
        return self.held


async def _noop_event_callback(*_args, **_kwargs) -> None:
    return


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
    device._running = True
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

    await device._process_event(down)
    mapping_ref["value"] = {
        "btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_b"),
    }
    await device._process_event(up)

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

    await device._process_event(down)
    await asyncio.sleep(0.04)

    mapping_ref["value"] = {
        "btn_side": MappingAction(
            action_type=ActionType.KEYBOARD,
            target="key_b",
            rapidfire_enabled=True,
            rapidfire_hold_ms=10,
            rapidfire_wait_ms=10,
        ),
    }
    await device._process_event(up)
    await asyncio.sleep(0.04)

    key_a = evdev.ecodes.KEY_A
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

    await device._process_event(down)
    mapping_ref["value"] = {
        "btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_b"),
    }
    mapping_ref["value"] = {
        "btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_c"),
    }
    await device._process_event(up)

    key_events = [e for e in keyboard.events if e[0] == evdev.ecodes.EV_KEY]
    assert (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1) in key_events
    assert (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0) in key_events
    assert all(code not in (evdev.ecodes.KEY_B, evdev.ecodes.KEY_C) for _, code, _ in key_events)
    assert device._held_source_actions == {}
    assert device._held_output_keys["keyboard"] == set()


@pytest.mark.asyncio
async def test_release_device_uses_grace_period_and_cleans_outputs() -> None:
    manager = DeviceManager(release_grace_s=0.05)
    dummy = _DummyGrabbedDevice("/dev/input/event-dummy")

    manager.grabbed_devices["1234:5678"] = [dummy]
    manager.active_mappings["1234:5678"] = {
        "btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_a")
    }
    manager._desired_paths["1234:5678"] = {dummy.path}

    result = await manager.release_device("1234:5678", immediate=False, grace_s=0.05)
    assert result["scheduled"] is True
    assert dummy.cleaned is False

    await asyncio.sleep(0.08)

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
    manager._desired_paths["1234:5678"] = {dummy.path}

    result = await manager.release_device("1234:5678", immediate=False, grace_s=0.03)
    assert result["scheduled"] is True

    await asyncio.sleep(0.04)
    assert "1234:5678" in manager.grabbed_devices
    assert dummy.released is False

    dummy.held = False
    await asyncio.sleep(0.05)

    assert dummy.released is True
    assert "1234:5678" not in manager.grabbed_devices
