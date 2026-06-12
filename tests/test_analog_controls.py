from pathlib import Path
from typing import BinaryIO

import pytest

import keymasq.session.analog_controls as analog_controls_module
from keymasq.common.models import (
    SAME_DEVICE_OUTPUT_ID,
    ActionType,
    AnalogActionThreshold,
    AnalogControlConfig,
    AnalogGamepadOutputConfig,
    AnalogMouseMotionConfig,
    MappingAction,
    validate_analog_control_config,
)
from keymasq.session.analog_controls import (
    AnalogControlManager,
    analog_control_mouse_wheel_template,
    analog_control_presets,
    analog_control_wasd_template,
)


def _write_analog_control(path: Path, name: str) -> None:
    path.write_text(f'name = "{name}"\n', encoding="utf-8")


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


def test_analog_control_failed_overwrite_preserves_existing_file_and_state(
    temp_config_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = AnalogControlManager()
    manager.save_analog_control(
        AnalogControlConfig(name="Saved Control", description="original")
    )
    path = temp_config_dir / "analog_controls" / "saved_control.toml"
    original_content = path.read_bytes()

    def fail_dump(_data: object, config_file: BinaryIO) -> None:
        config_file.write(b'name = "partial"\n')
        raise OSError("disk full")

    monkeypatch.setattr(analog_controls_module.tomli_w, "dump", fail_dump)

    with pytest.raises(OSError, match="disk full"):
        manager.save_analog_control(
            AnalogControlConfig(name="Saved Control", description="updated")
        )

    loaded = manager.get_analog_control("Saved Control")
    assert path.read_bytes() == original_content
    assert loaded is not None
    assert loaded.description == "original"
    assert sorted(item.name for item in (temp_config_dir / "analog_controls").iterdir()) == [
        "saved_control.toml"
    ]


def test_analog_control_delete_uses_loaded_noncanonical_path(temp_config_dir) -> None:
    analog_controls_dir = temp_config_dir / "analog_controls"
    _write_analog_control(analog_controls_dir / "custom.toml", "FPS Mouse")
    _write_analog_control(analog_controls_dir / "fps_mouse.toml", "Other Control")

    manager = AnalogControlManager()

    assert manager.delete_analog_control("FPS Mouse") is True
    assert not (analog_controls_dir / "custom.toml").exists()
    assert (analog_controls_dir / "fps_mouse.toml").exists()
    assert AnalogControlManager().list_analog_controls() == ["Other Control"]


def test_analog_control_rename_uses_loaded_noncanonical_path(temp_config_dir) -> None:
    analog_controls_dir = temp_config_dir / "analog_controls"
    _write_analog_control(analog_controls_dir / "custom.toml", "FPS Mouse")
    _write_analog_control(analog_controls_dir / "fps_mouse.toml", "Other Control")

    manager = AnalogControlManager()

    assert manager.rename_analog_control("FPS Mouse", "Look Mouse") is True
    assert not (analog_controls_dir / "custom.toml").exists()
    assert (analog_controls_dir / "fps_mouse.toml").exists()
    assert (analog_controls_dir / "look_mouse.toml").exists()
    assert AnalogControlManager().list_analog_controls() == ["Look Mouse", "Other Control"]


def test_analog_control_replacing_save_removes_loaded_noncanonical_path(
    temp_config_dir,
) -> None:
    analog_controls_dir = temp_config_dir / "analog_controls"
    _write_analog_control(analog_controls_dir / "custom.toml", "Old Name")
    _write_analog_control(analog_controls_dir / "old_name.toml", "Other Control")

    manager = AnalogControlManager()
    manager.save_analog_control(
        AnalogControlConfig(name="New Name"),
        replacing_name="Old Name",
    )

    assert not (analog_controls_dir / "custom.toml").exists()
    assert (analog_controls_dir / "old_name.toml").exists()
    assert sorted(path.name for path in analog_controls_dir.glob("*.toml")) == [
        "new_name.toml",
        "old_name.toml",
    ]
    assert AnalogControlManager().list_analog_controls() == ["New Name", "Other Control"]


def test_analog_control_manager_normalizes_obsolete_mouse_plus_digital(
    temp_config_dir,
) -> None:
    manager = AnalogControlManager()
    manager.save_analog_control(
        AnalogControlConfig(
            name="Old Combined",
            mouse_motion=AnalogMouseMotionConfig(enabled=True),
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
    )

    loaded = AnalogControlManager().get_analog_control("Old Combined")

    assert loaded is not None
    assert loaded.mouse_motion.enabled is False
    assert len(loaded.thresholds) == 1


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
                output_invert_x=True,
                output_invert_y=True,
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
    assert loaded.gamepad_output.output_invert_x is True
    assert loaded.gamepad_output.output_invert_y is True
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


def test_analog_control_mouse_area_round_trips(temp_config_dir) -> None:
    manager = AnalogControlManager()
    manager.save_analog_control(
        AnalogControlConfig(
            name="Mouse Area",
            mouse_motion=AnalogMouseMotionConfig(
                enabled=True,
                mode="area",
                area_radius_x=640,
                area_radius_y=360,
                area_start_enabled=True,
                area_start_x=100,
                area_start_y=200,
            ),
        )
    )

    loaded = AnalogControlManager().get_analog_control("Mouse Area")
    assert loaded is not None
    assert loaded.mouse_motion.mode == "area"
    assert loaded.mouse_motion.area_radius_x == 640
    assert loaded.mouse_motion.area_radius_y == 360
    assert loaded.mouse_motion.area_start_enabled is True
    assert loaded.mouse_motion.area_start_x == 100
    assert loaded.mouse_motion.area_start_y == 200


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


def test_analog_control_gamepad_output_max_direction_round_trips(
    temp_config_dir,
) -> None:
    manager = AnalogControlManager()
    manager.save_analog_control(
        AnalogControlConfig(
            name="Route Pedal Max",
            input_type="axis",
            gamepad_output=AnalogGamepadOutputConfig(
                enabled=True,
                output_direction="max",
            ),
        )
    )

    content = (
        temp_config_dir / "analog_controls" / "route_pedal_max.toml"
    ).read_text(encoding="utf-8")
    loaded = AnalogControlManager().get_analog_control("Route Pedal Max")

    assert 'output_direction = "max"' not in content
    assert loaded is not None
    assert loaded.gamepad_output.output_direction == "max"
    assert loaded.gamepad_output.output_invert is False


def test_analog_control_gamepad_output_both_direction_invert_round_trips(
    temp_config_dir,
) -> None:
    manager = AnalogControlManager()
    manager.save_analog_control(
        AnalogControlConfig(
            name="Route Pedal Both",
            input_type="axis",
            gamepad_output=AnalogGamepadOutputConfig(
                enabled=True,
                output_direction="both",
                output_invert=True,
            ),
        )
    )

    loaded = AnalogControlManager().get_analog_control("Route Pedal Both")

    assert loaded is not None
    assert loaded.gamepad_output.output_direction == "both"
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

    signed = AnalogControlConfig(
        name="Signed Axis",
        input_type="axis",
        thresholds=[
            AnalogActionThreshold(
                axis="x",
                trigger_min=-1.0,
                trigger_max=-0.5,
                release_min=-1.0,
                release_max=-0.45,
            )
        ],
    )
    assert signed.thresholds[0].trigger_min == -1.0

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

    with pytest.raises(ValueError, match="area mode requires a stick"):
        AnalogControlConfig(
            name="Bad Area",
            input_type="axis",
            mouse_motion=AnalogMouseMotionConfig(enabled=True, mode="area"),
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
    # Vertical (Y) up/down plus horizontal (X) left/right side-scrolling.
    assert [(t.axis, t.actions[0].target) for t in wheel] == [
        ("y", "rel_wheel:1"),
        ("y", "rel_wheel:-1"),
        ("x", "rel_hwheel:-1"),
        ("x", "rel_hwheel:1"),
    ]
    assert all(t.actions[0].rapidfire_enabled for t in wheel)


def test_analog_control_presets_filtered_by_input_type() -> None:
    stick = analog_control_presets("stick")
    axis = analog_control_presets("axis")
    assert {p.preset_id for p in stick} == {
        "mouse_move",
        "mouse_area",
        "scroll_wheel",
        "wasd",
    }
    assert {p.preset_id for p in axis} == {
        "trigger_left_click",
        "trigger_right_click",
        "trigger_scroll_up",
        "trigger_scroll_down",
    }
    assert all(p.input_type == "stick" for p in stick)
    assert all(p.input_type == "axis" for p in axis)


def test_analog_control_presets_none_returns_all() -> None:
    every = analog_control_presets(None)
    assert {p.preset_id for p in every} == {
        "mouse_move",
        "mouse_area",
        "scroll_wheel",
        "wasd",
        "trigger_left_click",
        "trigger_right_click",
        "trigger_scroll_up",
        "trigger_scroll_down",
    }


def test_analog_control_presets_build_valid_configs() -> None:
    for preset in analog_control_presets(None):
        config = preset.build(preset.default_name)
        assert config.name == preset.default_name
        assert config.input_type == preset.input_type
        # Should not raise — every preset is a valid, ready-to-use config.
        validate_analog_control_config(config)


def test_analog_control_mouse_move_preset_uses_velocity_mouse_motion() -> None:
    preset = next(p for p in analog_control_presets("stick") if p.preset_id == "mouse_move")
    config = preset.build("Mouse Move")
    assert config.mouse_motion.enabled is True
    assert config.mouse_motion.mode == "velocity"


def test_analog_control_mouse_area_preset_uses_area_mode() -> None:
    preset = next(p for p in analog_control_presets("stick") if p.preset_id == "mouse_area")
    config = preset.build("Mouse Area")
    assert config.mouse_motion.enabled is True
    assert config.mouse_motion.mode == "area"


def test_analog_control_trigger_presets_cover_clicks_and_scroll() -> None:
    targets = {}
    for preset in analog_control_presets("axis"):
        config = preset.build(preset.default_name)
        assert config.input_type == "axis"
        assert len(config.thresholds) == 1
        action = config.thresholds[0].actions[0]
        assert action.action_type == ActionType.MOUSE
        targets[preset.preset_id] = action.target
    assert targets == {
        "trigger_left_click": "btn_left",
        "trigger_right_click": "btn_right",
        "trigger_scroll_up": "rel_wheel:1",
        "trigger_scroll_down": "rel_wheel:-1",
    }


def test_analog_control_presets_round_trip_through_manager(temp_config_dir) -> None:
    manager = AnalogControlManager()
    for preset in analog_control_presets(None):
        config = preset.build(preset.default_name)
        manager.save_analog_control(config)
    saved = set(manager.list_analog_controls())
    assert {p.default_name for p in analog_control_presets(None)} <= saved


def test_analog_control_unique_name_accounts_for_storage_filename_collisions(
    temp_config_dir,
) -> None:
    manager = AnalogControlManager()
    manager.save_analog_control(AnalogControlConfig(name="Mouse_Move"))
    manager.save_analog_control(AnalogControlConfig(name="Mouse Move 2"))

    assert manager.unique_analog_control_name("Mouse Move") == "Mouse Move 3"
