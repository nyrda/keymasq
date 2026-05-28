import asyncio
from types import SimpleNamespace

import evdev
import pytest

from keymasq.common.ipc import CommandType
from keymasq.common.models import (
    SAME_DEVICE_OUTPUT_ID,
    ActionType,
    AnalogActionThreshold,
    AnalogControlConfig,
    AnalogGamepadOutputConfig,
    AnalogMouseMotionConfig,
    MappingAction,
    ProfileDeactivationPolicy,
)
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.runtime.analog_controls import (
    _axis_motion_delta,
    _motion_delta,
    normalize_axis_value,
    normalize_control_axis_value,
    preserved_analog_state_keys,
    process_analog_event,
    reset_analog_controls,
)
from keymasq.keymasqd.runtime.grabbed_device_outputs import release_all_keys
from keymasq.keymasqd.runtime.grabbed_device_types import (
    ActionExecutionDeps,
    GrabbedDeviceState,
    identity_uinput_writer,
)
from keymasq.keymasqd.runtime.repeat import RepeatHistoryEntry, RepeatRuntimeState


class FakeUInput:
    def __init__(self) -> None:
        self.events: list[tuple[int, int, int]] = []

    def write(self, event_type: int, code: int, value: int) -> None:
        self.events.append((event_type, code, value))

    def syn(self) -> None:
        pass


class FakeEvent:
    type = evdev.ecodes.EV_ABS
    code = evdev.ecodes.ABS_X

    def __init__(self, value: int) -> None:
        self.value = value


def _deps() -> ActionExecutionDeps:
    return ActionExecutionDeps(
        asyncio_mod=asyncio,
        fire_and_observe_fn=lambda coro, _label: asyncio.create_task(coro),
        evdev_mod=evdev,
        uinput_writer=identity_uinput_writer,
    )


def _runtime(mapping: dict[str, MappingAction], keyboard: FakeUInput) -> SimpleNamespace:
    return SimpleNamespace(
        hardware_id="1234:5678",
        keyboard_uinput=keyboard,
        mouse_uinput=FakeUInput(),
        gamepad_uinput=None,
        uinput=None,
        broadcast_callback=None,
        cursor_position_setter=None,
        recording_manager=None,
        macro_player=None,
        emergency_resetter=None,
        suppress_rel_getter=None,
        diagnostics_recorder=None,
        runtime_cleanup_callback=None,
        mapping_getter=lambda: mapping,
        state=GrabbedDeviceState(),
        analog_inputs={"left_stick": {"label": "Left Stick", "type": "stick"}},
        analog_axis_bindings={(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X): ("left_stick", "x")},
        analog_axis_output_codes={("left_stick", "x"): evdev.ecodes.ABS_X},
        analog_axis_ranges={("left_stick", "x"): (-32768, 32767)},
        analog_axis_calibrations={},
        resolve_gamepad_output=lambda _output_id, _context: None,
    )


def test_normalize_axis_value_maps_sides_independently() -> None:
    assert normalize_axis_value(-32768, -32768, 32767) == pytest.approx(-1.0)
    assert normalize_axis_value(32767, -32768, 32767) == pytest.approx(1.0)
    assert abs(normalize_axis_value(0, -32768, 32767)) < 0.001


def test_normalize_control_axis_value_maps_rest_to_zero_and_endpoint_to_one() -> None:
    assert normalize_control_axis_value(0, 0, 255, rest=0) == pytest.approx(0.0)
    assert normalize_control_axis_value(255, 0, 255, rest=0) == pytest.approx(1.0)
    assert normalize_control_axis_value(128, 0, 255, rest=0) == pytest.approx(
        0.5019,
        abs=0.0001,
    )
    assert normalize_control_axis_value(-32768, -32768, 0, rest=0) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_threshold_enter_and_release_emit_child_actions() -> None:
    keyboard = FakeUInput()
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Test",
                thresholds=[
                    AnalogActionThreshold(
                        axis="x",
                        trigger_min=0.65,
                        trigger_max=1.0,
                        release_min=0.55,
                        release_max=1.0,
                        actions=[MappingAction(action_type=ActionType.KEYBOARD, target="key_a")],
                    )
                ],
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)

    assert await process_analog_event(runtime, FakeEvent(32767), "abs_x", mapping, deps=_deps())
    assert keyboard.events[-1] == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)

    assert await process_analog_event(runtime, FakeEvent(0), "abs_x", mapping, deps=_deps())
    assert keyboard.events[-1] == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0)


@pytest.mark.asyncio
async def test_threshold_profile_trigger_end_lifetime_follows_threshold_lifecycle() -> None:
    events: list[tuple[CommandType, dict[str, object]]] = []
    action_triggers: list[dict[str, object]] = []
    action_trigger_event = asyncio.Event()
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
        if event_type == CommandType.ACTION_TRIGGER:
            action_triggers.append(data)
            await manager.track_profile_activation(
                str(data["profile_name"]),
                "activation-1",
                str(data["trigger_id"]),
                data["deactivation"],
            )
            action_trigger_event.set()
            return
        event = (event_type, data)
        events.append(event)
        if event == expected_deactivate:
            deactivate_event.set()

    manager = DeviceManager(broadcast_callback=broadcast)
    keyboard = FakeUInput()
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Test",
                thresholds=[
                    AnalogActionThreshold(
                        axis="x",
                        trigger_min=0.65,
                        trigger_max=1.0,
                        release_min=0.55,
                        release_max=1.0,
                        actions=[
                            MappingAction(
                                action_type=ActionType.PROFILE_ENABLE,
                                profile_name="Nav",
                                profile_deactivation=ProfileDeactivationPolicy(
                                    on_trigger_end=True
                                ),
                            )
                        ],
                    )
                ],
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.broadcast_callback = broadcast
    runtime.profile_activation_trigger_start_observer = (
        manager.observe_profile_trigger_start
    )
    runtime.profile_activation_trigger_end_observer = manager.observe_profile_trigger_end

    assert await process_analog_event(runtime, FakeEvent(32767), "abs_x", mapping, deps=_deps())
    await asyncio.wait_for(action_trigger_event.wait(), timeout=1.0)

    assert action_triggers == [
        {
            "action_type": "profile_enable",
            "profile_name": "Nav",
            "source_device": "1234:5678",
            "source_button": "left_stick#analog_threshold#0#0",
            "trigger_id": "1234:5678:left_stick#analog_threshold#0#0",
            "deactivation": {
                "on_trigger_end": True,
            },
        }
    ]
    assert [
        event for event in events if event[0] == CommandType.PROFILE_DEACTIVATE_REQUESTED
    ] == []

    assert await process_analog_event(runtime, FakeEvent(0), "abs_x", mapping, deps=_deps())
    await asyncio.wait_for(deactivate_event.wait(), timeout=1.0)

    assert expected_deactivate in events


