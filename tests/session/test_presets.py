from pathlib import Path

import pytest

from keymasq.common import paths
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.analog import AnalogControlConfig, AnalogMouseMotionConfig
from keymasq.common.model.core import ActionType, DeviceType
from keymasq.common.model.hardware import (
    AnalogInputDefinition,
    ButtonDefinition,
    EvdevDevice,
    HardwareConfig,
)
from keymasq.common.model.profiles import ProfileConfig
from keymasq.session.analog_controls import AnalogControlManager
from keymasq.session.presets import (
    apply_preset,
    load_presets,
    presets_for_hardware,
    write_preset_mappings,
)
from keymasq.session.profile.manager import ProfileManager

DESKTOP_PRESET = """
[preset]
name = "Desktop"
device_types = ["gamepad"]

[mapping.btn_south]
action = "keyboard"
target = "key_enter"

[mapping.btn_east]
action = "keyboard"
target = "key_esc"

[mapping.btn_mode]
action = "keyboard"
target = "key_leftmeta"

[mapping.right_stick]
action = "analog_control"
analog_control_name = "Pointer"

[mapping.left_trigger]
action = "analog_control"
analog_control_name = "Pointer"

[analog_controls.Pointer]
input_type = "stick"

[analog_controls.Pointer.mouse_motion]
enabled = true
speed = 700.0
"""

MINIMAL_MAPPING = """
[mapping.btn_south]
action = "keyboard"
target = "key_enter"
"""


def _gamepad(vendor_id: str = "045e", product_id: str = "028e") -> HardwareConfig:
    return HardwareConfig(
        vendor_id,
        product_id,
        "Controller",
        [EvdevDevice("/dev/input/event0", DeviceType.GAMEPAD, "pad")],
        [
            ButtonDefinition(id="btn_south", label="A", evdev="btn_south", source="pad"),
            ButtonDefinition(id="btn_east", label="B", evdev="btn_east", source="pad"),
        ],
        analog_inputs=[
            AnalogInputDefinition(id="right_stick", label="Right Stick", type="stick"),
            AnalogInputDefinition(id="left_trigger", label="Left Trigger", type="axis"),
        ],
    )


