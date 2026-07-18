from unittest.mock import Mock

import pytest

from keymasq.common.model.actions import (
    MappingAction,
    ProfileDeactivationPolicy,
)
from keymasq.common.model.core import (
    ActionType,
    DeviceType,
)
from keymasq.common.model.hardware import (
    ButtonDefinition,
    EvdevDevice,
    HardwareConfig,
)
from keymasq.common.model.profiles import (
    DeviceProfileLayer,
    ProfileConfig,
    WindowRule,
)
from keymasq.session.action_toml import (
    MACRO_RECORDING_SLOT_ACTION_TYPES,
    PROFILE_REF_ACTION_TYPES,
    mapping_action_from_toml,
    mapping_action_to_toml,
    mapping_action_type_from_toml,
)
from keymasq.session.hardware import HardwareManager
from keymasq.session.profile.manager import ProfileManager


def _parse_mapping_action_toml(data: dict[str, object]) -> MappingAction:
    action_type, action_data = mapping_action_type_from_toml(data, unknown_action="raise")
    return mapping_action_from_toml(
        action_data,
        action_type,
        rapidfire_warning_context="test config",
    )


def test_mapping_action_type_from_toml_supports_legacy_rapidfire_alias() -> None:
    action_type, action_data = mapping_action_type_from_toml(
        {"action": "rapidfire", "target": "key_a"},
        unknown_action="raise",
    )

    assert action_type == ActionType.KEYBOARD
    assert action_data["action"] == "keyboard"
    assert action_data["rapidfire_enabled"] is True


def test_mapping_action_from_toml_parses_false_rapidfire_string_as_disabled() -> None:
    action = _parse_mapping_action_toml(
        {
            "action": "keyboard",
            "target": "key_a",
            "rapidfire_enabled": "false",
        }
    )

    assert action.rapidfire_enabled is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("false", False),
        ("off", False),
        ("true", True),
        ("yes", True),
        (0, False),
        (1, True),
        ("not-a-boolean", False),
    ],
)
def test_mapping_action_from_toml_coerces_profile_trigger_end(
    value: object,
    expected: bool,
) -> None:
    action = _parse_mapping_action_toml(
        {
            "action": "profile_enable",
            "profile_name": "Gaming",
            "deactivation": {"on_trigger_end": value},
        }
    )

    if expected:
        assert action.profile_deactivation == ProfileDeactivationPolicy(on_trigger_end=True)
    else:
        assert action.profile_deactivation is None
        serialized = mapping_action_to_toml(
            action,
            rapidfire_warning_context="test config",
        )
        assert "deactivation" not in serialized


def test_mapping_action_type_from_toml_can_passthrough_unknown_actions() -> None:
    logger = Mock()

    action_type, action_data = mapping_action_type_from_toml(
        {"action": "future_action", "target": "key_a"},
        unknown_action="passthrough",
        logger=logger,
    )

    assert action_type == ActionType.PASSTHROUGH
    assert action_data["target"] == "key_a"
    logger.warning.assert_called_once()


def test_mapping_action_toml_round_trips_repeat_and_keys_fields() -> None:
    repeat = _parse_mapping_action_toml(
        {"action": "repeat", "repeat_categories": ["keyboard", "mouse"]}
    )
    keyed = mapping_action_to_toml(
        MappingAction(
            action_type=ActionType.EXEC,
            exec_ref=7,
            keys=["key_a", "key_b"],
        ),
        rapidfire_warning_context="test config",
    )

    assert repeat.action_type == ActionType.REPEAT
    assert mapping_action_to_toml(
        repeat,
        rapidfire_warning_context="test config",
    )["repeat_categories"] == ["keyboard", "mouse"]
    assert keyed["keys"] == ["key_a", "key_b"]