@pytest.mark.asyncio
async def test_threshold_repeat_profile_trigger_end_lifetime_follows_threshold_lifecycle() -> None:
    events: list[tuple[CommandType, dict[str, object]]] = []
    action_triggers: list[dict[str, object]] = []
    action_trigger_event = asyncio.Event()
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
        if event_type == CommandType.ACTION_TRIGGER:
            action_triggers.append(data)
            await manager.track_profile_activation(
                str(data["profile_name"]),
                "activation-1",
                str(data["trigger_id"]),
                data["deactivation"],
            )
            action_trigger_event.set()
            return
        event = (event_type, data)
        events.append(event)
        if event == expected_deactivate:
            deactivate_event.set()

    manager = DeviceManager(broadcast_callback=broadcast)
    keyboard = FakeUInput()
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Test",
                thresholds=[
                    AnalogActionThreshold(
                        axis="x",
                        trigger_min=0.65,
                        trigger_max=1.0,
                        release_min=0.55,
                        release_max=1.0,
                        actions=[MappingAction(action_type=ActionType.REPEAT)],
                    )
                ],
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.broadcast_callback = broadcast
    runtime.profile_activation_trigger_start_observer = (
        manager.observe_profile_trigger_start
    )
    runtime.profile_activation_trigger_end_observer = manager.observe_profile_trigger_end
    runtime.repeat_state = RepeatRuntimeState()
    runtime.repeat_state.history.append(
        RepeatHistoryEntry(
            category="special",
            action=MappingAction(
                action_type=ActionType.PROFILE_ENABLE,
                profile_name="Nav",
                profile_deactivation=ProfileDeactivationPolicy(on_trigger_end=True),
            ),
        )
    )

    assert await process_analog_event(runtime, FakeEvent(32767), "abs_x", mapping, deps=_deps())
    await asyncio.wait_for(action_trigger_event.wait(), timeout=1.0)

    assert action_triggers == [
        {
            "action_type": "profile_enable",
            "profile_name": "Nav",
            "source_device": "1234:5678",
            "source_button": "left_stick#analog_threshold#0#0",
            "trigger_id": "1234:5678:left_stick#analog_threshold#0#0",
            "deactivation": {
                "on_trigger_end": True,
            },
        }
    ]
    assert [
        event for event in events if event[0] == CommandType.PROFILE_DEACTIVATE_REQUESTED
    ] == []

    assert await process_analog_event(runtime, FakeEvent(0), "abs_x", mapping, deps=_deps())
    await asyncio.wait_for(deactivate_event.wait(), timeout=1.0)

    assert expected_deactivate in events


@pytest.mark.asyncio
async def test_threshold_activation_counts_parent_profile_action() -> None:
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
    keyboard = FakeUInput()
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            source_profile_name="Nav",
            analog_control_config=AnalogControlConfig(
                name="Test",
                thresholds=[
                    AnalogActionThreshold(
                        axis="x",
                        trigger_min=0.65,
                        trigger_max=1.0,
                        release_min=0.55,
                        release_max=1.0,
                        actions=[
                            MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
                            MappingAction(action_type=ActionType.KEYBOARD, target="key_b"),
                        ],
                    )
                ],
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.profile_activation_recorder = manager.record_profile_action

    await manager.track_profile_activation(
        "Nav",
        "activation-1",
        "trigger-1",
        {"after_actions": 1},
    )

    assert await process_analog_event(runtime, FakeEvent(32767), "abs_x", mapping, deps=_deps())
    await asyncio.wait_for(deactivate_event.wait(), timeout=1.0)

    assert events == [expected_deactivate]
    assert keyboard.events == [
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
    ]


@pytest.mark.asyncio
async def test_multiple_analog_controls_on_one_source_fan_out_independently() -> None:
    keyboard = FakeUInput()
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_configs=[
                AnalogControlConfig(
                    name="A",
                    thresholds=[
                        AnalogActionThreshold(
                            axis="x",
                            trigger_min=0.65,
                            trigger_max=1.0,
                            release_min=0.55,
                            release_max=1.0,
                            actions=[
                                MappingAction(action_type=ActionType.KEYBOARD, target="key_a")
                            ],
                        )
                    ],
                ),
                AnalogControlConfig(
                    name="B",
                    thresholds=[
                        AnalogActionThreshold(
                            axis="x",
                            trigger_min=0.65,
                            trigger_max=1.0,
                            release_min=0.55,
                            release_max=1.0,
                            actions=[
                                MappingAction(action_type=ActionType.KEYBOARD, target="key_b")
                            ],
                        )
                    ],
                ),
            ],
        )
    }
    runtime = _runtime(mapping, keyboard)

    assert await process_analog_event(runtime, FakeEvent(32767), "abs_x", mapping, deps=_deps())
    assert (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1) in keyboard.events
    assert (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1) in keyboard.events

    assert await process_analog_event(runtime, FakeEvent(0), "abs_x", mapping, deps=_deps())
    assert (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0) in keyboard.events
    assert (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0) in keyboard.events