def _write_preset(directory: Path, preset_id: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{preset_id}.toml").write_text(body, encoding="utf-8")


def _desktop_preset(tmp_path: Path):
    _write_preset(tmp_path / "presets", "desktop", DESKTOP_PRESET)
    return load_presets([tmp_path / "presets"])[0]


def _apply(preset, profile: ProfileConfig, hardware: HardwareConfig):
    return apply_preset(
        preset,
        profile,
        hardware,
        profile_manager=ProfileManager(),
        analog_control_manager=AnalogControlManager(),
    )


def test_apply_fills_unbound_and_passthrough_controls_and_keeps_existing_mappings(
    temp_config_dir, tmp_path
):
    hardware = _gamepad()
    profile = ProfileConfig(name="Desk")
    layer = profile.ensure_layer(hardware.hardware_id)
    layer.mappings["btn_south"] = MappingAction(ActionType.KEYBOARD, target="key_a")
    layer.mappings["btn_east"] = MappingAction(ActionType.PASSTHROUGH)

    plan = _apply(_desktop_preset(tmp_path), profile, hardware)

    saved = ProfileManager().get_profile("Desk")
    assert saved is not None
    mappings = saved.config.device_layers[hardware.hardware_id].mappings
    assert {source: (action.action_type, action.target) for source, action in mappings.items()} == {
        "btn_south": (ActionType.KEYBOARD, "key_a"),
        "btn_east": (ActionType.KEYBOARD, "key_esc"),
        "right_stick": (ActionType.ANALOG_CONTROL, None),
    }
    assert mappings["right_stick"].analog_control_name == "Pointer"
    assert plan.kept == ["btn_south"]
    assert plan.unavailable == ["btn_mode", "left_trigger"]
    created = AnalogControlManager().get_analog_control("Pointer")
    assert created is not None
    assert created.mouse_motion.speed == 700.0


def test_apply_reuses_a_saved_analog_control_without_overwriting_it(temp_config_dir, tmp_path):
    hardware = _gamepad()
    AnalogControlManager().save_analog_control(
        AnalogControlConfig(
            name="Pointer",
            input_type="stick",
            mouse_motion=AnalogMouseMotionConfig(enabled=True, speed=250.0),
        )
    )

    plan = _apply(_desktop_preset(tmp_path), ProfileConfig(name="Desk"), hardware)

    assert "right_stick" in plan.mappings
    assert plan.new_analog_controls == []
    kept = AnalogControlManager().get_analog_control("Pointer")
    assert kept is not None
    assert kept.mouse_motion.speed == 250.0


def test_apply_skips_a_stick_when_the_saved_control_of_that_name_is_for_an_axis(
    temp_config_dir, tmp_path
):
    hardware = _gamepad()
    AnalogControlManager().save_analog_control(
        AnalogControlConfig(name="Pointer", input_type="axis")
    )

    plan = _apply(_desktop_preset(tmp_path), ProfileConfig(name="Desk"), hardware)

    assert "right_stick" in plan.unavailable
    saved = ProfileManager().get_profile("Desk")
    assert saved is not None
    assert "right_stick" not in saved.config.device_layers[hardware.hardware_id].mappings


def test_a_mapping_is_not_written_when_its_analog_control_was_never_saved(
    temp_config_dir, tmp_path
):
    hardware = _gamepad()

    plan = write_preset_mappings(
        _desktop_preset(tmp_path),
        ProfileConfig(name="Desk"),
        hardware,
        {},
        profile_manager=ProfileManager(),
    )

    assert "right_stick" in plan.unavailable
    saved = ProfileManager().get_profile("Desk")
    assert saved is not None
    assert set(saved.config.device_layers[hardware.hardware_id].mappings) == {
        "btn_south",
        "btn_east",
    }


def test_a_failed_profile_save_leaves_the_profile_mappings_unchanged(
    temp_config_dir, tmp_path, monkeypatch
):
    hardware = _gamepad()
    profile = ProfileConfig(name="Desk")
    profile.ensure_layer(hardware.hardware_id).mappings["btn_south"] = MappingAction(
        ActionType.KEYBOARD, target="key_a"
    )
    profile_manager = ProfileManager()

    def fail_save(*_args, **_kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(profile_manager, "save_profile", fail_save)

    with pytest.raises(OSError, match="read-only filesystem"):
        apply_preset(
            _desktop_preset(tmp_path),
            profile,
            hardware,
            profile_manager=profile_manager,
            analog_control_manager=AnalogControlManager(),
        )

    assert list(profile.device_layers[hardware.hardware_id].mappings) == ["btn_south"]


def test_later_search_directories_override_earlier_ones_by_file_name(tmp_path):
    packaged, user = tmp_path / "packaged", tmp_path / "user"
    _write_preset(packaged, "desktop", '[preset]\nname = "Packaged"\n' + MINIMAL_MAPPING)
    _write_preset(packaged, "extra", '[preset]\nname = "Extra"\n' + MINIMAL_MAPPING)
    _write_preset(user, "desktop", '[preset]\nname = "Mine"\n' + MINIMAL_MAPPING)

    presets = load_presets([packaged, user, tmp_path / "missing"])

    assert {preset.preset_id: preset.name for preset in presets} == {
        "desktop": "Mine",
        "extra": "Extra",
    }
    assert {preset.preset_id: preset.path.parent for preset in presets} == {
        "desktop": user,
        "extra": packaged,
    }


@pytest.mark.parametrize(
    "body",
    [
        '[mapping.btn_south]\naction = "superkey"\nsuperkey_name = "Copy"\n',
        '[mapping.btn_south]\naction = "macro"\nmacro_name = "Farm"\n',
        '[mapping.motion_1]\naction = "motion_control"\nmotion_control_name = "Gyro"\n',
        '[mapping.btn_south]\naction = "no_such_action"\n',
        '[mapping.btn_south]\naciton = "keyboard"\ntarget = "key_enter"\n',
        "[preset]\ndevice_types = [42]\n" + MINIMAL_MAPPING,
        '[mapping.right_stick]\naction = "analog_control"\n',
        '[preset]\nname = "Empty"\n',
        "this is not toml",
    ],
)
def test_an_invalid_preset_file_is_skipped_without_hiding_valid_ones(tmp_path, body):
    _write_preset(tmp_path, "broken", body)
    _write_preset(tmp_path, "good", MINIMAL_MAPPING)

    assert [preset.preset_id for preset in load_presets([tmp_path])] == ["good"]


def test_every_bundled_preset_loads():
    bundled = sorted(path.stem for path in paths.BUNDLED_PRESETS_DIR.glob("*.toml"))

    assert bundled
    assert sorted(p.preset_id for p in load_presets([paths.BUNDLED_PRESETS_DIR])) == bundled


def test_presets_are_offered_by_device_type_and_model_with_model_specific_first(tmp_path):
    _write_preset(
        tmp_path, "any-gamepad", '[preset]\ndevice_types = ["gamepad"]\n' + MINIMAL_MAPPING
    )
    _write_preset(tmp_path, "deck", '[preset]\nhardware = ["28DE:1205"]\n' + MINIMAL_MAPPING)
    _write_preset(tmp_path, "keyboard", '[preset]\ndevice_types = ["keyboard"]\n' + MINIMAL_MAPPING)
    presets = load_presets([tmp_path])

    assert [p.preset_id for p in presets_for_hardware(presets, _gamepad("28de", "1205"))] == [
        "deck",
        "any-gamepad",
    ]
    assert [p.preset_id for p in presets_for_hardware(presets, _gamepad())] == ["any-gamepad"]


def test_search_order_is_bundled_then_data_dirs_then_user_config(monkeypatch):
    monkeypatch.setenv("XDG_DATA_DIRS", "/first:/second:relative")

    assert paths.preset_search_dirs() == [
        paths.BUNDLED_PRESETS_DIR,
        Path("/second/keymasq/presets"),
        Path("/first/keymasq/presets"),
        paths.PRESETS_DIR,
    ]