def test_mapping_action_toml_round_trips_mpris_command() -> None:
    action = _parse_mapping_action_toml({"action": "mpris", "command": "play-pause"})
    emitted = mapping_action_to_toml(action, rapidfire_warning_context="test config")

    assert action.action_type == ActionType.MPRIS
    assert action.mpris_command == "play_pause"
    assert emitted == {"action": "mpris", "command": "play_pause"}


def test_mapping_action_toml_round_trips_natural_mouse_move() -> None:
    action = _parse_mapping_action_toml(
        {
            "action": "mouse_move_natural_abs",
            "x": 640,
            "y": 480,
            "speed": 900.0,
            "jitter": 0.5,
            "curve": "natural",
            "tolerance": 3,
            "max_duration_ms": 2500,
            "rapidfire_enabled": True,
        }
    )
    emitted = mapping_action_to_toml(action, rapidfire_warning_context="test config")

    assert action.action_type == ActionType.MOUSE_MOVE_NATURAL_ABS
    assert action.rapidfire_enabled is False
    assert emitted == {
        "action": "mouse_move_natural_abs",
        "x": 640,
        "y": 480,
        "speed": 900.0,
        "jitter": 0.5,
        "curve": "natural",
        "tolerance": 3,
        "max_duration_ms": 2500,
    }


@pytest.mark.parametrize("action_type", MACRO_RECORDING_SLOT_ACTION_TYPES)
def test_mapping_action_toml_round_trips_macro_recording_slot_actions(
    action_type: ActionType,
) -> None:
    parsed = _parse_mapping_action_toml({"action": action_type.value, "recording_slot": 2})
    emitted = mapping_action_to_toml(parsed, rapidfire_warning_context="test config")

    assert parsed.action_type == action_type
    assert parsed.macro_recording_slot == 2
    assert emitted["recording_slot"] == 2


@pytest.mark.parametrize("action_type", PROFILE_REF_ACTION_TYPES)
def test_mapping_action_toml_round_trips_profile_ref_actions(
    action_type: ActionType,
) -> None:
    parsed = _parse_mapping_action_toml({"action": action_type.value, "target": "Gaming"})
    emitted = mapping_action_to_toml(parsed, rapidfire_warning_context="test config")

    assert parsed.action_type == action_type
    assert parsed.profile_name == "Gaming"
    assert emitted["target"] == "Gaming"
    assert emitted["profile_name"] == "Gaming"


def test_mapping_action_toml_helper_round_trips_shared_fields() -> None:
    macro_data = mapping_action_to_toml(
        MappingAction(
            action_type=ActionType.MACRO,
            macro_name="launch",
            macro_replay_mouse_movement=False,
            macro_replay_mouse_clicks=True,
            macro_speed=1.5,
            macro_loop_mode="count",
            macro_loop_count=3,
            macro_loop_stop_behavior="cancel_run",
            macro_move_to_start=True,
            macro_start_x=10,
            macro_start_y=20,
            macro_block_mouse_movement=True,
        ),
        rapidfire_warning_context="test config",
    )
    macro = _parse_mapping_action_toml(macro_data)

    assert macro.macro_name == "launch"
    assert macro.macro_speed == 1.5
    assert macro.macro_loop_count == 3
    assert macro.macro_loop_stop_behavior == "cancel_run"
    assert macro.macro_move_to_start is True
    assert macro.macro_start_x == 10
    assert macro.macro_start_y == 20
    assert macro.macro_block_mouse_movement is True

    axis_data = mapping_action_to_toml(
        MappingAction(
            action_type=ActionType.GAMEPAD_AXIS,
            target="abs_rx",
            output_id="virtual-gamepad-2",
            axis_value=12345,
            rapidfire_enabled=True,
            rapidfire_hold_ms=12,
            rapidfire_wait_ms=34,
            tap_enabled=True,
            tap_hold_ms=15,
        ),
        rapidfire_warning_context="test config",
    )
    axis = _parse_mapping_action_toml(axis_data)

    assert axis.action_type == ActionType.GAMEPAD_AXIS
    assert axis.target == "abs_rx"
    assert axis.output_id == "virtual-gamepad-2"
    assert axis.axis_value == 12345
    assert axis.rapidfire_enabled is True
    assert axis.rapidfire_hold_ms == 12
    assert axis.rapidfire_wait_ms == 34
    assert axis.tap_enabled is True
    assert axis.tap_hold_ms == 15

    profile_data = mapping_action_to_toml(
        MappingAction(
            action_type=ActionType.PROFILE_ENABLE,
            profile_name="Gaming",
            profile_deactivation=ProfileDeactivationPolicy(
                on_trigger_end=True,
                after_actions=2,
                timeout_ms=500,
            ),
        ),
        rapidfire_warning_context="test config",
    )
    profile = _parse_mapping_action_toml(profile_data)

    assert profile.action_type == ActionType.PROFILE_ENABLE
    assert profile.profile_name == "Gaming"
    assert profile.profile_deactivation == ProfileDeactivationPolicy(
        on_trigger_end=True,
        after_actions=2,
        timeout_ms=500,
    )


