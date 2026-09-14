import asyncio
from collections.abc import Callable

import evdev
import pytest

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.analog import AnalogControlConfig, AnalogMouseMotionConfig
from keymasq.common.model.core import ActionType, DeviceType, SuperkeyMode
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.runtime.grabbed_device.device import GrabbedDevice, log
from keymasq.keymasqd.runtime.grabbed_device.event.pipeline import (
    build_event_processing_deps,
    process_event,
)
from keymasq.keymasqd.superkey_state import SuperkeyActionData, SuperkeyConfig
from tests.keymasqd.device_manager_support import make_combo_grabbed_device


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

    async def reset_mapping_runtime_state(self, previous_mapping=None) -> None:
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


@pytest.mark.asyncio
@pytest.mark.parametrize("release_before_switch", [False, True])
async def test_pattern_gesture_finishes_after_mapping_replacement(release_before_switch):
    original = MappingAction(
        action_type=ActionType.SUPERKEY,
        superkey_config=SuperkeyConfig(
            name="original",
            mode=SuperkeyMode.PATTERN,
            double_tap_window_ms=30,
            tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
            double_tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_c")],
        ),
    )
    mapping_ref = {"value": {"btn_side": original}}
    device, keyboard = _build_grabbed_device(mapping_ref)
    down = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 1)
    up = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 0)
    try:
        await _process_event(device, down)
        if release_before_switch:
            await _process_event(device, up)
        previous = mapping_ref["value"]
        mapping_ref["value"] = {
            "btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_b")
        }
        await device.reset_mapping_runtime_state(previous_mapping=previous)
        assert device.has_held_source_inputs()
        if not release_before_switch:
            await _process_event(device, up)
        await _wait_until(lambda: "btn_side" not in device.state.superkey_machines)
        assert _has_key_event(keyboard, evdev.ecodes.KEY_A, 1)
        assert _has_key_event(keyboard, evdev.ecodes.KEY_A, 0)
        assert not device.has_held_source_inputs()
        await _process_event(device, down)
        await _process_event(device, up)
        assert _has_key_event(keyboard, evdev.ecodes.KEY_B, 1)
        assert _has_key_event(keyboard, evdev.ecodes.KEY_B, 0)
    finally:
        await device.reset_superkeys()


@pytest.mark.asyncio
async def test_pattern_hold_survives_repeated_mapping_updates():
    original = MappingAction(
        action_type=ActionType.SUPERKEY,
        superkey_config=SuperkeyConfig(
            name="hold",
            hold_threshold_ms=10,
            hold_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
        ),
    )
    mapping_ref = {"value": {"btn_side": original}}
    device, keyboard = _build_grabbed_device(mapping_ref)
    try:
        await _process_event(
            device, evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 1)
        )
        await _wait_until(lambda: _has_key_event(keyboard, evdev.ecodes.KEY_A, 1))
        for _ in range(3):
            previous = mapping_ref["value"]
            mapping_ref["value"] = {}
            await device.reset_mapping_runtime_state(previous_mapping=previous)
            assert not _has_key_event(keyboard, evdev.ecodes.KEY_A, 0)
        await _process_event(
            device, evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 0)
        )
        assert _has_key_event(keyboard, evdev.ecodes.KEY_A, 0)
        assert device.state.superkey_machines == {}
    finally:
        await device.reset_superkeys()


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_mapping", [False, True])
async def test_pattern_second_press_uses_original_gesture_after_switch(empty_mapping):
    original = MappingAction(
        action_type=ActionType.SUPERKEY,
        superkey_config=SuperkeyConfig(
            name="double",
            double_tap_window_ms=500,
            double_tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
        ),
    )
    mapping_ref = {"value": {"btn_side": original}}
    device, keyboard = _build_grabbed_device(mapping_ref)
    down = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 1)
    up = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 0)
    try:
        await _process_event(device, down)
        await _process_event(device, up)
        previous = mapping_ref["value"]
        mapping_ref["value"] = (
            {}
            if empty_mapping
            else {"btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_b")}
        )
        await device.reset_mapping_runtime_state(previous_mapping=previous)
        await _process_event(device, down)
        await _process_event(device, up)
        assert keyboard.events == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]
        assert device.state.superkey_machines == {}
        assert device.state.held_source_actions == {}
    finally:
        await device.reset_superkeys()


