import pytest

from keymasq.common.models import (
    SAME_DEVICE_OUTPUT_ID,
    ActionType,
    AnalogActionThreshold,
    AnalogControlConfig,
    AnalogGamepadOutputConfig,
    AnalogMouseMotionConfig,
    MappingAction,
)
from keymasq.session.analog_controls import (
    AnalogControlManager,
    analog_control_mouse_wheel_template,
    analog_control_wasd_template,
)


def test_gamepad_output_deadzone_defaults_to_zero() -> None:
    assert AnalogGamepadOutputConfig().deadzone == 0.0


def test_analog_control_manager_round_trip_rename_delete(temp_config_dir) -> None:
    manager = AnalogControlManager()
    config = AnalogControlConfig(
        name="FPS Mouse",
        thresholds=[
            AnalogActionThreshold(
                axis="x",
                trigger_min=0.65,
                trigger_max=1.0,
                release_min=0.55,
                release_max=1.0,
                actions=[MappingAction(action_type=ActionType.KEYBOARD, target="key_e")],
            )
        ],
    )

    manager.save_analog_control(config)

    loaded = AnalogControlManager().get_analog_control("FPS Mouse")
    assert loaded is not None
    assert loaded.thresholds[0].actions[0].target == "key_e"

    assert manager.rename_analog_control("FPS Mouse", "Look Mouse") is True
    assert AnalogControlManager().get_analog_control("Look Mouse") is not None
    assert manager.delete_analog_control("Look Mouse") is True
    assert AnalogControlManager().list_analog_controls() == []


def test_analog_control_save_replacing_name_removes_old_config(temp_config_dir) -> None:
    manager = AnalogControlManager()
    manager.save_analog_control(AnalogControlConfig(name="Old Name"))

    manager.save_analog_control(
        AnalogControlConfig(name="New Name"),
        replacing_name="Old Name",
    )

    reloaded = AnalogControlManager()
    assert reloaded.get_analog_control("Old Name") is None
    assert reloaded.get_analog_control("New Name") is not None
    assert sorted(path.name for path in (temp_config_dir / "analog_controls").glob("*.toml")) == [
        "new_name.toml"
    ]


def test_analog_control_validation_rejects_invalid_ranges() -> None:
    with pytest.raises(ValueError, match="activation range"):
        AnalogControlConfig(
            name="Bad",
            thresholds=[
                AnalogActionThreshold(
                    axis="x",
                    trigger_min=0.9,
                    trigger_max=0.6,
                    release_min=0.5,
                    release_max=1.0,
                )
            ],
        )

    with pytest.raises(ValueError, match="inside release"):
        AnalogControlConfig(
            name="Bad",
            thresholds=[
                AnalogActionThreshold(
                    axis="y",
                    trigger_min=-0.8,
                    trigger_max=-0.4,
                    release_min=-0.7,
                    release_max=-0.3,
                )
            ],
        )


def test_analog_control_validation_allows_overlapping_thresholds() -> None:
    config = AnalogControlConfig(
        name="Overlap",
        thresholds=[
            AnalogActionThreshold("x", 0.4, 0.8, 0.3, 0.9),
            AnalogActionThreshold("x", 0.6, 1.0, 0.5, 1.0),
        ],
    )

    assert len(config.thresholds) == 2


def test_analog_control_validation_rejects_superkey_threshold_actions() -> None:
    with pytest.raises(ValueError, match="invalid analog threshold action type: superkey"):
        AnalogControlConfig(
            name="Bad Child",
            thresholds=[
                AnalogActionThreshold(
                    axis="x",
                    trigger_min=0.65,
                    trigger_max=1.0,
                    release_min=0.55,
                    release_max=1.0,
                    actions=[
                        MappingAction(
                            action_type=ActionType.SUPERKEY,
                            superkey_name="Layer",
                        )
                    ],
                )
            ],
        )


def test_trigger_analog_control_uses_single_positive_axis(temp_config_dir) -> None:
    manager = AnalogControlManager()
    config = AnalogControlConfig(
        name="Trigger Pull",
        input_type="axis",
        thresholds=[
            AnalogActionThreshold(
                axis="x",
                trigger_min=0.5,
                trigger_max=1.0,
                release_min=0.45,
                release_max=1.0,
                actions=[MappingAction(action_type=ActionType.KEYBOARD, target="key_e")],
            )
        ],
    )

    manager.save_analog_control(config)

    loaded = AnalogControlManager().get_analog_control("Trigger Pull")
    assert loaded is not None
    assert loaded.input_type == "axis"
    assert loaded.thresholds[0].trigger_min == 0.5


def test_analog_control_gamepad_output_round_trips(temp_config_dir) -> None:
    manager = AnalogControlManager()
    manager.save_analog_control(
        AnalogControlConfig(
            name="Route Stick",
            gamepad_output=AnalogGamepadOutputConfig(
                enabled=True,
                output_id="virtual-gamepad-2",
                deadzone=0.2,
                target="right",
                sensitivity=1.5,
                response_curve=0.75,
            ),
        )
    )

    loaded = AnalogControlManager().get_analog_control("Route Stick")
    assert loaded is not None
    assert loaded.gamepad_output.enabled is True
    assert loaded.gamepad_output.output_id == "virtual-gamepad-2"
    assert loaded.gamepad_output.deadzone == 0.2
    assert loaded.gamepad_output.target == "right"
    assert loaded.gamepad_output.sensitivity == 1.5
    assert loaded.gamepad_output.response_curve == 0.75