@pytest.mark.asyncio
async def test_obsolete_mouse_plus_digital_runs_only_digital_actions() -> None:
    keyboard = FakeUInput()
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Old Combined",
                mouse_motion=AnalogMouseMotionConfig(
                    enabled=True,
                    speed=10000,
                    deadzone=0.0,
                    tick_ms=1,
                ),
                thresholds=[
                    AnalogActionThreshold(
                        axis="x",
                        trigger_min=0.65,
                        trigger_max=1.0,
                        release_min=0.55,
                        release_max=1.0,
                        actions=[MappingAction(action_type=ActionType.KEYBOARD, target="key_a")],
                    )
                ],
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)

    assert await process_analog_event(runtime, FakeEvent(32767), "abs_x", mapping, deps=_deps())
    await asyncio.sleep(0.01)

    assert (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1) in keyboard.events
    assert runtime.state.analog_mouse_tasks == {}
    assert runtime.mouse_uinput.events == []


@pytest.mark.asyncio
async def test_trigger_threshold_uses_positive_normalized_range() -> None:
    keyboard = FakeUInput()
    mapping = {
        "left_trigger": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Trigger",
                input_type="axis",
                thresholds=[
                    AnalogActionThreshold(
                        axis="x",
                        trigger_min=0.5,
                        trigger_max=1.0,
                        release_min=0.45,
                        release_max=1.0,
                        actions=[MappingAction(action_type=ActionType.KEYBOARD, target="key_a")],
                    )
                ],
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.analog_axis_bindings = {
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z): ("left_trigger", "x")
    }
    runtime.analog_axis_ranges = {("left_trigger", "x"): (0, 255)}
    event = FakeEvent(200)
    event.code = evdev.ecodes.ABS_Z

    assert await process_analog_event(runtime, event, "abs_z", mapping, deps=_deps())
    assert keyboard.events[-1] == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)

    event = FakeEvent(0)
    event.code = evdev.ecodes.ABS_Z
    assert await process_analog_event(runtime, event, "abs_z", mapping, deps=_deps())
    assert keyboard.events[-1] == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0)


@pytest.mark.asyncio
async def test_axis_threshold_can_use_negative_signed_range() -> None:
    keyboard = FakeUInput()
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Signed Axis",
                input_type="axis",
                thresholds=[
                    AnalogActionThreshold(
                        axis="x",
                        trigger_min=-1.0,
                        trigger_max=-0.5,
                        release_min=-1.0,
                        release_max=-0.45,
                        actions=[MappingAction(action_type=ActionType.KEYBOARD, target="key_a")],
                    )
                ],
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)

    assert await process_analog_event(
        runtime,
        FakeEvent(-25000),
        "abs_x",
        mapping,
        deps=_deps(),
    )
    assert keyboard.events[-1] == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)

    assert await process_analog_event(runtime, FakeEvent(0), "abs_x", mapping, deps=_deps())
    assert keyboard.events[-1] == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0)


@pytest.mark.asyncio
async def test_axis_mouse_motion_uses_direction_and_response_curve() -> None:
    keyboard = FakeUInput()
    mapping = {
        "left_trigger": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Axis Mouse",
                input_type="axis",
                mouse_motion=AnalogMouseMotionConfig(
                    enabled=True,
                    speed=10000,
                    deadzone=0.0,
                    sensitivity=2.0,
                    response_curve=2.0,
                    direction="up",
                    tick_ms=1,
                ),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.analog_axis_bindings = {
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z): ("left_trigger", "x")
    }
    runtime.analog_axis_ranges = {("left_trigger", "x"): (0, 255)}
    event = FakeEvent(64)
    event.code = evdev.ecodes.ABS_Z

    assert await process_analog_event(runtime, event, "abs_z", mapping, deps=_deps())
    await asyncio.sleep(0.01)
    await reset_analog_controls(runtime, deps=_deps())

    mouse_events = runtime.mouse_uinput.events
    assert any(
        event_type == evdev.ecodes.EV_REL
        and code == evdev.ecodes.REL_Y
        and value < 0
        for event_type, code, value in mouse_events
    )
    assert not any(code == evdev.ecodes.REL_X for _event_type, code, _value in mouse_events)


def test_preserved_analog_state_keys_only_keeps_unchanged_analog_controls() -> None:
    analog_action = MappingAction(
        action_type=ActionType.ANALOG_CONTROL,
        source_profile_name="Desktop",
        analog_control_config=AnalogControlConfig(
            name="Mouse",
            mouse_motion=AnalogMouseMotionConfig(enabled=True),
        ),
    )
    old_mapping = {
        "left_stick": analog_action,
        "right_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Old",
                mouse_motion=AnalogMouseMotionConfig(enabled=True),
            ),
        ),
    }
    new_mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            source_profile_name="Plex",
            analog_control_config=AnalogControlConfig(
                name="Mouse",
                mouse_motion=AnalogMouseMotionConfig(enabled=True),
            ),
        ),
        "right_stick": MappingAction(action_type=ActionType.SUPPRESS),
        "south": MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
    }

    assert preserved_analog_state_keys(old_mapping, new_mapping) == {"left_stick"}