class TestHardwareTOML:
    def test_hardware_roundtrip(self, temp_config_dir):
        original = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Test Device",
            evdev_devices=[
                EvdevDevice(
                    path="/dev/input/event5",
                    device_type=DeviceType.MOUSE,
                    capabilities=["btn_left", "rel_wheel"],
                ),
            ],
            buttons=[
                ButtonDefinition(id="btn_left", label="Left", evdev="btn_left", zone="left"),
                ButtonDefinition(
                    id="wheel_up", label="Scroll Up", evdev="rel_wheel", evdev_value=1, type="wheel"
                ),
            ],
            image="test.png",
        )

        manager = HardwareManager()
        manager.save_hardware(original)

        loaded = manager.get_hardware(original.hardware_id)

        assert loaded.name == original.name
        assert loaded.vendor_id == original.vendor_id
        assert loaded.product_id == original.product_id
        assert len(loaded.buttons) == len(original.buttons)
        assert loaded.buttons[0].id == "btn_left"
        assert loaded.buttons[1].evdev_value == 1

    def test_hardware_file_is_human_readable(self, temp_config_dir, sample_hardware_config):
        manager = HardwareManager()
        manager.save_hardware(sample_hardware_config)

        config_file = (
            temp_config_dir
            / "hardware"
            / f"{sample_hardware_config.hardware_id.replace(':', '_')}.toml"
        )

        content = config_file.read_text()

        assert "[hardware]" in content
        assert "name = " in content
        assert "vendor_id = " in content