@pytest.mark.asyncio
async def test_deferred_release_stops_analog_motion_while_button_remains_held():
    manager = DeviceManager(release_grace_s=0.01, held_release_retry_s=0.01)
    hardware_id = "1234:5678"
    manager.active_mappings[hardware_id] = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Stick Mouse",
                mouse_motion=AnalogMouseMotionConfig(
                    enabled=True, speed=10000, deadzone=0.0, tick_ms=1
                ),
            ),
        ),
        "btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
    }
    mouse = _FakeUInput()
    keyboard = _FakeUInput()
    device = GrabbedDevice(
        path="/dev/input/event-test",
        hardware_id=hardware_id,
        button_map={"btn_side": "btn_side"},
        mapping_getter=lambda: manager.active_mappings.get(hardware_id, {}),
        event_callback=_noop_event_callback,
        keyboard_uinput=keyboard,
        mouse_uinput=mouse,
        device_type=DeviceType.GAMEPAD,
        default_output="passthrough",
        analog_inputs={
            "left_stick": {
                "type": "stick",
                "axes": [{"role": "x", "evdev": "abs_x", "evdev_code": evdev.ecodes.ABS_X}],
            }
        },
    )
    device.uinput = _FakeUInput()
    device.running = True
    device.analog_axis_ranges = {("left_stick", "x"): (-32768, 32767)}
    manager.grabbed_devices[hardware_id] = [device]
    manager.grab_state.desired_paths[hardware_id] = {device.path}
    try:
        await _process_event(
            device, evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 1)
        )
        await _process_event(
            device, evdev.InputEvent(0, 0, evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 32767)
        )
        await _wait_until(lambda: bool(mouse.events))
        result = await manager.release_device(hardware_id, immediate=False)
        assert result["scheduled"] is True
        await _process_event(
            device, evdev.InputEvent(0, 0, evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 0)
        )
        event_count = len(mouse.events)
        await asyncio.sleep(0.04)
        assert len(mouse.events) == event_count
        assert hardware_id in manager.grabbed_devices
        assert device.has_held_source_inputs()
        assert not _has_key_event(keyboard, evdev.ecodes.KEY_A, 0)
        await _process_event(
            device, evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SIDE, 0)
        )
        assert _has_key_event(keyboard, evdev.ecodes.KEY_A, 0)
        await _wait_until(lambda: hardware_id not in manager.grabbed_devices)
    finally:
        await manager.release_device(hardware_id, immediate=True)


@pytest.mark.asyncio
async def test_retained_pattern_second_press_precedes_replacement_combo(monkeypatch):
    manager = DeviceManager()
    keyboard = _FakeUInput()
    manager.output_state.keyboard_uinput = keyboard
    mapping_ref = {
        "value": {
            "key_a": MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=SuperkeyConfig(
                    name="Double Tap",
                    double_tap_window_ms=500,
                    double_tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_c")],
                ),
            )
        }
    }
    device = make_combo_grabbed_device(
        monkeypatch,
        manager,
        button_map={"key_a": "key_a", "key_b": "key_b"},
        mapping_getter=lambda: mapping_ref["value"],
        keyboard_uinput=keyboard,
    )

    async def key(code, value):
        await _process_event(device, evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, code, value))

    try:
        await key(evdev.ecodes.KEY_A, 1)
        await key(evdev.ecodes.KEY_A, 0)
        previous = mapping_ref["value"]
        mapping_ref["value"] = {}
        await device.reset_mapping_runtime_state(previous_mapping=previous)
        await manager.set_combos(
            [
                {
                    "id": "replacement",
                    "name": "Replacement chord",
                    "steps": [
                        {
                            "events": [
                                {"hardware_id": device.hardware_id, "source": "kbd", "evdev": name}
                                for name in ("key_b", "key_a")
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_d"},
                }
            ]
        )
        await key(evdev.ecodes.KEY_B, 1)
        await key(evdev.ecodes.KEY_A, 1)
        await key(evdev.ecodes.KEY_A, 0)
        await key(evdev.ecodes.KEY_B, 0)
        assert keyboard.events == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_C, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_C, 0),
        ]
        assert device.state.superkey_machines == {}
        await key(evdev.ecodes.KEY_B, 1)
        await key(evdev.ecodes.KEY_A, 1)
        await key(evdev.ecodes.KEY_A, 0)
        await key(evdev.ecodes.KEY_B, 0)
        assert _has_key_event(keyboard, evdev.ecodes.KEY_D, 1)
        assert _has_key_event(keyboard, evdev.ecodes.KEY_D, 0)
    finally:
        await manager.set_combos([])
        await device.reset_superkeys()