@pytest.mark.asyncio
async def test_reset_analog_controls_preserves_unchanged_mouse_motion() -> None:
    keyboard = FakeUInput()
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Stick Mouse",
                mouse_motion=AnalogMouseMotionConfig(
                    enabled=True,
                    speed=10000,
                    deadzone=0.0,
                    tick_ms=1,
                ),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)

    assert await process_analog_event(runtime, FakeEvent(32767), "abs_x", mapping, deps=_deps())
    await asyncio.sleep(0.01)
    event_count = len(runtime.mouse_uinput.events)

    mapping["south"] = MappingAction(action_type=ActionType.SUPPRESS)
    await reset_analog_controls(runtime, deps=_deps(), preserve_state_keys={"left_stick"})
    await asyncio.sleep(0.01)
    await reset_analog_controls(runtime, deps=_deps())

    assert len(runtime.mouse_uinput.events) > event_count


def test_axis_mouse_motion_supports_bidirectional_signed_output() -> None:
    assert _axis_motion_delta(
        -0.5,
        signed_value=-0.5,
        direction="horizontal",
        speed=100,
        deadzone=0.0,
        sensitivity=1.0,
        response_curve=1.0,
        dt=1.0,
    ) == pytest.approx((-50.0, 0.0))
    assert _axis_motion_delta(
        0.5,
        signed_value=0.5,
        direction="vertical",
        speed=100,
        deadzone=0.0,
        sensitivity=1.0,
        response_curve=1.0,
        dt=1.0,
    ) == pytest.approx((0.0, 50.0))


def test_stick_mouse_motion_applies_split_horizontal_vertical_speed() -> None:
    assert _motion_delta(
        1.0,
        1.0,
        speed_x=100,
        speed_y=50,
        deadzone=0.0,
        sensitivity=1.0,
        response_curve=1.0,
        dt=1.0,
    ) == pytest.approx((70.7107, 35.3553), abs=0.001)


def test_stick_mouse_motion_preserves_zero_split_speed() -> None:
    assert _motion_delta(
        1.0,
        1.0,
        speed_x=0,
        speed_y=50,
        deadzone=0.0,
        sensitivity=1.0,
        response_curve=1.0,
        dt=1.0,
    ) == pytest.approx((0.0, 35.3553), abs=0.001)


@pytest.mark.asyncio
async def test_stick_mouse_area_emits_relative_delta_from_previous_area_position() -> None:
    keyboard = FakeUInput()
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Area Mouse",
                mouse_motion=AnalogMouseMotionConfig(
                    enabled=True,
                    mode="area",
                    area_radius_x=1000,
                    area_radius_y=500,
                    deadzone=0.0,
                ),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.analog_axis_bindings = {
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X): ("left_stick", "x"),
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Y): ("left_stick", "y"),
    }
    runtime.analog_axis_ranges = {
        ("left_stick", "x"): (-32768, 32767),
        ("left_stick", "y"): (-32768, 32767),
    }
    runtime.analog_axis_calibrations = {
        ("left_stick", "x"): {"center": 0},
        ("left_stick", "y"): {"center": 0},
    }

    assert await process_analog_event(runtime, FakeEvent(32767), "abs_x", mapping, deps=_deps())
    y_event = FakeEvent(32767)
    y_event.code = evdev.ecodes.ABS_Y
    assert await process_analog_event(runtime, y_event, "abs_y", mapping, deps=_deps())
    assert await process_analog_event(runtime, FakeEvent(0), "abs_x", mapping, deps=_deps())

    assert runtime.mouse_uinput.events == [
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 1000),
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, 500),
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, -1000),
    ]


@pytest.mark.asyncio
async def test_stick_mouse_area_can_jump_to_start_on_activation() -> None:
    keyboard = FakeUInput()
    starts: list[tuple[int, int]] = []

    async def set_cursor_position(x: int, y: int) -> dict[str, object]:
        starts.append((x, y))
        return {"status": "ok"}

    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Area Mouse",
                mouse_motion=AnalogMouseMotionConfig(
                    enabled=True,
                    mode="area",
                    area_radius_x=100,
                    area_radius_y=100,
                    area_start_enabled=True,
                    area_start_x=300,
                    area_start_y=400,
                    deadzone=0.0,
                ),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.cursor_position_setter = set_cursor_position

    assert await process_analog_event(runtime, FakeEvent(32767), "abs_x", mapping, deps=_deps())

    assert starts == [(300, 400)]
    assert runtime.mouse_uinput.events == [
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 100)
    ]


@pytest.mark.asyncio
async def test_overlapping_thresholds_activate_independently() -> None:
    keyboard = FakeUInput()
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Overlap",
                thresholds=[
                    AnalogActionThreshold(
                        "x",
                        0.4,
                        0.9,
                        0.3,
                        1.0,
                        [MappingAction(action_type=ActionType.KEYBOARD, target="key_a")],
                    ),
                    AnalogActionThreshold(
                        "x",
                        0.6,
                        1.0,
                        0.5,
                        1.0,
                        [MappingAction(action_type=ActionType.KEYBOARD, target="key_b")],
                    ),
                ],
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)

    assert await process_analog_event(runtime, FakeEvent(26000), "abs_x", mapping, deps=_deps())

    assert (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1) in keyboard.events
    assert (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1) in keyboard.events


@pytest.mark.asyncio
async def test_overlapping_thresholds_refcount_shared_output() -> None:
    keyboard = FakeUInput()
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Overlap",
                thresholds=[
                    AnalogActionThreshold(
                        "x",
                        0.4,
                        0.6,
                        0.3,
                        0.65,
                        [MappingAction(action_type=ActionType.KEYBOARD, target="key_w")],
                    ),
                    AnalogActionThreshold(
                        "x",
                        0.55,
                        1.0,
                        0.5,
                        1.0,
                        [MappingAction(action_type=ActionType.KEYBOARD, target="key_w")],
                    ),
                ],
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)

    assert await process_analog_event(runtime, FakeEvent(19000), "abs_x", mapping, deps=_deps())
    assert keyboard.events == [(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_W, 1)]

    assert await process_analog_event(runtime, FakeEvent(23000), "abs_x", mapping, deps=_deps())
    assert keyboard.events == [(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_W, 1)]

    assert await process_analog_event(runtime, FakeEvent(0), "abs_x", mapping, deps=_deps())
    assert keyboard.events == [
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_W, 1),
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_W, 0),
    ]


