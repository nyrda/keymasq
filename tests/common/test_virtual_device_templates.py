from typing import cast

import pytest

from keymasq.common.virtual_device_templates import (
    BUILTIN_VIRTUAL_DEVICE_TEMPLATES,
    LOGITECH_EXTREME_3D_TEMPLATE,
    XBOX_360_TEMPLATE,
    VirtualDeviceConfigError,
    config_from_json,
    config_to_json,
    resolve_virtual_devices,
    template_analog_inputs,
    template_sticks,
    virtual_device_config_from_toml,
)


def _custom_config_data() -> dict[str, object]:
    return {
        "templates": [
            {
                "id": "space-panel",
                "label": "Space Panel",
                "name": "Keymasq Space Panel",
                "vendor_id": "4b4d",
                "product_id": "2001",
                "version": "0100",
                "bustype": "usb",
                "buttons": [
                    {"id": "fire", "label": "Fire", "evdev": "btn_trigger"},
                    {"id": "mode", "label": "Mode", "evdev": "btn_thumb"},
                ],
                "axes": [
                    {
                        "id": "x",
                        "label": "X",
                        "evdev": "abs_x",
                        "minimum": -32768,
                        "maximum": 32767,
                        "rest": 0,
                    },
                    {
                        "id": "y",
                        "label": "Y",
                        "evdev": "abs_y",
                        "minimum": -32768,
                        "maximum": 32767,
                        "rest": 0,
                    },
                ],
            }
        ],
        "devices": [
            {
                "output_id": "space-rig",
                "template": "space-panel",
                "name": "My Space Rig",
                "product_id": "2002",
            }
        ],
    }


def test_builtin_templates_use_the_generic_model() -> None:
    assert BUILTIN_VIRTUAL_DEVICE_TEMPLATES == (
        XBOX_360_TEMPLATE,
        LOGITECH_EXTREME_3D_TEMPLATE,
    )
    assert XBOX_360_TEMPLATE.vendor_id == 0x045E
    assert XBOX_360_TEMPLATE.product_id == 0x028E
    assert len(XBOX_360_TEMPLATE.buttons) == 17
    assert len(XBOX_360_TEMPLATE.axes) == 8
    assert LOGITECH_EXTREME_3D_TEMPLATE.vendor_id == 0x046D
    assert LOGITECH_EXTREME_3D_TEMPLATE.product_id == 0xC215
    assert {button.evdev for button in LOGITECH_EXTREME_3D_TEMPLATE.buttons} >= {
        "btn_trigger",
        "btn_base6",
    }
    assert {axis.evdev for axis in LOGITECH_EXTREME_3D_TEMPLATE.axes} >= {
        "abs_x",
        "abs_y",
        "abs_rz",
        "abs_throttle",
    }


def test_user_templates_round_trip_and_resolve_with_builtin_instances() -> None:
    config = virtual_device_config_from_toml(_custom_config_data())

    assert config_from_json(config_to_json(config)) == config
    devices = resolve_virtual_devices(2, config)
    assert [device.output_id for device in devices] == [
        "virtual-gamepad-1",
        "virtual-gamepad-2",
        "space-rig",
    ]
    assert devices[0].template is XBOX_360_TEMPLATE
    assert devices[2].template.id == "space-panel"
    assert devices[2].name == "My Space Rig"
    assert devices[2].product_id == 0x2002


def test_joystick_template_exposes_named_analog_targets() -> None:
    analogs = template_analog_inputs(LOGITECH_EXTREME_3D_TEMPLATE)

    stick = cast(dict[str, object], analogs["stick-x__stick-y"])
    assert stick["type"] == "stick"
    assert stick["label"] == "Stick X / Stick Y"
    throttle = cast(dict[str, object], analogs["throttle"])
    assert throttle["type"] == "axis"
    axes = cast(list[dict[str, object]], throttle["axes"])
    assert axes == [
        {
            "role": "x",
            "evdev": "abs_throttle",
            "minimum": 0,
            "maximum": 255,
            "center": 255,
            "rest": 255,
        }
    ]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda data: data["templates"][0].update(id="xbox-360"),
            "reserved",
        ),
        (
            lambda data: data["templates"][0]["axes"].pop(),
            "2..16 axes",
        ),
        (
            lambda data: data["templates"][0]["buttons"].append(
                {"id": "again", "label": "Again", "evdev": "btn_trigger"}
            ),
            "evdev codes must be unique",
        ),
        (
            lambda data: data["devices"][0].update(template="missing"),
            "unknown device template",
        ),
    ],
)
def test_invalid_template_configs_are_rejected(mutate, message: str) -> None:
    data = _custom_config_data()
    mutate(data)

    with pytest.raises(VirtualDeviceConfigError, match=message):
        virtual_device_config_from_toml(data)


def test_templates_must_advertise_sdl_joystick_capabilities() -> None:
    data = _custom_config_data()
    templates = cast(list[dict[str, object]], data["templates"])
    templates[0]["buttons"] = [{"id": "extra", "label": "Extra", "evdev": "btn_trigger_happy1"}]

    with pytest.raises(VirtualDeviceConfigError, match="SDL-compatible"):
        virtual_device_config_from_toml(data)