def test_analog_control_same_device_output_round_trips(temp_config_dir) -> None:
    manager = AnalogControlManager()
    manager.save_analog_control(
        AnalogControlConfig(
            name="Route Same Device",
            gamepad_output=AnalogGamepadOutputConfig(
                enabled=True,
                output_id=SAME_DEVICE_OUTPUT_ID,
            ),
        )
    )

    loaded = AnalogControlManager().get_analog_control("Route Same Device")
    assert loaded is not None
    assert loaded.gamepad_output.output_id == SAME_DEVICE_OUTPUT_ID


def test_analog_control_mouse_zero_split_speed_round_trips(temp_config_dir) -> None:
    manager = AnalogControlManager()
    manager.save_analog_control(
        AnalogControlConfig(
            name="Zero Horizontal Mouse",
            mouse_motion=AnalogMouseMotionConfig(
                enabled=True,
                speed=900,
                speed_x=0,
                speed_y=700,
            ),
        )
    )

    loaded = AnalogControlManager().get_analog_control("Zero Horizontal Mouse")
    assert loaded is not None
    assert loaded.mouse_motion.speed == 900
    assert loaded.mouse_motion.speed_x == 0
    assert loaded.mouse_motion.speed_y == 700


def test_analog_control_gamepad_output_learned_target_round_trips(temp_config_dir) -> None:
    manager = AnalogControlManager()
    manager.save_analog_control(
        AnalogControlConfig(
            name="Route Pedal",
            input_type="axis",
            gamepad_output=AnalogGamepadOutputConfig(
                enabled=True,
                output_id="1234:5678",
                target="analog",
                target_analog_id="brake",
                output_rest=100,
                output_direction="min",
            ),
        )
    )

    loaded = AnalogControlManager().get_analog_control("Route Pedal")
    assert loaded is not None
    assert loaded.gamepad_output.output_id == "1234:5678"
    assert loaded.gamepad_output.target == "analog"
    assert loaded.gamepad_output.target_analog_id == "brake"
    assert loaded.gamepad_output.output_rest == 100
    assert loaded.gamepad_output.output_direction == "min"
    assert loaded.gamepad_output.output_invert is True


def test_analog_control_gamepad_axis_threshold_action_round_trips(temp_config_dir) -> None:
    manager = AnalogControlManager()
    manager.save_analog_control(
        AnalogControlConfig(
            name="Axis Threshold",
            thresholds=[
                AnalogActionThreshold(
                    axis="x",
                    trigger_min=0.65,
                    trigger_max=1.0,
                    release_min=0.55,
                    release_max=1.0,
                    actions=[
                        MappingAction(
                            action_type=ActionType.GAMEPAD_AXIS,
                            target="abs_rx",
                            output_id="virtual-gamepad-2",
                            axis_value=12345,
                        )
                    ],
                )
            ],
        )
    )

    loaded = AnalogControlManager().get_analog_control("Axis Threshold")

    assert loaded is not None
    action = loaded.thresholds[0].actions[0]
    assert action.action_type == ActionType.GAMEPAD_AXIS
    assert action.target == "abs_rx"
    assert action.output_id == "virtual-gamepad-2"
    assert action.axis_value == 12345


def test_axis_analog_control_accepts_mouse_motion_but_rejects_y_axis() -> None:
    config = AnalogControlConfig(
        name="Axis Mouse",
        input_type="axis",
        mouse_motion=AnalogMouseMotionConfig(
            enabled=True,
            speed=900,
            speed_x=700,
            speed_y=1100,
            sensitivity=1.5,
            response_curve=0.75,
            direction="vertical",
        ),
    )

    assert config.mouse_motion.enabled is True
    assert config.mouse_motion.speed_x == 700
    assert config.mouse_motion.speed_y == 1100
    assert config.mouse_motion.sensitivity == 1.5
    assert config.mouse_motion.response_curve == 0.75
    assert config.mouse_motion.direction == "vertical"

    with pytest.raises(ValueError, match="axis must be 'x'"):
        AnalogControlConfig(
            name="Bad Trigger",
            input_type="axis",
            thresholds=[
                AnalogActionThreshold(
                    axis="y",
                    trigger_min=0.5,
                    trigger_max=1.0,
                    release_min=0.45,
                    release_max=1.0,
                )
            ],
        )


def test_analog_control_templates_produce_threshold_actions() -> None:
    wasd = analog_control_wasd_template()
    assert [(t.axis, t.trigger_min, t.trigger_max, t.actions[0].target) for t in wasd] == [
        ("y", -1.0, -0.65, "key_w"),
        ("y", 0.65, 1.0, "key_s"),
        ("x", -1.0, -0.65, "key_a"),
        ("x", 0.65, 1.0, "key_d"),
    ]

    wheel = analog_control_mouse_wheel_template()
    assert wheel[0].actions[0].action_type == ActionType.MOUSE
    assert wheel[0].actions[0].rapidfire_enabled is True
    assert wheel[0].actions[0].rapidfire_hold_ms == 20
    assert wheel[0].actions[0].rapidfire_wait_ms == 60