@pytest.mark.asyncio
async def test_stick_gamepad_output_routes_axes_with_deadzone() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    resolved: list[str | None] = []
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Route Stick",
                gamepad_output=AnalogGamepadOutputConfig(
                    enabled=True,
                    output_id="virtual-gamepad-2",
                    deadzone=0.2,
                ),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.resolve_gamepad_output = lambda output_id, _context: (  # noqa: E731
        resolved.append(output_id)
        or SimpleNamespace(uinput=gamepad, bucket=f"gamepad:{output_id or 'default'}")
    )

    assert await process_analog_event(runtime, FakeEvent(32767), "abs_x", mapping, deps=_deps())
    assert resolved[-1] == "virtual-gamepad-2"
    assert (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 32767) in gamepad.events
    assert (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Y, 0) in gamepad.events

    assert await process_analog_event(runtime, FakeEvent(2000), "abs_x", mapping, deps=_deps())
    assert gamepad.events[-2] == (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 0)


@pytest.mark.asyncio
async def test_stick_gamepad_output_applies_sensitivity_and_response_curve() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Curve Stick",
                gamepad_output=AnalogGamepadOutputConfig(
                    enabled=True,
                    deadzone=0.0,
                    sensitivity=2.0,
                    response_curve=2.0,
                ),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.resolve_gamepad_output = lambda _output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket="gamepad",
    )

    assert await process_analog_event(runtime, FakeEvent(16384), "abs_x", mapping, deps=_deps())

    event_type, code, value = gamepad.events[-2]
    assert (event_type, code) == (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X)
    assert value == pytest.approx(16384, abs=4)


@pytest.mark.asyncio
async def test_trigger_gamepad_output_routes_axis_with_deadzone() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "left_trigger": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Route Trigger",
                input_type="axis",
                gamepad_output=AnalogGamepadOutputConfig(enabled=True, deadzone=0.2),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.analog_axis_bindings = {
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z): ("left_trigger", "x")
    }
    runtime.analog_axis_ranges = {("left_trigger", "x"): (0, 255)}
    runtime.resolve_gamepad_output = lambda _output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket="gamepad",
    )

    event = FakeEvent(128)
    event.code = evdev.ecodes.ABS_Z
    assert await process_analog_event(runtime, event, "abs_z", mapping, deps=_deps())
    assert gamepad.events[-1] == (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 96)

    event = FakeEvent(20)
    event.code = evdev.ecodes.ABS_Z
    assert await process_analog_event(runtime, event, "abs_z", mapping, deps=_deps())
    assert gamepad.events[-1] == (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0)


@pytest.mark.asyncio
async def test_trigger_gamepad_output_applies_sensitivity_and_response_curve() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "left_trigger": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Curve Trigger",
                input_type="axis",
                gamepad_output=AnalogGamepadOutputConfig(
                    enabled=True,
                    deadzone=0.0,
                    sensitivity=2.0,
                    response_curve=2.0,
                ),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.analog_axis_bindings = {
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z): ("left_trigger", "x")
    }
    runtime.analog_axis_ranges = {("left_trigger", "x"): (0, 255)}
    runtime.resolve_gamepad_output = lambda _output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket="gamepad",
    )
    event = FakeEvent(64)
    event.code = evdev.ecodes.ABS_Z

    assert await process_analog_event(runtime, event, "abs_z", mapping, deps=_deps())

    assert gamepad.events[-1] == (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 32)


@pytest.mark.asyncio
async def test_trigger_gamepad_output_can_route_to_opposite_trigger() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "right_trigger": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Swap Trigger",
                input_type="axis",
                gamepad_output=AnalogGamepadOutputConfig(enabled=True, target="left"),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.analog_axis_bindings = {
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RZ): ("right_trigger", "x")
    }
    runtime.analog_axis_ranges = {("right_trigger", "x"): (0, 255)}
    runtime.resolve_gamepad_output = lambda _output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket="gamepad",
    )
    event = FakeEvent(255)
    event.code = evdev.ecodes.ABS_RZ

    assert await process_analog_event(runtime, event, "abs_rz", mapping, deps=_deps())

    assert gamepad.events[-1] == (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 255)


@pytest.mark.asyncio
async def test_trigger_gamepad_output_routes_to_learned_axis_range() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "left_trigger": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Route Trigger",
                input_type="axis",
                gamepad_output=AnalogGamepadOutputConfig(
                    enabled=True,
                    target="analog",
                    target_analog_id="brake",
                    output_rest=100,
                    output_direction="min",
                    deadzone=0.0,
                ),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.analog_axis_bindings = {
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z): ("left_trigger", "x")
    }
    runtime.analog_axis_ranges = {("left_trigger", "x"): (0, 255)}
    runtime.resolve_gamepad_output = lambda _output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket="gamepad",
        analog_inputs={
            "brake": {
                "type": "axis",
                "axes": [
                    {
                        "role": "x",
                        "evdev": "abs_brake",
                        "evdev_code": evdev.ecodes.ABS_BRAKE,
                        "minimum": 0,
                        "maximum": 1023,
                        "rest": 0,
                    }
                ],
            }
        },
    )
    event = FakeEvent(255)
    event.code = evdev.ecodes.ABS_Z

    assert await process_analog_event(runtime, event, "abs_z", mapping, deps=_deps())

    assert gamepad.events[-1] == (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_BRAKE, 0)