class TestProfileTOML:
    def test_profile_roundtrip(self, temp_config_dir):
        original = ProfileConfig(
            name="Test Profile",
            enabled=True,
            is_permanent=True,
            priority=5,
            device_layers={
                "1234:5678": DeviceProfileLayer(
                    hardware_id="1234:5678",
                    mappings={
                        "btn_back": MappingAction(action_type=ActionType.KEYBOARD, target="key_1"),
                        "btn_forward": MappingAction(
                            action_type=ActionType.KEYBOARD,
                            target="btn_left",
                            rapidfire_enabled=True,
                            rapidfire_hold_ms=50,
                            rapidfire_wait_ms=30,
                        ),
                        "left_stick": MappingAction(
                            action_type=ActionType.ANALOG_CONTROL,
                            analog_control_name="FPS Mouse",
                        ),
                        "btn_repeat": MappingAction(
                            action_type=ActionType.REPEAT,
                            repeat_categories=["keyboard", "mouse"],
                            rapidfire_enabled=True,
                            rapidfire_hold_ms=40,
                            rapidfire_wait_ms=60,
                        ),
                    },
                )
            },
        )

        manager = ProfileManager()
        manager.save_profile(original)

        profiles = manager.list_profiles()

        assert len(profiles) == 1
        loaded = profiles[0].config
        layer = loaded.device_layers["1234:5678"]

        assert loaded.name == original.name
        assert loaded.enabled == original.enabled
        assert loaded.is_permanent == original.is_permanent
        assert loaded.priority == original.priority
        assert "btn_back" in layer.mappings
        assert layer.mappings["btn_back"].action_type == ActionType.KEYBOARD
        assert layer.mappings["btn_forward"].rapidfire_enabled is True
        assert layer.mappings["btn_forward"].rapidfire_hold_ms == 50
        assert layer.mappings["left_stick"].action_type == ActionType.ANALOG_CONTROL
        assert layer.mappings["left_stick"].analog_control_name == "FPS Mouse"
        assert layer.mappings["btn_repeat"].action_type == ActionType.REPEAT
        assert layer.mappings["btn_repeat"].repeat_categories == ["keyboard", "mouse"]
        assert layer.mappings["btn_repeat"].rapidfire_enabled is True
        assert layer.mappings["btn_repeat"].rapidfire_hold_ms == 40
        assert layer.mappings["btn_repeat"].rapidfire_wait_ms == 60

    def test_profile_analog_control_names_roundtrip(self, temp_config_dir):
        original = ProfileConfig(
            name="Analog Fanout",
            device_layers={
                "1234:5678": DeviceProfileLayer(
                    hardware_id="1234:5678",
                    mappings={
                        "left_stick": MappingAction(
                            action_type=ActionType.ANALOG_CONTROL,
                            analog_control_names=["FPS Mouse", "WASD"],
                        ),
                    },
                )
            },
        )

        manager = ProfileManager()
        manager.save_profile(original)
        content = manager.get_profile(original.name).path.read_text(encoding="utf-8")

        loaded = manager.get_profile(original.name).config
        action = loaded.device_layers["1234:5678"].mappings["left_stick"]

        assert action.analog_control_names == ["FPS Mouse", "WASD"]
        assert action.analog_control_name == "FPS Mouse"
        assert "analog_control_names = [" in content
        assert '"FPS Mouse"' in content
        assert '"WASD"' in content

    def test_rename_analog_control_references(self, temp_config_dir):
        manager = ProfileManager()
        manager.save_profile(
            ProfileConfig(
                name="Analog Profile",
                device_layers={
                    "1234:5678": DeviceProfileLayer(
                        hardware_id="1234:5678",
                        mappings={
                            "left_stick": MappingAction(
                                action_type=ActionType.ANALOG_CONTROL,
                                analog_control_name="Old Control",
                            )
                        },
                    )
                },
            )
        )

        assert manager.rename_analog_control_references("Old Control", "New Control") == 1

        reloaded = ProfileManager()
        layer = reloaded.list_profiles()[0].config.device_layers["1234:5678"]
        assert layer.mappings["left_stick"].analog_control_name == "New Control"

    def test_update_multi_analog_control_references(self, temp_config_dir):
        manager = ProfileManager()
        manager.save_profile(
            ProfileConfig(
                name="Analog Profile",
                device_layers={
                    "1234:5678": DeviceProfileLayer(
                        hardware_id="1234:5678",
                        mappings={
                            "left_stick": MappingAction(
                                action_type=ActionType.ANALOG_CONTROL,
                                analog_control_names=["Old Control", "Mouse"],
                            )
                        },
                    )
                },
            )
        )

        assert manager.rename_analog_control_references("Old Control", "New Control") == 1
        assert manager.replace_analog_control_with_suppress("Mouse") == 1

        reloaded = ProfileManager()
        action = (
            reloaded.list_profiles()[0].config.device_layers["1234:5678"].mappings["left_stick"]
        )
        assert action.action_type == ActionType.ANALOG_CONTROL
        assert action.analog_control_names == ["New Control"]
        assert action.analog_control_name == "New Control"

    def test_profile_gamepad_output_id_roundtrip(self, temp_config_dir):
        original = ProfileConfig(
            name="Routed Gamepad",
            device_layers={
                "1234:5678": DeviceProfileLayer(
                    hardware_id="1234:5678",
                    mappings={
                        "btn_back": MappingAction(
                            action_type=ActionType.GAMEPAD,
                            target="btn_a",
                            output_id="virtual-gamepad-2",
                        ),
                        "btn_forward": MappingAction(
                            action_type=ActionType.KEYBOARD,
                            target="key_a",
                            output_id="virtual-gamepad-3",
                        ),
                    },
                )
            },
        )

        manager = ProfileManager()
        manager.save_profile(original)
        content = manager.get_profile(original.name).path.read_text(encoding="utf-8")

        loaded = manager.get_profile(original.name).config
        layer = loaded.device_layers["1234:5678"]
        assert layer.mappings["btn_back"].output_id == "virtual-gamepad-2"
        assert layer.mappings["btn_forward"].output_id is None
        assert 'output_id = "virtual-gamepad-2"' in content
        assert 'output_id = "virtual-gamepad-3"' not in content

    def test_profile_gamepad_axis_roundtrip(self, temp_config_dir):
        original = ProfileConfig(
            name="Axis Gamepad",
            device_layers={
                "1234:5678": DeviceProfileLayer(
                    hardware_id="1234:5678",
                    mappings={
                        "btn_back": MappingAction(
                            action_type=ActionType.GAMEPAD_AXIS,
                            target="abs_x",
                            axis_value=-32768,
                            output_id="virtual-gamepad-2",
                            rapidfire_enabled=True,
                            rapidfire_hold_ms=40,
                            rapidfire_wait_ms=25,
                            tap_enabled=True,
                            tap_hold_ms=15,
                        ),
                    },
                )
            },
        )

        manager = ProfileManager()
        manager.save_profile(original)
        content = manager.get_profile(original.name).path.read_text(encoding="utf-8")

        loaded = manager.get_profile(original.name).config
        action = loaded.device_layers["1234:5678"].mappings["btn_back"]
        assert action.action_type == ActionType.GAMEPAD_AXIS
        assert action.target == "abs_x"
        assert action.axis_value == -32768
        assert action.output_id == "virtual-gamepad-2"
        assert action.rapidfire_enabled is True
        assert action.tap_enabled is True
        assert 'action = "gamepad_axis"' in content
        assert "value = -32768" in content

    def test_profile_gamepad_axis_missing_value_defaults_to_max(self, temp_config_dir):
        profile_path = temp_config_dir / "profiles" / "axis.toml"
        profile_path.write_text(
            """
[profile]
name = "Axis Default"
enabled = true

[devices."1234:5678".mapping.btn_side]
action = "gamepad_axis"
target = "abs_z"
""".strip(),
            encoding="utf-8",
        )

        manager = ProfileManager()

        action = (
            manager.get_profile("Axis Default")
            .config.device_layers["1234:5678"]
            .mappings["btn_side"]
        )
        assert action.axis_value == 255

    def test_profile_file_is_human_readable(self, temp_config_dir, sample_profile_config):
        manager = ProfileManager()
        manager.save_profile(sample_profile_config)

        config_file = manager.get_profile(sample_profile_config.name).path
        content = config_file.read_text()

        assert "[profile]" in content
        assert '[devices."1234:5678"]' in content
        assert "mapping" in content

    def test_profile_manual_unsupported_rapidfire_warns_and_strips(
        self,
        temp_config_dir,
        caplog: pytest.LogCaptureFixture,
    ):
        profile_path = temp_config_dir / "profiles" / "rapidfire.toml"
        profile_path.write_text(
            """
[profile]
name = "Rapidfire"
enabled = true

[devices."1234:5678".mapping.btn_side]
action = "exec"
cmd = "echo hi"
rapidfire_enabled = true
rapidfire_hold_ms = 40
rapidfire_wait_ms = 60
""".strip(),
            encoding="utf-8",
        )

        with caplog.at_level("WARNING", logger="keymasq-session.profiles"):
            manager = ProfileManager()

        profile = manager.get_profile("Rapidfire")
        assert profile is not None
        action = profile.config.device_layers["1234:5678"].mappings["btn_side"]
        assert action.action_type == ActionType.EXEC
        assert action.rapidfire_enabled is False
        assert "Ignoring rapidfire for unsupported exec action in profile config" in caplog.text

    def test_profile_compositor_dispatch_roundtrip(self, temp_config_dir):
        original = ProfileConfig(
            name="Hyprland Profile",
            enabled=True,
            device_layers={
                "1234:5678": DeviceProfileLayer(
                    hardware_id="1234:5678",
                    mappings={
                        "btn_back": MappingAction(
                            action_type=ActionType.COMPOSITOR_DISPATCH,
                            compositor_id="hyprland",
                            compositor_dispatcher="workspace",
                            compositor_args="e+1",
                        )
                    },
                )
            },
        )

        manager = ProfileManager()
        manager.save_profile(original)

        loaded = manager.list_profiles()[0].config
        action = loaded.device_layers["1234:5678"].mappings["btn_back"]

        assert action.action_type == ActionType.COMPOSITOR_DISPATCH
        assert action.compositor_id == "hyprland"
        assert action.compositor_dispatcher == "workspace"
        assert action.compositor_args == "e+1"

    def test_profile_niri_compositor_dispatch_roundtrip(self, temp_config_dir):
        original = ProfileConfig(
            name="Niri Profile",
            enabled=True,
            device_layers={
                "1234:5678": DeviceProfileLayer(
                    hardware_id="1234:5678",
                    mappings={
                        "btn_back": MappingAction(
                            action_type=ActionType.COMPOSITOR_DISPATCH,
                            compositor_id="niri",
                            compositor_dispatcher="focus_workspace",
                            compositor_args="name:web",
                        )
                    },
                )
            },
        )

        manager = ProfileManager()
        manager.save_profile(original)

        loaded = manager.list_profiles()[0].config
        action = loaded.device_layers["1234:5678"].mappings["btn_back"]

        assert action.action_type == ActionType.COMPOSITOR_DISPATCH
        assert action.compositor_id == "niri"
        assert action.compositor_dispatcher == "focus_workspace"
        assert action.compositor_args == "name:web"

    def test_profile_with_window_rules(self, temp_config_dir):
        profile = ProfileConfig(
            name="Game Profile",
            enabled=True,
            is_permanent=False,
            window_rules=[
                WindowRule(field="class", pattern="cs2"),
                WindowRule(field="title", pattern=".*Counter-Strike.*"),
            ],
            device_layers={},
        )

        manager = ProfileManager()
        manager.save_profile(profile)

        loaded = manager.list_profiles()[0].config

        assert len(loaded.window_rules) == 2
        assert loaded.window_rules[0].field == "class"
        assert loaded.window_rules[0].pattern == "cs2"
        assert loaded.window_rules[1].field == "title"


class TestTOMLEditing:
    def test_edit_hardware_manually(self, temp_config_dir, sample_hardware_config):
        manager = HardwareManager()
        manager.save_hardware(sample_hardware_config)

        config_file = (
            temp_config_dir
            / "hardware"
            / f"{sample_hardware_config.hardware_id.replace(':', '_')}.toml"
        )

        content = config_file.read_text()
        content = content.replace('name = "Test Mouse"', 'name = "My Custom Mouse"')
        config_file.write_text(content)

        manager2 = HardwareManager()
        loaded = manager2.get_hardware(sample_hardware_config.hardware_id)

        assert loaded.name == "My Custom Mouse"

    def test_edit_profile_manually(self, temp_config_dir, sample_profile_config):
        manager = ProfileManager()
        manager.save_profile(sample_profile_config)

        config_file = manager.get_profile(sample_profile_config.name).path
        content = config_file.read_text()
        content = content.replace("enabled = true", "enabled = false")
        config_file.write_text(content)

        manager2 = ProfileManager()
        loaded = manager2.list_profiles()[0].config

        assert loaded.enabled is False
