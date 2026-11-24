from pathlib import Path

import pytest

from keymasq.common import paths
from keymasq.common.model.core import DeviceType
from keymasq.common.model.hardware import (
    AnalogAxisDefinition,
    AnalogInputDefinition,
    ButtonDefinition,
    EvdevDevice,
    HardwareConfig,
)
from keymasq.session.hardware_presets import (
    apply_hardware_preset,
    hardware_presets_for_model,
    load_hardware_presets,
)

HEADER = '[preset]\nname = "Pad"\nhardware = ["1234:5678"]\n'
RELABEL = '[buttons.btn_base]\nlabel = "Quick Access"\n'


def _write(directory: Path, preset_id: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{preset_id}.toml").write_text(body, encoding="utf-8")


def _analog(analog_id: str, analog_type: str, axis: str) -> str:
    return f'[[analogs]]\nid = "{analog_id}"\ntype = "{analog_type}"\naxes = [{{ {axis} }}]\n'


def _preset(tmp_path: Path, body: str):
    _write(tmp_path / "presets", "pad", HEADER + body)
    return load_hardware_presets([tmp_path / "presets"])[0]


def _stick(analog_id: str, x: str, y: str, minimum: int, maximum: int) -> AnalogInputDefinition:
    return AnalogInputDefinition(
        id=analog_id,
        label=analog_id,
        type="stick",
        source="pad",
        axes=[
            AnalogAxisDefinition("x", x, 1, minimum, maximum, center=(minimum + maximum) // 2),
            AnalogAxisDefinition("y", y, 2, minimum, maximum, center=(minimum + maximum) // 2),
        ],
    )


def _detected() -> HardwareConfig:
    return HardwareConfig(
        "1234",
        "5678",
        "Pad",
        [EvdevDevice("/dev/input/event0", DeviceType.GAMEPAD, "pad")],
        [ButtonDefinition(id="btn_base", label="Base", evdev="btn_base", source="pad")],
        analog_inputs=[
            _stick("left_stick", "abs_x", "abs_y", -100, 100),
            _stick("hat_0", "abs_hat0x", "abs_hat0y", -32767, 32767),
            _stick("hat_2", "abs_hat2x", "abs_hat2y", 0, 255),
        ],
    )


def _layout(config: HardwareConfig) -> list[tuple[str, str, list[str]]]:
    return [
        (analog.id, analog.type, [axis.evdev for axis in analog.axes])
        for analog in config.analog_inputs
    ]


def test_apply_relabels_buttons_and_regroups_axes_in_place_of_the_detected_inputs(tmp_path):
    config = _detected()
    preset = _preset(
        tmp_path,
        RELABEL
        + """
[[analogs]]
id = "left_trackpad"
label = "Left Trackpad"
type = "stick"
axes = [{ role = "x", evdev = "abs_hat0x" }, { role = "y", evdev = "abs_hat0y", invert = true }]

[[analogs]]
id = "left_trigger"
type = "axis"
axes = [{ role = "x", evdev = "abs_hat2y" }]

[[analogs]]
id = "right_trigger"
type = "axis"
axes = [{ role = "x", evdev = "abs_hat2x" }]
""",
    )

    assert apply_hardware_preset(preset, config) == []

    assert config.buttons[0].label == "Quick Access"
    assert _layout(config) == [
        ("left_stick", "stick", ["abs_x", "abs_y"]),
        ("left_trackpad", "stick", ["abs_hat0x", "abs_hat0y"]),
        ("left_trigger", "axis", ["abs_hat2y"]),
        ("right_trigger", "axis", ["abs_hat2x"]),
    ]
    trackpad, left_trigger = config.analog_inputs[1], config.analog_inputs[2]
    assert trackpad.label == "Left Trackpad"
    assert trackpad.source == "pad"
    assert [(axis.center, axis.invert) for axis in trackpad.axes] == [(0, False), (0, True)]
    trigger_axis = left_trigger.axes[0]
    assert (trigger_axis.role, trigger_axis.evdev_code) == ("x", 2)
    assert (trigger_axis.minimum, trigger_axis.maximum) == (0, 255)
    assert (trigger_axis.center, trigger_axis.rest) == (None, None)


def test_an_axis_left_over_from_a_split_stick_becomes_its_own_input(tmp_path):
    config = _detected()
    preset = _preset(
        tmp_path,
        '[[analogs]]\nid = "left_trigger"\ntype = "axis"\n'
        'axes = [{ role = "x", evdev = "abs_hat2y" }]\n',
    )

    apply_hardware_preset(preset, config)

    assert _layout(config)[2:] == [
        ("left_trigger", "axis", ["abs_hat2y"]),
        ("abs_hat2x", "axis", ["abs_hat2x"]),
    ]


def test_controls_the_device_lacks_or_that_would_clash_are_reported_and_skipped(tmp_path):
    config = _detected()
    before = _layout(config)
    preset = _preset(
        tmp_path,
        """
[buttons.btn_mode]
label = "Steam"

[[analogs]]
id = "right_trackpad"
type = "stick"
axes = [{ role = "x", evdev = "abs_hat1x" }, { role = "y", evdev = "abs_hat1y" }]

[[analogs]]
id = "left_stick"
type = "stick"
axes = [{ role = "x", evdev = "abs_hat0x" }, { role = "y", evdev = "abs_hat0y" }]
""",
    )

    assert apply_hardware_preset(preset, config) == ["btn_mode", "right_trackpad", "left_stick"]
    assert _layout(config) == before


def test_an_override_that_contradicts_the_device_range_skips_the_whole_input(tmp_path):
    config = _detected()
    before = _layout(config)
    preset = _preset(
        tmp_path,
        """
[[analogs]]
id = "left_stick"
type = "stick"
axes = [{ role = "x", evdev = "abs_x", minimum = 300 }, { role = "y", evdev = "abs_y" }]
""",
    )

    assert apply_hardware_preset(preset, config) == ["left_stick"]
    assert _layout(config) == before
    assert config.analog_inputs[0].axes[0].minimum == -100


def test_a_range_override_moves_the_center_and_an_explicit_center_wins(tmp_path):
    config = _detected()
    preset = _preset(
        tmp_path,
        """
[[analogs]]
id = "left_stick"
type = "stick"
axes = [
    { role = "x", evdev = "abs_x", minimum = 0, maximum = 50 },
    { role = "y", evdev = "abs_y", center = 7 },
]
""",
    )

    apply_hardware_preset(preset, config)

    assert [(axis.minimum, axis.maximum, axis.center) for axis in config.analog_inputs[0].axes] == [
        (0, 50, 25),
        (-100, 100, 7),
    ]


@pytest.mark.parametrize(
    "body",
    [
        RELABEL,
        '[preset]\nhardware = ["not-a-model"]\n' + RELABEL,
        HEADER,
        HEADER + '[buttons.abs_x]\nlabel = "Axis"\n',
        HEADER + "[buttons.btn_base]\n",
        HEADER + _analog("Left Pad", "axis", 'role="x", evdev="abs_z"'),
        HEADER + _analog("pad", "dial", 'role="x", evdev="abs_z"'),
        HEADER + _analog("pad", "stick", 'role="x", evdev="abs_z"'),
        HEADER + _analog("pad", "axis", 'role="x", evdev="btn_a"'),
        HEADER + _analog("pad", "axis", 'role="x", evdev="abs_z", invert=true'),
        HEADER + _analog("pad", "axis", 'role="x", evdev="abs_z", minimum=5, maximum=5'),
        HEADER + _analog("pad", "axis", 'role="x", evdev="abs_z", maximum=5, rest=9'),
        HEADER + "device_types = [true]\n" + RELABEL,
        HEADER + 'device_types = [""]\n' + RELABEL,
        HEADER
        + _analog("one", "axis", 'role="x", evdev="abs_z"')
        + _analog("two", "axis", 'role="x", evdev="abs_z"'),
        "this is not toml",
    ],
)
def test_an_invalid_hardware_preset_is_skipped_without_hiding_valid_ones(tmp_path, body):
    _write(tmp_path, "broken", body)
    _write(tmp_path, "good", HEADER + RELABEL)

    assert [preset.preset_id for preset in load_hardware_presets([tmp_path])] == ["good"]


def test_a_user_file_overrides_a_packaged_preset_of_the_same_file_name(tmp_path):
    packaged, user = tmp_path / "packaged", tmp_path / "user"
    _write(packaged, "pad", HEADER + RELABEL)
    _write(packaged, "other", HEADER + RELABEL)
    _write(user, "pad", HEADER.replace("Pad", "Mine") + RELABEL)

    presets = load_hardware_presets([packaged, user, tmp_path / "missing"])

    assert {preset.preset_id: preset.name for preset in presets} == {"pad": "Mine", "other": "Pad"}


def test_presets_match_by_model_and_by_the_device_type_being_configured(tmp_path):
    _write(tmp_path, "any", '[preset]\nhardware = ["1234:5678", "ABCD:0001"]\n' + RELABEL)
    _write(
        tmp_path,
        "pad-only",
        '[preset]\nhardware = ["1234:5678"]\ndevice_types = ["gamepad"]\n' + RELABEL,
    )
    presets = load_hardware_presets([tmp_path])

    def matching(model_id: str, device_types: tuple[str, ...]) -> list[str]:
        return [p.preset_id for p in hardware_presets_for_model(presets, model_id, device_types)]

    assert matching("1234:5678", ("gamepad",)) == ["any", "pad-only"]
    assert matching("1234:5678", ("mouse", "keyboard")) == ["any"]
    assert matching("abcd:0001", ("gamepad",)) == ["any"]
    assert matching("1234:9999", ("gamepad",)) == []


def test_every_bundled_hardware_preset_loads():
    bundled = sorted(path.stem for path in paths.BUNDLED_HARDWARE_PRESETS_DIR.glob("*.toml"))

    assert bundled
    assert (
        sorted(p.preset_id for p in load_hardware_presets([paths.BUNDLED_HARDWARE_PRESETS_DIR]))
        == bundled
    )


def test_an_empty_xdg_data_dirs_falls_back_to_the_default_data_dirs(monkeypatch):
    monkeypatch.setenv("XDG_DATA_DIRS", "")

    assert paths.hardware_preset_search_dirs()[1:3] == [
        Path("/usr/share/keymasq/hardware_presets"),
        Path("/usr/local/share/keymasq/hardware_presets"),
    ]


def test_search_order_is_bundled_then_data_dirs_then_user_config(monkeypatch):
    monkeypatch.setenv("XDG_DATA_DIRS", "/first:/second")

    assert paths.hardware_preset_search_dirs() == [
        paths.BUNDLED_HARDWARE_PRESETS_DIR,
        Path("/second/keymasq/hardware_presets"),
        Path("/first/keymasq/hardware_presets"),
        paths.HARDWARE_PRESETS_DIR,
    ]