@pytest.mark.asyncio
async def test_axis_gamepad_output_both_directions_routes_signed_range() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "wheel_axis": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Wheel Axis",
                input_type="axis",
                gamepad_output=AnalogGamepadOutputConfig(
                    enabled=True,
                    target="analog",
                    target_analog_id="x_axis",
                    output_rest=0,
                    output_direction="both",
                    deadzone=0.0,
                    sensitivity=2.0,
                    response_curve=2.0,
                ),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.analog_axis_bindings = {
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X): ("wheel_axis", "x")
    }
    runtime.analog_axis_ranges = {("wheel_axis", "x"): (-32768, 32767)}
    runtime.resolve_gamepad_output = lambda _output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket="gamepad",
        analog_inputs={
            "x_axis": {
                "type": "axis",
                "axes": [
                    {
                        "role": "x",
                        "evdev": "abs_x",
                        "evdev_code": evdev.ecodes.ABS_X,
                        "minimum": -1000,
                        "maximum": 1000,
                        "rest": 0,
                    }
                ],
            }
        },
    )
    negative_event = FakeEvent(-32768)
    negative_event.code = evdev.ecodes.ABS_X
    middle_event = FakeEvent(-16384)
    middle_event.code = evdev.ecodes.ABS_X
    positive_event = FakeEvent(32767)
    positive_event.code = evdev.ecodes.ABS_X

    assert await process_analog_event(
        runtime,
        negative_event,
        "abs_x",
        mapping,
        deps=_deps(),
    )
    assert await process_analog_event(
        runtime,
        middle_event,
        "abs_x",
        mapping,
        deps=_deps(),
    )
    assert await process_analog_event(
        runtime,
        positive_event,
        "abs_x",
        mapping,
        deps=_deps(),
    )

    assert gamepad.events[-3:] == [
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, -1000),
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, -500),
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 1000),
    ]


@pytest.mark.asyncio
async def test_axis_gamepad_output_both_direction_trigger_range_stays_positive() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "left_trigger": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Trigger Axis",
                input_type="axis",
                gamepad_output=AnalogGamepadOutputConfig(
                    enabled=True,
                    output_direction="both",
                    deadzone=0.0,
                ),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.analog_axis_bindings = {
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z): ("left_trigger", "x")
    }
    runtime.analog_axis_ranges = {("left_trigger", "x"): (0, 255)}
    runtime.resolve_gamepad_output = lambda _output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket="gamepad",
    )
    released_event = FakeEvent(0)
    released_event.code = evdev.ecodes.ABS_Z
    pressed_event = FakeEvent(255)
    pressed_event.code = evdev.ecodes.ABS_Z

    assert await process_analog_event(
        runtime,
        released_event,
        "abs_z",
        mapping,
        deps=_deps(),
    )
    assert await process_analog_event(
        runtime,
        pressed_event,
        "abs_z",
        mapping,
        deps=_deps(),
    )

    assert gamepad.events[-2:] == [
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0),
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 255),
    ]


@pytest.mark.asyncio
async def test_trigger_gamepad_output_same_uses_learned_logical_trigger_label() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "axis_1": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Same Trigger",
                input_type="axis",
                gamepad_output=AnalogGamepadOutputConfig(enabled=True, deadzone=0.0),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.analog_inputs = {"axis_1": {"label": "Left Trigger", "type": "axis"}}
    runtime.analog_axis_bindings = {
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_GAS): ("axis_1", "x")
    }
    runtime.analog_axis_output_codes = {("axis_1", "x"): evdev.ecodes.ABS_GAS}
    runtime.analog_axis_ranges = {("axis_1", "x"): (0, 255)}
    runtime.resolve_gamepad_output = lambda _output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket="gamepad",
    )
    event = FakeEvent(255)
    event.code = evdev.ecodes.ABS_GAS

    assert await process_analog_event(runtime, event, "abs_gas", mapping, deps=_deps())

    assert gamepad.events[-1] == (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 255)


@pytest.mark.asyncio
async def test_trigger_gamepad_output_same_uses_standard_source_axis_code() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "axis_1": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Same Trigger",
                input_type="axis",
                gamepad_output=AnalogGamepadOutputConfig(enabled=True, deadzone=0.0),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.analog_inputs = {"axis_1": {"label": "Axis 1", "type": "axis"}}
    runtime.analog_axis_bindings = {
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RZ): ("axis_1", "x")
    }
    runtime.analog_axis_output_codes = {("axis_1", "x"): evdev.ecodes.ABS_RZ}
    runtime.analog_axis_ranges = {("axis_1", "x"): (0, 255)}
    runtime.resolve_gamepad_output = lambda _output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket="gamepad",
    )
    event = FakeEvent(255)
    event.code = evdev.ecodes.ABS_RZ

    assert await process_analog_event(runtime, event, "abs_rz", mapping, deps=_deps())

    assert gamepad.events[-1] == (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RZ, 255)


@pytest.mark.asyncio
async def test_generic_axis_gamepad_output_same_uses_learned_axis_code() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "axis_1": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Same Generic Axis",
                input_type="axis",
                gamepad_output=AnalogGamepadOutputConfig(enabled=True, deadzone=0.0),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.analog_inputs = {"axis_1": {"label": "Axis 1", "type": "axis"}}
    runtime.analog_axis_bindings = {
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_GAS): ("axis_1", "x")
    }
    runtime.analog_axis_output_codes = {("axis_1", "x"): evdev.ecodes.ABS_GAS}
    runtime.analog_axis_ranges = {("axis_1", "x"): (0, 255)}
    runtime.resolve_gamepad_output = lambda _output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket="gamepad",
    )
    event = FakeEvent(255)
    event.code = evdev.ecodes.ABS_GAS

    assert await process_analog_event(runtime, event, "abs_gas", mapping, deps=_deps())

    assert gamepad.events[-1] == (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_GAS, 255)