def test_layout_survives_custom_template_round_trip():
    from dataclasses import replace

    from keymasq.common.virtual_device_templates import VirtualDeviceConfig

    template = replace(LOGITECH_EXTREME_3D_TEMPLATE, id="custom-flight", builtin=False)
    config = VirtualDeviceConfig(templates=(template,))
    assert config_from_json(config_to_json(config)).templates[0].layout == "flight-stick"
    data = config_to_json(config)
    templates = cast(list[dict[str, object]], data["templates"])
    templates[0]["layout"] = "unknown"
    with pytest.raises(VirtualDeviceConfigError, match="layout"):
        config_from_json(data)


def test_numbered_batch_avoids_existing_codes_and_control_ids():
    from keymasq.common.virtual_device_templates import VirtualButton, numbered_button_batch

    existing = (VirtualButton("extra-button-2", "Existing", "btn_trigger_happy1"),)
    added = numbered_button_batch(existing, 3, reserved_ids={"extra-button-3"})
    assert [button.evdev for button in added] == [
        "btn_trigger_happy2",
        "btn_trigger_happy3",
        "btn_trigger_happy4",
    ]
    assert len({button.id for button in (*existing, *added)}) == 4
    assert "extra-button-3" not in {button.id for button in added}
    with pytest.raises(VirtualDeviceConfigError, match="unused TriggerHappy"):
        numbered_button_batch(XBOX_360_TEMPLATE.buttons, 41)


def test_full_trigger_happy_bank_round_trips_with_ordinary_buttons():
    from dataclasses import replace

    from keymasq.common.virtual_device_templates import (
        VirtualButton,
        VirtualDeviceConfig,
        VirtualDeviceInstance,
        numbered_button_batch,
    )

    # The flight-sim setup needs about 61 buttons per output, including all 40
    # TriggerHappy codes. Its ordinary joystick buttons must not consume that bank.
    ordinary = LOGITECH_EXTREME_3D_TEMPLATE.buttons + tuple(
        VirtualButton(f"button-{index}", str(index), f"btn_{index}") for index in range(9)
    )
    buttons = ordinary + numbered_button_batch(ordinary, 40)
    template = replace(LOGITECH_EXTREME_3D_TEMPLATE, id="flight", builtin=False, buttons=buttons)
    config = VirtualDeviceConfig(
        templates=(template,),
        devices=tuple(VirtualDeviceInstance(f"flight-{index}", "flight") for index in range(2)),
    )
    restored = config_from_json(config_to_json(config))
    assert restored == config
    assert [len(device.template.buttons) for device in resolve_virtual_devices(0, restored)] == [
        61,
        61,
    ]
    with pytest.raises(VirtualDeviceConfigError, match="unused TriggerHappy"):
        numbered_button_batch(buttons, 1)


def test_numbered_batch_counts_aliases_and_reuses_freed_codes():
    from keymasq.common.virtual_device_templates import VirtualButton, numbered_button_batch

    alias = VirtualButton("first", "First", "btn_trigger_happy")
    remaining = numbered_button_batch((alias,), 39)
    assert [button.evdev for button in remaining] == [
        f"btn_trigger_happy{index}" for index in range(2, 41)
    ]
    with pytest.raises(VirtualDeviceConfigError, match="unused TriggerHappy"):
        numbered_button_batch((alias, *remaining), 1)
    assert numbered_button_batch(remaining, 1)[0].evdev == "btn_trigger_happy1"


@pytest.mark.parametrize("field", ["minimum", "maximum", "rest", "fuzz", "flat", "resolution"])
@pytest.mark.parametrize("value", [-2147483649, 2147483648])
def test_axis_metadata_rejects_values_outside_kernel_integer_range(field, value):
    data = _custom_config_data()
    templates = cast(list[dict[str, object]], data["templates"])
    axes = cast(list[dict[str, object]], templates[0]["axes"])
    axes[0][field] = value
    with pytest.raises(VirtualDeviceConfigError, match="signed 32-bit"):
        virtual_device_config_from_toml(data)


def test_button_aliases_cannot_define_duplicate_controls():
    data = _custom_config_data()
    templates = cast(list[dict[str, object]], data["templates"])
    buttons = cast(list[dict[str, object]], templates[0]["buttons"])
    buttons.append({"id": "alias", "label": "Alias", "evdev": "btn_joystick"})
    with pytest.raises(VirtualDeviceConfigError, match="evdev codes must be unique"):
        virtual_device_config_from_toml(data)


@pytest.mark.parametrize("sentinel", ["abs_max", "abs_cnt"])
def test_axis_sentinels_are_not_template_controls(sentinel):
    data = _custom_config_data()
    templates = cast(list[dict[str, object]], data["templates"])
    axes = cast(list[dict[str, object]], templates[0]["axes"])
    axes[0]["evdev"] = sentinel
    with pytest.raises(VirtualDeviceConfigError, match="axis sentinel"):
        virtual_device_config_from_toml(data)


