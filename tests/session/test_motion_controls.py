import pytest

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.common.model.motion import MotionControlConfig
from keymasq.common.model.profiles import DeviceProfileLayer, ProfileConfig
from keymasq.session.config_loading import ConfigLoadError
from keymasq.session.motion_controls import MotionControlManager
from keymasq.session.profile.references import remove_motion_control


@pytest.mark.parametrize("remaining_names", [[], ["Tilt"]])
@pytest.mark.parametrize("embedded", [False, True])
def test_remove_named_motion_control_preserves_other_controls(remaining_names, embedded):
    inline = [MotionControlConfig(name="Embedded")] if embedded else []
    action = MappingAction(
        action_type=ActionType.MOTION_CONTROL,
        motion_control_names=["Aim", *remaining_names],
        motion_control_configs=inline,
        source_profile_name="Motion",
    )
    profile = ProfileConfig(
        name="Motion",
        device_layers={
            "controller": DeviceProfileLayer(
                hardware_id="controller", mappings={"motion_1": action}
            )
        },
    )

    result = remove_motion_control(profile, "Aim")

    assert result.config is not None
    updated = result.config.device_layers["controller"].mappings["motion_1"]
    assert updated.motion_control_names == remaining_names
    assert updated.motion_control_name == (remaining_names[0] if remaining_names else None)
    assert updated.motion_control_configs == inline
    assert updated.motion_control_config == (inline[0] if inline else None)
    if remaining_names or embedded:
        assert updated.action_type == ActionType.MOTION_CONTROL
        assert updated.source_profile_name == "Motion"
    else:
        assert updated.action_type == ActionType.SUPPRESS
    assert action.motion_control_names == ["Aim", *remaining_names]
    assert action.motion_control_configs == inline


def test_motion_loader_rejects_unknown_mode_and_preserves_valid_cache(temp_config_dir):
    manager = MotionControlManager()
    valid = MotionControlConfig(name="Aim")
    manager.save_motion_control(valid)
    (temp_config_dir / "motion_controls" / "invalid.toml").write_text(
        'name = "Invalid"\nmode = "future_mode"\n'
    )

    # Initial loading skips the bad file, so it never reaches the selector/editor.
    assert MotionControlManager().get_all_motion_controls() == {"Aim": valid}
    with pytest.raises(ConfigLoadError, match="motion control mode"):
        manager.reload()
    assert manager.get_all_motion_controls() == {"Aim": valid}


def test_motion_loader_normalizes_invalid_enum_fields(temp_config_dir):
    directory = temp_config_dir / "motion_controls"
    directory.mkdir(exist_ok=True)
    (directory / "aim.toml").write_text(
        'name = "Aim"\nmode = " MOUSE "\n'
        '[axis_routing]\nyaw = "invalid"\npitch = "invalid"\nroll = "invalid"\n'
        '[tilt]\nreference = "invalid"\npitch = "invalid"\nroll = "invalid"\n'
        '[analog]\nsource = "invalid"\nx_axis = "invalid"\ny_axis = "invalid"\n'
        'reference = "invalid"\n'
    )

    assert MotionControlManager().get_motion_control("Aim") == MotionControlConfig(name="Aim")