@pytest.mark.asyncio
async def test_stick_gamepad_output_can_route_to_opposite_stick() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "right_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Swap Stick",
                gamepad_output=AnalogGamepadOutputConfig(enabled=True, target="left"),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.analog_axis_bindings = {
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RX): ("right_stick", "x")
    }
    runtime.analog_axis_ranges = {("right_stick", "x"): (-32768, 32767)}
    runtime.resolve_gamepad_output = lambda _output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket="gamepad",
    )
    event = FakeEvent(32767)
    event.code = evdev.ecodes.ABS_RX

    assert await process_analog_event(runtime, event, "abs_rx", mapping, deps=_deps())

    assert (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 32767) in gamepad.events
    assert (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Y, 0) in gamepad.events


@pytest.mark.asyncio
async def test_stick_gamepad_output_same_uses_learned_axis_codes() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "learned_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Same Learned Stick",
                gamepad_output=AnalogGamepadOutputConfig(
                    enabled=True,
                    deadzone=0.0,
                    sensitivity=2.0,
                    response_curve=2.0,
                ),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.analog_axis_bindings = {
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RX): ("learned_stick", "x"),
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RY): ("learned_stick", "y"),
    }
    runtime.analog_axis_output_codes = {
        ("learned_stick", "x"): evdev.ecodes.ABS_RX,
        ("learned_stick", "y"): evdev.ecodes.ABS_RY,
    }
    runtime.analog_axis_ranges = {
        ("learned_stick", "x"): (-32768, 32767),
        ("learned_stick", "y"): (-32768, 32767),
    }
    runtime.resolve_gamepad_output = lambda _output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket="gamepad",
    )
    event = FakeEvent(16384)
    event.code = evdev.ecodes.ABS_RX

    assert await process_analog_event(runtime, event, "abs_rx", mapping, deps=_deps())

    assert gamepad.events[-2:] == [
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RX, pytest.approx(16384, abs=4)),
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RY, 0),
    ]


@pytest.mark.asyncio
async def test_stick_gamepad_output_routes_to_learned_stick_range() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Route Stick",
                gamepad_output=AnalogGamepadOutputConfig(
                    enabled=True,
                    target="analog",
                    target_analog_id="wheel_stick",
                    deadzone=0.0,
                ),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.resolve_gamepad_output = lambda _output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket="gamepad",
        analog_inputs={
            "wheel_stick": {
                "type": "stick",
                "axes": [
                    {
                        "role": "x",
                        "evdev": "abs_x",
                        "evdev_code": evdev.ecodes.ABS_X,
                        "minimum": -1000,
                        "maximum": 1000,
                        "center": 0,
                    },
                    {
                        "role": "y",
                        "evdev": "abs_y",
                        "evdev_code": evdev.ecodes.ABS_Y,
                        "minimum": 100,
                        "maximum": 1100,
                        "center": 600,
                    },
                ],
            }
        },
    )

    assert await process_analog_event(runtime, FakeEvent(32767), "abs_x", mapping, deps=_deps())

    assert gamepad.events[-2:] == [
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 1000),
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Y, 600),
    ]


@pytest.mark.asyncio
async def test_reset_analog_controls_centers_gamepad_output() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "right_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Route Right Stick",
                gamepad_output=AnalogGamepadOutputConfig(enabled=True),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.resolve_gamepad_output = lambda _output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket="gamepad",
    )

    await reset_analog_controls(runtime, deps=_deps())

    assert (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RX, 0) in gamepad.events
    assert (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RY, 0) in gamepad.events


@pytest.mark.asyncio
async def test_reset_analog_controls_centers_previous_gamepad_output_after_mapping_change() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Route Stick",
                gamepad_output=AnalogGamepadOutputConfig(
                    enabled=True,
                    output_id="virtual-gamepad-2",
                ),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.resolve_gamepad_output = lambda output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket=f"gamepad:{output_id or 'default'}",
    )

    assert await process_analog_event(runtime, FakeEvent(32767), "abs_x", mapping, deps=_deps())
    mapping.clear()
    await reset_analog_controls(runtime, deps=_deps())

    assert gamepad.events[-2:] == [
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 0),
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Y, 0),
    ]


@pytest.mark.asyncio
async def test_same_device_gamepad_output_resolves_to_source_hardware() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    resolved: list[str | None] = []
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Route Stick",
                gamepad_output=AnalogGamepadOutputConfig(
                    enabled=True,
                    output_id=SAME_DEVICE_OUTPUT_ID,
                ),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.resolve_gamepad_output = lambda output_id, _context: (  # noqa: E731
        resolved.append(output_id)
        or SimpleNamespace(uinput=gamepad, bucket=f"gamepad:{output_id or 'default'}")
    )

    assert await process_analog_event(runtime, FakeEvent(32767), "abs_x", mapping, deps=_deps())
    mapping.clear()
    await reset_analog_controls(runtime, deps=_deps())

    assert resolved == ["1234:5678", "1234:5678"]
    assert gamepad.events[-2:] == [
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 0),
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Y, 0),
    ]