def test_same_device_routing_sentinel_cannot_name_virtual_output():
    data = _custom_config_data()
    devices = cast(list[dict[str, object]], data["devices"])
    devices[0]["output_id"] = "same-device"
    with pytest.raises(VirtualDeviceConfigError, match="same-device.*reserved"):
        virtual_device_config_from_toml(data)


@pytest.mark.parametrize(
    "controls",
    [
        [("x", "abs_x"), ("y", "abs_y"), ("x__y", "abs_rz")],
        [("a__b", "abs_x"), ("c", "abs_y"), ("a", "abs_rx"), ("b__c", "abs_ry")],
    ],
)
def test_derived_analog_id_collisions_are_rejected(controls):
    data = _custom_config_data()
    templates = cast(list[dict[str, object]], data["templates"])
    templates[0]["axes"] = [
        {
            "id": control_id,
            "label": control_id,
            "evdev": code,
            "minimum": -100,
            "maximum": 100,
            "rest": 0,
        }
        for control_id, code in controls
    ]
    with pytest.raises(VirtualDeviceConfigError, match="derived analog ID.*not unique"):
        virtual_device_config_from_toml(data)


def test_existing_generated_analog_ids_remain_stable():
    data = _custom_config_data()
    config = virtual_device_config_from_toml(data)
    assert set(template_analog_inputs(config.templates[0])) == {"x__y"}


def _stick_config_data(sticks: list[dict[str, object]]) -> dict[str, object]:
    data = _custom_config_data()
    template = cast(list[dict[str, object]], data["templates"])[0]
    axes = cast(list[dict[str, object]], template["axes"])
    for axis_id, code in (
        ("rx", "abs_z"),
        ("ry", "abs_rz"),
        ("pad-x", "abs_hat1x"),
        ("pad-y", "abs_hat1y"),
    ):
        axes.append(
            {
                "id": axis_id,
                "label": axis_id,
                "evdev": code,
                "minimum": -100,
                "maximum": 100,
                "rest": 0,
            }
        )
    template["sticks"] = sticks
    return data


def test_declared_sticks_pair_any_axes_and_keep_inferred_pairs() -> None:
    data = _stick_config_data(
        [
            {"id": "right-stick", "label": "Right stick", "x": "rx", "y": "ry", "side": "right"},
            {"id": "touchpad", "label": "Touchpad", "x": "pad-x", "y": "pad-y"},
        ]
    )
    config = virtual_device_config_from_toml(data)

    assert config_from_json(config_to_json(config)) == config
    template = config.templates[0]
    assert [(stick.id, stick.side) for stick in template_sticks(template)] == [
        ("right-stick", "right"),
        ("touchpad", None),
        ("x__y", "left"),
    ]
    analogs = cast(dict[str, dict[str, object]], template_analog_inputs(template))
    assert set(analogs) == {"right-stick", "touchpad", "x__y"}
    assert analogs["right-stick"]["side"] == "right"
    touchpad_axes = cast(list[dict[str, object]], analogs["touchpad"]["axes"])
    assert [(axis["role"], axis["evdev"]) for axis in touchpad_axes] == [
        ("x", "abs_hat1x"),
        ("y", "abs_hat1y"),
    ]


def test_declared_stick_replaces_the_inferred_pair_on_its_axes() -> None:
    data = _stick_config_data([{"id": "main", "label": "Main", "x": "x", "y": "y"}])
    template = virtual_device_config_from_toml(data).templates[0]

    assert set(template_analog_inputs(template)) == {"main", "rx", "ry", "pad-x", "pad-y"}


def test_templates_without_sticks_keep_their_saved_format() -> None:
    config = virtual_device_config_from_toml(_custom_config_data())
    templates = cast(list[dict[str, object]], config_to_json(config)["templates"])
    assert "sticks" not in templates[0]


@pytest.mark.parametrize(
    ("sticks", "message"),
    [
        ([{"id": "s", "label": "S", "x": "rx", "y": "missing"}], "unknown axis"),
        ([{"id": "s", "label": "S", "x": "rx", "y": "rx"}], "two different axes"),
        (
            [
                {"id": "s", "label": "S", "x": "rx", "y": "ry"},
                {"id": "t", "label": "T", "x": "ry", "y": "pad-x"},
            ],
            "more than one stick",
        ),
        (
            [
                {"id": "s", "label": "S", "x": "rx", "y": "ry", "side": "left"},
                {"id": "t", "label": "T", "x": "pad-x", "y": "pad-y", "side": "left"},
            ],
            "one stick can be declared for each side",
        ),
        ([{"id": "s", "label": "S", "x": "rx", "y": "ry", "side": "up"}], "left or right"),
        ([{"id": "rx", "label": "S", "x": "rx", "y": "ry"}], "control IDs must be unique"),
        ([{"id": "x__y", "label": "S", "x": "rx", "y": "ry"}], "derived analog ID"),
    ],
)
def test_invalid_stick_declarations_are_rejected(sticks, message: str) -> None:
    with pytest.raises(VirtualDeviceConfigError, match=message):
        virtual_device_config_from_toml(_stick_config_data(sticks))