@pytest.mark.asyncio
async def test_reset_analog_controls_uses_custom_target_rest_after_mapping_change() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Route Stick",
                gamepad_output=AnalogGamepadOutputConfig(
                    enabled=True,
                    target="analog",
                    target_analog_id="wheel_stick",
                    deadzone=0.0,
                ),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.resolve_gamepad_output = lambda _output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket="gamepad",
        analog_inputs={
            "wheel_stick": {
                "type": "stick",
                "axes": [
                    {
                        "role": "x",
                        "evdev": "abs_x",
                        "evdev_code": evdev.ecodes.ABS_X,
                        "minimum": -1000,
                        "maximum": 1000,
                        "center": 100,
                    },
                    {
                        "role": "y",
                        "evdev": "abs_y",
                        "evdev_code": evdev.ecodes.ABS_Y,
                        "minimum": 100,
                        "maximum": 1100,
                        "center": 600,
                    },
                ],
            }
        },
    )

    assert await process_analog_event(runtime, FakeEvent(32767), "abs_x", mapping, deps=_deps())
    mapping.clear()
    await reset_analog_controls(runtime, deps=_deps())

    assert gamepad.events[-2:] == [
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 100),
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Y, 600),
    ]


@pytest.mark.asyncio
async def test_reset_analog_controls_uses_target_stick_midpoint_without_center() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Route Stick",
                gamepad_output=AnalogGamepadOutputConfig(
                    enabled=True,
                    target="analog",
                    target_analog_id="wheel_stick",
                    deadzone=0.0,
                ),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.resolve_gamepad_output = lambda _output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket="gamepad",
        analog_inputs={
            "wheel_stick": {
                "type": "stick",
                "axes": [
                    {
                        "role": "x",
                        "evdev": "abs_x",
                        "evdev_code": evdev.ecodes.ABS_X,
                        "minimum": 0,
                        "maximum": 255,
                    },
                    {
                        "role": "y",
                        "evdev": "abs_y",
                        "evdev_code": evdev.ecodes.ABS_Y,
                        "minimum": 100,
                        "maximum": 1100,
                    },
                ],
            }
        },
    )

    assert await process_analog_event(runtime, FakeEvent(32767), "abs_x", mapping, deps=_deps())
    mapping.clear()
    await reset_analog_controls(runtime, deps=_deps())

    assert gamepad.events[-2:] == [
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 128),
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Y, 600),
    ]


@pytest.mark.asyncio
async def test_release_all_keys_does_not_zero_custom_analog_reset_value() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Route Stick",
                gamepad_output=AnalogGamepadOutputConfig(
                    enabled=True,
                    target="analog",
                    target_analog_id="wheel_stick",
                    deadzone=0.0,
                ),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.resolve_gamepad_output = lambda _output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket="gamepad",
        analog_inputs={
            "wheel_stick": {
                "type": "stick",
                "axes": [
                    {
                        "role": "x",
                        "evdev": "abs_x",
                        "evdev_code": evdev.ecodes.ABS_X,
                        "minimum": -1000,
                        "maximum": 1000,
                        "center": 100,
                    },
                    {
                        "role": "y",
                        "evdev": "abs_y",
                        "evdev_code": evdev.ecodes.ABS_Y,
                        "minimum": 100,
                        "maximum": 1100,
                        "center": 600,
                    },
                ],
            }
        },
    )

    assert await process_analog_event(runtime, FakeEvent(32767), "abs_x", mapping, deps=_deps())
    await reset_analog_controls(runtime, deps=_deps())
    event_count_after_reset = len(gamepad.events)

    release_all_keys(
        runtime,
        evdev_mod=evdev,
        uinput_writer=identity_uinput_writer,
    )

    assert gamepad.events[event_count_after_reset:] == []
    assert gamepad.events[-2:] == [
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 100),
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Y, 600),
    ]


@pytest.mark.asyncio
async def test_release_all_keys_does_not_zero_neutral_custom_analog_axis() -> None:
    keyboard = FakeUInput()
    gamepad = FakeUInput()
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Route Stick",
                gamepad_output=AnalogGamepadOutputConfig(
                    enabled=True,
                    target="analog",
                    target_analog_id="wheel_stick",
                    deadzone=0.0,
                ),
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)
    runtime.resolve_gamepad_output = lambda _output_id, _context: SimpleNamespace(  # noqa: E731
        uinput=gamepad,
        bucket="gamepad",
        analog_inputs={
            "wheel_stick": {
                "type": "stick",
                "axes": [
                    {
                        "role": "x",
                        "evdev": "abs_x",
                        "evdev_code": evdev.ecodes.ABS_X,
                        "minimum": -1000,
                        "maximum": 1000,
                        "center": 100,
                    },
                    {
                        "role": "y",
                        "evdev": "abs_y",
                        "evdev_code": evdev.ecodes.ABS_Y,
                        "minimum": 100,
                        "maximum": 1100,
                        "center": 600,
                    },
                ],
            }
        },
    )

    assert await process_analog_event(runtime, FakeEvent(32767), "abs_x", mapping, deps=_deps())
    event_count_after_output = len(gamepad.events)

    release_all_keys(
        runtime,
        evdev_mod=evdev,
        uinput_writer=identity_uinput_writer,
    )

    assert (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Y, 0) not in gamepad.events[
        event_count_after_output:
    ]


@pytest.mark.asyncio
async def test_reset_analog_controls_releases_threshold_after_mapping_removed() -> None:
    keyboard = FakeUInput()
    mapping = {
        "left_stick": MappingAction(
            action_type=ActionType.ANALOG_CONTROL,
            analog_control_config=AnalogControlConfig(
                name="Test",
                thresholds=[
                    AnalogActionThreshold(
                        axis="x",
                        trigger_min=0.65,
                        trigger_max=1.0,
                        release_min=0.55,
                        release_max=1.0,
                        actions=[
                            MappingAction(action_type=ActionType.KEYBOARD, target="key_a")
                        ],
                    )
                ],
            ),
        )
    }
    runtime = _runtime(mapping, keyboard)

    assert await process_analog_event(runtime, FakeEvent(32767), "abs_x", mapping, deps=_deps())
    assert keyboard.events[-1] == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)

    mapping.clear()
    await reset_analog_controls(runtime, deps=_deps())

    assert keyboard.events[-1] == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0)
